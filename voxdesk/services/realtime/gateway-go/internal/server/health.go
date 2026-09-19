package server

import (
	"net/http"
)

// serveLiveness answers /healthz: the process is up. Mirrors the API's
// /health — liveness says nothing about dependencies or capacity, so a
// kubelet/load balancer can tell "dead process" apart from "busy process".
func (s *Server) serveLiveness(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// serveReadiness answers /readyz: the node is prepared to accept NEW
// sessions. A gateway at its connection ceiling returns 503 so the load
// balancer shifts new browsers to a node with headroom instead of letting
// them hit the upgrade-time 503 — the same "stop receiving work instead of
// failing every request" contract the API's /health/ready documents.
func (s *Server) serveReadiness(w http.ResponseWriter, r *http.Request) {
	current := s.registry.ConnectionsCurrent()
	capacity := int64(s.cfg.MaxConnections)
	headroom := capacity - current
	ready := headroom > 0

	// Engine segment: readiness REFLECTS the media plane. Three states:
	//   disabled — configured-off, explicitly said so (never silently);
	//   up/down  — the monitor's last probe. Down REJECTS readiness: a
	//   node that serves conversations without its SFU is not ready for
	//   traffic, per the deployment contract (the ws plane itself keeps
	//   serving / degrades per-connection independently of this verdict).
	engineCheck := map[string]any{"ok": true, "state": "disabled"}
	engineUp := true
	if s.engine != nil {
		engineUp = s.engine.Up()
		engineCheck = map[string]any{
			"ok":    engineUp,
			"state": map[bool]string{true: "up", false: "down"}[engineUp],
		}
		if !engineUp {
			engineCheck["last_error"] = s.engine.LastError()
		}
		if h := s.engine.LastHealth(); h != nil {
			engineCheck["engine_version"] = h.Version
			engineCheck["rooms"] = h.Rooms
		}
	}
	ready = ready && engineUp

	rooms, tenants := s.hub.Stats()
	body := map[string]any{
		"status":   map[bool]string{true: "ok", false: "unavailable"}[ready],
		"uptime_s": s.uptimeSeconds(),
		"checks": map[string]any{
			"capacity": map[string]any{
				"ok":       ready,
				"current":  current,
				"max":      capacity,
				"headroom": headroom,
			},
			"engine":  engineCheck,
			"rooms":   rooms,
			"tenants": tenants,
		},
	}
	if !ready {
		if headroom <= 0 {
			s.logf("[gateway] readiness unavailable: at connection capacity (%d/%d)", current, capacity)
		} else {
			s.logf("[gateway] readiness unavailable: media engine down (%s)", s.engine.LastError())
		}
		writeJSON(w, http.StatusServiceUnavailable, body)
		return
	}
	writeJSON(w, http.StatusOK, body)
}
