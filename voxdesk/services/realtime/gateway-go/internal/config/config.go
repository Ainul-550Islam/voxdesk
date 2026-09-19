// Package config loads the gateway's environment, mirroring the discipline
// of app/core/config.py: everything comes from the environment, every knob
// has a safe default or a loud refusal, and obviously-unset secrets are
// rejected at boot rather than discovered on the first forged connection.
package config

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// Defaults. The timeout trio is chosen for browser clients on mobile
// networks: pings frequent enough to keep NAT bindings and proxies (which
// commonly reap at 60 s) alive, a pong budget generous enough for a
// backgrounded tab to catch up, and a short pre-auth window so a socket that
// never identifies itself cannot hold a slot.
const (
	DefaultPort                    = 8790
	DefaultMaxConnections          = 10_000
	DefaultMaxConnsPerTenant       = 256
	DefaultMaxSubscriptionsPerConn = 32
	DefaultOutgoingBuffer          = 64
	DefaultMaxMessageBytes         = 16 * 1024
	DefaultMaxIngestPayloadBytes   = 64 * 1024
	DefaultWriteWait               = 10 * time.Second
	DefaultAuthTimeout             = 10 * time.Second
	DefaultPingInterval            = 20 * time.Second
	DefaultPongTimeout             = 60 * time.Second
	DefaultShutdownTimeout         = 10 * time.Second
	DefaultMessageRatePerSecond    = 20.0
	DefaultMessageBurst            = 40.0
	DefaultIdempotencyTTL          = 30 * time.Minute

	// Signaling relay (WebRTC session setup). DefaultSignalingMaxSessions
	// per tenant is comfortably below the per-tenant connection cap — a
	// session spends TWO connections only when fully joined, and 64 live
	// negotiations against 256 allowed sockets keeps notice-plane tabs
	// unstarved even at signaling saturation. The pending timeout is how
	// long a never-joined session may linger before the reaper ends it:
	// long enough for a QR-code/handover flow, too short to stockpile.
	DefaultSignalingMaxSessionsPerTenant = 64
	DefaultSignalingPendingTimeout       = 60 * time.Second

	// Engine steer mode constants (closed vocabulary, gate above).
	EngineSteerOff   = "off"
	EngineSteerV12   = "v1.2"
	EngineSteerForce = "force"

	// Media-engine backstop when a caller carries no deadline of its own.
	// 1.5s mirrors REALTIME_PUBLISH_TIMEOUT_SECONDS on the Python side:
	// any engine slower than that is, for session-setup purposes, DOWN —
	// the session lives on in degraded mode and readiness goes red.
	DefaultMediaEngineTimeoutSeconds = 1.5

	// idempotencyCapacity bounds the ingest replay cache. 50k event ids at
	// well under 100 bytes each is a few MB — cheap insurance that can never
	// grow into a memory problem of its own.
	idempotencyCapacity = 50_000
)

