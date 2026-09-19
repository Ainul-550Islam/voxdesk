use protocol::{
    ClientFrame, ErrorCode, MediaKind, MediaSessionId, ParticipantId, RoomId, ServerFrame, TrackId,
};
use routing::{RoomLimits, RouteTable};
use sessions::Store as SessionStore;
use signaling::{CallerContext, SignalCore};
use std::sync::Arc;
use std::time::Instant;

fn who(n: &str) -> ParticipantId {
    ParticipantId(n.to_string())
}
fn room() -> RoomId {
    RoomId("r1".to_string())
}
fn track(n: &str) -> TrackId {
    TrackId(n.to_string())
}

struct Rig {
    core: SignalCore,
    routes: Arc<RouteTable>,
}

fn rig() -> Rig {
    let routes = Arc::new(RouteTable::new(RoomLimits::default()));
    let core = SignalCore::new(
        Arc::new(SessionStore::new()),
        routes.clone(),
        "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89".into(),
        [203, 0, 113, 1],
        5000,
        "edge-test".into(),
    );
    Rig { core, routes }
}

fn caller() -> CallerContext {
    CallerContext {
        participant: who("none"),
        room: None,
        session: None,
        now: Instant::now(),
    }
}

#[test]
fn join_publish_subscribe_happy_path() {
    let Rig { core, routes } = rig();
    let mut alice = caller();
    let mut bob = caller();

    let fx = core.handle(
        &mut alice,
        ClientFrame::Join {
            room: room(),
            participant: who("alice"),
        },
    );
    let ready = fx.reply.first().expect("a reply");
    let ServerFrame::Ready {
        session,
        participant: p_ready,
        room: r_ready,
        ice_ufrag,
        ice_pwd,
    } = ready
    else {
        panic!("want Ready")
    };
    assert_eq!(
        (&p_ready.0, &r_ready.0),
        (&"alice".to_string(), &"r1".to_string())
    );
    assert_eq!(
        ice_ufrag.len(),
        6,
        "RFC 5245 ufrag minimum is 4; we issue 6"
    );
    assert_eq!(ice_pwd.len(), 24, "pwd minimum is 22; we issue 24");
    assert!(fx.new_session.is_some());
    let alice_session = session.clone();

    core.handle(
        &mut bob,
        ClientFrame::Join {
            room: room(),
            participant: who("bob"),
        },
    );

    // Publish → room fanout frame exists; routes table flipped.
    let fx = core.handle(
        &mut alice,
        ClientFrame::Publish {
            ssrc: None,
            session: None,
            track: track("mic"),
            kind: MediaKind::Audio,
        },
    );
    assert_eq!(
        fx.reply.len(),
        0,
        "publish is fire-and-forget for the publisher"
    );
    let ServerFrame::TrackPublished {
        participant: p_pub,
        track: t_pub,
        kind,
        ..
    } = fx.room_fanout.first().expect("fanout")
    else {
        panic!()
    };
    assert_eq!(
        (&p_pub.0, &t_pub.0, kind.as_str()),
        (&"alice".to_string(), &"mic".to_string(), "audio")
    );

    let fx = core.handle(
        &mut bob,
        ClientFrame::Subscribe {
            session: None,
            participant: who("alice"),
            track: track("mic"),
        },
    );
    assert!(fx.reply.is_empty(), "successful subscribe is silent");
    assert_eq!(
        routes.legs_for(&room(), &who("alice"), &track("mic")).len(),
        1
    );

    // SDP offer → answer carries the answer-side session creds + mid.
    let offer = "v=0\r\no=- 46107 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111 0\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\na=rtcp-mux\r\na=sendrecv\r\na=rtpmap:111 opus/48000/2\r\n";
    let fx = core.handle(
        &mut alice,
        ClientFrame::Offer {
            session: alice_session.clone(),
            sdp: offer.into(),
        },
    );
    let ServerFrame::Answer { sdp, session } = fx.reply.first().expect("answer") else {
        panic!()
    };
    assert_eq!(session, &alice_session);
    assert!(sdp.contains("m=audio 9 UDP/TLS/RTP/SAVPF 111 0"));
    assert!(sdp.contains("a=setup:passive"));

    // Leave: routing table drops the participant idempotently.
    core.handle(&mut alice, ClientFrame::Leave { session: None });
    assert_null(
        &routes.legs_for(&room(), &who("alice"), &track("mic")),
        "legs vanish after leave",
    );
    core.handle(&mut alice, ClientFrame::Leave { session: None }); // second Leave is a no-op on routing, not a panic
}

fn assert_null(legs: &Arc<Vec<ParticipantId>>, _label: &str) {
    assert_eq!(legs.len(), 0);
}

