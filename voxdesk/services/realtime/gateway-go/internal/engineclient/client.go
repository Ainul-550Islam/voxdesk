// Package engineclient is the gateway's typed, instrumented HTTP client for
// the Rust media engine's control plane (services/realtime/media-engine-rs).
//
// The protocol is the ENGINE's documented wire (see crates/engine on the
// Rust side — the text below is the same contract, stated here once, in
// Go terms):
//
//	Request:  POST {url}/v1/signal
//	          {"v":1,"id":"<16 lower-hex>","frame":{ ...ClientFrame... }}
//	Success:  200 {"v":1,"id":"<same>","frames":[ ...ServerFrame... ]}
//	Rejected: 200 {"v":1,"id":"<same-or-null>","error":{"code":..,"message":..}}
//	Health:   GET  {url}/v1/health
//	          200 {"v":1,"engine":"media-engine-rs","ready":true,...}
//
// Semantics, by requirement:
//
//   - Correlation: every request stamps a fresh 16-hex id; the reply MUST
//     echo it or the call fails FrameError — a mismatched reply belongs to
//     another request (proxying/stale-node bug) and is never delivered.
//   - Timeouts/cancellation: the caller's context governs end-to-end; the
//     client's Timeout is the backstop when no deadline came down. There
//     is no helper that can outlive either.
//   - Retries: NONE inside the client. Engine joins mint fresh state, so a
//     blind resubmit is a duplicate-leak; leaves are teardown and the
//     engine treats unknown sessions as quiet success, so callers needing
//     "retry a leave" can just call again — the idempotency lives in the
//     engine's API, not in re-submitted packets here.
//   - Liveness ("reconnect" policy): there is no long-lived connection to
//     reconnect; availability is a STATE MACHINE the Monitor loop drives
//     by health-probing with backoff (200ms → 5s). Callers read Up() to
//     decide whether to caller-fail fast; the Monitor itself is the only
//     writer, so Up/last-known-health are always a coherent pair.
//   - Graceful degradation is the CALLER's policy (the signaling router):
//     every method returns typed errors and NOTHING here panics, blocks
//     past its deadline, or logs secrets — ICE credentials inside join
//     replies are returned to the caller, never printed.
package engineclient

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// WireVersion is the control-protocol major this client speaks; the engine
// refuses any other with an "unsupported_version" envelope error.
const WireVersion = 1

const (
	pathSignal = "/v1/signal"
	pathHealth = "/v1/health"

	// maxFrameBody caps posted envelopes: the engine's 64 KiB cap starts
	// at the whole BODY, so a client-side generous-but-bounded estimate
	// keeps us safely under even with a large SDP inside.
	maxRequestBody = 48 * 1024
	// maxReplyBody bounds what we'll buffer from the engine (answers are
	// the biggest frames; anything larger is a bug or a hostile box).
	maxReplyBody = 1 << 20
)

// Frame is one signaling ClientFrame/ServerFrame object, kept generic on
// purpose: adding a frame vocabulary member is the protocol's change, not
// this transport's.
type Frame map[string]any

// JoinResult is what an accepted engine join handed us — the media
// session's capability triple, returned (not logged) for the caller to
// route onward.
type JoinResult struct {
	Session  string
	IceUfrag string
	IcePwd   string
}

// HealthInfo is the engine's self-report, verbatim enough for the
// readiness surface to summarize without inventing fields.
type HealthInfo struct {
	Engine       string `json:"engine"`
	Version      string `json:"version"`
	Ready        bool   `json:"ready"`
	Rooms        int64  `json:"rooms"`
	Participants int64  `json:"participants"`
	Tracks       int64  `json:"tracks"`
	UptimeMS     int64  `json:"uptime_ms"`
}

// EngineError is the engine's in-band refusal. It is NOT a transport
// failure: the round trip succeeded, the engine read us, and said no.
type EngineError struct {
	Code    string
	Message string
}

