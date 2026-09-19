#!/usr/bin/env python3
"""VoxDesk TLS certification (Step 13 section G, extended Step 16).

Validates a supplied staging URL's TLS posture:

  * URL scheme (must be https, or a documented localhost plain-HTTP exception)
  * TLS handshake (default trusted context — never ``verify=False``)
  * certificate validity dates, hostname match, expiry
  * certificate chain (reported where the runtime exposes it; the chain is
    validated by the trusted context during the handshake regardless)
  * HTTP -> HTTPS redirect behaviour
  * HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy headers
  * API response over HTTPS
  * WebSocket upgrade probe (only when ``--websocket`` is passed)

Security rules enforced here:

  * certificate validation is never disabled (no ``verify=False``, no custom
    trust stores, no hardcoded certificates, no pinning);
  * an invalid or untrusted certificate is never PASS;
  * an unreachable endpoint is BLOCKED (never PASS);
  * a non-localhost HTTP URL is FAIL; the only plain-HTTP exception is a
    localhost/loopback target (NOT_APPLICABLE — the documented non-TLS local
    staging case);
  * no private certificate material is captured or printed.

Usage:
    python scripts/verify_tls.py --url https://staging.example.com [--json] [--websocket]

Exit codes: 0 = PASS (or NOT_APPLICABLE localhost exception), 1 = FAIL,
            2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.release import ops  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_tls.py", description="VoxDesk TLS certification"
    )
    parser.add_argument("--url", required=True, help="staging base URL to verify")
    parser.add_argument("--websocket", action="store_true",
                        help="also probe the WebSocket upgrade handshake (read-only)")
    parser.add_argument("--json", action="store_true")
    return parser


def _https_probe_headers(url: str, timeout: float = 8.0) -> dict:
    """Read-only GET of ``url``; returns status + lower-cased headers.

    Never captures certificate material.
    """
    try:
        with httpx.Client(verify=True, timeout=timeout, follow_redirects=False) as client:
            resp = client.get(url)
            return {
                "status": resp.status_code,
                "headers": {k.lower(): v for k, v in resp.headers.items()},
            }
    except Exception as exc:  # noqa: BLE001 - network errors are data here
        return {"status": None, "headers": {}, "error": type(exc).__name__}


def _http_to_https_redirect(host: str, port: int, timeout: float = 8.0) -> dict:
    """Probe the plain-HTTP port and record whether it upgrades to HTTPS.

    Uses the default trusted client (verify=True; irrelevant for the http
    scheme, which performs no TLS at all). A reachable plain-HTTP endpoint
    that serves content instead of redirecting is a real finding.
    """
    out = {"probed": True, "http_port": 80}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.get(f"http://{host}:{port}/")
            location = resp.headers.get("location", "")
            out["status"] = resp.status_code
            out["location"] = location
            out["redirects_to_https"] = bool(location) and location.startswith("https://")
            out["serves_plain_http"] = (
                resp.status_code < 400 and not out["redirects_to_https"]
            )
    except Exception as exc:  # noqa: BLE001 - no listener / refused is fine
        out["unreachable"] = type(exc).__name__
    return out


def _chain_info(host: str, port: int, timeout: float = 8.0) -> dict:
    """Best-effort introspection of the verified certificate chain.

    Validation itself is unchanged: ``ssl.create_default_context()`` enforces
    chain + hostname during the handshake, so a successful connect here means
    the chain was already validated. This only reports how many certificates
    the runtime surfaced, and never prints any certificate material.
    """
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                info: dict = {"chain_validated": True}
                try:
                    chain = ssock.get_verified_chain()  # Python 3.13+
                    info["chain_len"] = len(chain)
                except Exception:  # noqa: BLE001 - runtime without verified-chain API
                    info["chain_len"] = None
                return info
    except Exception as exc:  # noqa: BLE001 - already reflected in core status
        return {"chain_validated": False, "chain_len": None, "error": type(exc).__name__}


def _websocket_probe(url: str, timeout: float = 8.0) -> dict:
    """Read-only WebSocket upgrade probe (never opens a media stream).

    Sends a plain HTTP request carrying the upgrade headers and records how the
    server answers: 101 (switched) or 426 (upgrade required) means the upgrade
    is acknowledged; anything else is reported as-is. A full media-stream
    handshake is deliberately NOT performed.
    """
    wss = url.replace("https://", "wss://").replace("http://", "ws://")
    out = {"websocket_url": wss, "checked": False}
    try:
        with httpx.Client(verify=True, timeout=timeout, follow_redirects=False) as client:
            resp = client.get(
                url,
                headers={
                    "Connection": "Upgrade",
                    "Upgrade": "websocket",
                    "Sec-WebSocket-Version": "13",
                    "Sec-WebSocket-Key": "AAAAAAAAAAAAAAAAAAAAAA==",
                },
            )
            out["checked"] = True
            out["status"] = resp.status_code
            out["upgrade_acknowledged"] = resp.status_code in (101, 426)
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out


def _final_status(core_status: str, extra: dict) -> str:
    """Lay the Step 16 hardening findings over the core classification.

    The core status is authoritative for the certificate itself; the extra
    probes can only downgrade a PASS, never upgrade a FAIL/BLOCKED.
    """
    if core_status != ops.STATUS_PASS:
        return core_status
    if extra.get("serves_plain_http"):
        return ops.STATUS_FAIL
    if extra.get("referrer_policy_missing"):
        return ops.STATUS_FAIL
    return core_status


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    url = (args.url or "").strip()
    result = ops.verify_tls(url, check_websocket=False)

    parsed = urlparse(url) if url else None
    scheme = (parsed.scheme or "") if parsed else ""
    host = (parsed.hostname or "") if parsed else ""
    port = (parsed.port or (443 if scheme == "https" else 80)) if parsed else 0

    extra: dict = {}
    if scheme == "https" and host:
        h = _https_probe_headers(url)
        extra["http_status"] = h.get("status")
        extra["referrer_policy"] = "referrer-policy" in h.get("headers", {})
        if h.get("status") is not None:
            extra["referrer_policy_missing"] = not extra["referrer_policy"]
        extra["http_redirect"] = _http_to_https_redirect(host, port)
        extra["serves_plain_http"] = bool(extra["http_redirect"].get("serves_plain_http"))
        extra["chain"] = _chain_info(host, port)
    if args.websocket and url:
        extra["websocket"] = _websocket_probe(url)

    final_status = _final_status(result["status"], extra)
    result["status"] = final_status
    result["extra"] = extra
    result["evidence"] = (
        f"TLS verification {final_status}: scheme={result['checks'].get('scheme')}, "
        f"expiry_days={result['checks'].get('expiry_days')}, "
        f"hsts={result['checks'].get('hsts')}, "
        f"x_frame={result['checks'].get('x_frame')}, "
        f"x_content_type={result['checks'].get('x_content_type')}, "
        f"referrer_policy={extra.get('referrer_policy')}, "
        f"plain_http_serving={extra.get('serves_plain_http')}, "
        f"chain_len={extra.get('chain', {}).get('chain_len')}"
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"TLS verification: {result['status']}")
        print(f"  {result['evidence']}")
        for key, value in result.get("checks", {}).items():
            print(f"  {key}: {value}")
        if extra:
            print("  --- extended checks (Step 16) ---")
            for key, value in extra.items():
                print(f"  {key}: {value}")
        if final_status == ops.STATUS_NA:
            print("  (localhost-only plain-HTTP staging target — TLS not applicable)")
    return ops.exit_code_for_status(final_status)


if __name__ == "__main__":
    sys.exit(main())
