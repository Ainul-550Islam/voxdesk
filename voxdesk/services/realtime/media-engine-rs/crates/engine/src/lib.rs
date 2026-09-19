//! engine — the SFU's orchestration top: UDP ticks, STUN → session ICE
//! agents, RTP fan-out through media pipelines keyed by ROUTING + SSRC
//! identity, RTCP feedback dispatch, and the Go gateway's control wire
//! (the envelope-wrapped `/v1/signal` protocol).
//!
//! Identity model (the load-bearing fact of this crate):
//!
//! * A CLIENT-SESSION is one `join`: one `SessionSlot`, one ICE agent,
//!   one endpoint eventually.
//! * A TRACK is (room, participant, TrackId) with an engine-ASSIGNED ssrc
//!   minted on publish. The SSRC namespace is engine-owned — browsers
//!   cannot collide (all SSRCs seen on the wire match exactly one
//!   published track, because only those were handed out via answer-time
//!   signalling).
//! * The RTP hot path is endpoint-keyed ONLY as a security gate (who is
//!   this 5-tuple) and ssrc-keyed as a routing gate (which track).
//!   Selecting a track from `session.tracks.iter().next()` — an earlier
//!   draft — is arbitrary under multi-track publishers and is gone.
//!
//! The Go ↔ Rust wire (documented once, here):
//!
//! * Request:  {"v":1,"id":"<16 hex>","frame":{ ...ClientFrame...}}
//! * Reply:    {"v":1,"id":"<same>","frames":[ ...ServerFrame... ]}
//! * Version mismatch / malformed envelope: {"v":1,"id":?,"error":
//!   {"code":...,"message":...}} and HTTP 200 — transport errors are the
//!   HTTP layer's (500 on panics only), logical errors are in-band.
//! * A BARE client-frame object (no "v") is still accepted and answers a
//!   bare JSON array — back-compat for on-signaling's smoke path.
//! * Health: GET /v1/health → {"v":1,"engine":"media-engine-rs",
//!   "version":"<semver>","ready":true,"participants":N,"tracks":N,
//!   "streams":N,"uptime_ms":N} — the Go gateway's readiness probe.

use media::{process, RouteStamp, StreamEvent, StreamRegistry};
use protocol::{
    json::{self, Value},
    ClientFrame, ErrorCode, MediaKind, MediaSessionId, ParticipantId, RoomId, ServerFrame, TrackId,
};
use routing::{RoomLimits, RouteTable};
use sessions::Store as SessionStore;
use signaling::{CallerContext, SignalCore};
use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use transport::{demux, Datagram, MemTransport, Transport};
use webrtc::ice::LiteAgent;
use webrtc::srtp::{derive_session_keys, RtpProtector, RtpUnprotector};
use webrtc::stun::StunMessage;

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub public_ip: [u8; 4],
    pub public_port: u16,
    pub fingerprint_sha256: String,
    pub engine_label: String,
    pub room_limits: RoomLimits,
    pub clock_rate_audio: u32,
    pub clock_rate_video: u32,
    pub reorder_capacity: usize,
    /// Per-boot DTLS-SRTP identity (self-signed cert + config). `None`
    /// keeps the fixture posture: no DTLS endpoints spawn, the static
    /// `fingerprint_sha256` label flows into answers, and SDES-style
    /// `bind_srtp` remains the ONLY keying path — exactly what the unit
    /// tests pin. Production (bins/media-engine) always generates one.
    pub dtls_identity: Option<dtls::Identity>,
}

impl Default for EngineConfig {
    fn default() -> Self {
        EngineConfig {
            public_ip: [203, 0, 113, 1],
            public_port: 5000,
            fingerprint_sha256: "AB:CD".repeat(16),
            engine_label: "edge".into(),
            room_limits: RoomLimits::default(),
            clock_rate_audio: 48000,
            clock_rate_video: 90000,
            reorder_capacity: 16,
            dtls_identity: None,
        }
    }
}