func (e *EngineError) Error() string { return fmt.Sprintf("engine: %s: %s", e.Code, e.Message) }

// FrameError marks transport/prototype-level breakage: HTTP non-200,
// unparseable or mis-correlated reply, version skew — anything where what
// came back was not the engine's sane envelope.
type FrameError struct{ Detail string }

func (e *FrameError) Error() string { return "engine wire: " + e.Detail }

// CallObserver receives one observation per signal call and per health
// probe — counters and latency are the embedding service's shape.
type CallObserver interface {
	ObserveSignal(op string, took time.Duration, err error)
	ObserveHealth(took time.Duration, err error)
	EngineUpChanged(up bool)
}

// Client is thread-safe by construction: its only mutable state is the
// availability pair, behind atomics.
type Client struct {
	url          string // base, no trailing slash
	http         *http.Client
	timeout      time.Duration
	observer     CallObserver
	newRequestID func() string // overridden in tests for determinism

	up      atomic.Bool
	lastErr atomic.Value // string, empty until first probe outcome

	logf func(string, ...any)

	mu         sync.Mutex // guards lastHealth
	lastHealth *HealthInfo
}

// New wires a client; url must be a bare http(s) base the caller already
// validated (config.Load owns that gate).
func New(url string, timeout time.Duration, observer CallObserver, httpClient *http.Client, logf func(string, ...any)) *Client {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: timeout + time.Second} // body+headers cushion beyond the ctx deadline
	}
	if logf == nil {
		logf = log.Printf
	}
	return &Client{
		url:  strings.TrimRight(url, "/"),
		http: httpClient, timeout: timeout, observer: observer,
		newRequestID: randomID, logf: logf,
	}
}

// ---- availability facade ------------------------------------------------

// Up reports the monitor's current view: false until the FIRST probe has
// succeeded (fail-closed on boot: an engine that never answered is not
// "available by default").
func (c *Client) Up() bool { return c.up.Load() }

