//! Engine-level DTLS-SRTP integration: join → offer → ICE nominate →
//! live DTLS handshake with a `dtls::Endpoint` playing the browser role,
//! routed through the same MemTransport discipline as the pipeline tests.
//! Plus the CM bind-path round-trip the handshake feeds
//! (`bind_srtp_split` direction mapping, real RTP decrypt).

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use engine::{Engine, EngineConfig};
use protocol::json::{self, Value};
use protocol::MediaSessionId;
use streams::packet::RtpPacket;
use transport::MemTransport;
use webrtc::srtp::{derive_session_keys, SrtpProtector, SrtpUnprotector};

fn engine_addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

fn join_frame(name: &str, room: &str) -> String {
    format!(r#"{{"type":"join","room":"{room}","participant":"{name}"}}"#)
}

fn ready_ice(ready: &[String]) -> (String, String, String) {
    let v = json::parse(&ready[0]).unwrap();
    (
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
    )
}

/// JSON-escape an SDP blob with real CRLFs into one offer frame.
fn offer_frame(session: &str, sdp: &str) -> String {
    let mut esc = String::with_capacity(sdp.len() + 32);
    for b in sdp.bytes() {
        match b {
            b'\r' => esc.push_str("\\r"),
            b'\n' => esc.push_str("\\n"),
            b'"' => esc.push_str("\\\""),
            other => esc.push(other as char),
        }
    }
    format!(r#"{{"type":"offer","session":"{session}","sdp":"{esc}"}}"#)
}

/// A browser-shaped offer body: ICE creds + DTLS settlement at the
/// media level, exactly where today's Unified-Plan browsers put them.
fn browser_offer(fingerprint: &str, setup: &str) -> String {
    format!(
        "v=0\r\n\
         o=- 7 2 IN IP4 127.0.0.1\r\n\
         s=-\r\n\
         t=0 0\r\n\
         a=group:BUNDLE 0\r\n\
         m=audio 9 UDP/TLS/RTP/SAVPF 111 0\r\n\
         c=IN IP4 0.0.0.0\r\n\
         a=mid:0\r\n\
         a=rtcp-mux\r\n\
         a=sendrecv\r\n\
         a=ice-ufrag:brow\r\n\
         a=ice-pwd:0123456789abcdefghijklmnop\r\n\
         a=fingerprint:sha-256 {fingerprint}\r\n\
         a=setup:{setup}\r\n\
         a=rtpmap:111 opus/48000/2\r\n"
    )
}

fn nominate(
    engine: &mut Engine,
    t: &Arc<MemTransport>,
    client: SocketAddr,
    ufrag: &str,
    pwd: &str,
    tid: [u8; 12],
) {
    nominate_with_remote(engine, t, client, ufrag, "", pwd, tid)
}

/// Post-offer nomination: the binding request's USERNAME must present
/// the OFFERED remote ufrag ("local:remote") — exactly what a browser
/// sends once SDP settled. An empty remote side is the pre-offer shape.
fn nominate_with_remote(
    engine: &mut Engine,
    t: &Arc<MemTransport>,
    client: SocketAddr,
    ufrag: &str,
    remote_ufrag: &str,
    pwd: &str,
    tid: [u8; 12],
) {
    let bind = webrtc::stun::StunBuilder::new(1, tid)
        .username(&format!("{ufrag}:{remote_ufrag}"))
        .use_candidate()
        .build_with_integrity(pwd);
    t.inject(client, bind);
    engine.media_step(1);
}

/// Join + nominate, returning ready creds.
fn join_and_prepare(
    engine: &mut Engine,
    t: &Arc<MemTransport>,
    name: &str,
    room: &str,
    client: SocketAddr,
    tid: [u8; 12],
) -> (String, String, String) {
    let ready = engine.on_signaling_frame(&join_frame(name, room));
    let (sid, ufrag, pwd) = ready_ice(&ready);
    nominate(engine, t, client, &ufrag, &pwd, tid);
    // The STUN response rides the sent queue; drop it so later drains
    // contain only what THIS test stage emitted.
    let _ = t.drain_sent();
    (sid, ufrag, pwd)
}

/// dtls_production engine: identity Some + matching answer fingerprint.
fn production_engine(port: u16) -> (Engine, Arc<MemTransport>, dtls::Identity) {
    let t = Arc::new(MemTransport::new(engine_addr(port), 4096));
    let identity = dtls::Identity::generate().expect("identity");
    let config = EngineConfig {
        fingerprint_sha256: identity.fingerprint().to_string(),
        dtls_identity: Some(identity.clone()),
        ..Default::default()
    };
    (Engine::new(config, t.clone()), t, identity)
}

fn answer_sdp(replies: &[String]) -> String {
    replies
        .iter()
        .find(|r| r.contains(r#""type":"answer""#))
        .cloned()
        .unwrap_or_else(|| panic!("no answer in {replies:?}"))
}

fn dtls_records(frames: &[(SocketAddr, Vec<u8>)]) -> Vec<Vec<u8>> {
    frames
        .iter()
        .filter(|(_, b)| b.len() >= 13 && (20..64).contains(&b[0]))
        .map(|(_, b)| b.clone())
        .collect()
}

// ---------------------------------------------------------------- tests

#[test]
fn fixture_engine_without_identity_drops_dtls_quietly() {
    // Default config is the documented fixture posture: no DTLS ever
    // binds, everything else keeps running.
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9700));
    let client = engine_addr(9701);
    let (sid, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", client, *b"transact000!");
    let sid = MediaSessionId(sid);

    let before = engine.stats.dtls_dropped;
    let fake_dtls = vec![22u8, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x99];
    t.inject(client, fake_dtls);
    engine.media_step(2);
    assert_eq!(
        engine.stats.dtls_dropped,
        before + 1,
        "unsupported traffic is refused"
    );
    assert_eq!(engine.stats.dtls_established, 0);
    assert!(
        engine.slots.get(&sid).unwrap().dtls.is_none(),
        "fixture never spawns"
    );
}

#[test]
fn offer_spawns_dtls_endpoint_and_answer_carries_boot_fingerprint() {
    let (mut engine, t, identity) = production_engine(9710);
    let client = engine_addr(9711);
    let (sid, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", client, *b"transact000!");

    let browser_id = dtls::Identity::generate().expect("browser identity");
    let replies = engine.on_signaling_frame(&offer_frame(
        &sid,
        &browser_offer(browser_id.fingerprint(), "actpass"),
    ));
    let sid = MediaSessionId(sid);

    let sdp = answer_sdp(&replies);
    assert!(
        sdp.contains(identity.fingerprint()),
        "answer carries THE boot identity's fingerprint, not a static label"
    );
    assert!(
        sdp.contains("a=setup:passive"),
        "actpass offer ⇒ passive us"
    );
    assert!(
        engine.slots.get(&sid).unwrap().dtls.is_some(),
        "offer spawns the session's RFC 5764 association"
    );
}

#[test]
fn passive_offer_flips_role_and_clienthello_flushes_at_nomination() {
    let (mut engine, t, _identity) = production_engine(9720);
    let client = engine_addr(9721);

    // Join, offer (NO nomination yet) — passive offerer ⇒ engine is the
    // DTLS CLIENT: the CH must already be buffered on the pending queue.
    let ready = engine.on_signaling_frame(&join_frame("alice", "r1"));
    let (sid, ufrag, pwd) = ready_ice(&ready);
    let browser_id = dtls::Identity::generate().expect("browser identity");
    let replies = engine.on_signaling_frame(&offer_frame(
        &sid,
        &browser_offer(browser_id.fingerprint(), "passive"),
    ));
    let sdp = answer_sdp(&replies);
    assert!(sdp.contains("a=setup:active"), "passive offer ⇒ active us");

    let sid = MediaSessionId(sid);
    let pending = engine.slots.get(&sid).unwrap().dtls_pending.len();
    assert!(
        pending > 0,
        "active role: ClientHello buffered pre-nomination"
    );

    // Nomination (post-offer ⇒ browser-flavored USERNAME) flushes the
    // pending CH onto the nominated 5-tuple.
    nominate_with_remote(
        &mut engine,
        &t,
        client,
        &ufrag,
        "brow",
        &pwd,
        *b"transact000!",
    );
    let sent = t.drain_sent();
    let records = dtls_records(&sent);
    assert!(
        records.iter().any(|r| r[0] == 22 && r.get(13) == Some(&1)),
        "a ClientHello (handshake type 1) reached the nominated endpoint"
    );
    assert!(
        engine.slots.get(&sid).unwrap().dtls_pending.is_empty(),
        "pending queue drained"
    );
}

#[test]
fn live_handshake_browser_role_gcm_negotiation_and_capture() {
    // REAL handshake through the engine's media path against a dimpl
    // client. Post-RFC-7714 posture: the engine's identity offers GCM
    // (both widths) ahead of CM, so a genuine three-profile client lands
    // on AEAD_AES_256_GCM and the bind gate ACCEPTS through the new GCM
    // crypto — the profile ledger stays clean (0 refused), keys bind,
    // counters advance. (CM as the negotiated floor is pinned in the
    // dtls crate's loopback suite.)
    let (mut engine, t, identity) = production_engine(9730);
    let client = engine_addr(9731);
    let (sid, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", client, *b"transact000!");

    let browser_id = dtls::Identity::generate().expect("browser identity");
    let _ = engine.on_signaling_frame(&offer_frame(
        &sid,
        &browser_offer(browser_id.fingerprint(), "actpass"),
    ));
    let sid = MediaSessionId(sid);
    let now = Instant::now();
    let mut browser = dtls::Endpoint::new(
        &browser_id,
        true,
        Some(identity.fingerprint().to_string()),
        now,
    );

    for p in browser.start_handshake(now).expect("browser CH").packets {
        t.inject(client, p);
    }

    let mut browser_keys: Option<dtls::Negotiated> = None;
    for round in 0..32 {
        engine.media_step((round + 1) * 10);
        for record in dtls_records(&t.drain_sent()) {
            let drive = browser
                .handle_packet(&record, Instant::now())
                .expect("browser consumes engine flight");
            for e in drive.events {
                let dtls::Event::Established(n) = e;
                browser_keys = Some(n);
            }
            for q in drive.packets {
                t.inject(client, q);
                engine.media_step((round + 1) * 10 + 1);
            }
        }
        if browser_keys.is_some() && engine.stats.dtls_established > 0 {
            break;
        }
    }

    let n = browser_keys.expect("browser side established (its own keys exist)");
    assert_eq!(n.profile.to_string(), "SRTP_AEAD_AES_256_GCM");
    assert_eq!(engine.stats.dtls_established, 1, "GCM handshake captured");
    assert_eq!(engine.stats.dtls_profile_refused, 0);
    assert_eq!(engine.stats.dtls_failed, 0);
    let slot = engine.slots.get(&sid).unwrap();
    assert!(slot.dtls.is_some(), "association persisted");
    assert!(
        slot.inbound.is_some() && slot.outbound.is_some(),
        "GCM keys bound"
    );
}

#[test]
fn fingerprint_mismatch_at_engine_tears_down_association() {
    // Browser PINs the wrong fingerprint for the engine: protocol-level
    // pin violation fails the association closed.
    let (mut engine, t, _identity) = production_engine(9740);
    let client = engine_addr(9741);
    let (sid, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", client, *b"transact000!");

    let browser_id = dtls::Identity::generate().expect("browser identity");
    let bogus = "DE:AD".repeat(16);
    let replies = engine.on_signaling_frame(&offer_frame(&sid, &browser_offer(&bogus, "actpass")));
    assert!(answer_sdp(&replies).contains("a=setup:passive"));
    let sid = MediaSessionId(sid);

    // Engine-side handshake starts on the browser's CH.
    let now = Instant::now();
    let mut browser = dtls::Endpoint::new(&browser_id, true, None, now);
    for p in browser.start_handshake(now).expect("CH").packets {
        t.inject(client, p);
    }
    for round in 0..32 {
        engine.media_step((round + 1) * 10);
        let records = dtls_records(&t.drain_sent());
        if records.is_empty() && engine.stats.dtls_failed > 0 {
            break;
        }
        for record in records {
            if let Ok(drive) = browser.handle_packet(&record, Instant::now()) {
                for q in drive.packets {
                    t.inject(client, q);
                    engine.media_step((round + 1) * 10 + 1);
                }
            }
        }
        if engine.stats.dtls_failed > 0 {
            break;
        }
    }
    assert_eq!(engine.stats.dtls_failed, 1, "pin violation failed loudly");
    assert_eq!(engine.stats.dtls_established, 0);
    let slot = engine.slots.get(&sid).unwrap();
    assert!(slot.dtls.is_none());
    assert!(slot.inbound.is_none() && slot.outbound.is_none());
}

#[test]
fn bind_srtp_split_decrypts_real_media_both_directions() {
    // The CM chain the handshake feeds (proved at dtls-crate level):
    // bind directional keys; a browser-side protector keyed with ITS
    // outbound (== engine INBOUND) decrypts at the engine and forwards
    // raw to the subscriber; the engine's outbound protect (== subscriber
    // INBOUND... engine writes ⇒ engine slot OUTBOUND) unwraps at the
    // consumer with the exact same pair.
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9750));
    let alice = engine_addr(9751);
    let bob = engine_addr(9752);
    let (sid_a, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", alice, *b"transact000!");
    let (sid_b, _, _) = join_and_prepare(&mut engine, &t, "bob", "r1", bob, *b"transact999!");

    let _ = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_a}","track":"mic","kind":"audio"}}"#
    ));
    let _ = engine.on_signaling_frame(&format!(
        r#"{{"type":"subscribe","session":"{sid_b}","participant":"alice","track":"mic"}}"#
    ));
    let ssrc = *engine
        .by_ssrc
        .iter()
        .find_map(|(ssrc, (owner, track))| (owner.0 == sid_a && track.0 == "mic").then_some(ssrc))
        .expect("ssrc minted");

    // Distinct per-direction pairs (RFC 5764 shape; synthetic here —
    // the REAL ones arrive via handshake, unit-proven at dtls level).
    let mut k_in = [0u8; 16];
    let mut s_in = [0u8; 14];
    let mut k_out = [0u8; 16];
    let mut s_out = [0u8; 14];
    for (i, b) in k_in.iter_mut().enumerate() {
        *b = 0x11 + i as u8;
        k_out[i] = 0x51 + i as u8;
    }
    for (i, b) in s_in.iter_mut().enumerate() {
        *b = 0x31 + i as u8;
        s_out[i] = 0x71 + i as u8;
    }
    assert!(engine.bind_srtp_split(
        &MediaSessionId(sid_a.clone()),
        dtls::CmKeys {
            inbound_key: k_in,
            inbound_salt: s_in,
            outbound_key: k_out,
            outbound_salt: s_out,
        }
    ));

    // Browser-alice writes protected media (her OUTBOUND pair == engine
    // alice-slot INBOUND pair).
    let payload = [0x7Eu8; 160];
    let pkt = RtpPacket::build(0x60, 500, 12_000, ssrc, false, &payload);
    let mut alice_out = SrtpProtector::new(derive_session_keys(&k_in, &s_in));
    let wire = alice_out.protect(&pkt);
    t.inject(alice, wire);
    let dispatched = engine.media_step(10);
    assert!(dispatched >= 1, "protected media dispatched");

    // Bob is unbound initially: forwards RAW. The sent queue must hold
    // the decrypted-original payload, addressed at bob.
    let sent = t.drain_sent();
    let to_bob: Vec<_> = sent
        .iter()
        .filter(|(to, bytes)| *to == bob && streams::packet::looks_like_rtp(bytes))
        .collect();
    assert_eq!(to_bob.len(), 1, "exactly one leg forwarded");
    let forwarded = RtpPacket::parse(to_bob[0].1.clone()).expect("parseable");
    assert_eq!(
        forwarded.payload(),
        &payload[..],
        "decrypted payload forwarded intact"
    );

    // OUTBOUND direction: bind bob's slot with its own directional
    // split; the engine must now PROTECT the leg toward bob, and bob's
    // unprotector (keyed identically) must unwrap the exact payload.
    let mut kb_out = [0u8; 16];
    let mut sb_out = [0u8; 14];
    for (i, b) in kb_out.iter_mut().enumerate() {
        *b = 0x91 + i as u8;
    }
    for (i, b) in sb_out.iter_mut().enumerate() {
        *b = 0xB1 + i as u8;
    }
    assert!(engine.bind_srtp_split(
        &MediaSessionId(sid_b.clone()),
        dtls::CmKeys {
            inbound_key: [0x61u8; 16],
            inbound_salt: [0x65u8; 14],
            outbound_key: kb_out,
            outbound_salt: sb_out,
        }
    ));
    let pkt2 = RtpPacket::build(0x60, 501, 12_160, ssrc, false, &payload);
    let mut alice_out2 = SrtpProtector::new(derive_session_keys(&k_in, &s_in));
    t.inject(alice, alice_out2.protect(&pkt2));
    engine.media_step(11);

    let sent2 = t.drain_sent();
    let to_bob2: Vec<_> = sent2
        .iter()
        .filter(|(to, bytes)| *to == bob && streams::packet::looks_like_rtp(bytes))
        .collect();
    assert_eq!(to_bob2.len(), 1, "protected leg forwarded");
    let mut bob_in = SrtpUnprotector::new(derive_session_keys(&kb_out, &sb_out));
    let back = bob_in
        .unprotect(&to_bob2[0].1)
        .expect("bob unwraps engine-protected media");
    assert_eq!(
        RtpPacket::parse(back).unwrap().payload(),
        &payload[..],
        "engine outbound == subscriber inbound pair"
    );
}

#[test]
fn dtls_retransmit_timer_rides_sweep_cadence() {
    // Active-role endpoint flushed at nomination, browser never answers:
    // after the flight RTO the sweep must re-emit a DTLS record.
    let (mut engine, t, _identity) = production_engine(9760);
    let client = engine_addr(9761);
    let ready = engine.on_signaling_frame(&join_frame("alice", "r1"));
    let (sid, ufrag, pwd) = ready_ice(&ready);
    let browser_id = dtls::Identity::generate().expect("browser identity");
    let _ = engine.on_signaling_frame(&offer_frame(
        &sid,
        &browser_offer(browser_id.fingerprint(), "passive"),
    ));
    let sid = MediaSessionId(sid);
    nominate_with_remote(
        &mut engine,
        &t,
        client,
        &ufrag,
        "brow",
        &pwd,
        *b"transact000!",
    );
    let first = dtls_records(&t.drain_sent());
    assert!(!first.is_empty(), "initial ClientHello flushed");
    assert!(engine.slots.get(&sid).unwrap().dtls.is_some());

    std::thread::sleep(Duration::from_millis(1200));
    engine.sweep_step();
    let resent = dtls_records(&t.drain_sent());
    assert!(
        resent.iter().any(|r| r[0] == 22),
        "sweep retransmitted the unanswered flight"
    );
    assert!(
        engine.slots.get(&sid).unwrap().dtls.is_some(),
        "one unanswered flight is a retransmit, not a teardown"
    );
}

#[test]
fn explicit_client_ssrc_attributes_media_and_collisions_refuse() {
    // GENUINE CLIENT SHAPE (RFC 3550): the client chooses its own SSRC,
    // tells the engine on publish, and media attribution binds THAT one.
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9770));
    let alice = engine_addr(9771);
    let bob = engine_addr(9772);
    let (sid_a, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", alice, *b"transact000!");
    let (sid_b, _, _) = join_and_prepare(&mut engine, &t, "bob", "r1", bob, *b"transact999!");

    let client_ssrc: u32 = 0x3E5A1C7D;
    let replies = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_a}","track":"mic","kind":"audio","ssrc":{client_ssrc}}}"#
    ));
    assert!(
        replies.iter().all(|r| !r.contains("error")),
        "publish with fresh explicit ssrc accepted: {replies:?}"
    );
    assert!(
        replies.iter().any(|r| r.contains("track.published")),
        "fanout still delivered"
    );
    let _ = engine.on_signaling_frame(&format!(
        r#"{{"type":"subscribe","session":"{sid_b}","participant":"alice","track":"mic"}}"#
    ));
    assert_eq!(
        engine
            .by_ssrc
            .get(&client_ssrc)
            .map(|(sid, _)| sid.0.clone()),
        Some(sid_a.clone()),
        "routing map binds the CLIENT's ssrc"
    );

    // Media with the client-chosen ssrc routes end to end.
    let payload = [0x42u8; 160];
    let pkt = RtpPacket::build(0x60, 900, 3_200, client_ssrc, false, &payload);
    t.inject(alice, pkt.raw);
    engine.media_step(10);
    let sent = t.drain_sent();
    let to_bob: Vec<_> = sent
        .iter()
        .filter(|(to, bytes)| *to == bob && streams::packet::looks_like_rtp(bytes))
        .collect();
    assert_eq!(
        to_bob.len(),
        1,
        "explicit-ssrc media forwarded to subscriber"
    );
    assert_eq!(
        RtpPacket::parse(to_bob[0].1.clone()).unwrap().payload(),
        &payload[..]
    );

    // Second track CLAIMING THE SAME ssrc — refused loudly, no rebinding.
    let replies = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_b}","track":"cam","kind":"video","ssrc":{client_ssrc}}}"#
    ));
    assert!(
        replies
            .iter()
            .any(|r| r.contains("\"error\"") && r.contains("wrong_state")),
        "collision refused: {replies:?}"
    );
    assert!(
        !replies.iter().any(|r| r.contains("track.published")),
        "no fanout for a refused publish"
    );
    assert_eq!(engine.stats.publish_refused, 1);
    assert!(
        !engine.by_ssrc.values().any(|(_, tk)| tk.0 == "cam"),
        "rejected track never binds"
    );

    // ssrc 0 is a protocol lie — refused too.
    let replies = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_b}","track":"screen","kind":"video","ssrc":0}}"#
    ));
    assert!(replies.iter().any(|r| r.contains("\"error\"")));
    assert_eq!(engine.stats.publish_refused, 2);

    // Fixture-shaped publish (no ssrc key) keeps the minted path.
    let replies = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_b}","track":"deck","kind":"audio"}}"#
    ));
    assert!(
        replies.iter().all(|r| !r.contains("\"error\"")),
        "minted publish still works: {replies:?}"
    );
}

