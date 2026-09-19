use webrtc::crypto::{
    aes128::Aes128,
    hmac::{hmac_sha1, hmac_sha256},
    sha1::sha1,
    sha256::sha256,
};
use webrtc::ice::{candidate_priority, parse_ipv4, AgentState, LiteAgent, RemoteCandidate};
use webrtc::sdp::{build_answer, parse_offer, AnswerContext, MediaDirection};
use webrtc::srtp::{derive_session_keys, SrtpProtector, SrtpUnprotector};
use webrtc::stun::{crc32, looks_like_stun, types as stun_types, StunBuilder, StunMessage};

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

// ---------------------------------------------------------- crypto

#[test]
fn fips_vectors_lock_the_primitives() {
    // SHA-1 (FIPS 180-1): "abc".
    assert_eq!(
        hex(&sha1(b"abc")),
        "a9993e364706816aba3e25717850c26c9cd0d89d"
    );
    // SHA-256 (FIPS 180-4): "abc".
    assert_eq!(
        hex(&sha256(b"abc")),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    // HMAC-SHA1 (RFC 2202 #2): key "Jefe".
    assert_eq!(
        hex(&hmac_sha1(b"Jefe", b"what do ya want for nothing?")),
        "effcdf6ae5eb2fa2d27416d5f184df9c259a7c79"
    );
    // HMAC-SHA256 (RFC 4231 #2): key "Jefe".
    assert_eq!(
        hex(&hmac_sha256(b"Jefe", b"what do ya want for nothing?")),
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    );
    // AES-128 (FIPS 197 §C.1).
    let key = [
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e,
        0x0f,
    ];
    let pt = [
        0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee,
        0xff,
    ];
    let aes = Aes128::new(&key);
    assert_eq!(
        hex(&aes.encrypt_block(&pt)),
        "69c4e0d86a7b0430d8cdb78070b4c55a"
    );
    // A second sanity anchor: all-zero key+block.
    let zero = [0u8; 16];
    assert_eq!(
        hex(&Aes128::new(&zero).encrypt_block(&zero)),
        "66e94bd4ef8a2c3b884cfa59ca342b2e"
    );
}

#[test]
fn ctr_keystream_round_trips_and_streams() {
    let key = [0x2Bu8; 16];
    let aes = Aes128::new(&key);
    let iv = [0x11u8; 16];
    let src: Vec<u8> = (0..100u8).collect();

    let mut enc = src.clone();
    aes.apply_keystream(&iv, &mut enc, 0);
    assert_ne!(enc, src);
    let mut dec = enc.clone();
    aes.apply_keystream(&iv, &mut dec, 0);
    assert_eq!(dec, src, "CTR is symmetric");
    // Offset-into-stream semantics: decrypting from byte 40 continues the same stream.
    let mut offset_dec = enc[40..].to_vec();
    aes.apply_keystream(&iv, &mut offset_dec, 40);
    assert_eq!(offset_dec, src[40..]);
}

// ---------------------------------------------------------- stun

#[test]
fn crc32_matches_the_ieee_reference() {
    // "123456789" CRC-32 — the canonical implementation sanity vector.
    assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
}

#[test]
fn stun_bind_round_trips_with_integrity_and_fingerprint() {
    let req = StunBuilder::new(stun_types::BINDING_REQUEST, *b"transaction!")
        .username("srv:cli")
        .use_candidate()
        .build_with_integrity("pwd");

    assert!(looks_like_stun(&req));
    let msg = StunMessage::parse(&req).unwrap();
    assert_eq!(msg.msg_type, stun_types::BINDING_REQUEST);
    assert_eq!(msg.attr(webrtc::stun::attrs::USERNAME).unwrap(), b"srv:cli");
    assert!(msg.use_candidate());
    assert!(msg.has_integrity);
    assert!(
        msg.fingerprint_ok,
        "fingerprint must verify on our own build"
    );
    assert!(msg.verify_integrity("pwd", &req), "integrity must verify");
    assert!(
        !msg.verify_integrity("wrong", &req),
        "wrong key must NOT verify"
    );
}

#[test]
fn stun_rejects_mangled_and_non_stun_bytes() {
    let mut req = StunBuilder::new(stun_types::BINDING_REQUEST, [0u8; 12])
        .username("u")
        .build_with_integrity("p");
    // Truncate mid-attribute → parse must fail rather than truncating attrs.
    let cut = req.len() - 3;
    assert!(StunMessage::parse(&req[..cut]).is_err());
    // Flip a byte INSIDE the integrity-covered body (the username value
    // sits at offset 24..25 in this build) → parse still frames, but the
    // HMAC no longer matches.
    req[24] ^= 0xFF;
    let msg = StunMessage::parse(&req).unwrap();
    assert!(!msg.verify_integrity("p", &req));
    // RTP (0x80 first byte) is not STUN.
    assert!(!looks_like_stun(&[
        0x80, 0x60, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1
    ]));
}

// ---------------------------------------------------------- ice

#[test]
fn candidate_parse_validates_shape_and_priorities() {
    let c = RemoteCandidate::parse("1 1 udp 2122194687 192.168.1.5 53433 typ host").unwrap();
    assert_eq!(
        (c.foundation, c.component, c.protocol.as_str(), c.priority),
        (1, 1, "udp", 2122194687)
    );
    assert_eq!(c.endpoint(), (53433, [192, 168, 1, 5]));
    assert!(matches!(
        RemoteCandidate::parse("1 1 tcp 1 10.0.0.1 9 typ host"),
        Err(webrtc::ice::IceError::NotUdp)
    ));
    assert!(RemoteCandidate::parse("nonsense").is_err());
    assert!(RemoteCandidate::parse("1 1 udp 9999999999 10.0.0.1 9 typ host").is_err());
    assert_eq!(candidate_priority(126, 32286, 1), 2122194687);
    assert_eq!(parse_ipv4("203.0.113.9"), Some([203, 0, 113, 9]));
    assert_eq!(
        parse_ipv4("01.2.3.4"),
        None,
        "leading-zero ambiguity refused"
    );
    assert_eq!(parse_ipv4("1.2.3.4.5"), None);
    assert_eq!(parse_ipv4("1.2.3"), None);
}

#[test]
fn lite_agent_lifecycle_offer_to_nomination() {
    let mut agent = LiteAgent::new("srvU".to_string(), "srvPwd".to_string(), [0xAB; 32]);
    agent.adopt_remote(
        "cliU",
        "cliPwd",
        vec![RemoteCandidate::parse("1 1 udp 2130706431 10.0.0.7 40000 typ host").unwrap()],
    );
    assert_eq!(agent.state(), AgentState::Gathering);

    // Browser sends a bind request USE-CANDIDATE, keyed with OUR password.
    let req = StunBuilder::new(stun_types::BINDING_REQUEST, *b"abcdefghijkl")
        .username("srvU:cliU")
        .use_candidate()
        .build_with_integrity("srvPwd");

    let msg = StunMessage::parse(&req).unwrap();
    let outcome = agent.handle_stun(&msg, &req, (40000, [10, 0, 0, 7]));
    assert!(outcome.response.is_some(), "bind response exists");
    assert_eq!(
        outcome.nominated,
        Some((40000, [10, 0, 0, 7])),
        "USE-CANDIDATE nominates the pair"
    );
    assert_eq!(agent.state(), AgentState::Selected);
    assert_eq!(agent.selected_endpoint(), Some((40000, [10, 0, 0, 7])));

    // Response must parse & verify with OUR password.
    let resp = outcome.response.unwrap();
    let resp_msg = StunMessage::parse(&resp).unwrap();
    assert_eq!(resp_msg.msg_type, stun_types::BINDING_SUCCESS);
    assert!(resp_msg.verify_integrity("srvPwd", &resp));
    let xor = resp_msg
        .attr(webrtc::stun::attrs::XOR_MAPPED_ADDRESS)
        .unwrap();
    assert_eq!(
        webrtc::stun::xor_mapped_ipv4(xor, &resp_msg.transaction_id),
        Some((40000, [10, 0, 0, 7]))
    );
}

#[test]
fn lite_agent_refuses_ufrag_mismatches_and_integrity_failures() {
    let mut agent = LiteAgent::new("srvU".to_string(), "srvPwd".to_string(), [0xAB; 32]);
    agent.adopt_remote("cliU", "cliPwd", vec![]);

    let bad_user = StunBuilder::new(stun_types::BINDING_REQUEST, [7u8; 12])
        .username("attacker:cliU")
        .build_with_integrity("srvPwd");
    let m1 = StunMessage::parse(&bad_user).unwrap();
    let o1 = agent.handle_stun(&m1, &bad_user, (1, [1, 1, 1, 1]));
    let r1 = StunMessage::parse(&o1.response.unwrap()).unwrap();
    assert_eq!(r1.msg_type, stun_types::BINDING_ERROR);

    let wrong_key = StunBuilder::new(stun_types::BINDING_REQUEST, [8u8; 12])
        .username("srvU:cliU")
        .build_with_integrity("not-the-pwd");
    let m2 = StunMessage::parse(&wrong_key).unwrap();
    let o2 = agent.handle_stun(&m2, &wrong_key, (2, [2, 2, 2, 2]));
    let r2 = StunMessage::parse(&o2.response.unwrap()).unwrap();
    assert_eq!(r2.msg_type, stun_types::BINDING_ERROR);
    assert_eq!(
        agent.state(),
        AgentState::Gathering,
        "failed auth must not nominate"
    );
}

// ---------------------------------------------------------- sdp

#[test]
fn offer_parses_and_answer_round_trips() {
    let offer_text = "v=0\r\no=- 46107 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\na=ice-ufrag:4ZcD\r\na=ice-pwd:asvd88fswlvdYIK7Haelu3fV\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111 0\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\na=rtcp-mux\r\na=sendrecv\r\na=rtpmap:111 opus/48000/2\r\na=rtpmap:0 PCMU/8000\r\na=candidate:1 1 udp 2130706431 10.0.0.5 50000 typ host\r\na=fingerprint:sha-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89\r\n";
    let offer = parse_offer(offer_text).unwrap();
    assert_eq!(offer.media.len(), 1);
    let m = &offer.media[0];
    assert_eq!(m.kind, "audio");
    assert!(m.rtcp_mux);
    assert_eq!(m.mid, "0");
    assert_eq!(m.direction, MediaDirection::SendRecv);
    assert_eq!(offer.session_ufrag.as_deref(), Some("4ZcD"));
    assert_eq!(m.candidates.len(), 1);
    assert_eq!(m.candidates[0].endpoint(), (50000, [10, 0, 0, 5]));
    assert_eq!(&m.rtpmap[0], &(111u8, "opus/48000/2".to_string()));

    let ctx = AnswerContext {
        local_ufrag: "srv".into(),
        local_pwd: "serverPasswordLongEnough321".into(),
        fingerprint_sha256: "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00".into(),
        public_ip: [203, 0, 113, 10],
        public_port: 5000,
        external_ip_label: "edge-1".into(),
    };
    let answer = build_answer(&offer, &ctx);
    assert!(answer.contains("m=audio 9 UDP/TLS/RTP/SAVPF 111 0"));
    assert!(answer.contains("a=ice-ufrag:srv"));
    assert!(answer.contains("a=ice-lite"));
    assert!(answer.contains("a=mid:0"));
    assert!(answer.contains("a=sendrecv\r\n"));
    assert!(answer.contains("a=candidate:1 1 udp 2128609535 203.0.113.10 5000 typ host"));
    assert!(answer.contains("a=end-of-candidates"));
    // The answer is itself parseable SDP.
    let re = parse_offer(&answer).unwrap();
    assert_eq!(re.media[0].candidates.len(), 1);
    assert!(re.media[0].rtcp_mux);
}

#[test]
fn malformed_offers_fail_fast() {
    assert!(matches!(parse_offer(""), Err(webrtc::sdp::SdpError::Empty)));
    assert!(matches!(
        parse_offer("v=0\r\nt=0 0\r\n"),
        Err(webrtc::sdp::SdpError::NoMedia)
    ));
    assert!(matches!(
        parse_offer("v=0\nbrokenline\nm=audio 9 UDP/TLS/RTP/SAVPF 111\n"),
        Err(webrtc::sdp::SdpError::LineSyntax(_))
    ));
}

// ---------------------------------------------------------- srtp

#[test]
fn rfc3711_b3_key_derivation_vectors() {
    // RFC 3711 §B.3: master key + salt → session keys.
    let master_key: [u8; 16] = [
        0xE1, 0xF9, 0x7A, 0x0D, 0x3E, 0x01, 0x8B, 0xE0, 0xD6, 0x4F, 0xA3, 0x2C, 0x06, 0xDE, 0x41,
        0x39,
    ];
    let master_salt: [u8; 14] = [
        0x0E, 0xC6, 0x75, 0xAD, 0x49, 0x8A, 0xFE, 0xEB, 0xB6, 0x96, 0x0B, 0x3A, 0xAB, 0xE6,
    ];
    let keys = derive_session_keys(&master_key, &master_salt);
    // RFC 3711 §B.3 exact strings (verified against the RFC text itself —
    // "6161..." library excerpts online pre-date a corrected pedition).
    assert_eq!(hex(&keys.aes), "c61e7a93744f39ee10734afe3ff7a087");
    assert_eq!(hex(&keys.auth), "cebe321f6ff7716b6fd4ab49af256a156d38baa4");
    assert_eq!(hex(&keys.salt), "30cbbc08863d8c85d49db34a9ae1");
}

#[test]
fn srtp_round_trip_across_a_wrap_with_replay_visible() {
    let master_key = [0xAA; 16];
    let master_salt = [0x55; 14];
    let mut tx = SrtpProtector::new(derive_session_keys(&master_key, &master_salt));
    let mut rx = SrtpUnprotector::new(derive_session_keys(&master_key, &master_salt));

    let ssrc = 0xDEADBEEFu32;
    // Start near the wrap to exercise ROC estimation in the same run.
    for seq in (0xFFFE..=0xFFFFu32).chain(0..=5).map(|s| s as u16) {
        let packet = streams::packet::RtpPacket::build(
            111,
            seq,
            160 * u32::from(seq),
            ssrc,
            false,
            b"cona-payload",
        );
        let wire = tx.protect(&packet);
        // Tamper check: flipping a payload bit MUST fail auth.
        let mut tampered = wire.clone();
        tampered[12] ^= 0x01;
        assert_eq!(
            rx.unprotect(&tampered),
            Err(webrtc::srtp::ProtectError::Tag)
        );

        let clear = rx.unprotect(&wire.clone()).unwrap();
        assert_eq!(clear.len(), wire.len() - 10, "auth tag removed");
        assert_eq!(&clear[12..], b"cona-payload");
        // Replay: same datagram again must be a replay verdict.
        assert_eq!(rx.unprotect(&wire), Err(webrtc::srtp::ProtectError::Replay));
    }
}

#[test]
fn srtp_out_of_order_within_window_still_unprotects() {
    let keys = derive_session_keys(&[1; 16], &[2; 14]);
    let mut tx = SrtpProtector::new(keys.clone());
    let mut rx = SrtpUnprotector::new(keys);
    let mk = |seq: u16| streams::packet::RtpPacket::build(111, seq, 0, 7, false, &[seq as u8]);
    let a = tx.protect(&mk(10));
    let b = tx.protect(&mk(11));
    let c = tx.protect(&mk(12));
    // Deliver out of order: 11, 10, 12 — all distinct, none replay.
    assert_eq!(rx.unprotect(&b).unwrap()[12], 11u8);
    assert_eq!(rx.unprotect(&a).unwrap()[12], 10u8);
    assert_eq!(rx.unprotect(&c).unwrap()[12], 12u8);
    // Duplicates of the earlier ones are replays now.
    assert!(rx.unprotect(&a).is_err());
}

#[test]
fn answer_setup_mirrors_offer_rfc5763() {
    use webrtc::sdp::{build_answer, parse_offer, AnswerContext};

    let mk_offer = |setup_line: &str| {
        format!(
            "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\n\
             m=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\n\
             {setup_line}\r\n"
        )
    };
    let ctx = AnswerContext {
        local_ufrag: "srv".into(),
        local_pwd: "serverPasswordLongEnough321".into(),
        fingerprint_sha256: "11:22".repeat(16),
        public_ip: [203, 0, 113, 10],
        public_port: 5000,
        external_ip_label: "edge".into(),
    };

    // actpass offerer ⇒ we take the passive role (setup:active from them).
    let offer = parse_offer(&mk_offer("a=setup:actpass")).unwrap();
    assert!(build_answer(&offer, &ctx).contains("a=setup:passive\r\n"));
    // active offerer ⇒ same: we respond to their initiation.
    let offer = parse_offer(&mk_offer("a=setup:active")).unwrap();
    assert!(build_answer(&offer, &ctx).contains("a=setup:passive\r\n"));
    // passive offerer ⇒ THEY cannot initiate; we MUST answer active or
    // DTLS never starts.
    let offer = parse_offer(&mk_offer("a=setup:passive")).unwrap();
    let answer = build_answer(&offer, &ctx);
    assert!(answer.contains("a=setup:active\r\n"));
    assert!(!answer.contains("a=setup:passive\r\n"));
}

#[test]
fn pion_signed_binding_request_verifies_our_integrity() {
    // Genuine pion/stun v3 output signed with the same password we verify
    // with — wire-compat acceptance probe for the it-pion lane.
    // REAL pion/ice v4 agent request, captured on the wire during the
    // it-pion lane (ufrag is the engine's; password is the engine's).
    let hex = concat!(
        "000100502112a442",
        "dcbe55151fb7f51d508cba81",
        "0006",
        "0017",
        "302f614558623a48436e484a51734a68626c4c5852634a00",
        "802a",
        "0008",
        "e6979e1c28185950",
        "0024",
        "0004",
        "7effffff",
        "0008",
        "0014",
        "31ebda7d0efc2c8f0bf0fdd306dbd10ad2b51c0a",
        "8028",
        "0004",
        "e6186179"
    );
    let bytes: Vec<u8> = (0..hex.len() / 2)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
        .collect();
    let pwd = "0/aEXbtI+zXkLHbt5jdYLfh3";
    let msg = webrtc::stun::StunMessage::parse(&bytes).expect("pion request parses");
    assert!(
        msg.verify_integrity(pwd, &bytes),
        "our integrity verify must accept the pion agent's RFC 8489 signature"
    );
}

// -------------------------------------------------- RFC 7714 (GCM)

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

#[test]
fn rfc7714_gcm128_encryption_vector() {
    // RFC 7714 §16.1.1: session key 000102...0f, session salt
    // "Quid pro quo" (12 bytes), ROC = 0, SSRC = 0x5501a0b2, SEQ = 0xf17b.
    use webrtc::srtp::{gcm_session_from_split, GcmProfile, SrtpGcmProtector};
    let key = unhex("000102030405060708090a0b0c0d0e0f");
    let salt = unhex("517569642070726f2071756f");
    let mut salt12 = [0u8; 12];
    salt12.copy_from_slice(&salt);
    let keys = gcm_session_from_split(GcmProfile::Aes128Gcm, &key, &salt12);

    let payload = unhex(concat!(
        "47616c6c696120657374206f6d6e697320646976",
        "69736120696e207061727465732074726573"
    ));
    let packet =
        streams::packet::RtpPacket::build(64, 0xf17b, 0x8041f8d3, 0x5501a0b2, false, &payload);
    let mut tx = SrtpGcmProtector::new(keys);
    let wire = tx.protect(&packet);

    // The RFC's exact "Encrypted and tagged packet" hex.
    let expect = unhex(concat!(
        "8040f17b8041f8d35501a0b2f24de3a3",
        "fb34de6cacba861c9d7e4bcabe633bd5",
        "0d294e6f42a5f47a51c7d19b36de3adf",
        "8833899d7f27beb16a9152cf765ee439",
        "0cce"
    ));
    assert_eq!(
        hex(&wire),
        hex(&expect),
        "RFC 7714 §16.1.1 KAT must match byte-for-byte"
    );
}

#[test]
fn rfc7714_gcm128_decryption_vector_and_tamper_rejects() {
    use webrtc::srtp::{gcm_session_from_split, GcmProfile, SrtpGcmUnprotector};
    let key = unhex("000102030405060708090a0b0c0d0e0f");
    let salt = unhex("517569642070726f2071756f");
    let mut salt12 = [0u8; 12];
    salt12.copy_from_slice(&salt);
    let keys = gcm_session_from_split(GcmProfile::Aes128Gcm, &key, &salt12);

    let wire = unhex(concat!(
        "8040f17b8041f8d35501a0b2f24de3a3",
        "fb34de6cacba861c9d7e4bcabe633bd5",
        "0d294e6f42a5f47a51c7d19b36de3adf",
        "8833899d7f27beb16a9152cf765ee439",
        "0cce"
    ));
    let mut rx = SrtpGcmUnprotector::new(keys);
    let clear = rx.unprotect(&wire.clone()).expect("KAT must unprotect");
    let expect = unhex(concat!(
        "8040f17b8041f8d35501a0b247616c6c",
        "696120657374206f6d6e697320646976",
        "69736120696e20706172746573207472",
        "6573"
    ));
    assert_eq!(hex(&clear), hex(&expect));

    // Fresh context: tag tamper is a hard error, cipher tamper too.
    let key = unhex("000102030405060708090a0b0c0d0e0f");
    let keys = gcm_session_from_split(GcmProfile::Aes128Gcm, &key, &salt12);
    let mut rx2 = SrtpGcmUnprotector::new(keys);
    let mut tampered = wire.clone();
    let last = tampered.len() - 1;
    tampered[last] ^= 0x01;
    assert_eq!(
        rx2.unprotect(&tampered),
        Err(webrtc::srtp::ProtectError::Tag)
    );
    let key = unhex("000102030405060708090a0b0c0d0e0f");
    let keys = gcm_session_from_split(GcmProfile::Aes128Gcm, &key, &salt12);
    let mut rx3 = SrtpGcmUnprotector::new(keys);
    let mut tampered = wire.clone();
    tampered[14] ^= 0x01;
    assert_eq!(
        rx3.unprotect(&tampered),
        Err(webrtc::srtp::ProtectError::Tag)
    );
}

#[test]
fn gcm256_round_trip_with_replay_rules() {
    use webrtc::srtp::{gcm_session_from_split, GcmProfile, SrtpGcmProtector, SrtpGcmUnprotector};
    let key = [0xC3u8; 32];
    let salt = [0x1Du8; 12];
    let make = || gcm_session_from_split(GcmProfile::Aes256Gcm, &key, &salt);
    let mut tx = SrtpGcmProtector::new(make());
    let mut rx = SrtpGcmUnprotector::new(make());
    for seq in (0xFFFE..=0xFFFFu32).chain(0..=3).map(|s| s as u16) {
        let packet = streams::packet::RtpPacket::build(111, seq, 0, 0xF00Du32, false, b"gcm-body");
        let wire = tx.protect(&packet);
        assert_eq!(wire.len(), packet.raw.len() + 16, "GCM tag is 16 bytes");
        let clear = rx.unprotect(&wire.clone()).unwrap();
        assert_eq!(&clear[12..], b"gcm-body");
        assert_eq!(rx.unprotect(&wire), Err(webrtc::srtp::ProtectError::Replay));
    }
}
