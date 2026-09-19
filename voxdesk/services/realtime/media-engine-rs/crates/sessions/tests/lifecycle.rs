use protocol::{ParticipantId, RoomId};
use sessions::{JoinOutcome, SessionState, Store};
use std::time::{Duration, Instant};

fn room() -> RoomId {
    RoomId("r".to_string())
}
fn who(n: &str) -> ParticipantId {
    ParticipantId(n.to_string())
}
fn t0() -> Instant {
    Instant::now()
}

#[test]
fn state_machine_moves_are_table_driven() {
    use SessionState::*;
    let legal = [
        (New, Negotiating),
        (Negotiating, Connected),
        (Connected, Draining),
        (Draining, Closed),
        (Connected, Closed),
        (Negotiating, Closed),
    ];
    let illegal = [
        (New, Connected), // never skip negotiation — ICE is not optional
        (New, Draining),
        (Negotiating, Draining),
        (Draining, Connected), // no resurrection
        (Closed, Connected),   // closed is a sink
    ];
    let store = Store::new();
    for (from, to) in legal {
        let now = t0();
        let outcome = store.join(&room(), &who("p"), now);
        let JoinOutcome::New(session) = outcome else {
            panic!("first join is New")
        };
        // Walk session to `from` legitimately.
        let path: &[SessionState] = match from {
            New => &[],
            Negotiating => &[Negotiating],
            Connected => &[Negotiating, Connected],
            Draining => &[Negotiating, Connected, Draining],
            Closed => &[],
        };
        for s in path {
            assert!(session.advance(*s).is_ok());
        }
        assert!(
            session.advance(to).is_ok(),
            "{from:?} → {to:?} must be legal"
        );
        let l = store.leave(session.id, now);
        assert!(l.is_some());
    }
    for (from, to) in illegal {
        let now = t0();
        let JoinOutcome::New(session) = store.join(&room(), &who("p"), now) else {
            panic!()
        };
        let path: &[SessionState] = match from {
            New => &[],
            Negotiating => &[Negotiating],
            Connected => &[Negotiating, Connected],
            Draining => &[Negotiating, Connected, Draining],
            Closed => &[],
        };
        for s in path {
            let _ = session.advance(*s);
        }
        if from == Closed {
            session.close(now);
            assert_eq!(session.state(), Closed);
        }
        assert!(
            session.advance(to).is_err(),
            "{from:?} → {to:?} must be refused"
        );
        store.leave(session.id, t0());
    }
}

#[test]
fn reconnect_rebinds_and_fences_the_old_incarnation() {
    let store = Store::new();
    let r = room();
    let now = t0();

    let outcome1 = store.join(&r, &who("dup"), now);
    let JoinOutcome::New(old) = outcome1 else {
        panic!()
    };
    old.advance(SessionState::Negotiating).unwrap();
    old.advance(SessionState::Connected).unwrap();

    // Same participant rejoins (ws reconnect): a FRESH session, old closes.
    let outcome2 = store.join(&r, &who("dup"), now + Duration::from_millis(5));
    let JoinOutcome::Rebound { fresh, previous_id } = outcome2 else {
        panic!("second join of same participant is a Rebound")
    };
    assert_eq!(previous_id, old.id);
    assert_ne!(fresh.id, old.id);
    assert_eq!(old.state(), SessionState::Closed, "old incarnation fenced");
    assert_eq!(fresh.state(), SessionState::New);

    // A THIRD join is also a Rebound — and the previous (already-closed)
    // incarnation is not double-counted.
    let outcome3 = store.join(&r, &who("dup"), now + Duration::from_millis(10));
    let JoinOutcome::Rebound { previous_id, .. } = outcome3 else {
        panic!()
    };
    assert_eq!(previous_id, fresh.id);
}

#[test]
fn sweep_distinguishes_negotiation_timeout_from_idle() {
    let store = Store::new();
    let r = room();
    let base = t0();

    let JoinOutcome::New(stuck) = store.join(&r, &who("stuck"), base) else {
        panic!()
    };
    let JoinOutcome::New(cozy) = store.join(&r, &who("cozy"), base) else {
        panic!()
    };
    cozy.advance(SessionState::Negotiating).unwrap();
    cozy.advance(SessionState::Connected).unwrap();

    // At negotiation_timeout + 1ms: only the stuck one dies.
    let gone = store.sweep(base + store.negotiation_timeout + Duration::from_millis(1));
    assert_eq!(gone.len(), 1);
    assert!(matches!(
        gone[0].0,
        sessions::SweepKind::NegotiationTimedOut
    ));
    assert_eq!(gone[0].1.id, stuck.id);
    assert_eq!(store.count(), 1);

    // Cozy idles out.
    let gone2 = store.sweep(base + store.idle_timeout + Duration::from_millis(1));
    assert_eq!(gone2.len(), 1);
    assert!(matches!(gone2[0].0, sessions::SweepKind::Idle));
    assert_eq!(store.count(), 0);

    // Touch keeps a socket alive.
    let base2 = t0();
    let JoinOutcome::New(active) = store.join(&r, &who("active"), base2) else {
        panic!()
    };
    active.advance(SessionState::Negotiating).unwrap();
    active.advance(SessionState::Connected).unwrap();
    active.touch(base2 + store.idle_timeout + Duration::from_millis(1));
    assert!(store
        .sweep(base2 + store.idle_timeout + Duration::from_millis(2))
        .is_empty());
}

#[test]
fn snapshots_dont_hold_the_registry_lock() {
    let store = Store::new();
    let now = t0();
    for i in 0..4 {
        let JoinOutcome::New(_) = store.join(&RoomId(format!("r{i}")), &who("p"), now) else {
            panic!()
        };
    }
    // Inside the closure-triggered snapshot, count() must still work —
    // i.e. snapshot_where drops its read borrow before returning.
    let snapshot = store.snapshot_where(|s| s.participant == who("p"));
    assert_eq!(snapshot.len(), 4);
    assert_eq!(store.count(), 4);
    // Mutations between snapshot & use don't panic (they're separate epochs).
    store.leave(snapshot[0].id, now);
}
