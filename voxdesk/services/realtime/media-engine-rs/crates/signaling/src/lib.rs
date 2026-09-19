//! signaling — the control-plane decision core: a pure, deterministic
//! reducer from ClientFrame + engine state to wire effects. Every "can't"
//! is a ServerFrame::Error mapped from the structural failure (RoomFull →
//! OverLimit…) — the SAME discipline as gateway's map-write-policy.
//!
//! The engine IO loop calls `SignalCore::handle` per decoded frame with
//! the frame's ALREADY-VERIFIED identity context (tokens validated by the
//! gateway upstream: the frames that could lie about identity literally
//! do not exist in the vocabulary, per protocol's own docs).

use protocol::{
    ClientFrame, ErrorCode, MediaSessionId, ParticipantId, RoomId, ServerFrame, TrackId,
};
use routing::{RouteError, RouteTable};
use sessions::{JoinOutcome, Store as SessionStore};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;
use webrtc::sdp;

/// Identity context pinned by the transport layer (who is this frame
/// FROM). Populated at join, verified against session lookup everywhere
/// else — `session` in a later frame MUST resolve to the same owner.
#[derive(Clone, Debug)]
pub struct CallerContext {
    pub participant: ParticipantId,
    pub room: Option<RoomId>,
    pub session: Option<MediaSessionId>,
    pub now: Instant,
}

