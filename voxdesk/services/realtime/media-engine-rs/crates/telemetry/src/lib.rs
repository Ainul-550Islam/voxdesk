//! telemetry — the engine's dependency-free metrics core: atomic
//! counters/gauges plus a Prometheus text exposition render.
//!
//! Same discipline as the gateway's observability/metrics package (this is
//! its Rust twin): no labels (per-room/per-participant cardinality is
//! unbounded; per-entity detail belongs in logs and events), one venture
//! gauge per structural invariant, and the text format rendered by hand so
//! the SAME Prometheus scraper that reads the API and the gateway reads
//! this process unchanged.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Instant;

/// The full registry of engine instruments. Constructed once at boot;
/// every method is `&self` and safe from any thread (atomics only, no
/// locks on the hot path — RTP forwarding must never queue behind a
/// scrape).
pub struct Registry {
    boot: Instant,

    pub packets_received: AtomicI64,
    pub packets_forwarded: AtomicI64,
    pub packets_dropped: AtomicI64, // backpressure tears, malformed, wrong state
    pub packets_replayed: AtomicI64, // anti-replay window rejects (SRTP)
    pub bytes_received: AtomicI64,
    pub bytes_forwarded: AtomicI64,

    pub stun_requests: AtomicI64,
    pub stun_responses: AtomicI64,
    pub ice_pairs_selected: AtomicI64,

    pub rooms_current: AtomicI64,
    pub participants_current: AtomicI64,
    pub tracks_current: AtomicI64,
    pub publishes_total: AtomicI64,
    pub subscribes_total: AtomicI64,

    pub control_frames_in: AtomicI64,
    pub control_frames_rejected: AtomicI64,
    pub control_rate_limited: AtomicI64,
}

impl Registry {
    pub fn new() -> Self {
        Self {
            boot: Instant::now(),
            packets_received: AtomicI64::new(0),
            packets_forwarded: AtomicI64::new(0),
            packets_dropped: AtomicI64::new(0),
            packets_replayed: AtomicI64::new(0),
            bytes_received: AtomicI64::new(0),
            bytes_forwarded: AtomicI64::new(0),
            stun_requests: AtomicI64::new(0),
            stun_responses: AtomicI64::new(0),
            ice_pairs_selected: AtomicI64::new(0),
            rooms_current: AtomicI64::new(0),
            participants_current: AtomicI64::new(0),
            tracks_current: AtomicI64::new(0),
            publishes_total: AtomicI64::new(0),
            subscribes_total: AtomicI64::new(0),
            control_frames_in: AtomicI64::new(0),
            control_frames_rejected: AtomicI64::new(0),
            control_rate_limited: AtomicI64::new(0),
        }
    }

    pub fn inc(counter: &AtomicI64) {
        counter.fetch_add(1, Ordering::Relaxed);
    }

    pub fn add(counter: &AtomicI64, n: i64) {
        counter.fetch_add(n, Ordering::Relaxed);
    }

    pub fn set(gauge: &AtomicI64, n: i64) {
        gauge.store(n, Ordering::Relaxed);
    }

    /// Merges a batch of per-(room,participant,track) gauge deltas faceless
    /// into the totals — the sessions crate computes structure and feeds
    /// these, so cardinality stays structural even with churny rooms.
    pub fn gauges(&self) -> (i64, i64, i64) {
        (
            self.rooms_current.load(Ordering::Relaxed),
            self.participants_current.load(Ordering::Relaxed),
            self.tracks_current.load(Ordering::Relaxed),
        )
    }

    /// Prometheus text exposition. Extra node-fed gauges (e.g. live UDP
    /// sockets) are passed in by the caller — the registry cannot know
    /// them, exactly like the gateway delegates rooms/tenants.
    pub fn render(&self, extra: &BTreeMap<String, i64>) -> String {
        let mut b = String::with_capacity(2048);

        let counter = |b: &mut String, name: &str, help: &str, v: &AtomicI64| {
            let _ = writeln!(b, "# HELP {} {}", name, help);
            let _ = writeln!(b, "# TYPE {} counter", name);
            let _ = writeln!(b, "{} {}", name, v.load(Ordering::Relaxed));
        };
        let gauge = |b: &mut String, name: &str, help: &str, v: i64| {
            let _ = writeln!(b, "# HELP {} {}", name, help);
            let _ = writeln!(b, "# TYPE {} gauge", name);
            let _ = writeln!(b, "{} {}", name, v);
        };

        counter(
            &mut b,
            "voxdesk_media_packets_received_total",
            "RTP/RTCP datagrams accepted on the UDP edge.",
            &self.packets_received,
        );
        counter(
            &mut b,
            "voxdesk_media_packets_forwarded_total",
            "Media datagrams enqueued to a subscriber leg.",
            &self.packets_forwarded,
        );
        counter(
            &mut b,
            "voxdesk_media_packets_dropped_total",
            "Datagrams refused (full leg queue, malformed, wrong state).",
            &self.packets_dropped,
        );
        counter(
            &mut b,
            "voxdesk_media_packets_replayed_total",
            "Datagrams rejected by the SRTP anti-replay window.",
            &self.packets_replayed,
        );
        counter(
            &mut b,
            "voxdesk_media_bytes_received_total",
            "Media bytes in.",
            &self.bytes_received,
        );
        counter(
            &mut b,
            "voxdesk_media_bytes_forwarded_total",
            "Media bytes out.",
            &self.bytes_forwarded,
        );
        counter(
            &mut b,
            "voxdesk_media_stun_requests_total",
            "STUN binding requests received.",
            &self.stun_requests,
        );
        counter(
            &mut b,
            "voxdesk_media_stun_responses_total",
            "STUN responses sent.",
            &self.stun_responses,
        );
        counter(
            &mut b,
            "voxdesk_media_ice_pairs_selected_total",
            "ICE-lite pairs nominated.",
            &self.ice_pairs_selected,
        );
        counter(
            &mut b,
            "voxdesk_media_publishes_total",
            "Tracks published since boot.",
            &self.publishes_total,
        );
        counter(
            &mut b,
            "voxdesk_media_subscribes_total",
            "Track subscriptions since boot.",
            &self.subscribes_total,
        );
        counter(
            &mut b,
            "voxdesk_media_control_frames_total",
            "Control frames accepted.",
            &self.control_frames_in,
        );
        counter(
            &mut b,
            "voxdesk_media_control_rejected_total",
            "Control frames refused (shape/state).",
            &self.control_frames_rejected,
        );
        counter(
            &mut b,
            "voxdesk_media_control_rate_limited_total",
            "Control frames shed by the per-session limiter.",
            &self.control_rate_limited,
        );

        gauge(
            &mut b,
            "voxdesk_media_rooms_current",
            "Live media rooms.",
            self.rooms_current.load(Ordering::Relaxed),
        );
        gauge(
            &mut b,
            "voxdesk_media_participants_current",
            "Live media participants.",
            self.participants_current.load(Ordering::Relaxed),
        );
        gauge(
            &mut b,
            "voxdesk_media_tracks_current",
            "Live forwarded tracks.",
            self.tracks_current.load(Ordering::Relaxed),
        );
        for (name, v) in extra {
            gauge(&mut b, name, "node-fed gauge", *v);
        }
        gauge(
            &mut b,
            "voxdesk_media_uptime_seconds",
            "Seconds since boot.",
            self.boot.elapsed().as_secs() as i64,
        );
        b
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}