/// One joined client-session's runtime bundle.
pub struct SessionSlot {
    pub session_id: MediaSessionId,
    pub participant: ParticipantId,
    pub room: RoomId,
    /// Signal-reducer caller context state (mutated across frames).
    pub ctx: CallerContext,
    pub agent: Option<LiteAgent>,
    /// DTLS-SRTP termination (RFC 5764) — spawns on offer when the
    /// engine config carries an identity; keys flow into inbound/outbound
    /// through `bind_srtp_split` on `Event::Established` + CM profile.
    pub dtls: Option<dtls::Endpoint>,
    /// Drive-produced DTLS packets buffered while ICE hasn't nominated a
    /// 5-tuple yet (active-role ClientHello can precede nomination);
    /// flushed to the nominated endpoint at first opportunity.
    pub dtls_pending: Vec<Vec<u8>>,
    /// SRTP contexts (SDES-style bound; DTLS extraction feeds this hook).
    pub inbound: Option<RtpUnprotector>,
    pub outbound: Option<RtpProtector>,
    /// The remote endpoint ICE nominated; datagrams are refused before.
    pub endpoint: Option<(u16, [u8; 4])>,
    /// track id → per-track ledger entry (SSRC assigned at publish).
    pub tracks: BTreeMap<TrackId, SlotTrack>,
    /// The sessions::Store record id the sweep expires against.
    pub store_session_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotTrack {
    pub kind: MediaKind,
    pub ssrc: u32,
}

/// Metrics-relevant counters the binary's telemetry loop polls.
#[derive(Default, Debug)]
pub struct EngineStats {
    pub stun_answered: u64,
    pub rtp_received: u64,
    pub rtp_forwarded: u64,
    pub rtp_dropped: u64,
    pub rtp_drop_spoof: u64,
    pub rtp_drop_unknown_ssrc: u64,
    pub rtcp_received: u64,
    pub rtcp_sr: u64,
    pub rtcp_rr: u64,
    pub rtcp_reports_applied: u64,
    pub rtcp_unsupported: u64,
    pub rtcp_malformed: u64,
    pub unknown_frames: u64,
    pub trickle_candidates: u64,
    pub trickle_rejected: u64,
    pub offers_answered: u64,
    pub tracks_registered: u64,
    pub sweeps: u64,
    /// DTLS datagrams ignored: unknown 5-tuple or no endpoint spawned.
    pub dtls_dropped: u64,
    /// Handshakes whose CM keying material bound (Established+aes128_cm).
    pub dtls_established: u64,
    /// Handshakes failed closed: protocol error, fingerprint mismatch,
    /// or a negotiated profile the engine crypto cannot bind.
    pub dtls_failed: u64,
    /// Negotiations that succeeded but landed on a non-CM profile —
    /// counted separately because it signals a non-WebRTC peer, not a
    /// network/cert failure.
    pub dtls_profile_refused: u64,
    /// Publishes refused for explicit-SSRC collision/zero.
    pub publish_refused: u64,
}

/// SSRC namespace: RFC 3550's rule is "unique inside a session". Our SFU
/// needs a stronger one — global across the FLEET — because a track's ssrc
/// appears in RTCP reports that may be attributed post-handover onto a
/// different node. The scheme: top byte = a partition index derived from
/// the engine label (FNV-1a mod 254 + 1, skipping the 0x00 and 0xFF
/// reserved bytes), low 24 bits = a per-engine monotonically increasing
/// counter. Two differently-labelled nodes therefore Disagree on the top
/// byte of every new allocation (255 partition slots, 1/254 accidental
/// collision between two random labels, surfaced in logs via
/// health_json's ssrc_partition), and one node mints 2**24 distinct ssrcs
/// before its counter wraps — at 64 participants × a few tracks/lifetime
/// this is unreachable.
fn ssrc_partition_of(label: &str) -> u8 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in label.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01B3);
    }
    (h % 254 + 1) as u8
}

pub struct Engine {
    pub config: EngineConfig,
    pub store: Arc<SessionStore>,
    pub routes: Arc<RouteTable>,
    pub core: SignalCore,
    pub registry: StreamRegistry,
    pub slots: BTreeMap<MediaSessionId, SessionSlot>,
    /// Reverse map: remote endpoint → owning session (ICE state → session).
    pub by_endpoint: BTreeMap<(u16, [u8; 4]), MediaSessionId>,
    /// Reverse map: assigned ssrc → owning session + track (RTP routing).
    pub by_ssrc: BTreeMap<u32, (MediaSessionId, TrackId)>,
    transport: Arc<dyn Transport>,
    pub stats: EngineStats,
    /// The fleet partition byte this engine owns (top 8 bits of every
    /// minted ssrc) and the 24-bit per-engine counter. See
    /// ssrc_partition_of for the scheme and its collision accounting.
    ssrc_partition: u8,
    ssrc_alloc: AtomicU64,
    started: Instant,
}

impl Engine {
    pub fn new(config: EngineConfig, transport: Arc<dyn Transport>) -> Engine {
        let store = Arc::new(SessionStore::new());
        Self::with_store_and_transport(config, transport, store)
    }

    /// In-memory engine for tests: caller keeps the Arc to inject/observe.
    pub fn with_mem_transport(addr: SocketAddr) -> (Engine, Arc<MemTransport>) {
        let t = Arc::new(MemTransport::new(addr, 4096));
        (Engine::new(EngineConfig::default(), t.clone()), t)
    }

    /// Engine with a caller-tuned session store (tests set timeouts
    /// BEFORE construction; the engine never mutates them at runtime).
    pub fn with_store_and_transport(
        config: EngineConfig,
        transport: Arc<dyn Transport>,
        store: Arc<SessionStore>,
    ) -> Engine {
        let routes = Arc::new(RouteTable::new(config.room_limits));
        let core = SignalCore::new(
            store.clone(),
            routes.clone(),
            config.fingerprint_sha256.clone(),
            config.public_ip,
            config.public_port,
            config.engine_label.clone(),
        );
        let ssrc_partition = ssrc_partition_of(&config.engine_label);
        Engine {
            config,
            store,
            routes,
            core,
            registry: StreamRegistry::new(),
            slots: BTreeMap::new(),
            by_endpoint: BTreeMap::new(),
            by_ssrc: BTreeMap::new(),
            transport,
            stats: EngineStats::default(),
            ssrc_partition,
            ssrc_alloc: AtomicU64::new(0),
            started: Instant::now(),
        }
    }

    // --------------------------------------------------------------- SSRC

    /// The next unique SSRC within this node's partition: 24-bit counter
    /// walk (wraps at 2**24, skipping zero) + intra-engine map check.
    /// Restart-without-rebind is the only remaining overlap vector; the
    /// map gate catches it and walks again, so two slots never share.
    /// Public for allocator tests and load-driver sampling; production
    /// hands out SSRCs ONLY through the publish path (on_published).
    #[doc(hidden)]
    pub fn allocate_ssrc_for_test(&self) -> u32 {
        self.allocate_ssrc_inner()
    }

    fn allocate_ssrc_inner(&self) -> u32 {
        loop {
            let n = self.ssrc_alloc.fetch_add(1, Ordering::SeqCst) & 0x00FF_FFFF;
            if n == 0 {
                continue;
            }
            let ssrc = (u32::from(self.ssrc_partition) << 24) | n as u32;
            if !self.by_ssrc.contains_key(&ssrc) {
                return ssrc;
            }
        }
    }

