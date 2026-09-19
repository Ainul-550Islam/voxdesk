use streams::{
    build_receiver_report, forward_distance, is_newer, parse_rtcp, JitterEstimator, LossStats,
    ReorderBuffer, ReplayWindow, ReportBlock, Rtcp, RtpPacket, SeqTracker, Verdict,
};

#[test]
fn packet_round_trip_and_header_math() {
    let pkt = RtpPacket::build(96, 0xBEEF, 0x11223344, 0xA5A5A5A5, true, b"opus-data");
    assert!(streams::looks_like_rtp(&pkt.raw));
    let parsed = RtpPacket::parse(pkt.raw.clone()).unwrap();
    assert_eq!(parsed, pkt);
    assert_eq!(parsed.payload(), b"opus-data");
    assert_eq!(parsed.sequence, 0xBEEF);
    assert_eq!(parsed.ssrc, 0xA5A5A5A5);
    assert!(parsed.marker);
    assert_eq!(parsed.raw.len(), 12 + 9);
}

#[test]
fn parse_rejects_malformed_datagrams() {
    // Too short
    assert!(RtpPacket::parse(vec![0x80; 5]).is_err());
    // Version 1
    let mut bad = vec![0x40, 96, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0];
    assert!(RtpPacket::parse(bad.clone()).is_err());
    // Extension says 2 words but datagram runs out
    bad[0] = 0x90; // V=2, X=1
    bad.extend_from_slice(&[0; 2]); // profile
    bad.extend_from_slice(&2u16.to_be_bytes());
    assert!(
        RtpPacket::parse(bad).is_err(),
        "truncated extension must fail"
    );
    // CSRC count 4 with no CSRC bytes
    let mut bad2 = vec![0x84, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0];
    assert!(RtpPacket::parse(std::mem::take(&mut bad2)).is_err());
}

#[test]
fn extension_and_padding_are_accounted() {
    // Hand-craft: V=2, P=1, X=1, CC=0 (0xB0), PT=111, ext 1 word, payload "AB", padding 4.
    let mut raw = vec![0xB0, 111, 0, 7, 0, 0, 0, 1, 0, 0, 0, 9];
    raw.extend_from_slice(&[0xBE, 0xDE, 0, 1]); // ext profile + 1 word len
    raw.extend_from_slice(&[9, 9, 9, 9]); // the extension word
    raw.extend_from_slice(b"AB");
    raw.extend_from_slice(&[0, 0, 0, 4]); // padding (last byte = count)
    let pkt = RtpPacket::parse(raw).unwrap();
    assert!(pkt.padding);
    assert_eq!(
        pkt.payload(),
        b"AB",
        "payload must exclude extension and padding"
    );
}

#[test]
fn sequence_distance_wraps_correctly() {
    assert_eq!(
        forward_distance(0xFFF0, 0x0005),
        0x15,
        "wrap-forward is ahead"
    );
    assert_eq!(forward_distance(5, 2), -3, "behind is negative");
    assert!(!is_newer(10, 9));
    assert!(is_newer(0xFFFF, 1), "1 is two ahead of 65535");
    assert_eq!(forward_distance(0, 0x8000).abs(), 32768);
}

#[test]
fn extended_sequence_tracks_the_rollover() {
    let mut t = SeqTracker::new();
    assert_eq!(t.extend(0xFFFE), 0xFFFE);
    assert_eq!(t.extend(0xFFFF), 0xFFFF);
    assert_eq!(t.extend(0x0000), 0x10000, "wrap must bump the roc");
    assert_eq!(t.extend(0x0001), 0x10001);
    // An old packet from the PREVIOUS cycle reports relative roc-1.
    let back = t.extend(t.highest());
    assert!(back >= 0x10000);
}

#[test]
fn replay_window_fresh_then_replay_across_slides() {
    let mut w = ReplayWindow::new();
    assert_eq!(w.check_and_set(7), Verdict::Fresh);
    assert_eq!(w.check_and_set(7), Verdict::Replay, "same index twice");
    assert_eq!(w.check_and_set(10), Verdict::Fresh, "window slides");
    assert_eq!(w.check_and_set(9), Verdict::Fresh);
    assert_eq!(w.check_and_set(9), Verdict::Replay);
    assert_eq!(
        w.check_and_set(7 + 1000),
        Verdict::Fresh,
        "huge jump resets window"
    );
    assert_eq!(
        w.check_and_set(10),
        Verdict::Replay,
        "ancient beyond window"
    );
}

#[test]
fn loss_stats_separates_loss_reorder_and_duplicate() {
    let mut s = LossStats::new();
    for seq in [100, 101, 103, 102, 103] {
        s.record(seq); // 102 arrives late, 103 twice
    }
    assert_eq!(s.received(), 4, "102,103,101,100 minus the duplicate");
    assert_eq!(s.duplicated(), 1);
    assert_eq!(s.cumulative_lost(), 0, "reorder is not loss");
    for seq in [104, 107, 108] {
        s.record(seq);
    }
    assert_eq!(s.cumulative_lost(), 2, "105,106 never arrived");
    assert!(s.fraction_lost8() > 0);
    assert_eq!(s.highest_extended(), 108);
}