// LastError is the probe-side failure text, "" when health is green.
func (c *Client) LastError() string {
	if v := c.lastErr.Load(); v != nil {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// LastHealth is the most recent successful health payload, nil before any
// green probe.
func (c *Client) LastHealth() *HealthInfo {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.lastHealth == nil {
		return nil
	}
	cp := *c.lastHealth
	return &cp
}

// Monitor runs the reconnect/availability state machine until ctx ends:
// probe, hold, back off on failure (200ms → capped 5s), notify on every
// TRANSITION. One monitor per client; the main goroutine owns it.
func (c *Client) Monitor(ctx context.Context) {
	backoff := 200 * time.Millisecond
	for {
		took, err := c.probe(ctx)
		if c.observer != nil {
			c.observer.ObserveHealth(took, err)
		}
		c.transition(err)
		wait := backoff
		if err != nil && backoff < 5*time.Second {
			backoff *= 2
		}
		if err == nil {
			backoff = 200 * time.Millisecond
			wait = 2 * time.Second // steady-state cadence
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

func (c *Client) transition(err error) {
	up := err == nil
	prev := c.up.Swap(up)
	if up {
		c.lastErr.Store("")
	} else {
		c.lastErr.Store(err.Error())
	}
	if prev != up {
		detail := ""
		if err != nil {
			detail = " (" + err.Error() + ")"
		}
		c.logf("[gateway] engine availability → %v%s", up, detail)
		if c.observer != nil {
			c.observer.EngineUpChanged(up)
		}
	}
}

// HealthNow is a one-shot probe that ALSO feeds the availability gauge —
// used by readiness when it wants the freshest view, and by tests.
func (c *Client) HealthNow(ctx context.Context) (HealthInfo, error) {
	took, err := c.probe(ctx)
	c.transition(err)
	if err != nil {
		return HealthInfo{}, err
	}
	_ = took
	return *c.LastHealth(), nil
}

func (c *Client) probe(ctx context.Context) (time.Duration, error) {
	ctx, cancel := c.deadline(ctx)
	defer cancel()
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url+pathHealth, http.NoBody)
	if err != nil {
		return 0, &FrameError{Detail: err.Error()}
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return time.Since(start), err
	}
	defer func() { _, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096)); resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return time.Since(start), &FrameError{Detail: "health http " + resp.Status}
	}
	var body struct {
		V int `json:"v"`
		HealthInfo
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxReplyBody)).Decode(&body); err != nil {
		return time.Since(start), &FrameError{Detail: "health body: " + err.Error()}
	}
	if body.V != WireVersion {
		return time.Since(start), &FrameError{Detail: fmt.Sprintf("wire version %d (this client speaks %d)", body.V, WireVersion)}
	}
	if !body.Ready {
		return time.Since(start), &EngineError{Code: "not_ready", Message: "engine reports ready=false"}
	}
	c.mu.Lock()
	h := body.HealthInfo
	c.lastHealth = &h
	c.mu.Unlock()
	return time.Since(start), nil
}

// ---- signaling calls ----------------------------------------------------

// Signal posts one client frame under a fresh correlation id and returns
// the engine's server frames verbatim. op names the metric/log bucket
// ("join", "leave", ...): it is the CALLER's vocabulary, not the wire's.
func (c *Client) Signal(ctx context.Context, op string, frame Frame) ([]Frame, error) {
	ctx, cancel := c.deadline(ctx)
	defer cancel()
	id := c.newRequestID()
	body, err := json.Marshal(map[string]any{"v": WireVersion, "id": id, "frame": frame})
	if err != nil {
		return nil, &FrameError{Detail: "marshal: " + err.Error()}
	}
	if len(body) > maxRequestBody {
		return nil, &FrameError{Detail: fmt.Sprintf("request %d B exceeds %d B client cap", len(body), maxRequestBody)}
	}
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url+pathSignal, bytes.NewReader(body))
	if err != nil {
		return nil, &FrameError{Detail: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Request-Id", id) // the engine echoes this in its logs
	resp, err := c.http.Do(req)
	took := time.Since(start)
	if err != nil {
		c.observe(op, took, err)
		return nil, err
	}
	defer func() { _, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096)); resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		err = &FrameError{Detail: "http " + resp.Status}
		c.observe(op, took, err)
		return nil, err
	}
	var wire struct {
		V      int     `json:"v"`
		ID     string  `json:"id"`
		Frames []Frame `json:"frames"`
		Err    *struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxReplyBody)).Decode(&wire); errBlank(err) {
		frameErr := &FrameError{Detail: "reply body: " + err.Error()}
		c.observe(op, took, frameErr)
		return nil, frameErr
	}
	if wire.Err != nil {
		err = &EngineError{Code: wire.Err.Code, Message: wire.Err.Message}
		c.observe(op, took, err)
		return nil, err
	}
	if wire.V != WireVersion {
		err = &FrameError{Detail: fmt.Sprintf("reply version %d (this client speaks %d)", wire.V, WireVersion)}
		c.observe(op, took, err)
		return nil, err
	}
	if wire.ID != id {
		err = &FrameError{Detail: fmt.Sprintf("correlation mismatch: sent %s, got %q", id, wire.ID)}
		c.observe(op, took, err)
		return nil, err
	}
	c.observe(op, took, nil)
	return wire.Frames, nil
}

// Join enrolls one verified edge participant as a media-session member.
func (c *Client) Join(ctx context.Context, room, participant string) (JoinResult, error) {
	frames, err := c.Signal(ctx, "join", Frame{"type": "join", "room": room, "participant": participant})
	if err != nil {
		return JoinResult{}, err
	}
	for _, f := range frames {
		if t, _ := f["type"].(string); t == "ready" {
			out := JoinResult{
				Session:  strField(f, "session"),
				IceUfrag: strField(f, "ice_ufrag"),
				IcePwd:   strField(f, "ice_pwd"),
			}
			if out.Session == "" {
				return JoinResult{}, &FrameError{Detail: "ready frame without session"}
			}
			return out, nil
		}
	}
	return JoinResult{}, &FrameError{Detail: "join returned no ready frame"}
}