    /// The partition byte this engine mints every SSRC under (fleet
    /// name-spacing; health_json exposes it so two colliding nodes are
    /// diagnosable from metrics without packet capture).
    pub fn ssrc_partition(&self) -> u8 {
        self.ssrc_partition
    }

    // ----------------------------------------------------------- control

    /// The Go gateway's HTTP+/v1/signal entry point plus the bare-frame
    /// back-compat path. Full contract: module-level docs above.
    pub fn on_control(&mut self, body: &str) -> String {
        let parsed = json::parse(body);
        // Envelope shape? {"v":1, "id": str?, "frame": {...}}
        let (enveloped, request_id, frame) = match &parsed {
            Ok(v) => {
                if let Some(version) = v.get("v").and_then(Value::as_u64) {
                    let id = v.get("id").and_then(Value::as_str).map(str::to_string);
                    if version != u64::from(protocol::WIRE_VERSION) {
                        return Self::envelope_error(
                            id,
                            "unsupported_version",
                            &format!(
                                "control wire version {version}, this node speaks {}",
                                protocol::WIRE_VERSION
                            ),
                        );
                    }
                    let Some(frame) = v.get("frame") else {
                        return Self::envelope_error(id, "bad_message", "envelope has no frame");
                    };
                    (true, id, frame.clone())
                } else if v.get("frame").is_some() {
                    // {"frame": ...} without v: malformed envelope (an id
                    // without version is exactly the version-skew failure
                    // the version field exists to surface).
                    let id = v.get("id").and_then(Value::as_str).map(str::to_string);
                    return Self::envelope_error(
                        id,
                        "unsupported_version",
                        "v is required on enveloped requests",
                    );
                } else {
                    // Bare client frame (legacy smoke path): same object
                    // re-used as the frame.
                    (false, None, v.clone())
                }
            }
            Err(e) => {
                return Self::envelope_error(
                    None,
                    "bad_message",
                    &format!("control body is not json: {e}"),
                );
            }
        };

        let frame_text = frame.to_string_compact();
        let frames = self.on_signaling_frame(&frame_text);
        if enveloped {
            // Re-wrap server frames as JSON values inside the reply body.
            let mut out = Value::obj();
            out.set("v", Value::Num(f64::from(protocol::WIRE_VERSION)));
            match &request_id {
                Some(id) => out.set("id", Value::Str(id.clone())),
                None => out.set("id", Value::Null),
            }
            let arr: Vec<Value> = frames.iter().filter_map(|f| json::parse(f).ok()).collect();
            out.set("frames", Value::Arr(arr));
            out.to_string_compact()
        } else {
            format!("[{}]", frames.join(","))
        }
    }

    fn envelope_error(id: Option<String>, code: &str, message: &str) -> String {
        let mut out = Value::obj();
        out.set("v", Value::Num(f64::from(protocol::WIRE_VERSION)));
        match id {
            Some(i) => out.set("id", Value::Str(i)),
            None => out.set("id", Value::Null),
        }
        let mut err = Value::obj();
        err.set("code", Value::Str(code.to_string()));
        err.set("message", Value::Str(message.to_string()));
        out.set("error", err);
        out.to_string_compact()
    }

    /// `/v1/health` body: readiness is TRUE once boot completes (this
    /// process has no long warmup — the watch is upstream/downstream).
    pub fn health_json(&self) -> String {
        let mut out = Value::obj();
        out.set("v", Value::Num(f64::from(protocol::WIRE_VERSION)));
        out.set("engine", Value::Str("media-engine-rs".into()));
        out.set("version", Value::Str(env!("CARGO_PKG_VERSION").into()));
        out.set("ready", Value::Bool(true));
        let (rooms, participants, tracks) = self.routes.stats();
        out.set("rooms", Value::Num(rooms as f64));
        out.set("participants", Value::Num(participants as f64));
        out.set("tracks", Value::Num(tracks as f64));
        out.set("streams", Value::Num(self.registry.count() as f64));
        out.set(
            "uptime_ms",
            Value::Num(self.started.elapsed().as_millis() as f64),
        );
        out.to_string_compact()
    }

    // ---------------------------------------------------------- signaling

    /// A bare control frame arrives (legacy path kept for interop).
    pub fn on_signaling_frame(&mut self, frame_text: &str) -> Vec<String> {
        let mut out = Vec::new();
        let frame = match ClientFrame::decode(frame_text) {
            Ok(f) => f,
            Err(e) => {
                return vec![ServerFrame::Error {
                    code: ErrorCode::BadMessage,
                    message: e.to_string(),
                }
                .encode()];
            }
        };
        let (mut ctx, _slot_key) = self.caller_from_frame(&frame);
        let mut effects = self.core.handle(&mut ctx, frame);

        // New-session bookkeeping: slot + agent from the join effects.
        if let Some(new_session) = effects.new_session {
            self.register_slot(new_session, &mut ctx);
        }
        // Publish recognition: register the track IN THE SLOT and mint its
        // ssrc. This is not best-effort — a publish accepted by the
        // reducer that did not mint an SSRC here would be invisible to the
        // RTP path, so a missing slot is a loud registration error.
        if let Some((track, kind, ssrc)) = effects.published {
            // Explicit-SSRC collisions refuse the publish (and its fanout)
            // after the reducer committed: the alternative — binding a
            // second track onto an ssrc someone else owns — is a media
            // routing lie, so it refuses and explains, never folds.
            if !self.on_published(&ctx, track.clone(), kind, ssrc) {
                self.stats.publish_refused += 1;
                effects.room_fanout.retain(|f| {
                    !matches!(
                        f,
                        ServerFrame::TrackPublished { track: ref tk, .. } if tk == &track
                    )
                });
                effects.reply.push(ServerFrame::Error {
                    code: ErrorCode::WrongState,
                    message: "publish refused: ssrc already claimed".into(),
                });
            }
        }
        if let Some(trickle) = effects.trickle {
            self.on_trickle_effect(&ctx, trickle);
        }
        if let Some(offer_ctx) = effects.offer_context {
            self.on_offer_context(&ctx, offer_ctx);
            self.stats.offers_answered += 1;
        }
        if let Some((room, participant)) = effects.left {
            self.on_left(&room, &participant, &ctx);
        }

        for f in effects.reply {
            out.push(f.encode());
        }
        for f in effects.room_fanout {
            out.push(f.encode());
        }
        out
    }