// Config is the immutable runtime configuration, built once at boot.
type Config struct {
	Port int

	// JWT verification of dashboard access tokens (HS256, shared with the
	// Python API — see app/auth/jwt.py). The tenant a connection may see is
	// derived from the verified token, never from anything the client sends
	// afterwards.
	JWTSecret   string
	JWTIssuer   string
	JWTAudience string

	// Shared secret for the server-to-server ingest endpoint
	// (POST /ingest/v1/publish). Compared in constant time. The Python
	// backend publishes realtime events here; browsers never can.
	IngestSecret string

	// Browser origins allowed to open the WebSocket. Empty means only
	// same-origin/socket clients (gorilla rejects cross-origin by default),
	// which is the fail-closed posture for a machine-only deployment.
	AllowedOrigins []string

	// Token for /metrics, mirroring METRICS_TOKEN on the Python side. Empty
	// leaves /metrics unauthenticated — acceptable only on a network the
	// scheduler/Prometheus already controls (the compose topology), so it is
	// a warning, not a refusal.
	MetricsToken string

	MaxConnections          int
	MaxConnsPerTenant       int
	MaxSubscriptionsPerConn int
	OutgoingBuffer          int
	MaxMessageBytes         int64
	MaxIngestPayloadBytes   int64

	WriteWait       time.Duration
	AuthTimeout     time.Duration
	PingInterval    time.Duration
	PongTimeout     time.Duration
	ShutdownTimeout time.Duration

	// Per-connection inbound frame limiter: a browser tab that has gone
	// berserk must not be able to spend hub CPU unboundedly.
	MessageRatePerSecond float64
	MessageBurst         float64

	// Ingest replay window: how long an event_id is remembered as "already
	// delivered". Sized for provider retries and short deploys, not for
	// forever — realtime events are worthless redelivered an hour late.
	IdempotencyTTL      time.Duration
	IdempotencyCapacity int

	// Signaling relay: concurrent point-to-point sessions per tenant, and
	// the lone-pending-session timeout the reaper applies.
	SignalingMaxSessionsPerTenant int
	SignalingPendingTimeout       time.Duration

	// Ingest fan-out transport (internal/broker). "memory" (default) is
	// the single-node broker: Publish delivers to local subscribers
	// synchronously and that is THE fan-out. "redis" adds a cross-node
	// Redis pub/sub hop on top (multiple gateway replicas each deliver to
	// THEIR local subscribers), with RedisURL pointing at the bus.
	BrokerKind string
	RedisURL   string

	// Media engine (services/realtime/media-engine-rs). Empty URL = the
	// engine plane is intentionally DISABLED (single-node dev); set in
	// staging/prod. When set, readiness reflects ENGINE availability per
	// the readiness contract — a gateway that cannot reach its SFU is not
	// ready to serve media sessions, though the websocket plane keeps
	// degrading gracefully per-connection.
	MediaEngineURL            string
	MediaEngineTimeoutSeconds float64

	// EngineSteer selects the browser media path: "off" (default; P2P
	// relay only, v1.0/v1.1), "v1.2" (steer depends on both members
	// upgrading via hello "ws":2; mixed pairs stay P2P), "force" (every
	// session steers regardless — the kill-switch-flipped operational
	// posture after migration). Validated against the closed set below; steer without
	// a MediaEngineURL is a boot REFUSAL (silent degrade would make calls
	// hang at ICE with zero surfacing).
	EngineSteer string
}

