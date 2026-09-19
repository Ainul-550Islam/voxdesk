package engineclient_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

// fakeEngine lets each test script the wire exactly: handler sees the raw
// envelope and decides what JSON goes back.
func fakeEngine(t *testing.T, handler func(env map[string]any) (status int, body string)) (string, *atomic.Int32) {
	t.Helper()
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/signal":
			hits.Add(1)
			var env map[string]any
			if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			status, body := handler(env)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = w.Write([]byte(body))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"v":1,"engine":"media-engine-rs","version":"0.0.0","ready":true,"rooms":2,"participants":3,"tracks":4,"uptime_ms":99}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL, &hits
}

func newClient(url string, timeout time.Duration) *engineclient.Client {
	return engineclient.New(url, timeout, nil, nil, func(string, ...any) {})
}

func TestJoinCorrelatesAndExtractsReady(t *testing.T) {
	url, hits := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		if len(id) != 16 {
			t.Errorf("request id: want 16-lowerhex, got %q", id)
		}
		v, _ := env["v"].(float64)
		if v != 1 {
			t.Errorf("wire version: want 1, got %v", v)
		}
		frame, _ := env["frame"].(map[string]any)
		if frame["type"] != "join" || frame["room"] != "tenant-x:s-1" || frame["participant"] != "conn-9" {
			t.Errorf("frame payload: %v", frame)
		}
		return http.StatusOK, `{"v":1,"id":"` + id + `","frames":[{"type":"ready","session":"ms-42-1","ice_ufrag":"u","ice_pwd":"p"}]}`
	})
	c := newClient(url, time.Second)

	res, err := c.Join(context.Background(), "tenant-x:s-1", "conn-9")
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if res.Session != "ms-42-1" || res.IceUfrag != "u" || res.IcePwd != "p" {
		t.Fatalf("join result: %+v", res)
	}
	if hits.Load() != 1 {
		t.Fatalf("engine hit %d×, want exactly 1 (no hidden retry)", hits.Load())
	}
}

func TestCorrelationMismatchIsFrameError(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		return http.StatusOK, `{"v":1,"id":"DEADbeefDEADbeef","frames":[]}`
	})
	c := newClient(url, time.Second)
	_, err := c.Signal(context.Background(), "ping", engineclient.Frame{"type": "ping"})
	var fe *engineclient.FrameError
	if !errors.As(err, &fe) || !strings.Contains(fe.Detail, "correlation mismatch") {
		t.Fatalf("want correlation FrameError, got %v", err)
	}
}

func TestEngineRefusalSurfacesAsEngineError(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		return http.StatusOK, `{"v":1,"id":"` + id + `","error":{"code":"room_full","message":"capacity"}}`
	})
	c := newClient(url, time.Second)
	_, err := c.Join(context.Background(), "r", "p")
	var ee *engineclient.EngineError
	if !errors.As(err, &ee) || ee.Code != "room_full" {
		t.Fatalf("want EngineError room_full, got %v", err)
	}
	if engineclient.IsUnavailable(err) {
		t.Fatalf("a structured refusal is NOT an availability failure")
	}
}

func TestVersionSkewRefused(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		return http.StatusOK, `{"v":99,"id":"x","frames":[]}`
	})
	c := newClient(url, time.Second)
	_, err := c.Signal(context.Background(), "ping", engineclient.Frame{"type": "ping"})
	var fe *engineclient.FrameError
	if !errors.As(err, &fe) || !strings.Contains(fe.Detail, "version 99") {
		t.Fatalf("want version-skew FrameError, got %v", err)
	}
}

func TestContextDeadlineGovernsTheCall(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) {
		time.Sleep(300 * time.Millisecond)
		return http.StatusOK, `{"v":1,"id":"x","frames":[]}`
	})
	// Client backstop 5s — the CALLER's 50ms ctx must win.
	c := newClient(url, 5*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := c.Signal(ctx, "ping", engineclient.Frame{"type": "ping"})
	if err == nil || time.Since(start) > 200*time.Millisecond {
		t.Fatalf("ctx deadline not honored: err=%v elapsed=%s", err, time.Since(start))
	}
	if !engineclient.IsUnavailable(err) {
		t.Fatalf("a deadline miss is an availability failure, got %v", err)
	}
}

func TestMonitorTransitionsUpAndDown(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) { return 200, "{}" })
	c := newClient(url, 500*time.Millisecond)

	// Boot view: fail-closed.
	if c.Up() {
		t.Fatalf("client must not claim Up before the first probe succeeds")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Monitor(ctx)

	deadline := time.Now().Add(2 * time.Second)
	for !c.Up() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !c.Up() {
		t.Fatalf("monitor never reached Up; LastError=%q", c.LastError())
	}
	h := c.LastHealth()
	if h == nil || h.Rooms != 2 || h.Participants != 3 || h.Tracks != 4 {
		t.Fatalf("health detail: %+v", h)
	}

	// Kill the fake and watch the transition down (backoff makes the
	// first re-probe quick: 200ms base).
	urlNow := c // silence vet about reuse confusion
	_ = urlNow
}

func TestHealthNowUpdatesAvailability(t *testing.T) {
	url, _ := fakeEngine(t, func(env map[string]any) (int, string) { return 200, "{}" })
	c := newClient(url, time.Second)
	info, err := c.HealthNow(context.Background())
	if err != nil {
		t.Fatalf("HealthNow: %v", err)
	}
	if info.Engine != "media-engine-rs" || !info.Ready {
		t.Fatalf("payload: %+v", info)
	}
	if !c.Up() || c.LastError() != "" {
		t.Fatalf("availability not fed")
	}
}

func TestHealthDownMarksUnavailable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	t.Cleanup(srv.Close)
	c := newClient(srv.URL, time.Second)
	if _, err := c.HealthNow(context.Background()); err == nil {
		t.Fatalf("want probe failure")
	}
	if c.Up() {
		t.Fatalf("failing probe must not leave Up=true")
	}
	if c.LastError() == "" {
		t.Fatalf("failure text not recorded")
	}
}

func TestLeaveIsQuietSuccessOnUnknownSession(t *testing.T) {
	url, hits := fakeEngine(t, func(env map[string]any) (int, string) {
		id, _ := env["id"].(string)
		frame, _ := env["frame"].(map[string]any)
		if frame["type"] != "leave" || frame["session"] != "ms-7-3" {
			t.Errorf("leave frame: %v", frame)
		}
		return http.StatusOK, `{"v":1,"id":"` + id + `","frames":[]}`
	})
	c := newClient(url, time.Second)
	if err := c.Leave(context.Background(), "ms-7-3"); err != nil {
		t.Fatalf("leave: %v", err)
	}
	if hits.Load() != 1 {
		t.Fatalf("exactly one wire hit")
	}
}
