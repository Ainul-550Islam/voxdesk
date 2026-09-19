package backpressure

import (
	"errors"
	"sync/atomic"
)

// ErrQueueFull is returned by Enqueue when the queue is at capacity and the
// active policy refused the frame. It is a flow-control signal, not an
// operational error: callers typically count it and move on.
var ErrQueueFull = errors.New("backpressure: queue full")

// Queue is a bounded, multi-producer/single-consumer FIFO with an explicit
// overload policy. It wraps a channel so consumers keep ordinary
// select/range ergonomics, while producers get a non-blocking,
// policy-driven enqueue with a drop counter.
//
// The gateway's producer/consumer split is exactly: many publishers (hub
// fan-out, the session's own replies) → one writer goroutine. The channel
// does the synchronization; this type owns the POLICY.
type Queue[T any] struct {
	ch chan T

	policy   Policy
	enqueued atomic.Int64 // frames admitted since construction (diagnostics)
	dropped  atomic.Int64 // frames refused by the policy since construction
}

// New returns a queue with room for capacity frames. capacity < 1 is
// normalized to 1 — a zero-capacity queue would refuse every frame, and
// that misconfiguration should degrade to "barely any buffer" rather than
// to a silent black hole.
func New[T any](capacity int, policy Policy) *Queue[T] {
	if capacity < 1 {
		capacity = 1
	}
	return &Queue[T]{ch: make(chan T, capacity), policy: policy}
}

// TryEnqueue offers one frame, applying the queue's overload policy:
//
//   - fast path (room available): admitted, true, nil;
//   - full under DropNewest: refused, false, ErrQueueFull (drop counted).
//
// It NEVER blocks — that is the entire point of the type.
func (q *Queue[T]) TryEnqueue(v T) (bool, error) {
	select {
	case q.ch <- v:
		q.enqueued.Add(1)
		return true, nil
	default:
		q.dropped.Add(1)
		return false, ErrQueueFull
	}
}

// Enqueue is TryEnqueue's bool-only form for consumers of the old channel
// contract (hub.Subscriber.Enqueue). Identical semantics.
func (q *Queue[T]) Enqueue(v T) bool {
	ok, _ := q.TryEnqueue(v)
	return ok
}

// C exposes the receive side for select loops. The consumer OWNS the read:
// a type whose contract is "one writer goroutine drains me" should not
// hide the channel it is selecting on. Never send on the returned channel;
// it is typed receive-only.
func (q *Queue[T]) C() <-chan T { return q.ch }

// Len reports frames currently buffered.
func (q *Queue[T]) Len() int { return len(q.ch) }

// Capacity reports the queue's bound.
func (q *Queue[T]) Capacity() int { return cap(q.ch) }

// Policy reports the active overload policy.
func (q *Queue[T]) Policy() Policy { return q.policy }

// EnqueuedTotal is how many frames were admitted since construction.
func (q *Queue[T]) EnqueuedTotal() int64 { return q.enqueued.Load() }

// DroppedTotal is how many frames the policy refused since construction —
// the per-queue source of the /metrics dropped gauge.
func (q *Queue[T]) DroppedTotal() int64 { return q.dropped.Load() }