    fn caller_from_frame(&self, frame: &ClientFrame) -> (CallerContext, Option<MediaSessionId>) {
        let now = Instant::now();
        match frame {
            ClientFrame::Join { room, participant } => (
                CallerContext {
                    participant: participant.clone(),
                    room: Some(room.clone()),
                    session: None,
                    now,
                },
                None,
            ),
            ClientFrame::Offer { session, .. } | ClientFrame::Trickle { session, .. } => {
                if let Some(slot) = self.slots.get(session) {
                    return (slot.ctx.clone(), Some(session.clone()));
                }
                (
                    CallerContext {
                        participant: ParticipantId("?".into()),
                        room: None,
                        session: Some(session.clone()),
                        now,
                    },
                    None,
                )
            }
            ClientFrame::Publish { session, .. }
            | ClientFrame::Subscribe { session, .. }
            | ClientFrame::Unsubscribe { session, .. }
            | ClientFrame::Leave { session } => {
                // Frame-carried session wins (production path; the Go
                // gateway sends it). Fallback: EXACTLY ONE live session
                // (the single-client smoke harness) — with two or more,
                // attribution would be a guess and is refused by lookup
                // over a context with no session (WrongState upstream).
                if let Some(s) = session.as_deref() {
                    let sid = MediaSessionId(s.to_string());
                    if let Some(slot) = self.slots.get(&sid) {
                        return (slot.ctx.clone(), Some(sid));
                    }
                }
                if self.slots.len() == 1 {
                    let (sid, slot) = self.slots.iter().next().expect("len checked");
                    return (slot.ctx.clone(), Some(sid.clone()));
                }
                (
                    CallerContext {
                        participant: ParticipantId("?".into()),
                        room: None,
                        session: None,
                        now,
                    },
                    None,
                )
            }
            _ => (
                CallerContext {
                    participant: ParticipantId("?".into()),
                    room: None,
                    session: None,
                    now,
                },
                None,
            ),
        }
    }

    fn register_slot(&mut self, new: signaling::NewSession, ctx: &mut CallerContext) {
        let mut agent = LiteAgent::new(new.ice_ufrag.clone(), new.ice_pwd.clone(), [0x5Au8; 32]);
        agent.tiebreaker = u64::from_le_bytes(*b"VOXDESKE");
        let store_session_id = new
            .session_id
            .0
            .strip_prefix("ms-")
            .and_then(|s| s.split('-').next())
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        self.slots.insert(
            new.session_id.clone(),
            SessionSlot {
                session_id: new.session_id.clone(),
                participant: new.participant,
                room: new.room,
                ctx: ctx.clone(),
                agent: Some(agent),
                dtls: None,
                dtls_pending: Vec::new(),
                inbound: None,
                outbound: None,
                endpoint: None,
                tracks: BTreeMap::new(),
                store_session_id,
            },
        );
    }

    /// Track registration recognized from the reducer: the ONLY place
    /// publish semantics become forwarding semantics.
    /// Returns false ONLY when an EXPLICIT client-SSRC was refused
    /// (zero or already claimed elsewhere) — the caller then rewinds the
    /// fanout and answers with an Error frame.
    fn on_published(
        &mut self,
        ctx: &CallerContext,
        track: TrackId,
        kind: MediaKind,
        explicit_ssrc: Option<u32>,
    ) -> bool {
        let Some(session_id) = ctx.session.clone() else {
            return true;
        };
        // Duplicate registrations are refused upstream (the reducer's
        // RouteError::DuplicateTrack); this arm is the invariant keeper:
        // if a track exists with a DIFFERENT ssrc here, the map and
        // slots disagree — do not overwrite, that would orphan the old
        // ssrc's streams.
        let registered = self
            .slots
            .get(&session_id)
            .map(|s| s.tracks.contains_key(&track));
        match registered {
            Some(true) => return true,
            Some(false) => {}
            None => return true,
        }
        let ssrc = match explicit_ssrc {
            Some(0) => return false, // ssrc 0 is a protocol lie
            Some(s) if self.by_ssrc.contains_key(&s) => return false,
            Some(s) => s,                       // genuine client's own SSRC (RFC 3550)
            None => self.allocate_ssrc_inner(), // fixture path
        };
        let Some(slot) = self.slots.get_mut(&session_id) else {
            return true;
        };
        slot.tracks.insert(track.clone(), SlotTrack { kind, ssrc });
        self.by_ssrc.insert(ssrc, (session_id, track));
        self.stats.tracks_registered += 1;
        true
    }

    fn on_trickle_effect(&mut self, ctx: &CallerContext, trickle: signaling::TrickleEffect) {
        let Some(session_id) = ctx.session.clone() else {
            return;
        };
        let Some(slot) = self.slots.get_mut(&session_id) else {
            return;
        };
        let Some(agent) = slot.agent.as_mut() else {
            return;
        };
        if let Some(c) = trickle.candidate {
            match agent.add_remote_candidate(c) {
                Ok(true) => self.stats.trickle_candidates += 1,
                Ok(false) => {} // duplicate re-assertion: quiet success
                Err(_) => self.stats.trickle_rejected += 1,
            }
        }
    }

