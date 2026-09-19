# Realtime gateway image — two stages, static binary, non-root.
#
# Build from the compose root: the build context is THIS directory, so
# nothing outside services/realtime/gateway-go/ is reachable on purpose —
# the gateway is a standalone Go module with exactly one dependency
# (gorilla/websocket, pinned in go.sum and verified by `go mod download`).

# ---- build -------------------------------------------------------------------
FROM golang:1.27-alpine AS build
WORKDIR /src

# Copy manifests first and resolve modules as their own layer: source edits
# must not bust the dependency cache, and go.sum verification happens here,
# not in the running container.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO off: the runtime stage carries no libc and no toolchain. -trimpath so
# the binary embeds no builder-host paths; -buildvcs=false because the build
# context may not be a git checkout (compose builds from an exported tree).
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -buildvcs=false \
    -o /out/gateway ./cmd/gateway

# ---- run ---------------------------------------------------------------------
FROM alpine:3.21

# ca-certificates for any future TLS outbound; wget (busybox) backs the
# HEALTHCHECK; a dedicated non-root user runs the binary — the gateway binds
# only :8790 and holds secrets in memory, and root buys it nothing.
RUN apk add --no-cache ca-certificates \
 && addgroup -S gateway \
 && adduser -S -G gateway -H -h /nonexistent gateway

USER gateway:gateway
COPY --from=build /out/gateway /usr/local/bin/gateway

EXPOSE 8790

# /healthz is liveness-only (process up, mux answering); /readyz additionally
# reports capacity and is what an orchestrator should gate rollouts on.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8790/healthz || exit 1

# Secrets arrive exclusively through the environment
# (VOXDESK_GATEWAY_JWT_SECRET / VOXDESK_GATEWAY_INGEST_SECRET); the process
# refuses to boot on missing, placeholder, or weak values, and that refusal
# is observable: the container exits non-zero before binding the port.
ENTRYPOINT ["/usr/local/bin/gateway"]
