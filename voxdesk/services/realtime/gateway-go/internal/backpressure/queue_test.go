package backpressure

import (
	"errors"
	"sync"
	"testing"
)

func TestQueueAdmitsUpToCapacityThenDropsNewest(t *testing.T) {
	t.Parallel()
	q := New[int](3, DropNewest)

	for i := 0; i < 3; i++ {
		if ok, err := q.TryEnqueue(i); !ok || err != nil {
			t.Fatalf("frame %d within capacity was refused: %v", i, err)
		}
	}
	ok, err := q.TryEnqueue(99)
	if ok || !errors.Is(err, ErrQueueFull) {
		t.Fatalf("over-capacity frame must be refused with ErrQueueFull, got ok=%v err=%v", ok, err)
	}
	if q.DroppedTotal() != 1 || q.EnqueuedTotal() != 3 {
		t.Fatalf("counters wrong: enqueued=%d dropped=%d", q.EnqueuedTotal(), q.DroppedTotal())
	}

	// FIFO order and drains-free-capacity behaviour: drain one, admit one.
	if got := <-q.C(); got != 0 {
		t.Fatalf("FIFO violated: first frame must be 0, got %d", got)
	}
	if !q.Enqueue(3) {
		t.Fatal("freed capacity must admit the next frame")
	}
	for _, want := range []int{1, 2, 3} {
		if got := <-q.C(); got != want {
			t.Fatalf("FIFO violated on drain: got %d, want %d", got, want)
		}
	}
	if q.Len() != 0 {
		t.Fatalf("empty queue reporting %d frames", q.Len())
	}
}

func TestQueueZeroCapacityIsNormalizedNotBlackHole(t *testing.T) {
	t.Parallel()
	q := New[string](0, DropNewest)
	if q.Capacity() < 1 {
		t.Fatalf("zero capacity must be normalized to at least 1, got %d", q.Capacity())
	}
	if !q.Enqueue("x") {
		t.Fatal("normalized queue must admit its single slot")
	}
}

func TestQueueConcurrentProducersNeverBlock(t *testing.T) {
	t.Parallel()
	const capacity = 128
	q := New[int](capacity, DropNewest)

	// Far more producers than capacity, no consumer for half the run:
	// producers must still complete (never parked on a full queue).
	var wg sync.WaitGroup
	for g := 0; g < 32; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 64; i++ {
				q.Enqueue(g*64 + i)
			}
		}(g)
	}
	wg.Wait()

	admitted := q.EnqueuedTotal()
	dropped := q.DroppedTotal()
	if admitted+dropped != 32*64 {
		t.Fatalf("accounting leak: admitted %d + dropped %d != offered %d", admitted, dropped, 32*64)
	}
	if admitted < int64(capacity) {
		t.Fatalf("a queue that never had more than %d consumers must fill its buffer, admitted %d", capacity, admitted)
	}
	if q.Len() != capacity {
		t.Fatalf("no consumer ran: buffer must be exactly full, len=%d", q.Len())
	}

	// Drain to quiesce the channel before test end.
	for q.Len() > 0 {
		<-q.C()
	}
}

func TestPolicyString(t *testing.T) {
	t.Parallel()
	if DropNewest.String() != "drop-newest" {
		t.Fatalf("unexpected policy name %q", DropNewest.String())
	}
	if Policy(42).String() == "" {
		t.Fatal("unknown policies must still render")
	}
}
