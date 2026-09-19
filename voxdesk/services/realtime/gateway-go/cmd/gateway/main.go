// Command gateway is the public WebSocket edge as a standalone binary.
//
// It binds 0.0.0.0:VOXDESK_GATEWAY_PORT (default 8790) and serves
//
//	GET  /ws                — browser realtime sessions (hello with a
//	                          dashboard access token, then tenant-pinned rooms)
//	POST /ingest/v1/publish — server-to-server event fan-out (the Python API)
//	GET  /healthz|/readyz   — probes
//	GET  /metrics           — Prometheus scrape
//
// Boot fails closed: with a missing or placeholder JWT secret or ingest
// secret the process refuses to start, exactly as app/core/config.py's
// validate_security() gates the API. There is no development bypass in this
// binary because it is the PUBLIC edge — a misconfigured internal service
// wastes a deploy, a misconfigured edge leaks live call data.
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/shutdown"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

func main() {
	// The boot logger exists before config is validated because config
	// errors themselves must be printable. Level comes straight from the
	// environment (config.Load deliberately holds no logger state);
	// unknown values fall back to Info — silence is never the default.
	logger := observability.New(os.Stderr, "[gateway] ",
		observability.ParseLevel(os.Getenv("VOXDESK_GATEWAY_LOG_LEVEL")))

	cfg, problems, warnings := config.Load(os.Getenv)
	for _, warning := range warnings {
		logger.Warnf("config warning: %s", warning)
	}
	if len(problems) > 0 {
		for _, problem := range problems {
			logger.Errorf("config error: %s", problem)
		}
		logger.Errorf("insecure configuration, refusing to start")
		os.Exit(1)
	}

	h := hub.New(cfg.MaxConnsPerTenant)
	registry := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)

	// The signaling relay shares the JWT edge and the hub's connection
	// registry: sessions bind two of the hub's already-authenticated
	// connections, so no second auth surface exists anywhere in the design.
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, registry)
	stopReaper := signaler.StartReaper()
	defer stopReaper()

	gateway := server.New(cfg, h, registry, verifier, signaler)

	// Media-engine link (Go → Rust control plane). Disabled when the URL
	// is unset (config.Load already warned); when present, the client is
	// built BEFORE serving begins so readiness has a deterministic view
	// from the first probe, and its availability monitor runs for as long
	// as the process does. The observer feeds the /metrics engine series;
	// transitions also flip the engine_up gauge exactly once per change.
	engMonitorCtx, stopEngMonitor := context.WithCancel(context.Background())
	defer stopEngMonitor()
	if cfg.MediaEngineURL != "" {
		eng := engineclient.New(
			cfg.MediaEngineURL,
			time.Duration(cfg.MediaEngineTimeoutSeconds*float64(time.Second)),
			engineMetricsObserver{registry},
			nil,
			nil,
		)
		gateway.SetEngine(eng)
		go eng.Monitor(engMonitorCtx)
		if cfg.EngineSteer != config.EngineSteerOff {
			logger.Infof("engine steer mode %s ACTIVE: steered sessions route browser media through the engine", cfg.EngineSteer)
		}
		logger.Infof("media engine at %s (timeout=%.2fs, steer=%s); monitor running", cfg.MediaEngineURL, cfg.MediaEngineTimeoutSeconds, cfg.EngineSteer)
	} else {
		logger.Warnf("VOXDESK_GATEWAY_MEDIA_ENGINE_URL unset: media-engine plane DISABLED (readiness reports engine: disabled)")
	}
	// Structured operational events (ingest publishes/rejects with the
	// API's request id attached) go to stdout as JSON lines — the same
	// stream shape the Python app's structured logs ship.
	gateway.UseEmitter(observability.NewLogEmitter(
		observability.New(os.Stdout, "[gateway-events] ", observability.Info)))

	srv := &http.Server{
		Addr:    listenAddr(cfg),
		Handler: gateway.Handler(),
		// Slowloris defence on the HTTP phase only. Read/Write timeouts are
		// deliberately UNSET: a WebSocket session is long-lived by design,
		// and the per-connection deadlines (auth timeout, heartbeat) own the
		// post-upgrade phase.
		ReadHeaderTimeout: 10 * time.Second,
	}
	srv.RegisterOnShutdown(gateway.CloseAll)

	go func() {
		logger.Infof("listening on %s (max_connections=%d, per_tenant=%d, signal_sessions_per_tenant=%d, signal_pending_timeout=%s)",
			srv.Addr, cfg.MaxConnections, cfg.MaxConnsPerTenant,
			cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Errorf("listen: %v", err)
			os.Exit(1)
		}
	}()

	// Graceful shutdown on SIGINT/SIGTERM (internal/shutdown owns the
	// contract): stop accepting, ask every session to close (1001 Going
	// Away via RegisterOnShutdown above), give in-flight frames a bounded
	// window to drain, then exit. A browser that ignores the close frame
	// never holds the process past ShutdownTimeout.
	signals, stopNotify := shutdown.Notify()
	defer stopNotify()
	<-signals

	if err := shutdown.HTTPServer(srv, cfg.ShutdownTimeout); err != nil {
		logger.Errorf("shutdown: %v", err)
	}
	logger.Infof("stopped")
}

// listenAddr formats the bind address for config.Port.
func listenAddr(cfg config.Config) string {
	return "0.0.0.0:" + strconv.Itoa(cfg.Port)
}

// engineMetricsObserver adapts the engineclient's typed observation hook
// onto the metrics registry — the package's declared seam for keeping
// transport facts (latency, failures, availability) OUT of the client and
// INSIDE the platform's one expositions surface.
type engineMetricsObserver struct{ reg *metrics.Registry }

func (o engineMetricsObserver) ObserveSignal(op string, took time.Duration, err error) {
	o.reg.EngineCall(op, took, err != nil)
}

func (o engineMetricsObserver) ObserveHealth(took time.Duration, err error) {
	o.reg.EngineProbe(took, err != nil)
}

func (o engineMetricsObserver) EngineUpChanged(up bool) {
	o.reg.SetEngineUp(up)
}
