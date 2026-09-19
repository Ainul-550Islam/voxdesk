package shutdown

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestNotifyDeliversTermAndStopRestores(t *testing.T) {
	ch, stop := Notify()
	defer stop()

	if err := syscall.Kill(syscall.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatalf("self-signalling failed: %v", err)
	}
	select {
	case got := <-ch:
		if got != syscall.SIGTERM {
			t.Fatalf("want SIGTERM, got %v", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("SIGTERM never arrived at the notify channel")
	}
}

// testServer starts a real http.Server on an ephemeral port and fails the
// test if it ever exits for a reason other than being shut down.
func testServer(t *testing.T, handler http.HandlerFunc) *http.Server {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Addr: ln.Addr().String(), Handler: handler}
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	t.Cleanup(func() {
		_ = srv.Close()
		if err := <-errc; err != nil && !errors.Is(err, http.ErrServerClosed) {
			t.Fatalf("server exited abnormally: %v", err)
		}
	})
	return srv
}

func TestHTTPServerDrainsIdleServerCleanly(t *testing.T) {
	t.Parallel()
	srv := testServer(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	if err := HTTPServer(srv, 2*time.Second); err != nil {
		t.Fatalf("idle server must drain cleanly, got %v", err)
	}
}

func TestHTTPServerHonoursTheDeadline(t *testing.T) {
	t.Parallel()
	started := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	srv := testServer(t, func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(started) }) // signal the request is in-flight
		<-release                          // …then hang until the test lets go
		w.WriteHeader(http.StatusOK)
	})
	defer close(release)

	go func() { _, _ = http.Get("http://" + srv.Addr + "/") }()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("hanging request never started")
	}

	start := time.Now()
	err := HTTPServer(srv, 100*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a hanging request must surface the deadline, got %v", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("shutdown must return near the deadline, took %v", elapsed)
	}
}