#[test]
fn publishes_before_join_and_foreign_sessions_are_refused() {
    let Rig { core, .. } = rig();
    let mut stranger = caller();

    let fx = core.handle(
        &mut stranger,
        ClientFrame::Publish {
            ssrc: None,
            session: None,
            track: track("mic"),
            kind: MediaKind::Audio,
        },
    );
    let Some(ServerFrame::Error { code, .. }) = fx.reply.first() else {
        panic!("want error")
    };
    assert_eq!(
        *code,
        ErrorCode::WrongState,
        "publish before any join is a state violation, not an unknown-room"
    );

    // A session id that was never minted: WrongState, not auth weirdness.
    let fx = core.handle(
        &mut stranger,
        ClientFrame::Offer {
            session: MediaSessionId("ms-9-9".into()),
            sdp: "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n".into(),
        },
    );
    let Some(ServerFrame::Error { code, .. }) = fx.reply.first() else {
        panic!()
    };
    assert_eq!(*code, ErrorCode::WrongState);

    // A malformed SDP (whose session IS valid) surfaces BadMessage.
    let mut alice = caller();
    let fx = core.handle(
        &mut alice,
        ClientFrame::Join {
            room: room(),
            participant: who("alice"),
        },
    );
    let Some(ServerFrame::Ready { session, .. }) = fx.reply.first() else {
        panic!()
    };
    let session = session.clone();
    let fx = core.handle(
        &mut alice,
        ClientFrame::Offer {
            session,
            sdp: "v=0\r\ntotally-broken-line\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n".into(),
        },
    );
    let Some(ServerFrame::Error { code, message }) = fx.reply.first() else {
        panic!()
    };
    assert_eq!(*code, ErrorCode::BadMessage);
    assert!(message.contains("rejected"));
}

#[test]
fn subscribe_to_phantom_track_fails_with_room_unknown() {
    let Rig { core, routes } = rig();
    let mut a = caller();
    let mut b = caller();
    core.handle(
        &mut a,
        ClientFrame::Join {
            room: room(),
            participant: who("a"),
        },
    );
    core.handle(
        &mut b,
        ClientFrame::Join {
            room: room(),
            participant: who("b"),
        },
    );

    let fx = core.handle(
        &mut b,
        ClientFrame::Subscribe {
            session: None,
            participant: who("a"),
            track: track("nope"),
        },
    );
    let Some(ServerFrame::Error { code, .. }) = fx.reply.first() else {
        panic!("want error")
    };
    assert_eq!(*code, ErrorCode::RoomUnknown);
    assert_eq!(routes.legs_for(&room(), &who("a"), &track("nope")).len(), 0);

    // Unsubscribe is quiet even when nothing was subscribed.
    let fx = core.handle(
        &mut b,
        ClientFrame::Unsubscribe {
            session: None,
            participant: who("a"),
            track: track("nope"),
        },
    );
    assert!(fx.reply.is_empty());
}

#[test]
fn duplicate_publish_is_over_limit_not_a_noop() {
    let Rig { core, .. } = rig();
    let mut a = caller();
    core.handle(
        &mut a,
        ClientFrame::Join {
            room: room(),
            participant: who("a"),
        },
    );
    core.handle(
        &mut a,
        ClientFrame::Publish {
            ssrc: None,
            session: None,
            track: track("mic"),
            kind: MediaKind::Audio,
        },
    );
    let fx = core.handle(
        &mut a,
        ClientFrame::Publish {
            ssrc: None,
            session: None,
            track: track("mic"),
            kind: MediaKind::Audio,
        },
    );
    let Some(ServerFrame::Error { code, .. }) = fx.reply.first() else {
        panic!()
    };
    assert_eq!(*code, ErrorCode::OverLimit);
}

#[test]
fn trickles_foreign_session_refused_valid_accepted() {
    let Rig { core, .. } = rig();
    let mut a = caller();
    let fx = core.handle(
        &mut a,
        ClientFrame::Join {
            room: room(),
            participant: who("a"),
        },
    );
    let Some(ServerFrame::Ready { session, .. }) = fx.reply.first() else {
        panic!()
    };
    let session = session.clone();
    assert!(core.handle(&mut a, ClientFrame::Ping).reply.is_empty());

    // Foreign-session trickle: refusal, not silence.
    let fx = core.handle(
        &mut a,
        ClientFrame::Trickle {
            session: MediaSessionId("any".into()),
            candidate: protocol::json::Value::obj(),
        },
    );
    let Some(ServerFrame::Error { code, .. }) = fx.reply.first() else {
        panic!("want error")
    };
    assert_eq!(*code, ErrorCode::WrongState);

    // Valid trickle for OUR session: quiet success + parsed effect.
    let mut cand = protocol::json::Value::obj();
    cand.set(
        "candidate",
        protocol::json::Value::Str(
            "candidate:1 1 UDP 2130706431 203.0.113.7 54400 typ host".into(),
        ),
    );
    let fx = core.handle(
        &mut a,
        ClientFrame::Trickle {
            session,
            candidate: cand,
        },
    );
    assert!(fx.reply.is_empty());
    let Some(t) = fx.trickle else {
        panic!("parsed trickle effect")
    };
    let c = t.candidate.expect("candidate parsed");
    assert_eq!(c.port, 54400);
}
