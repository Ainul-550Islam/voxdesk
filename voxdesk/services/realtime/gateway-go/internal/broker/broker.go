// Package broker is the ingest fan-out transport: the hop between "the
// API asked us to publish" and "this node's hub fans the event into local
// WebSocket rooms".
//
// The interface exists because the gateway has two deployment shapes:
//
//   - memory: one node. Publish delivers to local subscribers SYNCHRONOUSLY
//     and that is the whole fan-out — ingest's response counters stay
//     exact, because the publish call itself did the delivery.
//   - redis: many replicas behind a balancer. Publish delivers locally
//     synchronously (the response counters still reflect THIS node's
//     delivery) AND propagates the envelope over Redis pub/sub so every
//     other replica delivers to its own local subscribers. Own-node echoes
//     coming back over the bus are suppressed via an origin stamp, so a
//     publish never double-delivers at home.
//
// Topic payloads are opaque bytes here; the wire schema (what a publish
// envelope contains) belongs to the caller — the broker moves bytes and
// counts deliveries, it does not understand events.
package broker

import (
	"encoding/json"
	"errors"
	"time"
)

// ErrClosed is returned by Publish on a closed broker. Callers treat it as
// a hard failure (shutdown in flight).
var ErrClosed = errors.New("broker: closed")

// Envelope is one message heard on the bus — locally (from Publish) or
// remotely (from another node's bus hop). Payload is the caller's bytes,
// untouched.
type Envelope struct {
	Topic string
	// Payload is the published bytes. For locally-published messages this
	// is the exact slice handed to Publish; for bus-received ones it is a
	// fresh decode.
	Payload json.RawMessage
	// Origin is the node id that produced this envelope. A consumer that
	// runs on multiple nodes can use it to de-prioritise own-node facts;
	// the redis broker already guarantees it never sees its OWN echo.
	Origin string
	// Local is true when the envelope is heard by a subscriber on the same
	// node that published it (the synchronous delivery).
	Local bool
	// At is the broker-side hear time (publish time for local hops).
	At time.Time
}

// Stats is the delivery accounting a Handler returns. It mirrors the
// hub's fan-out counters one-for-one so ingest responses keep their exact
// (delivered, dropped) meaning regardless of which broker implementation
// produced them.
type Stats struct {
	Delivered int
	Dropped   int
}

// Add accumulates other into s (multi-subscriber aggregation).
func (s Stats) Add(other Stats) Stats {
	return Stats{Delivered: s.Delivered + other.Delivered, Dropped: s.Dropped + other.Dropped}
}

// Handler consumes one envelope and reports the local delivery accounting.
// For bus-received envelopes the returned Stats is informational only —
// there is no channel to return it across nodes, so remote handlers should
// count into metrics themselves (the server does) rather than relying on
// the caller.
type Handler func(Envelope) Stats

// Broker is the fan-out transport contract.
type Broker interface {
	// Publish hands one message to the topic's LOCAL subscribers
	// synchronously and returns their aggregated accounting. In redis
	// mode the bus propagation happens BEFORE Publish returns (single
	// socket write) but never determines the error: local delivery is
	// the contract, cross-node propagation is best-effort with internal
	// retry.
	Publish(topic string, payload []byte) (Stats, error)
	// Subscribe registers a local handler for new envelopes on topic.
	// The returned function unsubscribes exactly once.
	Subscribe(topic string, h Handler) (unsubscribe func())
	// NodeID is this node's stable identity (origin stamp).
	NodeID() string
	// Kind reports the transport: "memory" or "redis" (observability).
	Kind() string
	// Close stops all background work (the redis reader/reconnect loop)
	// and refuses subsequent Publishes with ErrClosed.
	Close() error
}
