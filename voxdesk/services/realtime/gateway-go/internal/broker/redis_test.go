package broker

import (
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/broker/brokertest"
)

// The fake RESP server lives in brokertest (real TCP, real pub/sub
// semantics, own-node echo included) so server-level e2e tests use the
// same double. These tests assert the broker's behaviour against it.

// recorder collects deliveries for assertions.
type recorder struct {
	mu   sync.Mutex
	envs []Envelope
}

func (r *recorder) handler(delivery Stats) Handler {
	return func(env Envelope) Stats {
		r.mu.Lock()
		r.envs = append(r.envs, env)
		r.mu.Unlock()
		return delivery
	}
}

func (r *recorder) waitFor(t *testing.T, n int) []Envelope {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		r.mu.Lock()
		if len(r.envs) >= n {
			out := append([]Envelope(nil), r.envs...)
			r.mu.Unlock()
			return out
		}
		count := len(r.envs)
		r.mu.Unlock()
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %d deliveries, have %d", n, count)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func startFake(t *testing.T) *brokertest.FakeRedis {
	t.Helper()
	f, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(f.Stop)
	return f
}

func newTestRedisBroker(t *testing.T, url string) *RedisBroker {
	t.Helper()
	b, err := NewRedis(url)
	if err != nil {
		t.Fatalf("NewRedis: %v", err)
	}
	t.Cleanup(func() { _ = b.Close() })
	select {
	case <-b.ReadyChan():
	case <-time.After(3 * time.Second):
		t.Fatalf("broker never connected to %s", url)
	}
	return b
}

func TestRedisCrossNodeDeliveryWithOwnEchoSuppressed(t *testing.T) {
	t.Parallel()
	fake := startFake(t)

	a := newTestRedisBroker(t, fake.URL())
	b := newTestRedisBroker(t, fake.URL())

	var ra, rb recorder
	a.Subscribe("ingest", ra.handler(Stats{Delivered: 7}))
	b.Subscribe("ingest", rb.handler(Stats{Delivered: 3}))
	if !fake.WaitForSubscribers("ingest", 2, 3*time.Second) {
		t.Fatal("both brokers must be subscribed to the bus")
	}

	stats, err := a.Publish("ingest", []byte(`{"event":"call.started"}`))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	// Local accounting is EXACTLY A's subscriber's Stats — B's remote
	// delivery cannot contaminate A's ingest response.
	if stats.Delivered != 7 {
		t.Fatalf("local stats must be the publisher's own only, got %+v", stats)
	}

	// A heard it exactly ONCE (the synchronous local hop — the bus echo
	// must be suppressed by origin), B exactly once (the bus hop).
	envsA := ra.waitFor(t, 1)
	envsB := rb.waitFor(t, 1)
	time.Sleep(100 * time.Millisecond) // any DOUBLE delivery would land now
	ra.mu.Lock()
	finalA := len(ra.envs)
	ra.mu.Unlock()
	if finalA != 1 {
		t.Fatalf("own echo must be suppressed: A delivered %d times", finalA)
	}
	if !envsA[0].Local || envsA[0].Origin != a.NodeID() {
		t.Fatalf("A's envelope must be the local hop: %+v", envsA[0])
	}
	if envsB[0].Local || envsB[0].Origin != a.NodeID() {
		t.Fatalf("B's envelope must be the bus hop stamped with A's origin: %+v", envsB[0])
	}
	if string(envsB[0].Payload) != `{"event":"call.started"}` {
		t.Fatalf("bus payload must round-trip byte-verbatim, got %s", envsB[0].Payload)
	}
}

func TestRedisPublishSurvivesBusOutageAndReconnects(t *testing.T) {
	t.Parallel()
	fake := startFake(t)
	a := newTestRedisBroker(t, fake.URL())

	var r recorder
	a.Subscribe("ingest", r.handler(Stats{Delivered: 1}))
	if !fake.WaitForSubscribers("ingest", 1, 3*time.Second) {
		t.Fatal("broker must be subscribed before the outage")
	}
	if _, err := a.Publish("ingest", []byte("one")); err != nil {
		t.Fatalf("publish 1: %v", err)
	}
	r.waitFor(t, 1)

	// Kill the bus. Local delivery must keep working, bus drops counted.
	fake.Stop()
	deadline := time.Now().Add(3 * time.Second)
	for a.Connected() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	stats, err := a.Publish("ingest", []byte("two"))
	if err != nil {
		t.Fatalf("outage publish must NOT fail for local delivery: %v", err)
	}
	if stats.Delivered != 1 {
		t.Fatalf("local stats during outage wrong: %+v", stats)
	}
	deadline = time.Now().Add(3 * time.Second)
	for a.BusDrops() < 1 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if a.BusDrops() < 1 {
		t.Fatal("outage publish must be counted as a bus drop")
	}

	// Bus comes back on the SAME address (how a restarted redis looks).
	fake2, err := brokertest.StartAt(fake.Addr)
	if err != nil {
		t.Fatalf("rebind %s: %v", fake.Addr, err)
	}
	t.Cleanup(fake2.Stop)

	// The supervisor must redial and RE-SUBSCRIBE the recorded topics.
	deadline = time.Now().Add(5 * time.Second)
	for a.ConnectCount() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if a.ConnectCount() < 2 {
		t.Fatalf("broker never reconnected (connects=%d)", a.ConnectCount())
	}
	if !fake2.WaitForSubscribers("ingest", 1, 3*time.Second) {
		t.Fatal("reconnect must re-subscribe the topic set")
	}
	if _, err := a.Publish("ingest", []byte("three")); err != nil {
		t.Fatalf("post-reconnect publish: %v", err)
	}
	envs := r.waitFor(t, 3) // one, two (local only), three (local; echo suppressed)
	if got := string(envs[2].Payload); got != "three" {
		t.Fatalf("third delivery payload = %q", got)
	}
}

func TestRedisClosedBrokerRefusesCleanly(t *testing.T) {
	t.Parallel()
	fake := startFake(t)
	b, err := NewRedis(fake.URL())
	if err != nil {
		t.Fatalf("NewRedis: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close idempotent: %v", err)
	}
	if _, err := b.Publish("t", nil); err == nil {
		t.Fatal("closed broker must refuse publishes")
	}
}

func TestParseRedisURLValidation(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		raw  string
		want redisParams
	}{
		{"redis://localhost", redisParams{addr: "localhost:6379"}},
		{"redis://:pw@h:1234", redisParams{addr: "h:1234", password: "pw"}},
		{"redis://alice:pw@h:6379/3", redisParams{addr: "h:6379", user: "alice", password: "pw", db: 3}},
		{"redis://h/0", redisParams{addr: "h:6379"}},
	} {
		got, err := parseRedisURL(tc.raw)
		if err != nil || got != tc.want {
			t.Errorf("parseRedisURL(%q) = %+v, %v; want %+v", tc.raw, got, err, tc.want)
		}
	}
	for _, bad := range []string{
		"rediss://h:6379",      // config guards this too; the parser stays honest
		"redis:///nodb",        // path-only means no host
		"redis://h/99",         // db out of range
		"redis://h/keyspace/1", // nested path is not a db
	} {
		if _, err := parseRedisURL(bad); err == nil {
			t.Errorf("parseRedisURL(%q) must fail", bad)
		}
	}
}
