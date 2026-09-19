//! media — the per-source stream state the SFU keeps for every published
//! (room, participant, track) flow: sequence tracking, replay rejection,
//! reorder holdback, jitter estimation. This is the tape the engine
//! reads BEFORE deciding a packet is fan-out-worthy; the per-subscriber
//! side (who receives it) is routing's snapshot.
//!
//! Why separate from streams/: streams/ is protocol mechanics; media/ is
//! the state machine instances of those mechanics per flowing source.

use protocol::{ParticipantId, RoomId, TrackId};
use std::collections::BTreeMap;
use streams::jitter::{JitterEstimator, ReorderBuffer};
use streams::packet::{self, RtpPacket};
use streams::seq::{LossStats, SeqTracker};

/// Everything the engine records per source.
pub struct SourceStream {
    pub room: RoomId,
    pub participant: ParticipantId,
    pub track: TrackId,
    pub ssrc: u32,
    /// SEQ → extended/roc estimation (the SRTP receiver's index helper).
    pub tracker: SeqTracker,
    /// Per-stream loss bookkeeping (dup detection included).
    pub loss: LossStats,
    pub jitter: JitterEstimator,
    /// Media clock rate (ts units / second) — 48k audio, 90k video —
    /// used for jitter arithmetic AND for wall-clock ↔ ts conversion.
    pub clock_rate: u32,
    /// RFC 3550-style reorder holdback: packets spend a bounded window
    /// here before being re-emitted in order.
    pub reorder: ReorderBuffer,
    /// Stats the telemetry surface reflects.
    pub received: u64,
    pub dropped_replay: u64,
    pub dropped_late: u64,
    pub unparseable: u64,
    /// Caller-seen quality facts from RECEIVER reports (RTCP feedback
    /// path: the source's outbound legs experience these losses).
    pub rr_total: u64,
    pub rr_fraction_lost_latest: u8,
    pub rr_cumulative_lost_latest: u32,
    /// Last received sender report's NTP seconds on this source (used to
    /// pair lsr/dlsr round-trip estimation when needed later).
    pub sr_total: u64,
    pub sr_ntp_seconds_latest: u32,
}

#[derive(Clone, Debug)]
pub struct RouteStamp {
    pub room: RoomId,
    pub participant: ParticipantId,
    pub track: TrackId,
}

/// What the engine does with one inbound datagram `process()`ed.
#[derive(Clone, Debug)]
pub enum StreamEvent {
    /// Emit to subscribers NOW (data is the parsed/verified packet).
    Forward(RtpPacket),
    /// Held in the reorder buffer (its turn will come via flush).
    Held,
    /// Verdicts to count on telemetry: replay dup detected.
    DroppedReplay,
    /// Too-stale window skipper (outside reorder window).
    DroppedOld,
}

pub struct StreamRegistry {
    /// (room, participant, track) → per-source runtime state.
    streams: BTreeMap<RouteStampKey, SourceStream>,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct RouteStampKey(pub RoomId, pub ParticipantId, pub TrackId);

impl Default for StreamRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl StreamRegistry {
    pub fn new() -> StreamRegistry {
        StreamRegistry {
            streams: BTreeMap::new(),
        }
    }

    /// Create-or-return the stream state for a publication.
    pub fn ensure(
        &mut self,
        stamp: &RouteStamp,
        ssrc: u32,
        capacity: usize,
        clock_rate: u32,
    ) -> &mut SourceStream {
        let key = RouteStampKey(
            stamp.room.clone(),
            stamp.participant.clone(),
            stamp.track.clone(),
        );
        self.streams
            .entry(key.clone())
            .or_insert_with(|| SourceStream {
                room: stamp.room.clone(),
                participant: stamp.participant.clone(),
                track: stamp.track.clone(),
                ssrc,
                tracker: SeqTracker::default(),
                loss: LossStats::new(),
                jitter: JitterEstimator::new(),
                clock_rate,
                reorder: ReorderBuffer::new(capacity.max(2)),
                received: 0,
                dropped_replay: 0,
                dropped_late: 0,
                unparseable: 0,
                rr_total: 0,
                rr_fraction_lost_latest: 0,
                rr_cumulative_lost_latest: 0,
                sr_total: 0,
                sr_ntp_seconds_latest: 0,
            })
    }

    pub fn remove(&mut self, stamp: &RouteStamp) -> bool {
        let key = RouteStampKey(
            stamp.room.clone(),
            stamp.participant.clone(),
            stamp.track.clone(),
        );
        self.streams.remove(&key).is_some()
    }

    pub fn count(&self) -> usize {
        self.streams.len()
    }

    pub fn get(&mut self, stamp: &RouteStamp) -> Option<&mut SourceStream> {
        let key = RouteStampKey(
            stamp.room.clone(),
            stamp.participant.clone(),
            stamp.track.clone(),
        );
        self.streams.get_mut(&key)
    }
}

/// How far the reorder buffer's gap-skipping may stretch (128 seqs of
/// audio = 2.5s at 48kHz — beyond that, waiting is worse than jumping).
pub const MAX_REORDER_GAP: u32 = 128;

/// Feed ONE wire datagram through one stream's pipeline:
///
/// 1. parse (shape),
/// 2. extended (roc estimation + loss bookkeeping),
/// 3. duplicate/loss accounting,
/// 4. jitter sample in timestamp units,
/// 5. reorder holdback,
/// 6. emit what's ready IN ORDER (possibly several).
pub fn process(stream: &mut SourceStream, raw: Vec<u8>, now_ms: u32) -> Vec<StreamEvent> {
    let mut events = Vec::new();
    let pkt = match packet::RtpPacket::parse(raw) {
        Ok(p) => p,
        Err(_) => {
            stream.unparseable += 1;
            events.push(StreamEvent::DroppedOld);
            return events;
        }
    };
    let extended = stream.tracker.extend(pkt.sequence);
    stream.loss.record(pkt.sequence);
    // Jitter: arrival-time in TS units (RFC 3550 A.8 works in the RTP
    // clock's own ticks so the estimator is rate-agnostic).
    let arrival_ts = now_ms as f64 * f64::from(stream.clock_rate) / 1000.0;
    stream.jitter.record(pkt.timestamp, arrival_ts);
    stream.received += 1;

    stream.reorder.insert(extended, pkt);
    let before_late = stream.reorder.dropped_late();
    while let Some(pkt) = stream.reorder.pop(MAX_REORDER_GAP) {
        events.push(StreamEvent::Forward(pkt));
    }
    // Reorder buffer's late-drops surface as their own counter.
    let late_delta = stream.reorder.dropped_late() - before_late;
    if late_delta > 0 {
        stream.dropped_late += late_delta;
    }
    if events.is_empty() {
        events.push(StreamEvent::Held);
    }
    events
}