// Load reads the environment (through getenv, so tests inject a map) and
// returns the Config plus a list of hard problems; len(problems) > 0 means
// REFUSE to boot. Warnings that do not block boot are returned as a second
// list, mirroring validate_security()'s log-but-continue items.
func Load(getenv func(string) string) (Config, []string, []string) {
	cfg := Config{
		Port:                          envInt(getenv, "VOXDESK_GATEWAY_PORT", DefaultPort),
		JWTSecret:                     strings.TrimSpace(getenv("VOXDESK_GATEWAY_JWT_SECRET")),
		JWTIssuer:                     envStr(getenv, "VOXDESK_GATEWAY_JWT_ISSUER", "voxdesk"),
		JWTAudience:                   envStr(getenv, "VOXDESK_GATEWAY_JWT_AUDIENCE", "voxdesk-api"),
		IngestSecret:                  strings.TrimSpace(getenv("VOXDESK_GATEWAY_INGEST_SECRET")),
		AllowedOrigins:                envList(getenv, "VOXDESK_GATEWAY_ALLOWED_ORIGINS"),
		MetricsToken:                  strings.TrimSpace(getenv("VOXDESK_GATEWAY_METRICS_TOKEN")),
		MaxConnections:                envInt(getenv, "VOXDESK_GATEWAY_MAX_CONNECTIONS", DefaultMaxConnections),
		MaxConnsPerTenant:             envInt(getenv, "VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT", DefaultMaxConnsPerTenant),
		MaxSubscriptionsPerConn:       DefaultMaxSubscriptionsPerConn,
		OutgoingBuffer:                DefaultOutgoingBuffer,
		MaxMessageBytes:               DefaultMaxMessageBytes,
		MaxIngestPayloadBytes:         DefaultMaxIngestPayloadBytes,
		WriteWait:                     envDuration(getenv, "VOXDESK_GATEWAY_WRITE_WAIT", DefaultWriteWait),
		AuthTimeout:                   envDuration(getenv, "VOXDESK_GATEWAY_AUTH_TIMEOUT", DefaultAuthTimeout),
		PingInterval:                  envDuration(getenv, "VOXDESK_GATEWAY_PING_INTERVAL", DefaultPingInterval),
		PongTimeout:                   envDuration(getenv, "VOXDESK_GATEWAY_PONG_TIMEOUT", DefaultPongTimeout),
		ShutdownTimeout:               DefaultShutdownTimeout,
		MessageRatePerSecond:          DefaultMessageRatePerSecond,
		MessageBurst:                  DefaultMessageBurst,
		IdempotencyTTL:                DefaultIdempotencyTTL,
		IdempotencyCapacity:           idempotencyCapacity,
		SignalingMaxSessionsPerTenant: envInt(getenv, "VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT", DefaultSignalingMaxSessionsPerTenant),
		SignalingPendingTimeout:       envDuration(getenv, "VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT", DefaultSignalingPendingTimeout),
		BrokerKind:                    strings.ToLower(envStr(getenv, "VOXDESK_GATEWAY_BROKER", "memory")),
		RedisURL:                      strings.TrimSpace(getenv("VOXDESK_GATEWAY_REDIS_URL")),
		MediaEngineURL:                strings.TrimSpace(getenv("VOXDESK_GATEWAY_MEDIA_ENGINE_URL")),
		MediaEngineTimeoutSeconds:     envFloat(getenv, "VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS", DefaultMediaEngineTimeoutSeconds),
		EngineSteer:                   strings.ToLower(envStr(getenv, "VOXDESK_GATEWAY_ENGINE_STEER", "off")),
	}

	var problems, warnings []string

	if cfg.Port < 1 || cfg.Port > 65535 {
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_PORT must be 1..65535, got %d", cfg.Port))
	}

	// The whole public edge stands on this secret. Running without it is not
	// "development mode", it is "anyone can read any tenant's live call feed".
	switch {
	case cfg.JWTSecret == "":
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET is required; it must match the API's JWT_SECRET")
	case validate.LooksPlaceholder(cfg.JWTSecret):
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET looks like a placeholder")
	case len(cfg.JWTSecret) < 32:
		problems = append(problems, "VOXDESK_GATEWAY_JWT_SECRET must be at least 32 characters")
	}

	switch {
	case cfg.IngestSecret == "":
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET is required; without it the ingest endpoint would be an unauthenticated publish-into-any-tenant path")
	case validate.LooksPlaceholder(cfg.IngestSecret):
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET looks like a placeholder")
	case len(cfg.IngestSecret) < 16:
		problems = append(problems, "VOXDESK_GATEWAY_INGEST_SECRET must be at least 16 characters")
	}

	if cfg.MaxConnections < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_MAX_CONNECTIONS must be at least 1")
	}
	if cfg.MaxConnsPerTenant < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT must be at least 1")
	}
	if cfg.PingInterval <= 0 || cfg.PongTimeout <= cfg.PingInterval {
		problems = append(problems, "VOXDESK_GATEWAY_PONG_TIMEOUT must be greater than VOXDESK_GATEWAY_PING_INTERVAL (a peer is only dead when it has missed at least one whole ping round-trip)")
	}
	if cfg.AuthTimeout <= 0 {
		problems = append(problems, "VOXDESK_GATEWAY_AUTH_TIMEOUT must be positive")
	}
	if cfg.SignalingMaxSessionsPerTenant < 1 {
		problems = append(problems, "VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT must be at least 1")
	}
	if cfg.SignalingPendingTimeout <= 0 {
		problems = append(problems, "VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT must be positive (a lone never-joined session must eventually be reaped)")
	}
	// Broker selection must be explicit or absent: a typo here ("rediss")
	// silently running the single-node broker in a multi-replica deploy
	// would deliver every event to only a fraction of connected clients,
	// so it is a boot REFUSAL, not a warning.
	switch cfg.BrokerKind {
	case "memory":
		if cfg.RedisURL != "" {
			warnings = append(warnings, "VOXDESK_GATEWAY_REDIS_URL is set but VOXDESK_GATEWAY_BROKER is memory; the URL is ignored")
		}
	case "redis":
		if cfg.RedisURL == "" {
			problems = append(problems, "VOXDESK_GATEWAY_BROKER=redis requires VOXDESK_GATEWAY_REDIS_URL (redis://[user:pass@]host:port[/db])")
		} else if !strings.HasPrefix(cfg.RedisURL, "redis://") {
			problems = append(problems, "VOXDESK_GATEWAY_REDIS_URL must start with redis:// (TLS-only rediss:// URLs are rejected: this edge terminates no broker TLS today)")
		}
	default:
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_BROKER must be memory or redis, got %q", cfg.BrokerKind))
	}

	// Media-engine wiring must be explicit and well-formed when present:
	// a typo here would silently run the gateway degraded with nobody told,
	// and readiness would mislead every orchestrator behind it.
	if cfg.MediaEngineURL != "" {
		if !strings.HasPrefix(cfg.MediaEngineURL, "http://") && !strings.HasPrefix(cfg.MediaEngineURL, "https://") {
			problems = append(problems, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL must start with http:// or https:// (bare host:port is rejected — scheme discipline is the transport contract)")
		}
		if cfg.MediaEngineTimeoutSeconds <= 0 || cfg.MediaEngineTimeoutSeconds > 30 {
			problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS must be in (0, 30], got %v", cfg.MediaEngineTimeoutSeconds))
		}
	} else {
		warnings = append(warnings, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL is empty; the media engine plane is DISABLED — readiness will report engine: disabled (dev single-node only)")
	}

	switch cfg.EngineSteer {
	case EngineSteerOff:
		if getenv("VOXDESK_GATEWAY_ENGINE_STEER") != "" && cfg.MediaEngineURL != "" {
			warnings = append(warnings, "VOXDESK_GATEWAY_ENGINE_STEER=off: engine link exists but all browser media stays P2P relay")
		}
	case EngineSteerV12, EngineSteerForce:
		if cfg.MediaEngineURL == "" {
			problems = append(problems, "VOXDESK_GATEWAY_ENGINE_STEER requires VOXDESK_GATEWAY_MEDIA_ENGINE_URL — steering to an unconfigured engine would hang every call at ICE")
		}
	default:
		problems = append(problems, fmt.Sprintf("VOXDESK_GATEWAY_ENGINE_STEER must be off, v1.2 or force, got %q", cfg.EngineSteer))
	}

	if cfg.MetricsToken == "" {
		warnings = append(warnings, "VOXDESK_GATEWAY_METRICS_TOKEN is empty; /metrics is unauthenticated — only acceptable on a private network")
	}
	if len(cfg.AllowedOrigins) == 0 {
		warnings = append(warnings, "VOXDESK_GATEWAY_ALLOWED_ORIGINS is empty; cross-origin browser connections will be rejected (same-origin and non-browser clients still work)")
	}

	return cfg, problems, warnings
}

func envStr(getenv func(string) string, key, fallback string) string {
	if v := strings.TrimSpace(getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envList(getenv func(string) string, key string) []string {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func envInt(getenv func(string) string, key string, fallback int) int {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return n
}

// envFloat accepts decimal values ("1.5" seconds is the natural unit the
// production env files documented); a non-numeric value falls back rather
// than misparsing — the LOG line points operators at the exact key.
func envFloat(getenv func(string) string, key string, fallback float64) float64 {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return v
}

func envDuration(getenv func(string) string, key string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return fallback
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return fallback
	}
	return d
}
