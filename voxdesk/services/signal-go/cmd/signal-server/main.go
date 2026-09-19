// Command signal-server is the signaling hub as a standalone binary. It
// binds 0.0.0.0:VOXDESK_SIGNAL_PORT (default 8765) and serves until the
// process is terminated — the Go counterpart of the Rust `signal-server`.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	osignal "os/signal"
	"syscall"
	"time"

	"github.com/voxdesk/signal-go/internal/signal"
)

func main() {
	port := os.Getenv("VOXDESK_SIGNAL_PORT")
	if port == "" {
		port = "8765"
	}
	addr := "0.0.0.0:" + port

	hub := signal.NewHub()
	server := signal.NewServer(hub)

	srv := &http.Server{
		Addr:    addr,
		Handler: server,
		// No read/write timeouts here: the hub manages per-connection
		// deadlines itself (idle reaping and write waits).
	}

	go func() {
		log.Printf("signal-server listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	// Graceful shutdown on SIGINT/SIGTERM.
	stop := make(chan os.Signal, 1)
	osignal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
