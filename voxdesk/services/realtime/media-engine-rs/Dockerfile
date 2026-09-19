# media-engine-rs — production image for the Rust media engine (SFU).
#
# Two stages mirror the gateway-go image discipline:
#   build: rust:bookworm, workspace-locked release build (Cargo.lock is
#          the ONLY dependency ground truth — pinned at audit time, the
#          same rule requirements.txt follows for Python).
#   run:   debian:bookworm-slim, non-root, no added packages.
#
# Build context MUST be this directory (services/realtime/media-engine-rs):
# the workspace manifest, the pinned Cargo.lock, and every crate+bin live
# relative to it.
FROM rust:1.90-bookworm AS build
WORKDIR /src

COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY bins ./bins

RUN cargo build --release --locked -p media-engine && \
    strip target/release/media-engine

FROM debian:bookworm-slim AS run
RUN groupadd --system --gid 10001 voxdesk && \
    useradd --system --uid 10001 --gid voxdesk --no-create-home --shell /usr/sbin/nologin voxdesk

COPY --from=build /src/target/release/media-engine /usr/local/bin/media-engine

# Addressing (all env-driven by the binary itself — the keys below are
# EXACTLY the ones bins/media-engine/src/main.rs reads):
#   VOXDESK_PORT          media (UDP+STUN/RTP/RTCP) port, bound 0.0.0.0
#   VOXDESK_CONTROL_ADDR  gateway-facing control endpoint; must be 0.0.0.0
#                         inside a container (loopback default is for
#                         local dev only)
#   VOXDESK_PUBLIC_IP     the candidate IPv4 advertised in SDP answers —
#                         per-deploy host address, NEVER a private guess
#   VOXDESK_ENGINE_LABEL  fleet label for SSRC-allocator entropy / logs
ENV VOXDESK_PORT=5000 \
    VOXDESK_CONTROL_ADDR=0.0.0.0:9001 \
    VOXDESK_ENGINE_LABEL=edge-1
USER voxdesk
EXPOSE 5000/udp
EXPOSE 9001/tcp

# Liveness through the control plane (bash built-in TCP probe; the run
# stage ships no curl and none is added for a probe).
HEALTHCHECK --interval=15s --timeout=3s --retries=5 --start-period=5s \
  CMD ["/bin/bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9001 && echo -e 'GET /v1/health HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n' >&3 && grep -q ready <&3"]

ENTRYPOINT ["/usr/local/bin/media-engine"]
