package broker

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"sync"
	"sync/atomic"
	"time"
)

// MemoryBroker is the single-node transport: Publish IS the fan-out.
// There is no background goroutine, no connection, nothing to reconnect —
// which is exactly why the default deployment has so little to fail.
//
// Handlers run synchronously inside the Publish call, OUTSIDE the
// subscription lock (snapshot-then-invoke, the same discipline as the
// hub): a handler that itself Publishes (an event cascade) can never
// self-deadlock, and one slow subscriber cannot hold the subscription
// table against a concurrent Subscribe.
type MemoryBroker struct {
	nodeID string

	mu       sync.Mutex // guards handlers only
	handlers map[string][]Handler
	closed   atomic.Bool
}

// NewMemory returns an empty memory broker with a fresh node id.
func NewMemory() *MemoryBroker {
	return &MemoryBroker{handlers: make(map[string][]Handler), nodeID: newNodeID()}
}

// newNodeID mints a node identity: "gw-" + 8 random hex bytes — short
// enough for log lines, unique enough that two nodes started in the same
// nanosecond still differ.
func newNodeID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "gw-norand"
	}
	return "gw-" + hex.EncodeToString(b[:])
}

// Publish implements Broker: snapshot subscribers, invoke each
// synchronously, aggregate. A closed broker refuses loudly (shutdown in
// flight must not silently deliver).
func (m *MemoryBroker) Publish(topic string, payload []byte) (Stats, error) {
	if m.closed.Load() {
		return Stats{}, ErrClosed
	}
	return m.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  m.nodeID,
		Local:   true,
		At:      time.Now(),
	}), nil
}

// deliver invokes the topic's subscriber snapshot and aggregates their
// accounting. Shared by Publish (local hop) and the redis broker (whose
// bus-received envelopes reuse the identical delivery path).
func (m *MemoryBroker) deliver(env Envelope) Stats {
	m.mu.Lock()
	// Snapshot keys alone: handler slices are append-only under mu and a
	// concurrent unsubscribe nils entries in place, which deliverOn skips.
	topicHandlers := append([]Handler(nil), m.handlers[env.Topic]...)
	m.mu.Unlock()

	var total Stats
	for _, h := range topicHandlers {
		if h == nil {
			continue
		}
		total = total.Add(m.call(h, env))
	}
	return total
}

// call invokes one handler, containing a panic: a misbehaving subscriber
// must take its own accounting to zero, not kill the publisher's HTTP
// handler goroutine. Same philosophy as net/http recovering handler
// panics per-request.
func (m *MemoryBroker) call(h Handler, env Envelope) (stats Stats) {
	defer func() {
		if recover() != nil {
			stats = Stats{}
		}
	}()
	return h(env)
}

// Subscribe implements Broker. The unsubscribe nils the slot in place
// (deliver snapshots the slice and skips nils), so an in-flight Publish
// that already captured the handler completes undisturbed — unsubscribe
// visibility applies to SUBSEQUENT publishes, matching the hub's
// snapshot-then-invoke discipline.
func (m *MemoryBroker) Subscribe(topic string, h Handler) (unsubscribe func()) {
	m.mu.Lock()
	m.handlers[topic] = append(m.handlers[topic], h)
	idx := len(m.handlers[topic]) - 1
	m.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			m.mu.Lock()
			if handlers := m.handlers[topic]; idx < len(handlers) {
				m.handlers[topic][idx] = nil
			}
			m.mu.Unlock()
		})
	}
}

// NodeID implements Broker.
func (m *MemoryBroker) NodeID() string { return m.nodeID }

// Kind implements Broker.
func (m *MemoryBroker) Kind() string { return "memory" }

// Close implements Broker: idempotent; subsequent Publishes fail with
// ErrClosed (in-flight ones complete).
func (m *MemoryBroker) Close() error {
	m.closed.Store(true)
	return nil
}
