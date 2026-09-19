// Package shutdown owns the gateway's termination contract, extracted from
// cmd/gateway/main.go so the policy (which signals, which deadline, which
// drain order) is a tested unit instead of a dozen inline lines.
//
// The contract itself is unchanged:
//
//   - SIGINT or SIGTERM begins shutdown (Kubernetes sends TERM on pod
//     eviction; Ctrl-C sends INT in development);
//   - the HTTP server stops accepting new work and asks every live
//     WebSocket session to close (http.Server.RegisterOnShutdown →
//     hub.CloseAll with 1001 Going Away);
//   - in-flight frames get a bounded drain window: a browser that ignores
//     its close frame never holds the process past ShutdownTimeout.
package shutdown

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// Notify returns a buffered channel carrying the shutdown-triggering
// signals (SIGINT, SIGTERM) and a stop function that detaches it.
// Buffered with capacity 2: a double-TERM (impatient operator, or TERM
// followed by the orchestrator's escalation) must be DROP-able without
// blocking the sender — the gateway exits on the FIRST signal and does not
// implement a hard-kill escalation path, so queuing more would only park
// the kernel's signal delivery.
//
// Callers own calling stop() (typically defer right after Notify), which
// restores default handling: crucial in shared processes and tests, where
// a leaked Notify would swallow a later TERM meant for someone else.
func Notify() (<-chan os.Signal, func()) {
	ch := make(chan os.Signal, 2)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	return ch, func() { signal.Stop(ch) }
}

// HTTPServer gracefully shuts srv down, giving in-flight work at most
// timeout to finish. It is a thin, honest wrapper over srv.Shutdown: the
// timeout is applied as a context deadline (http.Server's OWN drain
// semantics — close listeners, idle connections immediately, active ones
// at request end), and the returned error is whatever Shutdown reports
// (context.DeadlineExceeded when the window expired, nil on a clean
// drain). Registered OnShutdown callbacks (the gateway's CloseAll) run as
// part of the drain, exactly as net/http documents.
//
// The timeout applies to the drain, not to time spent waiting — a caller
// that wants a whole-phase budget owns the wait itself.
func HTTPServer(srv *http.Server, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return srv.Shutdown(ctx)
}
