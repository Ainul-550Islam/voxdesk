//! Jitter handling, two independent pieces:
//!
//! * [`JitterEstimator`]: RFC 3550 §6.4.1 / Appendix A.8's interarrival
//!   jitter estimator, the exact value RTCP receiver reports carry.
//! * [`ReorderBuffer`]: a small sequence-ordered holding pen that trades
//!   a bounded delay for in-order delivery — reorder a handful of packets
//!   rather than feeding the decoder (or the mixer) a permuted stream.

use crate::packet::RtpPacket;
use std::collections::BTreeMap;

// ---------------------------------------------------------------------------
// RFC 3550 A.8 jitter estimator
// ---------------------------------------------------------------------------

/// J = J + (|D(i-1,i)| - J)/16 over arrival-vs-timestamp transit deltas,
/// computed in timestamp units.
#[derive(Clone, Debug, Default)]
pub struct JitterEstimator {
    prev_transit: Option<f64>,
    jitter: f64,
}

impl JitterEstimator {
    pub fn new() -> JitterEstimator {
        JitterEstimator::default()
    }

    /// Feed one packet. `arrival` and the RTP timestamp must share a clock
    /// DOMAIN RATIO: callers convert arrival time into the stream's
    /// timestamp units (e.g. seconds×90000 for video, ×48000 for opus).
    /// This is the RFC's transit-time formulation with float units; the
    /// RFC's integer version quantises away sub-sample deltas that matter
    /// at 48 kHz.
    pub fn record(&mut self, timestamp: u32, arrival_in_ts_units: f64) {
        let transit = arrival_in_ts_units - f64::from(timestamp);
        if let Some(prev) = self.prev_transit {
            let d = (transit - prev).abs();
            self.jitter += (d - self.jitter) / 16.0;
        }
        self.prev_transit = Some(transit);
    }

    /// The running estimate, in timestamp units.
    pub fn jitter(&self) -> f64 {
        self.jitter
    }

    /// Rounded to the RTCP RR field's integer shape (timestamp units).
    pub fn jitter_u32(&self) -> u32 {
        self.jitter.round() as u32
    }
}

// ---------------------------------------------------------------------------
// Sequence-ordered reorder buffer
// ---------------------------------------------------------------------------

/// Holds out-of-order packets keyed by EXTENDED sequence number, releasing
/// them in order. Bounded two ways:
///
/// * CAPACITY (packets): a burst bigger than the buffer flushes the
///   oldest — a sender whose ordering is that broken gets forwarded in
///   receipt order, which is what a zero-buffer engine would do anyway;
/// * no time guarantee is implied: media is RTP-paced, so the consumer's
///   own cadence drains it. (Voice pipelines that need a time-scale play
///   out to the mixer, which owns the 20 ms metronome.)
pub struct ReorderBuffer {
    by_seq: BTreeMap<u32, RtpPacket>,
    /// Next extended seq to release, once stream order is established.
    next: Option<u32>,
    capacity: usize,
    dropped_late: u64,
    flushed: u64,
}

impl ReorderBuffer {
    pub fn new(capacity: usize) -> ReorderBuffer {
        ReorderBuffer {
            by_seq: BTreeMap::new(),
            next: None,
            capacity: capacity.max(2),
            dropped_late: 0,
            flushed: 0,
        }
    }

    /// Insert one packet (extended seq from SeqTracker). Returns how many
    /// packets were force-released to make room (0 or 1).
    pub fn insert(&mut self, ext_seq: u32, packet: RtpPacket) -> usize {
        if let Some(next) = self.next {
            if ext_seq < next {
                // Older than what we've already released: this stream's
                // latecomers go straight to the drop counter — forwarding
                // them backwards in time helps nothing.
                self.dropped_late += 1;
                return 0;
            }
        }
        if self.next.is_none() {
            self.next = Some(ext_seq);
        }
        if self.by_seq.contains_key(&ext_seq) {
            self.dropped_late += 1;
            return 0;
        }
        self.by_seq.insert(ext_seq, packet);
        if self.by_seq.len() > self.capacity {
            // Force-release the OLDEST to cap memory and delay: pops out
            // of order relative to the stream, in order relative to time.
            if let Some((&first, _)) = self.by_seq.iter().next() {
                self.next = Some(first.wrapping_add(1));
                self.by_seq.remove(&first);
                self.flushed += 1;
                return 1;
            }
        }
        0
    }

    /// Release the next in-order packet when its seq is exactly `next`;
    /// also releases when a GAP is older than `max_gap`seq numbers (the
    /// lost packet inside the gap is then declared lost, not waited on
    /// forever — a permanent stall is worse than one skip).
    pub fn pop(&mut self, max_gap: u32) -> Option<RtpPacket> {
        let next = self.next?;
        if self.by_seq.is_empty() {
            return None;
        }
        if let Some(pkt) = self.by_seq.remove(&next) {
            self.next = Some(next.wrapping_add(1));
            return Some(pkt);
        }
        // Is the lowest buffered seq beyond the patience window?
        let (&lowest, _) = self.by_seq.iter().next().unwrap();
        if lowest.wrapping_sub(next) > max_gap {
            self.next = Some(lowest);
            return self.by_seq.remove(&lowest).inspect(|_pkt| {
                self.next = Some(lowest.wrapping_add(1));
            });
        }
        None
    }

    pub fn len(&self) -> usize {
        self.by_seq.len()
    }

    pub fn is_empty(&self) -> bool {
        self.by_seq.is_empty()
    }

    pub fn dropped_late(&self) -> u64 {
        self.dropped_late
    }

    pub fn flushed(&self) -> u64 {
        self.flushed
    }
}
