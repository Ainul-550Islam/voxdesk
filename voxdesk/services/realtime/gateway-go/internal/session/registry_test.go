package session

import (
	"testing"
	"time"
)

// The registry's invariants are what make the manager's guarantees physical:
// every index agrees with every other, so there is NEVER a path where a
// session knows a member the conn-index doesn't, or a tenant count the id
// index disagrees with. These tests poke the indexes directly.

func TestRegistryIndexesStayConsistentAcrossTheLifecycle(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// All three indexes agree.
	if _, ok := m.reg.get(id); !ok {
		t.Fatal("id index lost the session")
	}
	if got := m.reg.countForTenant(tenantA); got != 1 {
		t.Fatalf("tenant count = %d", got)
	}
	for _, conn := range []string{connA1, connA2} {
		if bound, ok := m.reg.sessionForConn(conn); !ok || bound != id {
			t.Errorf("conn index for %s = (%q, %v)", conn, bound, ok)
		}
	}

	// Removal cleans ALL of them — including both member bindings.
	m.ConnDropped(connA1)
	if _, ok := m.reg.get(id); ok {
		t.Error("id index kept an ended session")
	}
	if got := m.reg.countForTenant(tenantA); got != 0 {
		t.Errorf("tenant count after removal = %d", got)
	}
	for _, conn := range []string{connA1, connA2} {
		if _, ok := m.reg.sessionForConn(conn); ok {
			t.Errorf("conn index kept a binding for %s past session end", conn)
		}
	}
}

func TestRemoveDoesNotUnbindAConnectionOwnedByAnotherSession(t *testing.T) {
	// A connection that ended its old session and immediately started a new
	// one must not lose the NEW binding when the OLD session id is removed
	// twice (a delayed reaper racing a reconnect is the shape this guards).
	m, _ := newTestManager(8, time.Minute)
	first := mustStart(t, m, tenantA, connA1)
	m.ConnDropped(connA1) // ends first, unbinds connA1
	second := mustStart(t, m, tenantA, connA1)

	// Replay the removal of the FIRST id — stale deletes happen when two
	// lifecycle paths race (a reaper event vs an explicit end).
	m.reg.remove(first)

	if bound, ok := m.reg.sessionForConn(connA1); !ok || bound != second {
		t.Fatalf("stale removal stole the new session's binding: (%q, %v)", bound, ok)
	}
}

func TestTenantsCountTracksOnlyOccupiedTenants(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	mustStart(t, m, tenantA, connA1)
	mustStart(t, m, tenantB, connB1)
	if _, tenants := m.Stats(); tenants != 2 {
		t.Fatalf("tenants = %d", tenants)
	}
	m.ConnDropped(connA1)
	if _, tenants := m.Stats(); tenants != 1 {
		t.Errorf("empty tenant must leave the gauge: tenants = %d", tenants)
	}
}
