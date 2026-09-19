package server

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/observability"
)

// Handler returns the gateway's single HTTP mux. The route table is the
// whole public surface and is deliberately tiny:
//
//	GET  /ws                  — the WebSocket edge (dashboard clients)
//	POST /ingest/v1/publish   — server-to-server event ingest (the API)
//	GET  /healthz             — liveness (load balancer)
//	GET  /readyz              — readiness (capacity-aware)
//	GET  /metrics             — Prometheus scrape (token-gated when configured)
//	anything else             — JSON 404; unknown paths never receive HTML
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", s.serveWS)
	mux.HandleFunc("/ingest/v1/publish", s.requirePost(s.serveIngest))
	mux.HandleFunc("/healthz", s.requireGet(s.serveLiveness))
	mux.HandleFunc("/readyz", s.requireGet(s.serveReadiness))
	mux.HandleFunc("/metrics", s.requireGet(s.serveMetrics))
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusNotFound, map[string]string{"detail": "not found"})
	})
	// One middleware for the whole surface: every request leaves with a
	// correlation id — the API's inbound X-Request-ID when it sent one
	// (the API → gateway trace continues unbroken), a fresh id otherwise,
	// so even probe traffic is quoteable in logs by id.
	return observability.RequestIDMiddleware(mux)
}

// requireGet rejects non-GET methods with a JSON 405 (no wrong-verb
// ambiguity on a public surface).
func (s *Server) requireGet(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "method not allowed"})
			return
		}
		next(w, r)
	}
}

// requirePost rejects non-POST methods with a JSON 405.
func (s *Server) requirePost(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "method not allowed"})
			return
		}
		next(w, r)
	}
}

// writeJSON is the one response helper every handler uses: JSON body,
// explicit content type, no server-generated HTML anywhere (the Python
// app's error discipline, expressed in Go).
func writeJSON(w http.ResponseWriter, status int, body any) {
	data, err := json.Marshal(body)
	if err != nil {
		status = http.StatusInternalServerError
		data = []byte(`{"detail":"internal error"}`)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(data)
}

// uptimeSeconds backs readiness and the uptime gauge.
func (s *Server) uptimeSeconds() int64 {
	return int64(time.Since(s.startedAt).Seconds())
}