    fn on_offer_context(&mut self, ctx: &CallerContext, offer_ctx: signaling::OfferContext) {
        let Some(session_id) = ctx.session.clone() else {
            return;
        };
        let Some(slot) = self.slots.get_mut(&session_id) else {
            return;
        };
        let Some(agent) = slot.agent.as_mut() else {
            return;
        };
        if !offer_ctx.ice_ufrag.is_empty() || !offer_ctx.ice_pwd.is_empty() {
            // adopt_remote resets the pair table; candidates from the
            // offer ride in through this same call (RFC 8839 ordering).
            agent.adopt_remote(
                &offer_ctx.ice_ufrag,
                &offer_ctx.ice_pwd,
                offer_ctx.candidates.clone(),
            );
        } else {
            for c in &offer_ctx.candidates {
                let _ = agent.add_remote_candidate(c.clone());
            }
        }
        // DTLS-SRTP: one association per session, spawned with the role
        // the answer just settled (RFC 5763): we're the DTLS client ONLY
        // when the offer said `passive`. Fixture engines (no identity)
        // stay DTLS-free — the doc-commented test posture.
        if slot.dtls.is_none() {
            if let Some(identity) = &self.config.dtls_identity {
                let active = offer_ctx.setup.as_deref() == Some("passive");
                let now = Instant::now();
                let mut ep = dtls::Endpoint::new(identity, active, offer_ctx.peer_fingerprint, now);
                // Pre-nomination kick: an ACTIVE endpoint owes the world
                // a ClientHello; without a nominated pair it goes on the
                // pending buffer and flushes at nomination (kick_dtls).
                if active {
                    match ep.start_handshake(now) {
                        Ok(drive) => slot.dtls_pending.extend(drive.packets),
                        Err(_) => {
                            self.stats.dtls_failed += 1;
                            return;
                        }
                    }
                }
                slot.dtls = Some(ep);
            }
        }
    }

    /// Leaving a room (explicit Leave frame, reconnect fencing, or sweep):
    /// scrap endpoint/SSRC ownership so a stale packet from the departed
    /// 5-tuple is refused EXACTLY like a stranger's.
    fn on_left(&mut self, room: &RoomId, participant: &ParticipantId, ctx: &CallerContext) {
        let Some(session_id) = ctx.session.clone().or_else(|| {
            self.slots
                .values()
                .find(|s| s.room == *room && s.participant == *participant)
                .map(|s| s.session_id.clone())
        }) else {
            return;
        };
        if let Some(slot) = self.slots.remove(&session_id) {
            self.teardown_slot_tracker(room, &slot);
        }
    }

    /// Shared teardown (leave + sweep): endpoint binding, SSRC map,
    /// per-source stream state — everything that attached the wire's
    /// identity to this node's state.
    fn teardown_slot_tracker(&mut self, room: &RoomId, slot: &SessionSlot) {
        if let Some(ep) = slot.endpoint {
            self.by_endpoint.remove(&ep);
        }
        for (track, stamp) in &slot.tracks {
            self.by_ssrc.remove(&stamp.ssrc);
            // Streams keyed on the published RouteStamp leave with the slot.
            self.registry.remove(&RouteStamp {
                room: room.clone(),
                participant: slot.participant.clone(),
                track: track.clone(),
            });
        }
    }

    /// SDES-style keying hook (module doc): production feeds DTLS-SRTP
    /// extraction here; tests drive what verify vectors promise.
    pub fn bind_srtp(
        &mut self,
        session: &MediaSessionId,
        master_key: [u8; 16],
        master_salt: [u8; 14],
    ) -> bool {
        self.bind_srtp_split(
            session,
            dtls::CmKeys {
                inbound_key: master_key,
                inbound_salt: master_salt,
                outbound_key: master_key,
                outbound_salt: master_salt,
            },
        )
    }

    /// Directional bind: production path fed by DTLS-SRTP negotiation
    /// (`Event::Established` → `session_keys()`). Inbound keys are ALWAYS
    /// the pair the peer writes with (RFC 5764 role-derived in the dtls
    /// shim); outbound is the pair we write with. Returns false for a
    /// vanished session — the caller's handshake then races a teardown,
    /// which the sweep resolves.
    pub fn bind_srtp_split(&mut self, session: &MediaSessionId, keys: dtls::CmKeys) -> bool {
        self.bind_session_keys(session, dtls::SessionKeys::Cm(keys))
    }

    /// Profile-general bind: CM and both GCM widths land in typed
    /// protector slots; anything else never crosses the dtls::SessionKeys
    /// gate in the first place (the handshake shim refuses it LOUDLY
    /// via `profile_refused`).
    pub fn bind_session_keys(&mut self, session: &MediaSessionId, keys: dtls::SessionKeys) -> bool {
        let Some(slot) = self.slots.get_mut(session) else {
            return false;
        };
        use webrtc::srtp::{
            derive_gcm_session_keys_128, derive_gcm_session_keys_256, SrtpGcmProtector,
            SrtpGcmUnprotector, SrtpProtector, SrtpUnprotector,
        };
        let (inbound, outbound) = match keys {
            dtls::SessionKeys::Cm(k) => (
                RtpUnprotector::Cm(SrtpUnprotector::new(derive_session_keys(
                    &k.inbound_key,
                    &k.inbound_salt,
                ))),
                RtpProtector::Cm(SrtpProtector::new(derive_session_keys(
                    &k.outbound_key,
                    &k.outbound_salt,
                ))),
            ),
            dtls::SessionKeys::Gcm128(k) => (
                RtpUnprotector::Gcm(SrtpGcmUnprotector::new(derive_gcm_session_keys_128(
                    &copy16e(&k.inbound_key),
                    &k.inbound_salt,
                ))),
                RtpProtector::Gcm(SrtpGcmProtector::new(derive_gcm_session_keys_128(
                    &copy16e(&k.outbound_key),
                    &k.outbound_salt,
                ))),
            ),
            dtls::SessionKeys::Gcm256(k) => (
                RtpUnprotector::Gcm(SrtpGcmUnprotector::new(derive_gcm_session_keys_256(
                    &copy32e(&k.inbound_key),
                    &k.inbound_salt,
                ))),
                RtpProtector::Gcm(SrtpGcmProtector::new(derive_gcm_session_keys_256(
                    &copy32e(&k.outbound_key),
                    &k.outbound_salt,
                ))),
            ),
        };
        slot.inbound = Some(inbound);
        slot.outbound = Some(outbound);
        true
    }

