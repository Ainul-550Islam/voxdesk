//! transport — UDP I/O behind a trait the engine tests drive in-memory.
//!
//! std-only constraint: no recvmmsg. The batch API instead does bounded
//! nonblocking drains (`recv_batch`: read-while-would-block, up to cap)
//! which amortizes the worst syscall thunder-herd: one epoll-style
//! wakeup is traded for a bounded read loop the caller sizes.
//!
//! The trait deliberately exposes `std::net::SocketAddr`-shaped
//! endpoints — `SocketAddr` is std, and IPv6-wrapped reachability stays
//! someone else's problem (SFU candidate is IPv4-only anyway).

use std::io;
use std::net::{SocketAddr, UdpSocket};
use std::time::Duration;
use streams::{packet::looks_like_rtp, rtcp::looks_like_rtcp};

pub mod demux {
    use super::*;

    /// What the engine's dispatcher thinks a datagram is (RFC 7983's
    /// first-byte table, reduced to the protocols we speak).
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum FrameKind {
        Stun,
        Dtls,
        Rtp,
        Rtcp,
        Unknown,
    }

    /// RFC 7983 §7 binding table, narrowed:
    ///   0..=2    → STUN
    ///   20..=63  → DTLS records (content types 20..=25 allocated, the
    ///               rest of the range reserved by RFC 7983; record headers
    ///               are 13 bytes, so shorter claims are noise)
    ///   128..=191 (V=2) with RTCP-range byte-1 → RTCP
    ///   remaining V=2 → RTP
    ///
    /// Returns `Unknown` for everything else — the receiver logs & drops
    /// rather than guessing (the gateway's ingest discipline, applied to
    /// the datagram layer).
    pub fn classify(data: &[u8]) -> FrameKind {
        // DTLS BEFORE STUN: content types 22..25 (and the rest of the
        // RFC 7983 DTLS range) all satisfy the STUN arm's loose top-bits
        // mask — the more specific protocol claim wins by being checked
        // first here.
        if data.len() >= 13 && (20..64).contains(&data[0]) {
            return FrameKind::Dtls;
        }
        if data.len() >= 20 && data[0] & 0xC0 == 0 {
            return FrameKind::Stun;
        }
        // RTCP first: the RR minimum is 8 bytes — shorter than RTP's 12 —
        // and byte1's range is disjoint from legal RTP payload types per
        // the RFC 5761 carve-out, so ordering here is unambiguous.
        if looks_like_rtcp(data) {
            return FrameKind::Rtcp;
        }
        if looks_like_rtp(data) {
            return FrameKind::Rtp;
        }
        FrameKind::Unknown
    }
}

#[derive(Clone, Debug)]
pub struct Datagram {
    pub from: SocketAddr,
    pub bytes: Vec<u8>,
}

/// The socket surface the engine consumes. Object-safe so tests can
/// inject fakes through the same type.
pub trait Transport: Send + Sync {
    /// Read up to `cap` datagrams. Duration is the overall budget —
    /// implementations must not overshoot (media loop cadence).
    fn recv_batch(
        &self,
        buf: &mut Vec<Datagram>,
        cap: usize,
        budget: Duration,
    ) -> io::Result<usize>;

    /// Send one datagram; returns whether the kernel accepted it.
    fn send(&self, to: SocketAddr, payload: &[u8]) -> io::Result<()>;

    fn local_addr(&self) -> io::Result<SocketAddr>;
}

/// The production transport: raw UDP. recv_batch is a would-block pump
/// sized by `cap`, wrapped in an overall budget guard (each iteration
/// starts with `set_read_timeout(remaining)`, the simplest std-only way
/// to honor budget without a poll loop).
pub struct UdpTransport {
    socket: UdpSocket,
}

impl UdpTransport {
    pub fn bind(addr: SocketAddr) -> io::Result<UdpTransport> {
        let socket = UdpSocket::bind(addr)?;
        socket.set_read_timeout(Some(Duration::from_millis(2)))?;
        Ok(UdpTransport { socket })
    }

    pub fn socket(&self) -> &UdpSocket {
        &self.socket
    }
}

impl Transport for UdpTransport {
    fn recv_batch(
        &self,
        buf: &mut Vec<Datagram>,
        cap: usize,
        budget: Duration,
    ) -> io::Result<usize> {
        let start = std::time::Instant::now();
        let mut got = 0usize;
        let mut scratch = vec![0u8; 65535];

        while got < cap {
            let remaining = budget.saturating_sub(start.elapsed());
            if remaining.is_zero() {
                break;
            }
            self.socket
                .set_read_timeout(Some(remaining.min(Duration::from_millis(2))))?;
            match self.socket.recv_from(&mut scratch) {
                Ok((n, from)) => {
                    buf.push(Datagram {
                        from,
                        bytes: scratch[..n].to_vec(),
                    });
                    got += 1;
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::TimedOut => break,
                Err(e) => return Err(e),
            }
        }
        Ok(got)
    }

    fn send(&self, to: SocketAddr, payload: &[u8]) -> io::Result<()> {
        self.socket.send_to(payload, to).map(|_| ())
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }
}

/// An in-memory transport for engine tests: paired queues with real
/// socket addresses, strict budget observance, and drop-all on
/// saturation (a saturated test transport behaves like a full NIC ring —
/// datagrams vanish, they do NOT back up).
pub struct MemTransport {
    addr: SocketAddr,
    inbound: std::sync::Mutex<std::collections::VecDeque<Datagram>>,
    outbound: std::sync::Mutex<Vec<(SocketAddr, Vec<u8>)>>,
    capacity: usize,
}

impl MemTransport {
    pub fn new(addr: SocketAddr, capacity: usize) -> MemTransport {
        MemTransport {
            addr,
            inbound: std::sync::Mutex::new(std::collections::VecDeque::new()),
            outbound: std::sync::Mutex::new(Vec::new()),
            capacity,
        }
    }

    /// Feed a datagram as if it arrived off the wire.
    pub fn inject(&self, from: SocketAddr, bytes: Vec<u8>) {
        let mut q = self.inbound.lock().unwrap_or_else(|p| p.into_inner());
        if q.len() < self.capacity {
            q.push_back(Datagram { from, bytes });
        }
        // Saturated: silently drop — the engine's drop-aware path must
        // see this EXACTLY like a full socket buffer, not as a backpressure
        // loop.
    }

    /// Snapshot + drain what the engine sent (test assertions hook here).
    pub fn drain_sent(&self) -> Vec<(SocketAddr, Vec<u8>)> {
        std::mem::take(&mut *self.outbound.lock().unwrap_or_else(|p| p.into_inner()))
    }

    pub fn queued(&self) -> usize {
        self.inbound.lock().unwrap_or_else(|p| p.into_inner()).len()
    }
}

impl Transport for MemTransport {
    fn recv_batch(
        &self,
        buf: &mut Vec<Datagram>,
        cap: usize,
        _budget: Duration,
    ) -> io::Result<usize> {
        let mut q = self.inbound.lock().unwrap_or_else(|p| p.into_inner());
        let mut got = 0usize;
        while got < cap {
            match q.pop_front() {
                Some(d) => {
                    buf.push(d);
                    got += 1;
                }
                None => break,
            }
        }
        Ok(got)
    }

    fn send(&self, to: SocketAddr, payload: &[u8]) -> io::Result<()> {
        self.outbound
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push((to, payload.to_vec()));
        Ok(())
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        Ok(self.addr)
    }
}
