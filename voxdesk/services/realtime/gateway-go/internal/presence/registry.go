package presence

// This file holds the Registry's bookkeeping internals (locked helpers)
// and the subscription surface for change consumers — separated from the
// mutating API in presence.go so the transition rules read in one place.

// OnChange subscribes fn to every STATE-CHANGING transition the registry
// records. Callbacks run synchronously AFTER the registry's mutex is
// released, in registration order; a callback that re-enters the registry
// (reads are fine) cannot deadlock. No-op events (duplicate Online of an
// already-recorded session, Offline of a never-recorded one) are NOT
// delivered — subscribers only ever see real transitions.
//
// The one production subscriber today is the /metrics presence gauge; the
// callback interface (rather than a hardwired metric call) is what keeps
// this package free of an observability import.
func (r *Registry) OnChange(fn func(Event)) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.onChange = append(r.onChange, fn)
}

// emit fans a transition out to subscribers. Only state-changing kinds are
// delivered; KindUnchanged is bookkeeping noise.
func (r *Registry) emit(event Event) {
	if event.Kind == KindUnchanged {
		return
	}
	r.mu.Lock()
	subs := append([]func(Event){}, r.onChange...)
	r.mu.Unlock()
	for _, fn := range subs {
		fn(event)
	}
}

// totalUsersLocked sums reachable (tenant, user) pairs. Callers hold mu.
func (r *Registry) totalUsersLocked() int {
	total := 0
	for _, users := range r.byTenant {
		total += len(users)
	}
	return total
}
