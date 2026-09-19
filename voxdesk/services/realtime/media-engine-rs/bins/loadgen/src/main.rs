//! loadgen — media-engine throughput + integrity harness.
//!
//! Modes:
//! * `--bench-kpps N` — one-process benchmark: the full engine loop on an
//!   in-memory transport; N thousand inbound RTP packets with every 64th
//!   injected TWICE (exercising the replay gate in-band, not just timing
//!   the hot path), printing pps achieved + drop counters.
//! * `--target A.B.C.D:PORT --pps R --seconds S --size BYTES` — real UDP
//!   sender of well-formed RTP at a fixed send rate.

use std::net::SocketAddr;
use std::time::{Duration, Instant};

fn flag_value(args: &[String], name: &str) -> Option<String> {
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == name {
            return it.next().cloned();
        }
    }
    None
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!("loadgen --bench-kpps N | --target A.B.C.D:PORT --pps R --seconds S [--size BY]");
        std::process::exit(2);
    }
    match args[0].as_str() {
        "--bench-kpps" => bench(args.get(1).map(|s| s.parse().unwrap_or(100)).unwrap_or(100)),
        "--target" => send_external(&args),
        _ => {
            eprintln!("unknown mode");
            std::process::exit(2);
        }
    }
}

/// The one-process throughput measurement: talks to the engine through
/// its MEMORY transport, so the number measures the code, not the NIC.
fn bench(kpps: usize) {
    let (mut engine, t) =
        engine::Engine::with_mem_transport(SocketAddr::from(([10, 0, 0, 1], 9000)));
    let client = SocketAddr::from(([10, 0, 0, 9], 5001));

    // Enroll + nominate one participant so the RTP lookup resolves.
    let join = r#"{"type":"join","room":"bench","participant":"gen"}"#;
    let ready = engine.on_signaling_frame(join);
    let v = protocol::json::parse(&ready[0]).unwrap();
    let sid = v
        .get("session")
        .and_then(|x| x.as_str())
        .unwrap()
        .to_string();
    let ufrag = v
        .get("ice_ufrag")
        .and_then(|x| x.as_str())
        .unwrap()
        .to_string();
    let pwd = v
        .get("ice_pwd")
        .and_then(|x| x.as_str())
        .unwrap()
        .to_string();
    let bind = webrtc::stun::StunBuilder::new(1, [9u8; 12])
        .username(&format!("{ufrag}:"))
        .use_candidate()
        .build_with_integrity(&pwd);
    t.inject(client, bind);
    engine.media_step(1);

    use protocol::{ParticipantId, RoomId, TrackId};
    // Register through the REAL publish path (session-attributed frame) —
    // the SSRC is minted by the engine on accept.
    let fx = engine.on_signaling_frame(&format!(
        r#"{{"type":"publish","session":"{sid}","track":"mic","kind":"audio"}}"#
    ));
    assert!(
        fx.iter().any(|f| f.contains("track.published")),
        "publish accepted: {fx:?}"
    );
    let ssrc: u32 = engine
        .by_ssrc
        .iter()
        .find_map(|(ssrc, (owner, track))| {
            (owner.0 == sid && track == &TrackId("mic".into())).then_some(*ssrc)
        })
        .expect("ssrc minted on publish");

    // Measure: kpps×1000 valid packets, every 64th also sent TWICE.
    let n = kpps * 1000usize;
    let payload = [0xE5u8; 160];
    let mut seq: u16 = 10_000;
    let mut packet = streams::packet::RtpPacket::build(111, seq, 0, ssrc, true, &payload);
    let start = Instant::now();
    let mut now_ms: u64 = 1;
    for i in 0..n {
        t.inject(client, packet.raw.clone());
        if i % 64 == 63 {
            t.inject(client, packet.raw.clone()); // replay copy
        }
        seq = seq.wrapping_add(1);
        packet = streams::packet::RtpPacket::build(
            111,
            seq,
            seq as u32 * 960,
            ssrc,
            seq.is_multiple_of(1600),
            &payload,
        );
        now_ms = now_ms.wrapping_add(20); // ~audio cadence
        engine.media_step(now_ms as u32);
    }
    let elapsed = start.elapsed();
    let sent = t.drain_sent();

    let stamp = media::RouteStamp {
        room: RoomId("bench".into()),
        participant: ParticipantId("gen".into()),
        track: TrackId("mic".into()),
    };
    let received = engine.registry.get(&stamp).map(|s| s.received).unwrap_or(0);
    let dropped_dup = engine
        .registry
        .get(&stamp)
        .map(|s| s.loss.duplicated())
        .unwrap_or(0);
    let stats = &engine.stats;
    println!("loadgen bench:");
    println!("  injected       = {n} (+{} duplicate copies)", n / 64);
    println!("  elapsed        = {:.3}s", elapsed.as_secs_f64());
    println!(
        "  achieved       = {:.0} pps",
        n as f64 / elapsed.as_secs_f64()
    );
    println!("  received       = {received}");
    println!("  duplicates     = {dropped_dup}");
    println!(
        "  forwarded      = {} (no subscriber legs — this is a pipeline number)",
        stats.rtp_forwarded
    );
    println!("  unknown_frames = {}", stats.unknown_frames);
    println!("  drain_sent     = {}", sent.len());
    // Sanity: the RECEIVED counter races include every injected datagram
    // (replay copies are RECEIVED too — they die at the duplicate gate
    // just after), and every copy must show up as a duplicate.
    assert_eq!(received as usize, n + n / 64, "all packets accounted for");
    assert_eq!(dropped_dup as usize, n / 64, "every replay detected");
}

fn send_external(args: &[String]) {
    let target: SocketAddr = args[0].parse().expect("target A.B.C.D:PORT");
    let pps: usize = flag_value(args, "--pps")
        .map(|s| s.parse().unwrap())
        .unwrap_or(1000);
    let seconds: usize = flag_value(args, "--seconds")
        .map(|s| s.parse().unwrap())
        .unwrap_or(5);
    let size: usize = flag_value(args, "--size")
        .map(|s| s.parse().unwrap())
        .unwrap_or(160);

    let socket = std::net::UdpSocket::bind("0.0.0.0:0").expect("bind");
    let local = socket.local_addr().expect("addr");
    println!("sending {local} → {target} at {pps}pps × {seconds}s (size {size}b payload)");

    let payload = vec![0xABu8; size];
    let interval = Duration::from_micros((1_000_000usize / pps.max(1)) as u64);
    let mut sent = 0usize;
    let mut bytes = 0usize;
    let mut seq: u16 = 2000;
    let start = Instant::now();
    let end = start + Duration::from_secs(seconds as u64);
    while Instant::now() < end {
        let pkt = streams::packet::RtpPacket::build(
            111,
            seq,
            seq as u32 * 960,
            0xBA5E_CAFE,
            false,
            &payload,
        );
        if socket.send_to(&pkt.raw, target).is_ok() {
            sent += 1;
            bytes += pkt.raw.len();
        }
        seq = seq.wrapping_add(1);
        std::thread::sleep(interval);
    }
    let elapsed = start.elapsed();
    println!(
        "sent {sent} packets ({bytes} bytes) in {:.2}s = {:.0}pps",
        elapsed.as_secs_f64(),
        sent as f64 / elapsed.as_secs_f64()
    );
}