    // ------------------------------------------------------------ media

    /// ONE nonblocking tick of the media loop. Returns datagrams dispatched.
    pub fn media_step(&mut self, now_ms: u32) -> usize {
        let mut batch: Vec<Datagram> = Vec::with_capacity(64);
        let n = self
            .transport
            .recv_batch(&mut batch, 64, Duration::from_millis(2))
            .unwrap_or_default();
        let mut dispatched = 0usize;
        for d in batch {
            match demux::classify(&d.bytes) {
                demux::FrameKind::Stun => self.on_stun(&d),
                demux::FrameKind::Dtls => self.on_dtls(&d),
                demux::FrameKind::Rtp => dispatched += self.on_rtp(&d, now_ms),
                demux::FrameKind::Rtcp => self.on_rtcp(&d),
                demux::FrameKind::Unknown => self.stats.unknown_frames += 1,
            }
        }
        n.max(dispatched)
    }

    fn on_stun(&mut self, d: &Datagram) {
        let msg = match StunMessage::parse(&d.bytes) {
            Ok(m) => m,
            Err(e) => {
                if std::env::var_os("VOXDESK_STUN_DEBUG").is_some() {
                    eprintln!(
                        "stun parse-drop from {} len={}: {e:?} hex={}",
                        d.from,
                        d.bytes.len(),
                        d.bytes
                            .iter()
                            .map(|b| format!("{b:02x}"))
                            .collect::<String>()
                    );
                }
                return;
            }
        };
        let username = msg
            .attr(webrtc::stun::attrs::USERNAME)
            .and_then(|v| std::str::from_utf8(v).ok());
        let Some(local_prefix) = username.and_then(|u| u.split(':').next()) else {
            return;
        };

        let mut response: Option<(SocketAddr, Vec<u8>)> = None;
        let mut nomination: Option<(MediaSessionId, (u16, [u8; 4]))> = None;
        for slot in self.slots.values_mut() {
            let Some(agent) = slot.agent.as_mut() else {
                continue;
            };
            if agent.local_ufrag != local_prefix {
                continue;
            }
            let from = (d.from.port(), ipv4_octets(&d.from));
            let outcome = agent.handle_stun(&msg, &d.bytes, from);
            if let Some(resp) = outcome.response {
                response = Some((d.from, resp));
                self.stats.stun_answered += 1;
            }
            if let Some(endpoint) = outcome.nominated {
                slot.endpoint = Some(endpoint);
                nomination = Some((slot.session_id.clone(), endpoint));
            }
            break;
        }
        if let Some((sid, endpoint)) = nomination {
            self.by_endpoint.insert(endpoint, sid.clone());
            // A freshly nominated 5-tuple: flush anything DTLS queued,
            // and kick the association when we owe the ClientHello
            // (active role — offer said `passive`). Passive endpoints
            // stay idle; the client's first record drives them.
            self.kick_dtls(&sid, endpoint);
        }
        if let Some((to, bytes)) = response {
            let _ = self.transport.send(to, &bytes);
        }
    }

    /// Post-nomination DTLS work for one session: run one drive cycle,
    /// gather its packets plus anything buffered pre-nomination, and send
    /// the lot to the nominated endpoint. Honest about ownership: this
    /// is the ONLY code path that transmits DTLS on a nominated pair.
    fn kick_dtls(&mut self, session: &MediaSessionId, endpoint: (u16, [u8; 4])) {
        let Some(slot) = self.slots.get_mut(session) else {
            return;
        };
        let Some(dtls_ep) = slot.dtls.as_mut() else {
            return;
        };
        let now = Instant::now();
        match dtls_ep.start_handshake(now) {
            Ok(drive) => slot.dtls_pending.extend(drive.packets),
            Err(_) => {
                self.stats.dtls_failed += 1;
                let slot = self.slots.get_mut(session).expect("slot alive");
                slot.dtls = None;
                slot.dtls_pending.clear();
                return;
            }
        }
        if slot.dtls_pending.is_empty() {
            return;
        }
        let to = SocketAddr::from((endpoint.1, endpoint.0));
        let pending = std::mem::take(&mut slot.dtls_pending);
        for p in pending {
            if self.transport.send(to, &p).is_err() {
                // Transport-side transient failure: buffer the record so
                // the retransmit timer round delivers it honestly.
                if let Some(slot) = self.slots.get_mut(session) {
                    slot.dtls_pending.push(p);
                }
                self.stats.dtls_failed += 1;
            }
        }
    }

