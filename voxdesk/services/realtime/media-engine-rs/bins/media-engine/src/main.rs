//! media-engine — the SFU node process.
//!
//! Threads (supervisor-owned, crash = whole-node-crash design):
//!
//! * `media` — spins `Engine::media_step(now_ms)` on a ~1ms cadence from
//!   the UDP transport; sweeps sessions every ~100ms.
//! * `control` — a minimal std-only HTTP/1.1 server:
//!   - `POST /v1/signal` — body is either a bare engine client-frame
//!     (back-compat smoke path) or an ENVELOPE {"v":1,"id":..,"frame":..}
//!     sent by the Go gateway's engine client; replies in the same shape.
//!   - `GET /v1/health` → the Go gateway's readiness probe payload
//!     ({"v":1,"ready":true,...} under protocol::WIRE_VERSION).
//!   - `GET /healthz` → 200 `ok`; `GET /metrics` → text exposition.
//!
//! Config is env-only (12-factor, same style as gateway-go).

use concurrency::Supervisor;
use engine::{Engine, EngineConfig};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use transport::UdpTransport;

fn env_or(key: &str, def: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| def.to_string())
}

fn parse_ipv4_or_die(s: &str) -> [u8; 4] {
    webrtc::ice::parse_ipv4(s)
        .unwrap_or_else(|| panic!("VOXDESK_PUBLIC_IP is not a dotted quad: {s}"))
}