#[test]
fn jitter_estimator_converges_like_rfc_a8() {
    let mut j = JitterEstimator::new();
    // Perfectly paced stream: transit deltas of exactly 0 each step at 48 kHz units.
    let mut arrival = 0.0;
    for i in 0..64u32 {
        j.record(i * 960, arrival); // 20 ms opus frames
        arrival += 960.0;
    }
    assert!(j.jitter() < 1e-9, "regular stream has zero jitter");
    // Now inject a constant +480 (10 ms) wander every frame: |D| = 480.
    let mut j2 = JitterEstimator::new();
    let mut a = 0.0;
    for i in 0..64u32 {
        let wobble = if i % 2 == 0 { 480.0 } else { -480.0 };
        j2.record(i * 960, a + wobble);
        a += 960.0;
    }
    assert!(j2.jitter() > 100.0, "wander must register: {}", j2.jitter());
}

#[test]
fn reorder_buffer_resequences_and_bounds_delay() {
    let mut b = ReorderBuffer::new(8);
    let mk = |s: u16| RtpPacket::build(0, s, 0, 1, false, &[s as u8]);
    b.insert(11, mk(11));
    b.insert(13, mk(13));
    b.insert(12, mk(12));
    assert_eq!(b.pop(2).unwrap().sequence, 11);
    assert_eq!(b.pop(2).unwrap().sequence, 12);
    assert_eq!(b.pop(2).unwrap().sequence, 13);
    assert!(b.pop(2).is_none());
    assert_eq!(b.dropped_late(), 0);

    // Gap beyond max_gap: declared lost, stream continues in order.
    b.insert(20, mk(20));
    b.insert(24, mk(24));
    assert_eq!(b.pop(2).unwrap().sequence, 20);
    assert_eq!(
        b.pop(2).unwrap().sequence,
        24,
        "patience exhausted → skip the hole"
    );
}

#[test]
fn reorder_buffer_caps_memory_and_drops_latecomers() {
    let mut b = ReorderBuffer::new(4);
    let mk = |s: u16| RtpPacket::build(0, s, 0, 1, false, &[0]);
    for s in 1..=6 {
        b.insert(s as u32, mk(s));
    }
    assert!(b.len() <= 4, "capacity caps memory: {}", b.len());
    assert!(b.flushed() >= 1, "overflow force-released the oldest");
    // After the flush, next-advanced past released packets: those are now late.
    let next = 1u32; // packet 1 was first inserted and flushed
    let late_drops_before = b.dropped_late();
    b.insert(next, mk(1));
    assert_eq!(
        b.dropped_late(),
        late_drops_before + 1,
        "re-inserting a flushed seq must be dropped-late"
    );
}

#[test]
fn rtcp_receiver_report_round_trip() {
    let block = ReportBlock {
        ssrc: 0xDEADBEEF,
        fraction_lost: 12,
        cumulative_lost: 333,
        highest_seq_ext: 0x0001_FFEE,
        jitter: 555,
        lsr: 0x01020304,
        dlsr: 0x05060708,
    };
    let raw = build_receiver_report(0x01020304, std::slice::from_ref(&block));
    assert!(streams::rtcp::looks_like_rtcp(&raw));
    let reports = parse_rtcp(&raw).unwrap();
    assert_eq!(
        reports,
        vec![Rtcp::ReceiverReport {
            ssrc: 0x01020304,
            reports: vec![block]
        }]
    );
}

#[test]
fn rtcp_compound_walk_skips_unknown_and_reports_sender() {
    let mut raw = Vec::new();
    // SDES count 0, len 1 (header only beyond): V=2,RC=0, PT=202, len=0 → 4 bytes
    raw.extend_from_slice(&[0x80, 202, 0, 0]);
    // SR: V=2, PT=200, len=6 → 7 words = 28 bytes
    let mut sr = vec![0x80, 200, 0, 6];
    sr.extend_from_slice(&0x11111111u32.to_be_bytes());
    sr.extend_from_slice(&0xAAAA0000u32.to_be_bytes());
    sr.extend_from_slice(&0x0000BBBBu32.to_be_bytes());
    sr.extend_from_slice(&777u32.to_be_bytes());
    sr.extend_from_slice(&888u32.to_be_bytes());
    sr.extend_from_slice(&999u32.to_be_bytes());
    raw.extend_from_slice(&sr);
    let reports = parse_rtcp(&raw).unwrap();
    assert_eq!(reports.len(), 2);
    assert!(matches!(
        reports[0],
        Rtcp::Unknown {
            packet_type: 202,
            ..
        }
    ));
    match &reports[1] {
        Rtcp::SenderReport {
            ssrc,
            rtp_timestamp,
            packet_count,
            ..
        } => {
            assert_eq!(*ssrc, 0x11111111);
            assert_eq!(*rtp_timestamp, 777);
            assert_eq!(*packet_count, 888);
        }
        other => panic!("want SR, got {other:?}"),
    }
    // Truncated compound must error, not panic.
    assert!(parse_rtcp(&raw[..10]).is_err());
}
