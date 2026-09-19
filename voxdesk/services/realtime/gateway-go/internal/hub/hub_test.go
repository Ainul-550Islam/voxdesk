package hub

import (
	"encoding/json"
	"sync"
	"testing"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// recorder is a Subscriber that captures offers instead of writing to a
// socket. queueSize 0 models a full buffer (every Enqueue reports drop).
type recorder struct {
	session   string
	tenant    string
	queueSize int
	used      int

	mu     sync.Mutex
	frames []any
	closed []closeCall
}

type closeCall struct {
	code   int
	reason string
}

func (r *recorder) Session() string { return r.session }
func (r *recorder) Tenant() string  { return r.tenant }

func (r *recorder) Enqueue(msg any) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.used >= r.queueSize {
		return false
	}
	r.used++
	r.frames = append(r.frames, msg)
	return true
}

func (r *recorder) RequestClose(code int, reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed = append(r.closed, closeCall{code, reason})
}

func (r *recorder) snapshot() []any {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]any, len(r.frames))
	copy(out, r.frames)
	return out
}

const (
	tenantA = "11111111-1111-1111-1111-111111111111"
	tenantB = "22222222-2222-2222-2222-222222222222"
)

func authed(t *testing.T, h *Hub, sessionID, tenantID string, queue int) *recorder {
	t.Helper()
	// The tenant is pinned on the recorder only AFTER BindTenant succeeds,
	// exactly like a real Connection: Tenant() returns "" pre-auth.
	rec := &recorder{session: sessionID, queueSize: queue}
	h.Register(rec)
	if err := h.BindTenant(sessionID, tenantID); err != nil {
		t.Fatalf("BindTenant: %v", err)
	}
	rec.tenant = tenantID
	return rec
}

