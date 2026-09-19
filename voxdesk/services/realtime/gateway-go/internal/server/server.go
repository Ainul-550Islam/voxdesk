// Package server wires the public edge together: the HTTP mux (websocket
// upgrade, health, readiness, metrics, ingest), the shared gateway state
// (hub, verifier, registry), and upgrade-time policy (capacity, origin).
package server

import (
	"log"
	"net/http"
	"sync"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/idempotency"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/presence"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
	wsconn "github.com/voxdesk/realtime/gateway-go/internal/websocket"
)

// Server is the gateway's front door. Build with New, expose through
// Handler, terminate with CloseAll.
type Server struct {
	cfg       config.Config
	hub       *hub.Hub
	registry  *metrics.Registry
	verifier  *auth.Verifier
	signaler  *signaling.Router
	upgrader  gorilla.Upgrader
	replay    *idempotency.Store
	presence  *presence.Registry
	events    observability.Emitter
	bus       broker.Broker
	closeOnce sync.Once
	startedAt time.Time
	logf      func(format string, args ...any)

	// engine is the media-plane availability view; nil means the engine
	// plane is intentionally disabled (config.MediaEngineURL == "") and
	// readiness reports `engine: disabled` instead of guessing.
	engine *engineclient.Client
}

// SetEngine attaches the media-engine client AFTER construction. It is a
// separate setter (not a New parameter) because New's arity is a public
// contract for the existing wiring paths and tests; the engine link was
// added without reshaping them. MUST be called before Handler is served.
func (s *Server) SetEngine(eng *engineclient.Client) {
	s.engine = eng
	if s.signaler != nil {
		s.signaler.SetEngine(eng)
		s.signaler.SetSteerMode(s.cfg.EngineSteer)
	}
}

// Engine exposes the attached client (nil when disabled) — the readiness
// handler and the supervisor loop read availability through it.
func (s *Server) Engine() *engineclient.Client { return s.engine }

// New returns the fully wired server. signaler may be nil — a deployment
// that only wants the notice plane gets clean refusals on signaling frames
// instead of a crash (the read loop checks; nothing here dereferences it
// other than handing it to connections).
func New(cfg config.Config, h *hub.Hub, reg *metrics.Registry, verifier *auth.Verifier, signaler *signaling.Router) *Server {
	s := &Server{
		cfg:       cfg,
		hub:       h,
		registry:  reg,
		verifier:  verifier,
		signaler:  signaler,
		upgrader:  wsconn.NewUpgrader(cfg),
		replay:    idempotency.NewStore(cfg.IdempotencyTTL, cfg.IdempotencyCapacity),
		presence:  presence.New(),
		events:    observability.NopEmitter,
		startedAt: time.Now(),
		logf:      log.Printf,
	}
	// The presence gauge is fed by subscription (not polled at scrape
	// time): every real transition publishes its authoritative post-change
	// total, so the gauge self-heals instead of drifting on a missed
	// decrement. Emitting on change is also why presence.OnChange exists.
	s.presence.OnChange(func(ev presence.Event) {
		reg.SetPresenceUsers(int64(ev.TotalUsers))
	})

	// The ingest fan-out transport (internal/broker). "memory" (default):
	// Publish IS the hub fan-out, synchronous, so ingest responses keep
	// their exact (delivered, dropped) meaning. "redis": the same LOCAL
	// publish plus a pub/sub hop for other replicas, own-echo suppressed.
	s.bus = newBrokerFromConfig(cfg, s.logf)
	s.bus.Subscribe(ingestTopic, s.handleIngestEnvelope)
	return s
}

// newBrokerFromConfig builds the configured transport. An unparsable redis
// URL cannot reach here (config.Load refuses boot on it); a second-layer
// parse failure degrades to the memory broker with a loud log rather than
// a boot crash — the single-node fan-out still works, which is exactly
// what a manual failover to VOXDESK_GATEWAY_BROKER=memory would produce.
func newBrokerFromConfig(cfg config.Config, logf func(string, ...any)) broker.Broker {
	if cfg.BrokerKind == "redis" {
		rb, err := broker.NewRedis(cfg.RedisURL, broker.WithRedisLogger(logf))
		if err != nil {
			logf("[gateway] BROKER DEGRADED: redis init failed (%v); falling back to memory broker", err)
			return broker.NewMemory()
		}
		return rb
	}
	return broker.NewMemory()
}

// UseEmitter replaces the (default: no-op) structured event sink. main
// installs a LogEmitter at boot; tests leave the nop in place so behaviour
// stays byte-for-byte deterministic.
func (s *Server) UseEmitter(em observability.Emitter) {
	if em != nil {
		s.events = em
	}
}

// Hub exposes the registry for shutdown (main calls CloseAll through it).
func (s *Server) Hub() *hub.Hub { return s.hub }

// Signaler exposes the relay for lifecycle wiring (main starts the reaper).
func (s *Server) Signaler() *signaling.Router { return s.signaler }

// Config exposes the runtime configuration to the handler files.
func (s *Server) Config() config.Config { return s.cfg }

// Registry exposes the metric counters to the handler files.
func (s *Server) Registry() *metrics.Registry { return s.registry }

// Presence exposes the node-local presence registry (tenant → online
// users). The /metrics handler renders its TotalUsers gauge from it.
func (s *Server) Presence() *presence.Registry { return s.presence }

// Broker exposes the ingest fan-out transport (diagnostics: Kind/NodeID;
// redis-mode accounting when enabled).
func (s *Server) Broker() broker.Broker { return s.bus }

// serveWS upgrades one HTTP request and runs the session to completion.
// Any pre-upgrade refusal is a plain HTTP response (the client never became
// a websocket peer); anything after the upgrade is the lifecycle's concern.
func (s *Server) serveWS(w http.ResponseWriter, r *http.Request) {
	// Global capacity gate, checked BEFORE the upgrade so a flood of
	// sockets is refused cheaply (HTTP 503) rather than upgraded and then
	// closed expensively. Read from the registry gauge: the hub counts
	// authenticated sockets, but the cap must cover pre-auth ones too
	// (that is precisely the flood shape).
	if s.registry.ConnectionsCurrent() >= int64(s.cfg.MaxConnections) {
		s.registry.ConnRefused()
		http.Error(w, http.StatusText(http.StatusServiceUnavailable), http.StatusServiceUnavailable)
		return
	}

	socket, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		// gorilla has already written the refusal response (origin policy,
		// handshake shape); nothing more to say.
		s.registry.ConnRefused()
		return
	}

	conn, err := wsconn.NewConnection(socket, s.cfg, s.hub, s.verifier, s.registry, s.signaler, s.presence)
	if err != nil {
		_ = socket.Close()
		s.logf("[gateway] session id generation failed: %v", err)
		return
	}
	conn.Serve()
}

// CloseAll asks every live connection to close (server shutdown) and
// stops the ingest bus (the redis supervisor's reconnect loop must not
// outlive the process's listen loop). Idempotent: http.Server calls it
// through RegisterOnShutdown, and main never calls it twice — but an
// orchestrated double-shutdown must not double-close channels.
func (s *Server) CloseAll() {
	s.closeOnce.Do(func() {
		s.hub.CloseAll(protocol.CloseGoingAway, "server shutting down")
		if err := s.bus.Close(); err != nil {
			s.logf("[gateway] broker close: %v", err)
		}
	})
}