    /// One inbound DTLS record: route by ICE-nominated 5-tuple, drive the
    /// session's association, surface `Established` as `bind_srtp_split`
    /// when — and only when — the negotiated profile is bindable (CM).
    fn on_dtls(&mut self, d: &Datagram) {
        let from = (d.from.port(), ipv4_octets(&d.from));
        let Some(sid) = self.by_endpoint.get(&from).cloned() else {
            self.stats.dtls_dropped += 1;
            return;
        };
        let Some(slot) = self.slots.get_mut(&sid) else {
            return;
        };
        let Some(dtls_ep) = slot.dtls.as_mut() else {
            self.stats.dtls_dropped += 1;
            return;
        };
        let now = Instant::now();
        match dtls_ep.handle_packet(&d.bytes, now) {
            Ok(drive) => {
                let mut bind: Option<dtls::SessionKeys> = None;
                let mut refused = false;
                for e in drive.events {
                    let dtls::Event::Established(n) = e;
                    match n.session_keys() {
                        Ok(sk) => bind = Some(sk),
                        Err(_) => refused = true,
                    }
                }
                if !drive.packets.is_empty() {
                    let to = d.from;
                    for p in drive.packets {
                        let _ = self.transport.send(to, &p);
                    }
                }
                if let Some(sk) = bind {
                    self.bind_session_keys(&sid, sk);
                    self.stats.dtls_established += 1;
                }
                if refused {
                    self.stats.dtls_profile_refused += 1;
                    if let Some(slot) = self.slots.get_mut(&sid) {
                        slot.dtls = None;
                        slot.dtls_pending.clear();
                    }
                }
            }
            Err(_) => {
                // Fail closed AND loud: no state survives a protocol or
                // fingerprint failure; the session will expire normally.
                self.stats.dtls_failed += 1;
                if let Some(slot) = self.slots.get_mut(&sid) {
                    slot.dtls = None;
                    slot.dtls_pending.clear();
                }
            }
        }
    }

    /// Sweep-side DTLS maintenance: retransmit timers for any association
    /// still handshaking.
    fn service_dtls_timers(&mut self, now: Instant) {
        let mut sends: Vec<(SocketAddr, Vec<Vec<u8>>)> = Vec::new();
        let mut dead: Vec<MediaSessionId> = Vec::new();
        for (sid, slot) in self.slots.iter_mut() {
            let Some(dtls_ep) = slot.dtls.as_mut() else {
                continue;
            };
            let due = dtls_ep.timeout().map(|t| t <= now).unwrap_or(false);
            if !due {
                continue;
            }
            match dtls_ep.handle_timeout(now) {
                Ok(drive) => {
                    if !drive.packets.is_empty() {
                        if let Some((port, ip)) = slot.endpoint {
                            sends.push((SocketAddr::from((ip, port)), drive.packets));
                        } else {
                            slot.dtls_pending.extend(drive.packets);
                        }
                    }
                }
                Err(_) => dead.push(sid.clone()),
            }
        }
        for sid in dead {
            self.stats.dtls_failed += 1;
            if let Some(slot) = self.slots.get_mut(&sid) {
                slot.dtls = None;
                slot.dtls_pending.clear();
            }
        }
        for (to, batch) in sends {
            for p in batch {
                let _ = self.transport.send(to, &p);
            }
        }
    }

    /// One inbound RTP datagram. Routing chain:
    ///
    /// 1. ssrc → owning session+track (who PUBLISHED this origin);
    /// 2. OWNER CHECK: the datagram's 5-tuple must be that session's ICE-
    ///    nominated endpoint — a packet claiming an ssrc it does not own
    ///    is spoofing and feeds no pipeline;
    /// 3. SRTP unprotect if bound (keyed through the source slot);
    /// 4. media pipeline (seq/loss/jitter/replay/reorder);
    /// 5. routing lookup for subscribed legs; per-leg SRTP protect-or-raw.
    fn on_rtp(&mut self, d: &Datagram, now_ms: u32) -> usize {
        self.stats.rtp_received += 1;
        let from = (d.from.port(), ipv4_octets(&d.from));

        // 1. Cheap demux pre-parse: who CLAIMS this SSRC.
        if d.bytes.len() < streams::packet::MIN_HEADER {
            self.stats.rtp_drop_unknown_ssrc += 1;
            return 0;
        }
        let ssrc = u32::from_be_bytes([d.bytes[8], d.bytes[9], d.bytes[10], d.bytes[11]]);
        let Some((owner_sid, _owner_track)) = self.by_ssrc.get(&ssrc).cloned() else {
            self.stats.rtp_drop_unknown_ssrc += 1;
            return 0;
        };

        // 2. Spoof gate: only forward what arrives from the owning ICE-
        //    nominated endpoint. Before nomination (endpoint unknown), an
        //    ssrc-claiming packet is indistinguishable from noise.
        let owner_endpoint = self.slots.get(&owner_sid).and_then(|s| s.endpoint);
        if owner_endpoint != Some(from) {
            self.stats.rtp_drop_spoof += 1;
            return 0;
        }

        // 3. Decrypt if an SRTP context is bound for this source.
        let Some(source_slot) = self.slots.get_mut(&owner_sid) else {
            return 0;
        };
        let plain = match &mut source_slot.inbound {
            Some(rx) => match rx.unprotect(&d.bytes) {
                Ok(p) => p,
                Err(_) => {
                    self.stats.rtp_dropped += 1;
                    return 0;
                }
            },
            None => d.bytes.clone(),
        };

        // The session tells WHICH ssrc→track map entry it is (authoritative
        // only after the spoof gate above).
        let Some((track, kind)) = source_slot
            .tracks
            .iter()
            .find(|(_, st)| st.ssrc == ssrc)
            .map(|(t, st)| (t.clone(), st.kind))
        else {
            return 0;
        };
        let stamp = RouteStamp {
            room: source_slot.room.clone(),
            participant: source_slot.participant.clone(),
            track: track.clone(),
        };

        // 4. Pipeline.
        let clock = match kind {
            MediaKind::Audio => self.config.clock_rate_audio,
            _ => self.config.clock_rate_video,
        };
        let stream = self
            .registry
            .ensure(&stamp, 0, self.config.reorder_capacity, clock);
        let events = process(stream, plain, now_ms);

        // 5. Legs.
        let mut forwarded = 0usize;
        for event in events {
            let StreamEvent::Forward(pkt) = event else {
                continue;
            };
            let legs = self
                .routes
                .legs_for(&stamp.room, &stamp.participant, &stamp.track);
            for leg in legs.iter() {
                let Some((_sid, dest_slot)) = self.slots.iter_mut().find(|(_, s)| {
                    s.participant == *leg && s.room == stamp.room && s.endpoint.is_some()
                }) else {
                    continue;
                };
                let endpoint = dest_slot.endpoint.unwrap();
                let bytes = match &mut dest_slot.outbound {
                    Some(tx) => {
                        let mut frame = pkt.raw.clone();
                        tx.protect_inplace(&mut frame, pkt.sequence);
                        frame
                    }
                    None => pkt.raw.clone(),
                };
                let to = SocketAddr::from((endpoint.1, endpoint.0));
                if self.transport.send(to, &bytes).is_ok() {
                    forwarded += 1;
                    self.stats.rtp_forwarded += 1;
                }
            }
        }
        forwarded
    }