fn log(msg: &str) {
    eprintln!(r#"{{"svc":"media-engine","msg":"{msg}"}}"#);
}

fn now_ms_of(anchor: Instant) -> u32 {
    (anchor.elapsed().as_millis() as u64).min(u32::MAX as u64) as u32
}

fn main() {
    // Per-boot DTLS-SRTP identity (standard SFU practice): self-signed
    // ECDSA P-256 cert, generated before any answer is ever emitted; its
    // SHA-256 fingerprint IS the `a=fingerprint` every answer carries.
    let identity = dtls::Identity::generate().expect("dtls identity generation");
    // Fixture escape hatch: VOXDESK_DTLS_FINGERPRINT pins the ANSWER TEXT
    // for harnesses that never terminate a browser DTLS (live gateway IT).
    // If it disagrees with the real identity's fingerprint, no browser can
    // complete DTLS — so the mismatch is shouted at boot, not papered over.
    let fingerprint = match std::env::var("VOXDESK_DTLS_FINGERPRINT") {
        Ok(pinned) => {
            if pinned != identity.fingerprint() {
                log(&format!(
                    "WARN: VOXDESK_DTLS_FINGERPRINT pins an answer-text that does NOT match the boot identity ({}); browsers will fail DTLS. Fixture-only posture.",
                    identity.fingerprint()
                ));
            }
            pinned
        }
        Err(_) => identity.fingerprint().to_string(),
    };
    log(&format!(
        "dtls identity active (answer fingerprint {})",
        fingerprint
    ));

    let config = EngineConfig {
        public_ip: parse_ipv4_or_die(&env_or("VOXDESK_PUBLIC_IP", "127.0.0.1")),
        public_port: env_or("VOXDESK_PORT", "5000").parse().expect("port u16"),
        fingerprint_sha256: fingerprint,
        engine_label: env_or("VOXDESK_ENGINE_LABEL", "edge-local"),
        room_limits: routing::RoomLimits {
            max_participants: env_or("VOXDESK_MAX_PARTICIPANTS", "64")
                .parse()
                .expect("u32"),
            max_tracks_per_participant: 4,
            max_subscriptions_per_participant: 64,
        },
        dtls_identity: Some(identity),
        ..Default::default()
    };

    let udp_addr = format!("0.0.0.0:{}", config.public_port);
    let transport = UdpTransport::bind(udp_addr.parse().expect("udp listen"))
        .unwrap_or_else(|e| panic!("udp bind {udp_addr}: {e}"));
    log(&format!("udp listen {}", udp_addr));

    let engine = Arc::new(Mutex::new(Engine::new(config, Arc::new(transport))));
    let anchor = Instant::now();
    let tick_count = Arc::new(AtomicU64::new(0));

    let sup = Supervisor::new();

    // ------------------------------------------------- media loop thread
    {
        let engine = Arc::clone(&engine);
        let tick = Arc::clone(&tick_count);
        sup.spawn("media", move |shutdown| {
            while !shutdown.tripped() {
                let handled = {
                    let mut engine = engine.lock().unwrap_or_else(|p| p.into_inner());
                    engine.media_step(now_ms_of(anchor))
                };
                let t = tick.fetch_add(1, Ordering::Relaxed);
                if t % 100 == 99 {
                    let mut engine = engine.lock().unwrap_or_else(|p| p.into_inner());
                    engine.sweep_step();
                }
                if handled == 0 {
                    std::thread::sleep(std::time::Duration::from_millis(1));
                }
            }
        })
        .expect("spawn media");
    }

    // ------------------------------------------- control HTTP listener
    let control_addr = env_or("VOXDESK_CONTROL_ADDR", "127.0.0.1:5010");
    let listener = TcpListener::bind(&control_addr)
        .unwrap_or_else(|e| panic!("control bind {control_addr}: {e}"));
    log(&format!("control http {}", control_addr));

    {
        let engine = Arc::clone(&engine);
        sup.spawn("control", move |shutdown| {
            // Accept loop with a short blocking timeout so shutdown is
            // promptly observed (same pattern as gateway's accept pacing).
            listener
                .set_nonblocking(true)
                .expect("listener nonblocking");
            while !shutdown.tripped() {
                match listener.accept() {
                    Ok((stream, _)) => {
                        handle_connection(&engine, stream);
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        })
        .expect("spawn control");
    }

    // std-only signal handling: there's no std SIGTERM channel, so the
    // node relies on the supervisor tree's drop semantics and ptrace
    // attach for crash diagnostics; SIGINT/SIGTERM do an OS-level exit
    // (same discipline as gateway's fast-exit mode).
    let _sup = sup; // kept alive; threads exit when the process does.
    loop {
        std::thread::sleep(std::time::Duration::from_secs(60));
    }
}

fn handle_connection(engine: &Arc<Mutex<Engine>>, mut stream: TcpStream) {
    let mut head = String::new();
    let mut reader = BufReader::new(match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    });

    // Read request line + headers (tiny; no chunked, no upgrade).
    let mut content_length = 0usize;
    let mut request_line = String::new();
    loop {
        head.clear();
        match reader.read_line(&mut head) {
            Ok(0) | Err(_) => return,
            Ok(_) => {
                let line = head.trim_end();
                if request_line.is_empty() {
                    request_line = line.to_string();
                }
                if let Some((name, value)) = line.split_once(':') {
                    if name.trim().eq_ignore_ascii_case("content-length") {
                        content_length = value.trim().parse().unwrap_or(0);
                    }
                }
                if line.is_empty() {
                    break;
                }
            }
        }
    }

    let mut body = vec![0u8; content_length.min(64 * 1024)];
    if reader.read_exact(&mut body).is_err() {
        write_response(
            &mut stream,
            400,
            "application/json",
            br#"{"error":"truncated request body"}"#,
        );
        return;
    }

    let mut parts = request_line.split_whitespace();
    let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));

    match (method, path) {
        ("GET", "/healthz") => write_response(&mut stream, 200, "text/plain", b"ok"),
        ("GET", "/v1/health") => {
            let engine = engine.lock().unwrap_or_else(|p| p.into_inner());
            write_response(
                &mut stream,
                200,
                "application/json",
                engine.health_json().as_bytes(),
            );
        }
        ("GET", "/metrics") => {
            let engine = engine.lock().unwrap_or_else(|p| p.into_inner());
            let st = &engine.stats;
            let mut body = String::from("# HELP voxdesk_media_udp_frames_total UDP datagrams dispatched.\n# TYPE voxdesk_media_udp_frames_total counter\n");
            body.push_str(&format!(
                "voxdesk_media_udp_frames_total{{kind=\"stun\"}} {}\n",
                st.stun_answered
            ));
            body.push_str(&format!(
                "voxdesk_media_udp_frames_total{{kind=\"rtp\"}} {}\n",
                st.rtp_forwarded
            ));
            body.push_str(&format!(
                "voxdesk_media_udp_frames_total{{kind=\"unknown\"}} {}\n",
                st.unknown_frames
            ));
            body.push_str("# HELP voxdesk_media_rtcp_total RTCP datagrams seen, by outcome.\n# TYPE voxdesk_media_rtcp_total counter\n");
            body.push_str(&format!(
                "voxdesk_media_rtcp_total{{kind=\"sr\"}} {}\n",
                st.rtcp_sr
            ));
            body.push_str(&format!(
                "voxdesk_media_rtcp_total{{kind=\"rr\"}} {}\n",
                st.rtcp_rr
            ));
            body.push_str(&format!(
                "voxdesk_media_rtcp_total{{kind=\"reports_applied\"}} {}\n",
                st.rtcp_reports_applied
            ));
            body.push_str(&format!(
                "voxdesk_media_rtcp_total{{kind=\"unsupported\"}} {}\n",
                st.rtcp_unsupported
            ));
            body.push_str(&format!(
                "voxdesk_media_rtcp_total{{kind=\"malformed\"}} {}\n",
                st.rtcp_malformed
            ));
            body.push_str("# HELP voxdesk_media_rtp_dropped_total RTP datagrams refused, by reason.\n# TYPE voxdesk_media_rtp_dropped_total counter\n");
            body.push_str(&format!(
                "voxdesk_media_rtp_dropped_total{{kind=\"spoof\"}} {}\n",
                st.rtp_drop_spoof
            ));
            body.push_str(&format!(
                "voxdesk_media_rtp_dropped_total{{kind=\"unknown_ssrc\"}} {}\n",
                st.rtp_drop_unknown_ssrc
            ));
            body.push_str(&format!(
                "voxdesk_media_rtp_dropped_total{{kind=\"other\"}} {}\n",
                st.rtp_dropped
            ));
            body.push_str("# HELP voxdesk_media_dtls_total DTLS-SRTP handshakes, by outcome.\n# TYPE voxdesk_media_dtls_total counter\n");
            body.push_str(&format!(
                "voxdesk_media_dtls_total{{outcome=\"established\"}} {}\n",
                st.dtls_established
            ));
            body.push_str(&format!(
                "voxdesk_media_dtls_total{{outcome=\"failed\"}} {}\n",
                st.dtls_failed
            ));
            body.push_str(&format!(
                "voxdesk_media_dtls_total{{outcome=\"profile_refused\"}} {}\n",
                st.dtls_profile_refused
            ));
            body.push_str(&format!(
                "voxdesk_media_publish_refused_total{} {}\n",
                "", st.publish_refused
            ));
            let (rooms, participants, tracks) = engine.routes.stats();
            body.push_str(&format!(
                "# TYPE voxdesk_engine_rooms_current gauge\nvoxdesk_engine_rooms_current {}\n",
                rooms
            ));
            body.push_str(&format!(
                "# TYPE voxdesk_engine_participants_current gauge\nvoxdesk_engine_participants_current {}\n",
                participants
            ));
            body.push_str(&format!(
                "# TYPE voxdesk_engine_tracks_current gauge\nvoxdesk_engine_tracks_current {}\n",
                tracks
            ));
            write_response(
                &mut stream,
                200,
                "text/plain; version=0.0.4",
                body.as_bytes(),
            );
        }
        ("POST", "/v1/signal") => {
            let text = match std::str::from_utf8(&body) {
                Ok(t) => t.to_string(),
                Err(_) => {
                    write_response(&mut stream, 400, "application/json", br#"{"v":1,"id":null,"error":{"code":"bad_message","message":"body must be utf-8"}}"#);
                    return;
                }
            };
            // Envelope- AND bare-shape handled inside the engine: the
            // shape of the reply ALWAYS mirrors the shape of the request
            // (see crates/engine: on_control's documented contract).
            let out = {
                let mut engine = engine.lock().unwrap_or_else(|p| p.into_inner());
                engine.on_control(&text)
            };
            write_response(&mut stream, 200, "application/json", out.as_bytes());
        }
        _ => write_response(
            &mut stream,
            404,
            "application/json",
            br#"{"error":"not found"}"#,
        ),
    }
}

fn write_response(stream: &mut TcpStream, code: u16, content_type: &str, body: &[u8]) {
    let reason = match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        _ => "Internal",
    };
    let head = format!(
        "HTTP/1.1 {code} {reason}\r\ncontent-type: {content_type}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body);
}
