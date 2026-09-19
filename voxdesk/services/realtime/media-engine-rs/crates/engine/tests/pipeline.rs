//! Engine pipeline tests: join → trickle/offer → publish (SSRC minted
//! HERE, not anywhere else) → RTP/RTCP end-to-end through the owned
//! routes. These describe the contract the Go gateway's engine client
//! drives in production, in testable local form.

use engine::Engine;
use protocol::json::{self, Value};
use protocol::{MediaSessionId, ParticipantId, RoomId, TrackId};
use std::net::SocketAddr;
use std::sync::Arc;

fn engine_addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

fn ipv4(addr: SocketAddr) -> [u8; 4] {
    match addr {
        SocketAddr::V4(a) => a.ip().octets(),
        _ => panic!("v4 only"),
    }
}

fn join_frame(name: &str, room: &str) -> String {
    format!(r#"{{"type":"join","room":"{room}","participant":"{name}"}}"#)
}

fn ready_ice(frames: &[String]) -> (String, String, String) {
    for f in frames {
        let v = json::parse(f).unwrap();
        if v.get("type").and_then(Value::as_str) == Some("ready") {
            return (
                v.get("session")
                    .and_then(Value::as_str)
                    .unwrap()
                    .to_string(),
                v.get("ice_ufrag")
                    .and_then(Value::as_str)
                    .unwrap()
                    .to_string(),
                v.get("ice_pwd")
                    .and_then(Value::as_str)
                    .unwrap()
                    .to_string(),
            );
        }
    }
    panic!("no Ready frame in {frames:?}");
}

/// Join, ICE-bind, and capture the session id (production's handshake
/// shape, minimum).
fn join_and_nominate(
    engine: &mut Engine,
    t: &Arc<transport::MemTransport>,
    name: &str,
    room: &str,
    client: SocketAddr,
    tid: [u8; 12],
) -> String {
    let ready = engine.on_signaling_frame(&join_frame(name, room));
    let (sid, ufrag, pwd) = ready_ice(&ready);
    let bind = webrtc::stun::StunBuilder::new(1, tid)
        .username(&format!("{ufrag}:"))
        .use_candidate()
        .build_with_integrity(&pwd);
    t.inject(client, bind);
    engine.media_step(1);
    let sent = t.drain_sent();
    let resp_msg = webrtc::stun::StunMessage::parse(&sent[0].1).unwrap();
    assert_eq!(resp_msg.msg_type, 0x0101, "bind succeeded");
    assert!(resp_msg.verify_integrity(&pwd, &sent[0].1));
    assert!(
        engine
            .by_endpoint
            .contains_key(&(client.port(), ipv4(client))),
        "endpoint correlated"
    );
    sid
}

fn publish(session: &str, track: &str, kind: &str) -> String {
    format!(r#"{{"type":"publish","session":"{session}","track":"{track}","kind":"{kind}"}}"#)
}

fn subscribe(session: &str, participant: &str, track: &str) -> String {
    format!(
        r#"{{"type":"subscribe","session":"{session}","participant":"{participant}","track":"{track}"}}"#
    )
}

fn minted_ssrc(engine: &Engine, session: &str, track: &str) -> u32 {
    let sid = MediaSessionId(session.into());
    let tid = TrackId(track.into());
    engine
        .by_ssrc
        .iter()
        .find_map(|(ssrc, (owner, owner_track))| {
            (owner == &sid && owner_track == &tid).then_some(*ssrc)
        })
        .unwrap_or_else(|| panic!("no ssrc minted for {session}/{track}"))
}

#[test]
fn join_trickle_offer_publish_subscribe_full_fanout() {
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9500));
    let client_a = engine_addr(9601);
    let client_b = engine_addr(9602);

    let sid_a = join_and_nominate(&mut engine, &t, "alice", "r1", client_a, *b"transact000!");
    let sid_b = join_and_nominate(&mut engine, &t, "bob", "r1", client_b, *b"transact999!");

    // Trickled candidate lands on alice's agent (RFC 8839 discipline).
    let trickle = format!(
        r#"{{"type":"trickle","session":"{sid_a}","candidate":{{"candidate":"candidate:1 1 UDP 2130706431 203.0.113.9 54400 typ host","sdpMid":"0"}}}}"#
    );
    let count_of = |e: &Engine, sid: &str| {
        e.slots
            .get(&MediaSessionId(sid.into()))
            .unwrap()
            .agent
            .as_ref()
            .unwrap()
            .remote_candidates()
            .len()
    };
    let before = count_of(&engine, &sid_a);
    let replies = engine.on_signaling_frame(&trickle);
    assert!(
        replies.is_empty(),
        "valid trickle: quiet success (replies were {replies:?})"
    );
    let mid = count_of(&engine, &sid_a);
    assert_eq!(mid, before + 1, "trickle persisted to agent pair table");
    assert!(engine
        .slots
        .get(&MediaSessionId(sid_a.clone()))
        .unwrap()
        .agent
        .as_ref()
        .unwrap()
        .remote_candidates()
        .iter()
        .any(|c| c.port == 54400));

    // Duplicate trickle: deduped, STILL quiet success.
    let _ = engine.on_signaling_frame(&trickle);
    assert_eq!(
        count_of(&engine, &sid_a),
        mid,
        "duplicate trickle pooled, not multiplied"
    );

    // Malformed trickle: the control surface says NO with BadMessage.
    let bad = format!(
        r#"{{"type":"trickle","session":"{sid_a}","candidate":{{"candidate":"not-a-candidate"}}}}"#
    );
    let replies = engine.on_signaling_frame(&bad);
    let v = json::parse(&replies[0]).unwrap();
    assert_eq!(v.get("code").and_then(Value::as_str), Some("bad_message"));

    // end-of-candidates marker.
    let eoc = format!(r#"{{"type":"trickle","session":"{sid_a}","candidate":null}}"#);
    assert!(engine.on_signaling_frame(&eoc).is_empty());

    // Publish mic on alice: engine mints the SSRC at THAT point.
    let fx = engine.on_signaling_frame(&publish(&sid_a, "mic", "audio"));
    let ssrc = minted_ssrc(&engine, &sid_a, "mic");
    let published = fx.iter().any(|f| {
        json::parse(f).unwrap().get("type").and_then(Value::as_str) == Some("track.published")
    });
    assert!(published, "route fanout announced");
    assert!(
        engine
            .slots
            .get(&MediaSessionId(sid_a.clone()))
            .unwrap()
            .tracks[&TrackId("mic".into())]
            .ssrc
            != 0,
        "slot ledgers real ssrc, got {:?}",
        engine
            .slots
            .get(&MediaSessionId(sid_a.clone()))
            .unwrap()
            .tracks
    );

    // Bob subscribes (frame path, session-scoped).
    let subs = engine.on_signaling_frame(&subscribe(&sid_b, "alice", "mic"));
    assert!(
        subs.iter()
            .all(|f| json::parse(f).unwrap().get("type").and_then(Value::as_str) != Some("error")),
        "subscribe succeeded: {subs:?}"
    );

    // RTP from alice with the minted SSRC → exactly one datagram to bob.
    let pkt = streams::packet::RtpPacket::build(111, 7000, 160 * 7, ssrc, true, b"media-payload");
    t.inject(client_a, pkt.raw.clone());
    engine.media_step(3);
    let sent = t.drain_sent();
    assert_eq!(sent.len(), 1, "exactly one leg: {sent:?}");
    assert_eq!(sent[0].0, client_b);
    let decoded = streams::packet::RtpPacket::parse(sent[0].1.clone()).unwrap();
    assert_eq!(
        decoded.ssrc, ssrc,
        "ssrc identity preserved through the sfu"
    );
    assert_eq!(decoded.payload(), b"media-payload");

    // Spoof: same ssrc, wrong 5-tuple → refused on the spoof counter.
    let attacker = engine_addr(9700);
    let forged = streams::packet::RtpPacket::build(111, 7001, 160 * 8, ssrc, false, b"hijack");
    t.inject(attacker, forged.raw.clone());
    engine.media_step(4);
    assert!(t.drain_sent().is_empty());
    assert!(engine.stats.rtp_drop_spoof >= 1, "spoof ledger moved");

    // Unknown ssrc (never minted) → refused on unknown_ssrc.
    let ghost = streams::packet::RtpPacket::build(111, 7002, 160 * 9, 0xDEAD_BEEF, false, b"ghost");
    t.inject(client_a, ghost.raw.clone());
    engine.media_step(5);
    assert!(t.drain_sent().is_empty());
    assert!(engine.stats.rtp_drop_unknown_ssrc >= 1);

    // RTCP receiver report from bob claims loss on alice's mic: the
    // engine applies the block to alice's stream record.
    let block = streams::rtcp::ReportBlock {
        ssrc,
        fraction_lost: 25,
        cumulative_lost: 3,
        highest_seq_ext: (1 << 16) | 7002,
        jitter: 41,
        lsr: 0,
        dlsr: 0,
    };
    let rr = streams::rtcp::build_receiver_report(0xB0B_FACE, &[block]);
    t.inject(client_b, rr);
    engine.media_step(6);
    assert_eq!(engine.stats.rtcp_rr, 1);
    assert_eq!(engine.stats.rtcp_reports_applied, 1);
    let stamp = media::RouteStamp {
        room: RoomId("r1".into()),
        participant: ParticipantId("alice".into()),
        track: TrackId("mic".into()),
    };
    let stream = engine.registry.get(&stamp).expect("alice mic stream");
    assert_eq!(stream.rr_fraction_lost_latest, 25);
    assert_eq!(stream.rr_cumulative_lost_latest, 3);

    // An SR is counted, not silently discarded.
    let sr = {
        let mut v = Vec::new();
        v.extend(&[0x80, 200, 0, 6]); // v2 sr len
        v.extend(&ssrc.to_be_bytes());
        v.extend(&0xE83Au32.to_be_bytes()); // ntp_msw
        v.extend(&0x1000u32.to_be_bytes()); // ntp_lsw
        v.extend(&0u32.to_be_bytes()); // rtp_ts
        v.extend(&1u32.to_be_bytes()); // packets
        v.extend(&160u32.to_be_bytes()); // octets
        v
    };
    t.inject(client_a, sr);
    engine.media_step(7);
    assert_eq!(engine.stats.rtcp_sr, 1);
    assert_eq!(engine.stats.rtcp_malformed, 0);

    // Leave: the departed publisher's ssrc ownership is scrapped; a late
    // packet replaying her SSRC lands on unknown-ssrc, not fallout.
    let lfx = engine.on_signaling_frame(&format!(r#"{{"type":"leave","session":"{sid_a}"}}"#));
    let _ = lfx;
    assert!(
        !engine
            .by_ssrc
            .values()
            .any(|(owner, _)| owner == &MediaSessionId(sid_a.clone())),
        "ownership scrapped on leave"
    );
}

#[test]
fn multi_track_publishers_each_get_distinct_ssrc_and_route() {
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9530));
    let client = engine_addr(9603);
    let sid = join_and_nominate(&mut engine, &t, "multi", "r2", client, *b"multitrack12");

    engine.on_signaling_frame(&publish(&sid, "mic", "audio"));
    engine.on_signaling_frame(&publish(&sid, "cam", "video"));
    assert_eq!(engine.stats.tracks_registered, 2);
    let ssrc_mic = minted_ssrc(&engine, &sid, "mic");
    let ssrc_cam = minted_ssrc(&engine, &sid, "cam");
    assert_ne!(ssrc_mic, ssrc_cam, "allocator never collides in one slot");

    // Viewer subscribes ONLY the camera: mic datagrams share the session
    // but land in no legs.
    let viewer_addr = engine_addr(9604);
    let sid_v = join_and_nominate(
        &mut engine,
        &t,
        "viewer",
        "r2",
        viewer_addr,
        *b"viewertid__!",
    );
    engine.on_signaling_frame(&subscribe(&sid_v, "multi", "cam"));

    t.inject(
        client,
        streams::packet::RtpPacket::build(111, 1, 0, ssrc_mic, false, b"mic-frame").raw,
    );
    t.inject(
        client,
        streams::packet::RtpPacket::build(120, 2, 90, ssrc_cam, false, b"cam-frame").raw,
    );
    engine.media_step(10);
    let sent = t.drain_sent();
    assert_eq!(sent.len(), 1, "exactly the subscribed leg: {sent:?}");
    assert_eq!(sent[0].0, viewer_addr);
    assert_eq!(
        streams::packet::RtpPacket::parse(sent[0].1.clone())
            .unwrap()
            .payload(),
        b"cam-frame"
    );
}

#[test]
fn control_wire_envelope_contract() {
    let (mut engine, _t) = Engine::with_mem_transport(engine_addr(9560));

    // Enveloped join: reply is enveloped, correlation id round-trips.
    let body = r#"{"v":1,"id":"a1b2c3d4e5f60001","frame":{"type":"join","room":"r9","participant":"envelope-test"}}"#;
    let reply = engine.on_control(body);
    let v = json::parse(&reply).unwrap();
    assert_eq!(v.get("v").and_then(Value::as_u64), Some(1));
    assert_eq!(
        v.get("id").and_then(Value::as_str),
        Some("a1b2c3d4e5f60001")
    );
    let frames = v
        .get("frames")
        .and_then(Value::as_arr)
        .expect("frames array");
    assert_eq!(frames[0].get("type").and_then(Value::as_str), Some("ready"));

    // Bare legacy frame: reply is the JSON array (back-compat).
    let bare = engine.on_control(r#"{"type":"ping"}"#);
    assert_eq!(
        bare, "[]",
        "ping has no reply frames in today's vocabulary: {bare}"
    );

    // Version skew: envelope error, id preserved when parseable.
    let skew = engine.on_control(r#"{"v":99,"id":"x","frame":{"type":"ping"}}"#);
    let v = json::parse(&skew).unwrap();
    assert_eq!(
        v.get("error")
            .and_then(|e| e.get("code"))
            .and_then(Value::as_str),
        Some("unsupported_version")
    );
    assert!(v
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .unwrap()
        .contains("99"));

    // Garbage body: structured error, never a panic.
    let bad = engine.on_control("{not json");
    let v = json::parse(&bad).unwrap();
    assert_eq!(
        v.get("error")
            .and_then(|e| e.get("code"))
            .and_then(Value::as_str),
        Some("bad_message")
    );
}

#[test]
fn fleet_ssrc_partitions_do_not_overlap_across_labels() {
    // Two differently-labelled engines get different partition bytes
    // (labels chosen with DISTINCT partitions by construction of
    // ssrc_partition_of — verified directly here), and 512 allocations
    // from each produce two disjoint sets.
    let mk = |label: &str| {
        let cfg = engine::EngineConfig {
            engine_label: label.to_string(),
            ..Default::default()
        };
        Engine::new(
            cfg,
            Arc::new(transport::MemTransport::new(engine_addr(9990), 16)),
        )
    };
    let label_a = String::from("edge-1");
    let mut label_b = String::new();
    {
        let pa = mk(&label_a).ssrc_partition();
        for i in 2..=32u32 {
            let label = format!("edge-{i}");
            if mk(&label).ssrc_partition() != pa {
                label_b = label;
                break;
            }
        }
        assert!(
            !label_b.is_empty(),
            "expected two distinct partitions among edge-1..edge-32"
        );
    }
    let ea = mk(&label_a);
    let eb = mk(&label_b);
    assert_ne!(ea.ssrc_partition(), eb.ssrc_partition());

    // Sampled allocations: every minted ssrc's top byte IS the engine's
    // partition byte; the two sample sets share no member.
    let mut set = std::collections::BTreeSet::new();
    for _ in 0..512 {
        // allocate_ssrc walks the collision gate against by_ssrc — with
        // no slots registered, every draw is fresh by construction of the
        // counter; insert into the local sample set here for the cross-
        // node disjointness assertion.
        let s = ea.allocate_ssrc_for_test();
        assert_eq!((s >> 24) as u8, ea.ssrc_partition());
        assert!(set.insert(s));
    }
    for _ in 0..512 {
        let s = eb.allocate_ssrc_for_test();
        assert_eq!((s >> 24) as u8, eb.ssrc_partition());
        assert!(set.insert(s), "cross-node overlap at ssrc {s:#x}");
    }
}

#[test]
fn health_json_reflects_live_state() {
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9570));
    let before = json::parse(&engine.health_json()).unwrap();
    assert_eq!(
        before.get("engine").and_then(Value::as_str),
        Some("media-engine-rs")
    );
    assert!(
        before.get("ready").and_then(Value::as_str) == Some("true")
            || before.get("ready") == Some(&Value::Bool(true))
    );
    let sid = join_and_nominate(
        &mut engine,
        &t,
        "h",
        "hroom",
        engine_addr(9610),
        *b"health_tid!!",
    );
    engine.on_signaling_frame(&publish(&sid, "mic", "audio"));
    let after = json::parse(&engine.health_json()).unwrap();
    assert_eq!(after.get("rooms").and_then(Value::as_u64), Some(1));
    assert_eq!(after.get("participants").and_then(Value::as_u64), Some(1));
    assert_eq!(after.get("tracks").and_then(Value::as_u64), Some(1));
}