#[test]
fn bind_gcm256_split_decrypts_real_media_both_directions() {
    // GCM twin of the CM bind test: master material as the handshake
    // would hand it, directional GCM protectors keyed from BOTH sides'
    // knowledge — engine decrypts the browser's wire and writes its own
    // outbound leg the consumer's GCM context can unwrap, byte-exact.
    use webrtc::srtp::{derive_gcm_session_keys_256, SrtpGcmProtector, SrtpGcmUnprotector};
    let (mut engine, t) = Engine::with_mem_transport(engine_addr(9760));
    let alice = engine_addr(9761);
    let bob = engine_addr(9762);
    let (sid_a, _, _) = join_and_prepare(&mut engine, &t, "alice", "r1", alice, *b"transact000!");
    let (sid_b, _, _) = join_and_prepare(&mut engine, &t, "bob", "r1", bob, *b"transact999!");
    let _ = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid_a}","track":"mic","kind":"audio"}}"#
    ));
    let _ = engine.on_signaling_frame(&format!(
        r#"{{"type":"subscribe","session":"{sid_b}","participant":"alice","track":"mic"}}"#
    ));
    let ssrc = *engine
        .by_ssrc
        .iter()
        .find_map(|(ssrc, (owner, track))| (owner.0 == sid_a && track.0 == "mic").then_some(ssrc))
        .expect("ssrc minted");

    let mut k_in = [0u8; 32];
    let mut k_out = [0u8; 32];
    let mut s_in = [0u8; 12];
    let mut s_out = [0u8; 12];
    for (i, b) in k_in.iter_mut().enumerate() {
        *b = 0x10 + i as u8;
        k_out[i] = 0x50 + i as u8;
    }
    for (i, b) in s_in.iter_mut().enumerate() {
        *b = 0x30 + i as u8;
        s_out[i] = 0x70 + i as u8;
    }
    assert!(engine.bind_session_keys(
        &MediaSessionId(sid_a.clone()),
        dtls::SessionKeys::Gcm256(dtls::GcmKeys {
            inbound_key: k_in.to_vec(),
            inbound_salt: s_in,
            outbound_key: k_out.to_vec(),
            outbound_salt: s_out,
        })
    ));

    // Alice's browser sends GCM-protected media (her OUTBOUND master ==
    // engine's alice-slot INBOUND master).
    let payload = [0x5Au8; 160];
    let pkt = RtpPacket::build(0x60, 700, 22_000, ssrc, false, &payload);
    let mut alice_out = SrtpGcmProtector::new(derive_gcm_session_keys_256(&k_in, &s_in));
    let wire = alice_out.protect(&pkt);
    t.inject(alice, wire);
    assert!(engine.media_step(10) >= 1, "GCM media dispatched");

    let sent = t.drain_sent();
    let to_bob: Vec<_> = sent
        .iter()
        .filter(|(to, bytes)| *to == bob && streams::packet::looks_like_rtp(bytes))
        .collect();
    assert_eq!(to_bob.len(), 1, "one GCM leg forwarded");
    // Bob's slot is unbound: clear forwarding of the DECRYPTED payload.
    let forwarded = RtpPacket::parse(to_bob[0].1.clone()).expect("parseable");
    assert_eq!(forwarded.payload(), &payload[..]);

    // OUTBOUND: bind bob GCM; engine protects toward him; HIS
    // unprotector unwraps the engine's write, byte-exact, 16-byte tag
    // growth accounted.
    let mut kb_out = [0u8; 32];
    let mut sb_out = [0u8; 12];
    for (i, b) in kb_out.iter_mut().enumerate() {
        *b = 0x90 + i as u8;
    }
    for (i, b) in sb_out.iter_mut().enumerate() {
        *b = 0xB0 + i as u8;
    }
    assert!(engine.bind_session_keys(
        &MediaSessionId(sid_b.clone()),
        dtls::SessionKeys::Gcm256(dtls::GcmKeys {
            inbound_key: kb_out.to_vec(),
            inbound_salt: sb_out,
            outbound_key: kb_out.to_vec(),
            outbound_salt: sb_out,
        })
    ));
    let pkt2 = RtpPacket::build(0x60, 701, 22_160, ssrc, false, &payload);
    let mut alice_out2 = SrtpGcmProtector::new(derive_gcm_session_keys_256(&k_in, &s_in));
    // Fresh seq 701: same alice stream continuing (its own tracker state).
    let wire2 = alice_out2.protect(&pkt2);
    t.inject(alice, wire2);
    assert!(engine.media_step(20) >= 1);
    let sent = t.drain_sent();
    let to_bob: Vec<_> = sent
        .iter()
        .filter(|(to, bytes)| *to == bob && streams::packet::looks_like_rtp(bytes))
        .collect();
    assert_eq!(to_bob.len(), 1, "protected leg toward bob");
    let mut bob_in = SrtpGcmUnprotector::new(derive_gcm_session_keys_256(&kb_out, &sb_out));
    let clear = bob_in
        .unprotect(&to_bob[0].1)
        .expect("bob unwraps engine's GCM write");
    let parsed = RtpPacket::parse(clear).expect("clear parses");
    assert_eq!(
        parsed.payload(),
        &payload[..],
        "engine GCM outbound is bytes-exact"
    );
}
