use protocol::{MediaKind, ParticipantId, RoomId, TrackId};
use routing::{RoomLimits, RouteError, RouteTable};

fn room(name: &str) -> RoomId {
    RoomId(name.to_string())
}
fn who(name: &str) -> ParticipantId {
    ParticipantId(name.to_string())
}
fn track(name: &str) -> TrackId {
    TrackId(name.to_string())
}

fn seeded() -> RouteTable {
    let rt = RouteTable::new(RoomLimits::default());
    for p in ["alice", "bob", "carol"] {
        assert_eq!(
            rt.join(&room("r1"), who(p), 1).unwrap(),
            if p == "alice" {
                1
            } else if p == "bob" {
                2
            } else {
                3
            }
        );
    }
    rt.publish(&room("r1"), &who("alice"), track("mic"), MediaKind::Audio)
        .unwrap();
    rt.publish(&room("r1"), &who("alice"), track("cam"), MediaKind::Video)
        .unwrap();
    rt
}

#[test]
fn fanout_follows_subscriptions_exactly() {
    let rt = seeded();
    let legs = rt.legs_for(&room("r1"), &who("alice"), &track("mic"));
    assert!(legs.is_empty(), "no subscriptions ⇒ no legs");

    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .unwrap();
    rt.subscribe(&room("r1"), &who("carol"), &who("alice"), &track("mic"))
        .unwrap();
    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("cam"))
        .unwrap();

    assert_eq!(
        *rt.legs_for(&room("r1"), &who("alice"), &track("mic")),
        vec![who("bob"), who("carol")]
    );
    assert_eq!(
        *rt.legs_for(&room("r1"), &who("alice"), &track("cam")),
        vec![who("bob")]
    );
    // Well-behaved callers all joined r1, so re-lookup is cheap.
    assert_eq!(
        rt.legs_for(&room("r-void"), &who("alice"), &track("mic"))
            .len(),
        0
    );
}

#[test]
fn subscribing_to_unpublished_track_is_a_routing_lie_refused() {
    let rt = seeded();
    assert_eq!(
        rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("phantom")),
        Err(RouteError::NoSuchTrack)
    );
    assert_eq!(
        rt.subscribe(&room("r1"), &who("bob"), &who("dave"), &track("mic")),
        Err(RouteError::NoSuchParticipant)
    );
    // Idempotent re-assertion.
    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .unwrap();
    assert!(rt
        .subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .is_ok());
    assert_eq!(
        rt.legs_for(&room("r1"), &who("alice"), &track("mic")).len(),
        1
    );
}

#[test]
fn caps_bite_at_join_publish_subscribe() {
    let rt = RouteTable::new(RoomLimits {
        max_participants: 2,
        max_tracks_per_participant: 1,
        max_subscriptions_per_participant: 1,
    });
    assert!(rt.join(&room("r"), who("a"), 1).is_ok());
    assert!(rt.join(&room("r"), who("b"), 2).is_ok());
    assert_eq!(rt.join(&room("r"), who("c"), 3), Err(RouteError::RoomFull));
    // Duplicate re-join is not a cap hit.
    assert!(rt.join(&room("r"), who("a"), 4).is_ok());

    rt.publish(&room("r"), &who("a"), track("t1"), MediaKind::Audio)
        .unwrap();
    assert_eq!(
        rt.publish(&room("r"), &who("a"), track("t2"), MediaKind::Audio),
        Err(RouteError::TrackLimit)
    );
    assert_eq!(
        rt.publish(&room("r"), &who("a"), track("t1"), MediaKind::Audio),
        Err(RouteError::DuplicateTrack)
    );

    // b has one subscription slot: to a publisher with multiple tracks we'd need 2.
    rt.subscribe(&room("r"), &who("b"), &who("a"), &track("t1"))
        .unwrap();
    rt.publish(&room("r"), &who("b"), track("tb"), MediaKind::Video)
        .unwrap();
    assert_eq!(
        rt.subscribe(&room("r"), &who("b"), &who("b"), &track("tb")),
        Err(RouteError::SubscriptionLimit)
    );
}

#[test]
fn leave_and_unpublish_scrub_routes() {
    let rt = seeded();
    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .unwrap();
    assert!(!rt
        .legs_for(&room("r1"), &who("alice"), &track("mic"))
        .is_empty());

    assert!(rt.unpublish(&room("r1"), &who("alice"), &track("mic")));
    assert!(
        rt.legs_for(&room("r1"), &who("alice"), &track("mic"))
            .is_empty(),
        "unpublished track has no legs"
    );
    assert!(
        !rt.unpublish(&room("r1"), &who("alice"), &track("mic")),
        "second unpublish is false"
    );

    // Publish again, resubscribe, then LEAVE must scrub both directions.
    rt.publish(&room("r1"), &who("alice"), track("mic"), MediaKind::Audio)
        .unwrap();
    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .unwrap();
    let gone = rt.leave(&room("r1"), &who("bob")).expect("bob existed");
    assert_eq!(gone.id, who("bob"));
    assert!(
        rt.legs_for(&room("r1"), &who("alice"), &track("mic"))
            .is_empty(),
        "departed subscriber leaves no leg"
    );
    let now = rt.leave(&room("r1"), &who("alice")).expect("alice existed");
    assert_eq!(
        now.published.len(),
        2,
        "leave returns the departed record incl. tracks"
    );
    assert!(rt.leave(&room("r1"), &who("alice")).is_none());
    // Empty rooms disappear from stats.
    rt.leave(&room("r1"), &who("carol"));
    assert_eq!(rt.stats().0, 0, "empty room evaporates");
}

#[test]
fn snapshot_updates_visible_through_legs_for_after_each_mutation() {
    let rt = seeded();
    rt.subscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"))
        .unwrap();
    assert_eq!(
        rt.legs_for(&room("r1"), &who("alice"), &track("mic")).len(),
        1
    );
    // Snapshot from the last read must not mask the next mutation.
    rt.subscribe(&room("r1"), &who("carol"), &who("alice"), &track("mic"))
        .unwrap();
    let legs = rt.legs_for(&room("r1"), &who("alice"), &track("mic"));
    assert_eq!(legs.len(), 2, "stale snapshot would hide carol");
    rt.unsubscribe(&room("r1"), &who("bob"), &who("alice"), &track("mic"));
    assert_eq!(
        *rt.legs_for(&room("r1"), &who("alice"), &track("mic")),
        vec![who("carol")]
    );
}

#[test]
fn room_summary_lists_everyone_elses_tracks() {
    let rt = seeded();
    let summary = rt.room_summary(&room("r1"), &who("carol"));
    assert_eq!(summary.len(), 2, "alice's mic + cam");
    assert!(summary.iter().all(|(p, _, _)| *p == who("alice")));
    // The publisher's own view excludes themself.
    assert!(rt.room_summary(&room("r1"), &who("alice")).is_empty());
    let (rooms, participants, tracks) = rt.stats();
    assert_eq!((rooms, participants, tracks), (1, 3, 2));
}