func TestPublishOnlyReachesSameTenantSameRoom(t *testing.T) {
	h := New(8)
	aCalls := authed(t, h, "sess-a-calls", tenantA, 8)
	aMetrics := authed(t, h, "sess-a-metrics", tenantA, 8)
	bCalls := authed(t, h, "sess-b-calls", tenantB, 8)

	if _, err := h.Subscribe("sess-a-calls", "calls"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Subscribe("sess-a-metrics", "metrics"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Subscribe("sess-b-calls", "calls"); err != nil {
		t.Fatal(err)
	}

	// THE tenant-isolation proof: publishing into (A, calls) reaches exactly
	// one socket — not the other room of A, not the same room of B.
	delivered, dropped := h.Publish(tenantA, "calls", "call.updated", "", json.RawMessage(`{"x":1}`))
	if delivered != 1 || dropped != 0 {
		t.Fatalf("delivered/dropped = %d/%d, want 1/0", delivered, dropped)
	}
	if got := len(aCalls.snapshot()); got != 1 {
		t.Errorf("tenant A calls subscriber got %d frames, want 1", got)
	}
	if got := len(aMetrics.snapshot()); got != 0 {
		t.Errorf("tenant A metrics subscriber got %d frames, want 0", got)
	}
	if got := len(bCalls.snapshot()); got != 0 {
		t.Errorf("TENANT B received %d frames from tenant A's room — isolation breach", got)
	}
}

func TestSubscribeRequiresBoundTenant(t *testing.T) {
	h := New(8)
	rec := &recorder{session: "sess-anon", tenant: "", queueSize: 8}
	h.Register(rec)
	if _, err := h.Subscribe("sess-anon", "calls"); err == nil {
		t.Error("unauthenticated session must not join rooms")
	}
}

func TestPerTenantConnectionCap(t *testing.T) {
	h := New(2)
	authed(t, h, "s1", tenantA, 8)
	authed(t, h, "s2", tenantA, 8)
	third := &recorder{session: "s3", queueSize: 8}
	h.Register(third)
	if err := h.BindTenant("s3", tenantA); err != ErrOverTenantCap {
		t.Fatalf("third connection: err = %v, want ErrOverTenantCap", err)
	}
	// ...while another tenant is unaffected.
	authed(t, h, "s4", tenantB, 8)
	// Unregister frees the slot.
	h.Unregister("s1")
	fifth := &recorder{session: "s5", queueSize: 8}
	h.Register(fifth)
	if err := h.BindTenant("s5", tenantA); err != nil {
		t.Fatalf("after unregister the slot must be reusable: %v", err)
	}
}

func TestCannotBindSessionToTwoTenants(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	if err := h.BindTenant("s1", tenantB); err == nil {
		t.Error("re-binding to a different tenant must be refused")
	}
	if err := h.BindTenant("s1", tenantA); err != nil {
		t.Errorf("idempotent re-bind to the SAME tenant must succeed: %v", err)
	}
}

func TestBackpressureDropIsCountedNotStalling(t *testing.T) {
	h := New(8)
	slow := authed(t, h, "sess-slow", tenantA, 1) // one-slot queue
	fast := authed(t, h, "sess-fast", tenantA, 8)
	_, _ = h.Subscribe("sess-slow", "calls")
	_, _ = h.Subscribe("sess-fast", "calls")

	payload := json.RawMessage(`{"n":0}`)
	h.Publish(tenantA, "calls", "metrics.tick_15s", "", payload) // slow gets 1, fills its slot
	delivered, dropped := h.Publish(tenantA, "calls", "metrics.tick_15s", "", payload)

	if delivered != 1 || dropped != 1 {
		t.Fatalf("second publish: delivered/dropped = %d/%d, want 1/1", delivered, dropped)
	}
	if got := len(fast.snapshot()); got != 2 {
		t.Errorf("fast subscriber = %d frames, want 2 — a slow peer must not stall the room", got)
	}
	_ = slow
}

func TestUnsubscribeIsIdempotent(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	_, _ = h.Subscribe("s1", "calls")
	h.Unsubscribe("s1", "calls")
	h.Unsubscribe("s1", "calls") // leaving twice must not error or corrupt
	if got := h.PeerCount(tenantA, "calls"); got != 0 {
		t.Errorf("PeerCount = %d, want 0", got)
	}
	h.Unsubscribe("s1", "never-joined") // never joined: silent no-op by contract
}

func TestUnregisterScrubsRoomsAndCounts(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	_, _ = h.Subscribe("s1", "calls")
	h.Unregister("s1")
	h.Unregister("s1") // racing close: must be a no-op
	rooms, tenants := h.Stats()
	if rooms != 0 || tenants != 0 {
		t.Errorf("Stats = (%d rooms, %d tenants), want (0, 0)", rooms, tenants)
	}
}

func TestPeersAcknowledgementCountsJoinAfterAdd(t *testing.T) {
	h := New(8)
	authed(t, h, "s1", tenantA, 8)
	authed(t, h, "s2", tenantA, 8)
	peers1, _ := h.Subscribe("s1", "calls")
	peers2, _ := h.Subscribe("s2", "calls")
	if peers1 != 1 || peers2 != 2 {
		t.Errorf("peers = %d then %d, want 1 then 2 (count AFTER join)", peers1, peers2)
	}
}

func TestCloseAllClosesEveryConnectionOnce(t *testing.T) {
	h := New(8)
	a := authed(t, h, "s1", tenantA, 8)
	b := authed(t, h, "s2", tenantB, 8)
	h.CloseAll(protocol.CloseGoingAway, "server shutting down")
	for _, rec := range []*recorder{a, b} {
		rec.mu.Lock()
		got := len(rec.closed)
		code := -1
		if got > 0 {
			code = rec.closed[0].code
		}
		rec.mu.Unlock()
		if got != 1 || code != protocol.CloseGoingAway {
			t.Errorf("session %s: closed %d times with code %d, want once with 1001", rec.session, got, code)
		}
	}
}

func TestConcurrentPublishTenantIsolation(t *testing.T) {
	// Mirrors signal-go's concurrent smoke test: hammer the hub with
	// concurrent publishers and subscribers and assert nothing ever crosses
	// the tenant line (run with -race).
	h := New(64)
	a := authed(t, h, "sa", tenantA, 4096)
	b := authed(t, h, "sb", tenantB, 4096)
	_, _ = h.Subscribe("sa", "calls")
	_, _ = h.Subscribe("sb", "calls")

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for n := 0; n < 200; n++ {
				h.Publish(tenantA, "calls", "metrics.tick_15s", "", json.RawMessage(`{"t":"a"}`))
			}
		}()
		go func() {
			defer wg.Done()
			for n := 0; n < 200; n++ {
				h.Publish(tenantB, "calls", "metrics.tick_15s", "", json.RawMessage(`{"t":"b"}`))
			}
		}()
	}
	wg.Wait()

	for _, frame := range a.snapshot() {
		delivery := frame.(protocol.Delivery)
		if string(delivery.Payload) != `{"t":"a"}` {
			t.Fatalf("TENANT B PAYLOAD REACHED TENANT A: %s", delivery.Payload)
		}
	}
	for _, frame := range b.snapshot() {
		delivery := frame.(protocol.Delivery)
		if string(delivery.Payload) != `{"t":"b"}` {
			t.Fatalf("TENANT A PAYLOAD REACHED TENANT B: %s", delivery.Payload)
		}
	}
}
