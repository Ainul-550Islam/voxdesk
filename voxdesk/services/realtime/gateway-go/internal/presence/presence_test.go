package presence

import (
	"fmt"
	"sync"
	"testing"
)

const (
	tenantA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
	tenantB = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
	userU1  = "cccccccc-3333-4333-8333-cccccccccccc"
	userU2  = "dddddddd-4444-4444-8444-dddddddddddd"
)

func TestTracksUsersNotSockets(t *testing.T) {
	t.Parallel()
	r := New()

	if ev := r.Online(tenantA, userU1, "s1"); ev.Kind != KindUserOnline || ev.TotalUsers != 1 {
		t.Fatalf("first session must mark the user online, got %+v", ev)
	}
	if ev := r.Online(tenantA, userU1, "s2"); ev.Kind != KindSessionJoined || ev.UserSessions != 2 || ev.TotalUsers != 1 {
		t.Fatalf("second tab must not double-count the user, got %+v", ev)
	}
	if !r.IsOnline(tenantA, userU1) || r.IsOnline(tenantA, userU2) {
		t.Fatal("IsOnline answers do not match the recorded state")
	}

	if ev := r.Offline(tenantA, userU1, "s2"); ev.Kind != KindSessionLeft || ev.TotalUsers != 1 {
		t.Fatalf("closing one of two tabs must keep the user online, got %+v", ev)
	}
	if ev := r.Offline(tenantA, userU1, "s1"); ev.Kind != KindUserOffline || ev.TotalUsers != 0 || ev.TenantUsers != 0 {
		t.Fatalf("closing the last tab must mark the user offline, got %+v", ev)
	}
	if r.IsOnline(tenantA, userU1) || r.TenantCount() != 0 {
		t.Fatal("fully-offline tenant must leave no residue")
	}
}

func TestIdempotentDuplicateAndUnmatchedOps(t *testing.T) {
	t.Parallel()
	r := New()

	r.Online(tenantA, userU1, "s1")
	if ev := r.Online(tenantA, userU1, "s1"); ev.Kind != KindUnchanged {
		t.Fatalf("duplicate join must be a no-op, got %+v", ev)
	}
	if got := r.TotalUsers(); got != 1 {
		t.Fatalf("duplicate join inflated state: %d users", got)
	}
	if ev := r.Offline(tenantA, userU1, "never-seen"); ev.Kind != KindUnchanged {
		t.Fatalf("unmatched leave must be a no-op, got %+v", ev)
	}
	if ev := r.Offline(tenantB, userU1, "s1"); ev.Kind != KindUnchanged {
		t.Fatalf("leave against the wrong tenant must be a no-op, got %+v", ev)
	}
	if ev := r.Online("", userU1, "s9"); ev.Kind != KindUnchanged || r.TotalUsers() != 1 {
		t.Fatalf("unauthenticated socket must not produce presence, got %+v", ev)
	}
}

func TestTenantsAreIsolatedAndSnapshotsSorted(t *testing.T) {
	t.Parallel()
	r := New()
	r.Online(tenantB, userU2, "s-b")
	r.Online(tenantA, userU2, "s-a2")
	r.Online(tenantA, userU1, "s-a1")

	if got := r.OnlineUsers(tenantA); len(got) != 2 || got[0] != userU1 || got[1] != userU2 {
		t.Fatalf("tenant A users wrong or unsorted: %v", got)
	}
	if got := r.SessionsOf(tenantA, userU1); len(got) != 1 || got[0] != "s-a1" {
		t.Fatalf("session snapshot wrong: %v", got)
	}
	if got := r.TotalUsers(); got != 3 { // (A,u1) (A,u2) (B,u2) are distinct
		t.Fatalf("TotalUsers counts (tenant,user) pairs: got %d, want 3", got)
	}
}

func TestOnChangeSeesOnlyRealTransitions(t *testing.T) {
	t.Parallel()
	r := New()
	var kinds []Kind
	var lastTotal int
	var mu sync.Mutex
	r.OnChange(func(ev Event) {
		mu.Lock()
		defer mu.Unlock()
		kinds = append(kinds, ev.Kind)
		lastTotal = ev.TotalUsers
	})

	r.Online(tenantA, userU1, "s1")
	r.Online(tenantA, userU1, "s1") // duplicate: must NOT fire
	r.Offline(tenantA, userU1, "s1")

	mu.Lock()
	defer mu.Unlock()
	want := []Kind{KindUserOnline, KindUserOffline}
	if len(kinds) != len(want) {
		t.Fatalf("subscriber saw %v, want %v", kinds, want)
	}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("subscriber saw %v, want %v", kinds, want)
		}
	}
	if lastTotal != 0 {
		t.Fatalf("event snapshots must reflect post-transition state, last total %d", lastTotal)
	}
}

func TestOnChangeMayReenterRegistry(t *testing.T) {
	t.Parallel()
	r := New()
	var spoke sync.Once
	r.OnChange(func(Event) { spoke.Do(func() { _ = r.TotalUsers() }) })
	r.Online(tenantA, userU1, "s1") // must not deadlock with a reading subscriber
}

func TestConcurrentChurnKeepsAccountingExact(t *testing.T) {
	t.Parallel()
	r := New()
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			user := fmt.Sprintf("user-%d", g)
			for i := 0; i < 50; i++ {
				sess := fmt.Sprintf("s-%d-%d", g, i)
				r.Online(tenantA, user, sess)
				r.Offline(tenantA, user, sess)
			}
			// Leave one permanent session so the user stays online.
			r.Online(tenantA, user, fmt.Sprintf("perm-%d", g))
		}(g)
	}
	wg.Wait()
	if got := r.TotalUsers(); got != 8 {
		t.Fatalf("after churn exactly the 8 permanent users must remain, got %d", got)
	}
	if got := r.TenantCount(); got != 1 {
		t.Fatalf("tenant residue wrong, got %d", got)
	}
}
