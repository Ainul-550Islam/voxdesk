package broker

import (
	"errors"
	"testing"
)

func TestMemoryPublishDeliversSynchronouslyAndAggregates(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	var got []Envelope
	b.Subscribe("calls", func(env Envelope) Stats {
		got = append(got, env)
		return Stats{Delivered: 3, Dropped: 1}
	})
	b.Subscribe("calls", func(env Envelope) Stats { return Stats{Delivered: 2} })
	b.Subscribe("metrics", func(env Envelope) Stats {
		t.Errorf("other-topic handler must never fire")
		return Stats{}
	})

	stats, err := b.Publish("calls", []byte(`{"k":1}`))
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	if stats.Delivered != 5 || stats.Dropped != 1 {
		t.Fatalf("Stats must aggregate across subscribers, got %+v", stats)
	}
	if len(got) != 1 || !got[0].Local || got[0].Origin != b.NodeID() {
		t.Fatalf("local envelope shaped wrong: %+v", got)
	}
	if string(got[0].Payload) != `{"k":1}` {
		t.Fatalf("payload must pass through byte-verbatim, got %s", got[0].Payload)
	}
}

func TestMemoryUnsubscribeIsExactOnceAndSkipsInFlightCopy(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	calls := 0
	unsub := b.Subscribe("t", func(Envelope) Stats { calls++; return Stats{} })

	if _, err := b.Publish("t", nil); err != nil || calls != 1 {
		t.Fatalf("pre-unsub publish: calls=%d err=%v", calls, err)
	}
	unsub()
	unsub() // idempotent
	if _, err := b.Publish("t", nil); err != nil || calls != 1 {
		t.Fatalf("unsubscribed handler must not fire: calls=%d err=%v", calls, err)
	}
}

func TestMemoryHandlerPanicIsContained(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	b.Subscribe("t", func(Envelope) Stats { panic("boom") })
	delivered := false
	b.Subscribe("t", func(Envelope) Stats { delivered = true; return Stats{Delivered: 4} })

	stats, err := b.Publish("t", nil)
	if err != nil {
		t.Fatalf("panicking subscriber must not fail the publish: %v", err)
	}
	if !delivered || stats.Delivered != 4 {
		t.Fatalf("panicking subscriber must not starve the rest: %+v delivered=%v", stats, delivered)
	}
}

func TestMemoryClosedRefusesPublishes(t *testing.T) {
	t.Parallel()
	b := NewMemory()
	if err := b.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := b.Close(); err != nil {
		t.Fatalf("close must be idempotent: %v", err)
	}
	if _, err := b.Publish("t", nil); !errors.Is(err, ErrClosed) {
		t.Fatalf("closed broker must refuse with ErrClosed, got %v", err)
	}
}

func TestMemoryIdentityAndKind(t *testing.T) {
	t.Parallel()
	a, b := NewMemory(), NewMemory()
	if a.NodeID() == "" || a.NodeID() == b.NodeID() {
		t.Fatalf("node ids must be non-empty and unique: %q vs %q", a.NodeID(), b.NodeID())
	}
	if a.Kind() != "memory" {
		t.Fatalf("kind: %q", a.Kind())
	}
}
