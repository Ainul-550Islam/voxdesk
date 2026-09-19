use std::net::SocketAddr;
use std::time::Duration;
use transport::{demux, Datagram, MemTransport, Transport, UdpTransport};

fn addr(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

#[test]
fn demux_follows_rfc7983_first_bytes() {
    use transport::demux::FrameKind::*;
    // STUN: first bits 00, cookie at 4..8.
    let stun = {
        let mut v = vec![0x00u8; 20];
        v[2] = 0;
        v[3] = 8;
        v[4..8].copy_from_slice(&[0x21, 0x12, 0xA4, 0x42]);
        v
    };
    assert_eq!(demux::classify(&stun), Stun);
    // RTP: V=2 in first byte top bits, PT 111.
    let rtp = vec![0x80, 111, 0, 1, 0, 0, 0, 9, 0, 0, 0, 1];
    assert_eq!(demux::classify(&rtp), Rtp);
    // RTCP: V=2 but byte1 in 192..=223 (per RFC 5761's interleave range).
    let rtcp = vec![0x80, 200, 0, 1, 0, 0, 0, 0];
    assert_eq!(demux::classify(&rtcp), Rtcp);
    // DTLS: content types 20..=25 carry the RFC 7983 reservation
    // (20..=63) — and must WIN over the STUN arm (22..25 satisfy the
    // loose top-bits mask that arm tests, so order matters).
    for content in [20u8, 21, 22, 23, 24, 25] {
        let dtls = vec![content, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1];
        assert_eq!(
            demux::classify(&dtls),
            Dtls,
            "content type {content} is DTLS"
        );
    }
    // Edge of the reserved range: 19 is unassigned (STUN-ambiguous),
    // 64 is TURN-channel — neither is DTLS.
    let mut nineteen = vec![19u8, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1];
    nineteen.resize(20, 0); // STUN needs the full message floor to claim it
    assert_eq!(demux::classify(&nineteen), Stun);
    assert_eq!(demux::classify(&[0x40; 16]), Unknown, "TURN channel marker");
    // And a DTLS claim must present a full record header (13 bytes).
    assert_eq!(
        demux::classify(&[22u8; 12]),
        Unknown,
        "truncated record header"
    );
    // Garbage: first bits say neither STUN nor V=2.
    assert_eq!(demux::classify(&[0x40; 16]), Unknown);
    // Too-short is Unknown — not a guess category.
    assert_eq!(demux::classify(&[0x80]), Unknown);
}

#[test]
fn mem_transport_batches_and_drops_like_a_ring() {
    let t = MemTransport::new(addr(9600), 2);
    for i in 0..5 {
        t.inject(addr(9601), vec![i]);
    }
    assert_eq!(t.queued(), 2, "capacity saturated: drops, no growth");
    let mut got = Vec::new();
    let n = t.recv_batch(&mut got, 4, Duration::ZERO).unwrap();
    assert_eq!(n, 2, "batch drains what's there");
    // Saturated ring holds the FIRST two — FIFO with tail-drop.
    assert_eq!(got[0].bytes, vec![0u8]);
    assert_eq!(got[1].bytes, vec![1u8]);
}

#[test]
fn mem_transport_sends_are_recorded() {
    let t = MemTransport::new(addr(9600), 8);
    t.send(addr(9700), b"one").unwrap();
    t.send(addr(9700), b"two").unwrap();
    let sent = t.drain_sent();
    assert_eq!(sent.len(), 2);
    assert_eq!(&sent[0].1, b"one");
    assert!(t.drain_sent().is_empty(), "drain consumes");
}

#[test]
fn udp_transport_roundtrip_on_loopback() {
    // Real socket test: bind two sockets, send → batched recv.
    let a = UdpTransport::bind(addr(0)).unwrap();
    let b = UdpTransport::bind(addr(0)).unwrap();
    let a_addr = a.local_addr().unwrap();
    let b_addr = b.local_addr().unwrap();

    b.send(a_addr, b"ping").unwrap();
    a.send(b_addr, b"pong").unwrap();

    let mut buf: Vec<Datagram> = Vec::new();
    // Generous real budget — CI hosts under load can lag a real UDP hop.
    let n = a
        .recv_batch(&mut buf, 4, Duration::from_millis(400))
        .unwrap();
    assert!(n >= 1);
    assert!(&buf[0].bytes == b"ping");
    assert_eq!(buf[0].from.port(), b_addr.port());

    // recv_batch returns quickly with nothing to read at budget expiry.
    let mut empty = Vec::new();
    let n0 = a
        .recv_batch(&mut empty, 4, Duration::from_millis(50))
        .unwrap();
    assert_eq!(n0, 0);
    assert!(empty.is_empty());
}
