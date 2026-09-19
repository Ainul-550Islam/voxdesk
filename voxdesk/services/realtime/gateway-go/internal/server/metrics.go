package server

import (
	"net/http"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
)

// serveMetrics answers /metrics with the Prometheus text exposition.
//
// Access control mirrors the API's METRICS_TOKEN: when a token is
// configured, the scraper must present it as `Authorization: Bearer <token>`
// (header only — a query-param token would land in access logs, which is
// exactly where a scrape credential must never appear). When no token is
// configured the endpoint is open, acceptable only on the private compose
// network Prometheus already lives on; config.Load surfaces that as a
// boot-time warning so it is never a surprise.
func (s *Server) serveMetrics(w http.ResponseWriter, r *http.Request) {
	if s.cfg.MetricsToken != "" {
		if !bearerMatches(r.Header.Get("Authorization"), s.cfg.MetricsToken) {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "invalid metrics token"})
			return
		}
	}

	rooms, tenants := s.hub.Stats()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(s.registry.Render(rooms, tenants)))
}

// bearerMatches compares a presented `Bearer x` credential with the
// expected one in constant time, via the shared auth helpers (one
// comparison implementation for every secret this edge sees — the whole
// point of the package split).
func bearerMatches(header, expected string) bool {
	token, ok := auth.ExtractBearer(header)
	if !ok {
		return false
	}
	return auth.ConstantTimeTokenEqual(token, expected)
}