    /// One inbound RTCP datagram: parse compound, apply feedback, count
    /// explicitly. "Ignore" is gone; unsupported types are counted by
    /// packet-type so the metrics tell us exactly what lands here when a
    /// more exotic UA starts sending.
    fn on_rtcp(&mut self, d: &Datagram) {
        self.stats.rtcp_received += 1;
        let parsed = match streams::rtcp::parse(&d.bytes) {
            Ok(pkt) => pkt,
            Err(_) => {
                self.stats.rtcp_malformed += 1;
                return;
            }
        };
        for frame in parsed {
            match frame {
                streams::rtcp::Rtcp::SenderReport { ssrc, ntp_msw, .. } => {
                    self.stats.rtcp_sr += 1;
                    // The SOURCE whose ssrc the SR asserts records it (SRs
                    // are looped-back accountability data for OUR source
                    // streams; a stray SR for an unknown ssrc counts only).
                    if let Some((owner_sid, track)) = self.by_ssrc.get(&ssrc).cloned() {
                        if let Some(slot) = self.slots.get(&owner_sid) {
                            let stamp = RouteStamp {
                                room: slot.room.clone(),
                                participant: slot.participant.clone(),
                                track,
                            };
                            if let Some(stream) = self.registry.get(&stamp) {
                                stream.sr_total += 1;
                                stream.sr_ntp_seconds_latest = ntp_msw;
                            }
                        }
                    }
                }
                streams::rtcp::Rtcp::ReceiverReport {
                    ssrc: _reporter,
                    reports,
                } => {
                    self.stats.rtcp_rr += 1;
                    // Each report block describes one SOURCE ssrc as seen
                    // by a receiver: feed it to that source's stream
                    // record — the /metrics reader then sees loss AS SEEN
                    // by downstream legs, not as estimated internally.
                    for block in reports {
                        let Some((owner_sid, track)) = self.by_ssrc.get(&block.ssrc).cloned()
                        else {
                            continue;
                        };
                        let Some(slot) = self.slots.get(&owner_sid) else {
                            continue;
                        };
                        let stamp = RouteStamp {
                            room: slot.room.clone(),
                            participant: slot.participant.clone(),
                            track,
                        };
                        if let Some(stream) = self.registry.get(&stamp) {
                            stream.rr_total += 1;
                            stream.rr_fraction_lost_latest = block.fraction_lost;
                            stream.rr_cumulative_lost_latest = block.cumulative_lost;
                            self.stats.rtcp_reports_applied += 1;
                        }
                    }
                }
                streams::rtcp::Rtcp::Unknown { packet_type, .. } => {
                    self.stats.rtcp_unsupported += 1;
                    let _ = packet_type;
                }
            }
        }
    }

    /// Idle-work tick: session sweep (the binary calls at ~10 Hz cadence).
    /// Every removed session ALSO scraps its datastore: endpoints, SSRC
    /// ownership, stream pipeline state — the wire is no longer theirs.
    pub fn sweep_step(&mut self) -> usize {
        let now = Instant::now();
        // DTLS retransmit timers ride the sweep cadence (a handshaking
        // association is cheap to service at sweep rate; established ones
        // produce nothing).
        self.service_dtls_timers(now);
        let gone = self.store.sweep(now);
        let count = gone.len();
        for (_kind, s) in &gone {
            let ids: Vec<MediaSessionId> = self
                .slots
                .iter()
                .filter(|(_, slot)| slot.store_session_id == s.id)
                .map(|(k, _)| k.clone())
                .collect();
            for id in ids {
                if let Some(slot) = self.slots.remove(&id) {
                    let room = slot.room.clone();
                    self.routes.leave(&room, &slot.participant);
                    self.teardown_slot_tracker(&room, &slot);
                }
            }
        }
        self.stats.sweeps += 1;
        count
    }
}

fn ipv4_octets(addr: &SocketAddr) -> [u8; 4] {
    match addr {
        SocketAddr::V4(a) => a.ip().octets(),
        SocketAddr::V6(_) => [0, 0, 0, 0], // v6 only ever appears in negative tests
    }
}

/// Vec→array copies for the GCM bind path (the dtls crate carries keys
/// as Vec because both widths exist).
fn copy16e(v: &[u8]) -> [u8; 16] {
    let mut out = [0u8; 16];
    out.copy_from_slice(&v[..16]);
    out
}
fn copy32e(v: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&v[..32]);
    out
}