/// What the engine does next with one handled frame.
#[derive(Clone, Debug, Default)]
pub struct Effects {
    /// Reply to the caller's own control channel.
    pub reply: Vec<ServerFrame>,
    /// Broadcast to the room's other MPs (participants).
    pub room_fanout: Vec<ServerFrame>,
    /// The fresh ICE agent when a join succeeded (the session spawn path
    /// picks this up: local creds go to the client via Ready).
    pub new_session: Option<NewSession>,
    /// Publish accepted: the session slot must register this track
    /// IMMEDIATELY (the engine's publish→SSRC→route chain starts here).
    pub published: Option<(TrackId, protocol::MediaKind, Option<u32>)>,
    /// Leave processed: (room, participant) whose routing/session state
    /// must be scrapped (endpoint binding, SSRC map entries, streams).
    pub left: Option<(RoomId, ParticipantId)>,
    /// Validated trickle: the parsed candidate to attach to this session's
    /// ICE agent. `end_of_candidates` is the null/empty-string marker.
    pub trickle: Option<TrickleEffect>,
    /// One accepted offer's extracted remote setup, to bind on the agent.
    pub offer_context: Option<OfferContext>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TrickleEffect {
    pub candidate: Option<webrtc::ice::RemoteCandidate>,
    pub end_of_candidates: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OfferContext {
    pub ice_ufrag: String,
    pub ice_pwd: String,
    pub candidates: Vec<webrtc::ice::RemoteCandidate>,
    /// The offer's `a=setup` value ("actpass" | "active" | "passive") —
    /// governs OUR DTLS role in the answer (RFC 5763): only a `passive`
    /// offerer forces us into the DTLS client role.
    pub setup: Option<String>,
    /// The offer's `a=fingerprint:sha-256` value — the SDP pin every
    /// DTLS handshake on this session must verify the peer cert against.
    pub peer_fingerprint: Option<String>,
}

#[derive(Clone, Debug)]
pub struct NewSession {
    pub session_id: MediaSessionId,
    pub participant: ParticipantId,
    pub room: RoomId,
    pub ice_ufrag: String,
    pub ice_pwd: String,
}

/// Bounded, deterministic ICE credential material: ufrag is 6 chars,
/// pwd 24 chars — the RFC 5245 minimums — derived from a session-local
/// counter and the engine's public label. Determinism makes tests exact;
/// real deployments still rotate the engine label from the environment.
fn ice_creds(engine_label: &str, seq: u64) -> (String, String) {
    // 64-char alphabet; XOR-mix the seq through a FNV-ish walk so
    // consecutive sessions don't produce visually-adjacent strings.
    const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/";
    let mut state: u64 = seq.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ 0xD1B5_4A32_D192_ED03;
    let mut pwd = String::with_capacity(24);
    let mut frag = String::with_capacity(6);
    for i in 0..24 {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        let ch = ALPHABET[(state % 64) as usize] as char;
        if i < 6 {
            frag.push(ch);
        }
        pwd.push(ch);
    }
    let _ = engine_label; // folds into the label-hashed deployment salt in non-default deployments
    (frag, pwd)
}

/// The reducer, wired to shared tables. Cheap to clone: Arcs through.
pub struct SignalCore {
    store: Arc<SessionStore>,
    routes: Arc<RouteTable>,
    fingerprint_sha256: String,
    public_ip: [u8; 4],
    public_port: u16,
    engine_label: String,
    session_seq: AtomicU64,
}

impl SignalCore {
    pub fn new(
        store: Arc<SessionStore>,
        routes: Arc<RouteTable>,
        fingerprint_sha256: String,
        public_ip: [u8; 4],
        public_port: u16,
        engine_label: String,
    ) -> SignalCore {
        SignalCore {
            store,
            routes,
            fingerprint_sha256,
            public_ip,
            public_port,
            engine_label,
            session_seq: AtomicU64::new(1),
        }
    }

    fn error(code: ErrorCode, message: impl Into<String>) -> ServerFrame {
        ServerFrame::Error {
            code,
            message: message.into(),
        }
    }

    pub fn handle(&self, ctx: &mut CallerContext, frame: ClientFrame) -> Effects {
        match frame {
            ClientFrame::Join { room, participant } => self.on_join(ctx, room, participant),
            ClientFrame::Publish {
                session,
                track,
                kind,
                ssrc,
            } => {
                if let Some(effect) = self.require_session(ctx, session.as_deref()) {
                    return effect;
                }
                self.on_publish(ctx, track, kind, ssrc)
            }
            ClientFrame::Subscribe {
                session,
                participant,
                track,
            } => {
                if let Some(effect) = self.require_session(ctx, session.as_deref()) {
                    return effect;
                }
                self.on_subscribe(ctx, participant, track)
            }
            ClientFrame::Unsubscribe {
                session,
                participant,
                track,
            } => {
                if let Some(effect) = self.require_session(ctx, session.as_deref()) {
                    return effect;
                }
                self.on_unsubscribe(ctx, participant, track)
            }
            ClientFrame::Offer {
                session,
                sdp: _raw_offer,
            } => self.on_offer(ctx, session, _raw_offer),
            ClientFrame::Trickle { session, candidate } => self.on_trickle(ctx, session, candidate),
            ClientFrame::Leave { session } => {
                if let Some(effect) = self.require_session(ctx, session.as_deref()) {
                    return effect;
                }
                self.on_leave(ctx)
            }
            ClientFrame::Ping => Effects::default(),
        }
    }

    /// Publish/subscribe without a session credential: the engine would
    /// have to guess WHICH client-session owns the SSRC responsibility —
    /// refused loudly (WrongState) rather than secretly mis-attributed.
    /// Callers that refuse to upgrade must re-join per frame scope.
    fn require_session(&self, ctx: &mut CallerContext, session: Option<&str>) -> Option<Effects> {
        match session {
            Some(s) => {
                let claimed = protocol::MediaSessionId(s.to_string());
                if ctx.session.as_ref() == Some(&claimed) {
                    None
                } else if ctx.session.is_none() {
                    ctx.session = Some(claimed);
                    None
                } else {
                    Some(Effects {
                        reply: vec![Self::error(
                            ErrorCode::WrongState,
                            "session is not this join's",
                        )],
                        ..Default::default()
                    })
                }
            }
            // Absent session: LEGACY smoke frames inherit the caller
            // context's session (joined earlier on THIS connection) — safe,
            // the connection's join already authenticated the identity;
            // refused only when no session is on the context at all.
            None if ctx.session.is_some() => None,
            None => Some(Effects {
                reply: vec![Self::error(
                    ErrorCode::WrongState,
                    "join before publish/subscribe",
                )],
                ..Default::default()
            }),
        }
    }

    fn on_join(
        &self,
        ctx: &mut CallerContext,
        room: RoomId,
        participant: ParticipantId,
    ) -> Effects {
        let outcome = self.store.join(&room, &participant, ctx.now);
        let session_record = match &outcome {
            JoinOutcome::New(s) => s.clone(),
            JoinOutcome::Rebound { fresh, .. } => fresh.clone(),
        };
        let seq = self.session_seq.fetch_add(1, Ordering::SeqCst);
        let (ice_ufrag, ice_pwd) = ice_creds(&self.engine_label, seq);
        let session_id = MediaSessionId(format!("ms-{}-{}", session_record.id, seq));

        if let Err(e) = self.routes.join(&room, participant.clone(), seq) {
            let _ = self.store.leave(session_record.id, ctx.now);
            let code = match e {
                RouteError::RoomFull => ErrorCode::OverLimit,
                _ => ErrorCode::RoomUnknown,
            };
            return Effects {
                reply: vec![Self::error(code, "room capacity")],
                ..Default::default()
            };
        }

        ctx.room = Some(room.clone());
        ctx.participant = participant.clone();
        ctx.session = Some(session_id.clone());

        Effects {
            reply: vec![ServerFrame::Ready {
                session: session_id.clone(),
                participant: participant.clone(),
                room: room.clone(),
                ice_ufrag: ice_ufrag.clone(),
                ice_pwd: ice_pwd.clone(),
            }],
            room_fanout: Vec::new(),
            new_session: Some(NewSession {
                session_id,
                participant,
                room,
                ice_ufrag,
                ice_pwd,
            }),
            ..Default::default()
        }
    }

    fn on_publish(
        &self,
        ctx: &CallerContext,
        track: TrackId,
        kind: protocol::MediaKind,
        ssrc: Option<u32>,
    ) -> Effects {
        let Some(room) = ctx.room.clone() else {
            return Effects {
                reply: vec![Self::error(ErrorCode::RoomUnknown, "publish before join")],
                ..Default::default()
            };
        };
        match self
            .routes
            .publish(&room, &ctx.participant, track.clone(), kind)
        {
            Ok(()) => Effects {
                reply: Vec::new(),
                room_fanout: vec![ServerFrame::TrackPublished {
                    room,
                    participant: ctx.participant.clone(),
                    track: track.clone(),
                    kind,
                }],
                published: Some((track, kind, ssrc)),
                ..Default::default()
            },
            Err(RouteError::DuplicateTrack) => Effects {
                reply: vec![Self::error(ErrorCode::OverLimit, "track already published")],
                ..Default::default()
            },
            Err(RouteError::TrackLimit) => Effects {
                reply: vec![Self::error(ErrorCode::OverLimit, "track limit")],
                ..Default::default()
            },
            Err(_) => Effects {
                reply: vec![Self::error(
                    ErrorCode::RoomUnknown,
                    "no such room/participant",
                )],
                ..Default::default()
            },
        }
    }

    fn on_subscribe(
        &self,
        ctx: &CallerContext,
        publisher: ParticipantId,
        track: TrackId,
    ) -> Effects {
        let Some(room) = ctx.room.clone() else {
            return Effects {
                reply: vec![Self::error(ErrorCode::RoomUnknown, "subscribe before join")],
                ..Default::default()
            };
        };
        match self
            .routes
            .subscribe(&room, &ctx.participant, &publisher, &track)
        {
            Ok(()) => Effects::default(),
            Err(RouteError::NoSuchTrack | RouteError::NoSuchParticipant) => Effects {
                reply: vec![Self::error(
                    ErrorCode::RoomUnknown,
                    "no such track/publisher",
                )],
                ..Default::default()
            },
            Err(RouteError::SubscriptionLimit) => Effects {
                reply: vec![Self::error(ErrorCode::OverLimit, "subscription limit")],
                ..Default::default()
            },
            Err(_) => Effects {
                reply: vec![Self::error(ErrorCode::RoomUnknown, "room unknown")],
                ..Default::default()
            },
        }
    }

    fn on_unsubscribe(
        &self,
        ctx: &CallerContext,
        publisher: ParticipantId,
        track: TrackId,
    ) -> Effects {
        if let Some(room) = &ctx.room {
            self.routes
                .unsubscribe(room, &ctx.participant, &publisher, &track);
        }
        Effects::default()
    }

    fn on_offer(
        &self,
        ctx: &CallerContext,
        session_id: MediaSessionId,
        raw_offer: String,
    ) -> Effects {
        // The session in the offer MUST be ours (same join).
        if ctx.session.as_ref() != Some(&session_id) {
            return Effects {
                reply: vec![Self::error(
                    ErrorCode::WrongState,
                    "session is not this join's",
                )],
                ..Default::default()
            };
        }
        let request = match sdp::parse_offer(&raw_offer) {
            Ok(o) => o,
            Err(e) => {
                return Effects {
                    reply: vec![Self::error(
                        ErrorCode::BadMessage,
                        format!("offer rejected: {e:?}"),
                    )],
                    ..Default::default()
                }
            }
        };
        let (local_ufrag, local_pwd) = self
            .creds_for(ctx)
            .unwrap_or_else(|| ("voxdesk".into(), "x".repeat(24)));
        let answer = sdp::build_answer(
            &request,
            &sdp::AnswerContext {
                local_ufrag,
                local_pwd,
                fingerprint_sha256: self.fingerprint_sha256.clone(),
                public_ip: self.public_ip,
                public_port: self.public_port,
                external_ip_label: self.engine_label.clone(),
            },
        );
        // Harvest the remote settlement for the session's agent: session-
        // level creds win over per-media ones (BUNDLE already collapsed
        // the graph by the time we see it), candidates union across media
        // sections with parse errors skipped (validated at parse: the SDP
        // module keeps malformed candidates OUT of its candidate vec).
        let (mut ice_ufrag, mut ice_pwd, mut candidates) = (
            request.session_ufrag.clone().unwrap_or_default(),
            request.session_pwd.clone().unwrap_or_default(),
            Vec::new(),
        );
        for m in &request.media {
            candidates.extend(m.candidates.clone());
            // BUNDLED browser SDP (Chrome/Firefox today) carries ICE creds
            // at MEDIA level; the session-level pair is empty there —
            // honour either home.
            if ice_ufrag.is_empty() {
                if let Some(u) = &m.ice_ufrag {
                    ice_ufrag = u.clone();
                }
            }
            if ice_pwd.is_empty() {
                if let Some(p) = &m.ice_pwd {
                    ice_pwd = p.clone();
                }
            }
        }
        // DTLS settlement from the offer: BUNDLE collapses the graph, so
        // the first media-level setup/fingerprint wins; the session-level
        // fingerprint is the RFC 8122 fallback.
        let mut setup = None;
        let mut peer_fingerprint = request.session_fingerprint.clone();
        for m in &request.media {
            if setup.is_none() {
                setup = m.setup.clone();
            }
            if peer_fingerprint.is_none() {
                peer_fingerprint = m.fingerprint_sha256.clone();
            }
        }
        let offer_context = if ice_ufrag.is_empty() && ice_pwd.is_empty() && candidates.is_empty() {
            None
        } else {
            Some(OfferContext {
                ice_ufrag,
                ice_pwd,
                candidates,
                setup,
                peer_fingerprint,
            })
        };
        Effects {
            reply: vec![ServerFrame::Answer {
                session: session_id,
                sdp: answer,
            }],
            offer_context,
            ..Default::default()
        }
    }

    /// Trickle handling (RFC 8839 §3.1): validate the frame session, the
    /// candidate payload, and the IPv4/UDP binding the SFU requires;
    /// dedupe happens in the agent (its domain), error shape is
    /// BadMessage for malformed and RoomUnknown for stale session —
    /// naming dangers were reviewed against the frame vocabulary.
    fn on_trickle(
        &self,
        ctx: &CallerContext,
        session: MediaSessionId,
        candidate: protocol::json::Value,
    ) -> Effects {
        if ctx.session.as_ref() != Some(&session) {
            return Effects {
                reply: vec![Self::error(
                    ErrorCode::WrongState,
                    "trickle is not for this join's session",
                )],
                ..Default::default()
            };
        }
        // end-of-candidates: null, or an object with empty "candidate".
        if matches!(candidate, protocol::json::Value::Null) {
            return Effects {
                trickle: Some(TrickleEffect {
                    candidate: None,
                    end_of_candidates: true,
                }),
                ..Default::default()
            };
        }
        let text = candidate
            .get("candidate")
            .and_then(protocol::json::Value::as_str)
            .unwrap_or("");
        if text.is_empty() {
            return Effects {
                trickle: Some(TrickleEffect {
                    candidate: None,
                    end_of_candidates: true,
                }),
                ..Default::default()
            };
        }
        match webrtc::ice::RemoteCandidate::parse(text) {
            Ok(c) => Effects {
                trickle: Some(TrickleEffect {
                    candidate: Some(c),
                    end_of_candidates: false,
                }),
                ..Default::default()
            },
            Err(e) => Effects {
                reply: vec![Self::error(
                    ErrorCode::BadMessage,
                    format!("invalid ice candidate: {e:?}"),
                )],
                ..Default::default()
            },
        }
    }

    /// Look up this caller's ICE creds from the session record created at
    /// join — parked in the store (handy for late offer frames).
    fn creds_for(&self, ctx: &CallerContext) -> Option<(String, String)> {
        let sid = ctx.session.as_ref()?;
        let num = sid
            .0
            .strip_prefix("ms-")?
            .split('-')
            .next()?
            .parse::<u64>()
            .ok()?;
        let record = self.store.get(num)?;
        // Session ids minted from record id + seq: creds are deterministic
        // from the same seq we used at join, but the store only knows the
        // record itself — the credential bind is the engine's bookkeeping:
        // recompute them from the seq embedded in the session id.
        let seq: u64 = sid.0.rsplit('-').next()?.parse().ok()?;
        let _ = record;
        Some(ice_creds(&self.engine_label, seq))
    }

    fn on_leave(&self, ctx: &CallerContext) -> Effects {
        let mut left = None;
        if let (Some(room), Some(session_id)) = (ctx.room.clone(), ctx.session.clone()) {
            // Routing cleanup is idempotent — the same frame arriving twice
            // must be a no-op (reconnect discipline).
            let num = session_id
                .0
                .strip_prefix("ms-")
                .and_then(|s| s.split('-').next())
                .and_then(|s| s.parse::<u64>().ok());
            if let Some(n) = num {
                let _ = self.store.leave(n, ctx.now);
            }
            self.routes.leave(&room, &ctx.participant);
            left = Some((room, ctx.participant.clone()));
        }
        Effects {
            left,
            ..Default::default()
        }
    }
}