// Leave releases engine state for one media session; unknown sessions are
// the engine's quiet success, so retry-at-ambiguity is caller-simple and
// safe — but this client itself never retries (see package contract).
func (c *Client) Leave(ctx context.Context, session string) error {
	_, err := c.Signal(ctx, "leave", Frame{"type": "leave", "session": session})
	return err
}

// Offer forwards one steered session's SDP offer; the returned frames may
// contain "answer" AND "track.published" fanout entries — dispatching them
// is the caller's (router's) concern, under the engine's documented
// frame-in/frame-out pairing.
func (c *Client) Offer(ctx context.Context, session, sdp string) ([]Frame, error) {
	return c.Signal(ctx, "offer", Frame{"type": "offer", "session": session, "sdp": sdp})
}

// Trickle forwards one candidate payload verbatim (null = end-of-candidates).
// The engine's quiet-success path returns zero frames; a refusal rides
// inside the frames as an "error" entry.
func (c *Client) Trickle(ctx context.Context, session string, candidate any) ([]Frame, error) {
	return c.Signal(ctx, "trickle", Frame{"type": "trickle", "session": session, "candidate": candidate})
}

// Publish registers (track, kind) on the steered session; frames may
// include the room-fanout "track.published" the router replays to the peer.
func (c *Client) Publish(ctx context.Context, session, track, kind string) ([]Frame, error) {
	return c.Signal(ctx, "publish", Frame{"type": "publish", "session": session, "track": track, "kind": kind})
}

// Subscribe enrolls this session for the OTHER member's track:
// `participant` is that member's engine-side participant id, pinned by the
// router to the peer's connection id.
func (c *Client) Subscribe(ctx context.Context, session, participant, track string) error {
	_, err := c.Signal(ctx, "subscribe", Frame{"type": "subscribe", "session": session, "participant": participant, "track": track})
	return err
}

// Unsubscribe withdraws the enrollment (idempotent peer-side by the same
// teardown argument as Leave: replays are engine-quiet-success).
func (c *Client) Unsubscribe(ctx context.Context, session, participant, track string) error {
	_, err := c.Signal(ctx, "unsubscribe", Frame{"type": "unsubscribe", "session": session, "participant": participant, "track": track})
	return err
}

// ---- internals ----------------------------------------------------------

// deadline applies the client backstop when the caller gave none; a caller
// deadline earlier than the backstop wins, as it should.
func (c *Client) deadline(ctx context.Context) (context.Context, context.CancelFunc) {
	if _, ok := ctx.Deadline(); ok {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, c.timeout)
}

func (c *Client) observe(op string, took time.Duration, err error) {
	if c.observer != nil {
		c.observer.ObserveSignal(op, took, err)
	}
}

func strField(f Frame, k string) string {
	if v, ok := f[k].(string); ok {
		return v
	}
	return ""
}

func randomID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is a boot-environment bug; a time-based id
		// keeps correlation WORKING (still unique in practice) while the
		// crash-looping alternative keeps the whole feature down.
		return fmt.Sprintf("%016x", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

// IsUnavailable reports whether err means "the engine process/box is not
// reachable" (dial/timeout) versus "the engine said no" (EngineError) —
// the router uses this split: unavailable degrades quietly (media path
// already absent); refused is a PROTOCOL surprise worth a louder log.
func IsUnavailable(err error) bool {
	var ee *EngineError
	if errors.As(err, &ee) {
		return false
	}
	var fe *FrameError
	if errors.As(err, &fe) {
		return true // HTTP packaging failed: treat as not-our-friend-now
	}
	return true
}

// errBlank exists only to keep gofmt-plus-linters happy about a long if
// initializer; it is a plain nil check.
func errBlank(err error) bool { return err != nil }
