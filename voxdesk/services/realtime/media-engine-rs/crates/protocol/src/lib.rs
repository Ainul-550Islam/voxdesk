//! protocol — the media engine's wire vocabulary.
//!
//! Two layers, one crate:
//!
//! * [`json`]: the dependency-free JSON codec every control message
//!   serializes through (and the one the `livekit` grant bags reuse).
//! * [`frame`]: the media control-plane frames themselves — the small,
//!   closed set of messages a browser/SDK and this engine exchange while
//!   negotiating media (join/publish/subscribe/offer/answer/trickle/leave),
//!   mirroring gateway-go's protocol discipline: a closed vocabulary,
//!   explicit refused-vs-invalid error codes, no client-asserted identity
//!   fields anywhere security hangs on.

/// Go↔Rust control-plane contract version. Bump ONLY for breaking
/// changes; additive JSON fields do not count (every consumer tolerates
/// unknown fields by contract).
pub const WIRE_VERSION: u32 = 1;

pub mod json;

use json::Value;

/// Newtypes over the string identifiers that flow through the system.
/// These exist for the same reason gateway-go validates UUIDs at the edge:
/// "room" and "track" must never be confusable positions in a function
/// signature, and a wrong-typed id is a compile error, not a prod bug.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RoomId(pub String);

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TrackId(pub String);

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ParticipantId(pub String);

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct MediaSessionId(pub String);

impl std::fmt::Display for RoomId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::fmt::Display for TrackId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::fmt::Display for ParticipantId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::fmt::Display for MediaSessionId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Media kinds the engine forwards. Kept exhaustive-encodable so an
/// unknown kind is a protocol error at decode time, not a silent passthrough.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum MediaKind {
    Audio,
    Video,
}

impl MediaKind {
    pub fn as_str(self) -> &'static str {
        match self {
            MediaKind::Audio => "audio",
            MediaKind::Video => "video",
        }
    }

    pub fn parse(raw: &str) -> Option<MediaKind> {
        match raw {
            "audio" => Some(MediaKind::Audio),
            "video" => Some(MediaKind::Video),
            _ => None,
        }
    }
}

/// Error codes on the refusal wire — the closed set a client switch can
/// exhaustively handle (same discipline as gateway-go's protocol/error.go).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorCode {
    BadMessage,
    AuthFailed,
    RoomUnknown,
    TrackUnknown,
    OverLimit,
    WrongState,
    ServerBusy,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::BadMessage => "bad_message",
            ErrorCode::AuthFailed => "auth_failed",
            ErrorCode::RoomUnknown => "room_unknown",
            ErrorCode::TrackUnknown => "track_unknown",
            ErrorCode::OverLimit => "over_limit",
            ErrorCode::WrongState => "wrong_state",
            ErrorCode::ServerBusy => "server_busy",
        }
    }

    pub fn parse(raw: &str) -> Option<ErrorCode> {
        Some(match raw {
            "bad_message" => ErrorCode::BadMessage,
            "auth_failed" => ErrorCode::AuthFailed,
            "room_unknown" => ErrorCode::RoomUnknown,
            "track_unknown" => ErrorCode::TrackUnknown,
            "over_limit" => ErrorCode::OverLimit,
            "wrong_state" => ErrorCode::WrongState,
            "server_busy" => ErrorCode::ServerBusy,
            _ => return None,
        })
    }
}

/// Control frames the ENGINE sends to a connected client.
#[derive(Clone, Debug, PartialEq)]
pub enum ServerFrame {
    /// Session pinned to a participant; the client may now publish/subscribe.
    Ready {
        session: MediaSessionId,
        participant: ParticipantId,
        room: RoomId,
        /// ICE-lite credentials the client must use for its DTLS handshake.
        ice_ufrag: String,
        ice_pwd: String,
    },
    /// A peer published a track the client subscribes (or now can).
    TrackPublished {
        room: RoomId,
        participant: ParticipantId,
        track: TrackId,
        kind: MediaKind,
    },
    TrackUnpublished {
        room: RoomId,
        participant: ParticipantId,
        track: TrackId,
    },
    /// SDP negotiation pass (the engine is ICE-lite: it always answers).
    Answer {
        session: MediaSessionId,
        sdp: String,
    },
    /// A relayed ICE candidate from the engine (host candidate of this node).
    Trickle {
        session: MediaSessionId,
        candidate: Value,
    },
    Error {
        code: ErrorCode,
        message: String,
    },
}

/// Control frames the engine ACCEPTS from a client. Note what is absent:
/// no tenant, no identity claim — the frames that could lie about identity
/// literally do not exist in this vocabulary (auth happened at the edge).
#[derive(Clone, Debug, PartialEq)]
pub enum ClientFrame {
    /// Join after token verification upstream; carries the grant-derived
    /// room+participant the edge ALREADY VERIFIED (session id is the
    /// capability handed down from the gateway, not self-asserted).
    Join {
        room: RoomId,
        participant: ParticipantId,
    },
    /// `session` (v1.1 additive field) is REQUIRED by the engine for SSRC
    /// attribution: attribution via "latest join on the connection" was
    /// rejected as context-ambiguous under multi-participant pooling.
    Publish {
        session: Option<String>,
        track: TrackId,
        kind: MediaKind,
        /// SSRC the CLIENT will publish this track with (v1.2 additive):
        /// genuine WebRTC clients choose their own SSRC per RFC 3550 and
        /// the engine must attribute media to IT — minting one the client
        /// never hears about makes every RTP a drop-unknown-ssrc. Absent
        /// keeps the fixture path: the engine mints (test fixtures only).
        ssrc: Option<u32>,
    },
    Subscribe {
        session: Option<String>,
        participant: ParticipantId,
        track: TrackId,
    },
    Unsubscribe {
        session: Option<String>,
        participant: ParticipantId,
        track: TrackId,
    },
    Offer {
        session: MediaSessionId,
        sdp: String,
    },
    Trickle {
        session: MediaSessionId,
        candidate: Value,
    },
    /// Leave is cleaned up THROUGH the same session-attribution rule as
    /// publish (v1.1 additive field); a bare unit Leave still decodes for
    /// single-connection smoke harnesses.
    Leave {
        session: Option<String>,
    },
    Ping,
}

// ---------------------------------------------------------------------------
// Frame ⇄ JSON encoding. Hand-explicit (no derive macros here): the field
// names below ARE the interoperability contract with sdk/js clients, so
// they are written out where a reviewer can diff them.
// ---------------------------------------------------------------------------

impl ServerFrame {
    pub fn to_json(&self) -> Value {
        let mut o = Value::obj();
        match self {
            ServerFrame::Ready {
                session,
                participant,
                room,
                ice_ufrag,
                ice_pwd,
            } => {
                o.set("type", Value::Str("ready".into()));
                o.set("session", Value::Str(session.0.clone()));
                o.set("participant", Value::Str(participant.0.clone()));
                o.set("room", Value::Str(room.0.clone()));
                o.set("ice_ufrag", Value::Str(ice_ufrag.clone()));
                o.set("ice_pwd", Value::Str(ice_pwd.clone()));
            }
            ServerFrame::TrackPublished {
                room,
                participant,
                track,
                kind,
            } => {
                o.set("type", Value::Str("track.published".into()));
                o.set("room", Value::Str(room.0.clone()));
                o.set("participant", Value::Str(participant.0.clone()));
                o.set("track", Value::Str(track.0.clone()));
                o.set("kind", Value::Str(kind.as_str().into()));
            }
            ServerFrame::TrackUnpublished {
                room,
                participant,
                track,
            } => {
                o.set("type", Value::Str("track.unpublished".into()));
                o.set("room", Value::Str(room.0.clone()));
                o.set("participant", Value::Str(participant.0.clone()));
                o.set("track", Value::Str(track.0.clone()));
            }
            ServerFrame::Answer { session, sdp } => {
                o.set("type", Value::Str("answer".into()));
                o.set("session", Value::Str(session.0.clone()));
                o.set("sdp", Value::Str(sdp.clone()));
            }
            ServerFrame::Trickle { session, candidate } => {
                o.set("type", Value::Str("trickle".into()));
                o.set("session", Value::Str(session.0.clone()));
                o.set("candidate", candidate.clone());
            }
            ServerFrame::Error { code, message } => {
                o.set("type", Value::Str("error".into()));
                o.set("code", Value::Str(code.as_str().into()));
                o.set("message", Value::Str(message.clone()));
            }
        }
        o
    }

    pub fn encode(&self) -> String {
        self.to_json().to_string_compact()
    }
}

/// Decode failure categories: Unknown keeps a live session (frames from a
/// newer client), Malformed is a protocol violation worth logging.
#[derive(Clone, Debug, PartialEq)]
pub enum DecodeError {
    UnknownType(String),
    Malformed(String),
}

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DecodeError::UnknownType(t) => write!(f, "unknown frame type '{}'", t),
            DecodeError::Malformed(m) => write!(f, "malformed frame: {}", m),
        }
    }
}

impl ClientFrame {
    pub fn decode(text: &str) -> Result<ClientFrame, DecodeError> {
        let v = json::parse(text).map_err(|e| DecodeError::Malformed(e.to_string()))?;
        let ty = v
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| DecodeError::Malformed("missing type".into()))?;
        let str_field = |name: &str| -> Result<String, DecodeError> {
            v.get(name)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or_else(|| DecodeError::Malformed(format!("missing string field '{}'", name)))
        };
        Ok(match ty {
            "join" => ClientFrame::Join {
                room: RoomId(str_field("room")?),
                participant: ParticipantId(str_field("participant")?),
            },
            "publish" => ClientFrame::Publish {
                session: v.get("session").and_then(Value::as_str).map(str::to_string),
                track: TrackId(str_field("track")?),
                kind: MediaKind::parse(&str_field("kind")?)
                    .ok_or_else(|| DecodeError::Malformed("unknown media kind".into()))?,
                ssrc: match v.get("ssrc") {
                    None => None,
                    Some(Value::Num(n))
                        if *n >= 0.0 && *n <= u32::MAX as f64 && n.fract() == 0.0 =>
                    {
                        Some(*n as u32)
                    }
                    Some(_) => {
                        return Err(DecodeError::Malformed(
                            "ssrc must be a uint32 when present".into(),
                        ))
                    }
                },
            },
            "subscribe" => ClientFrame::Subscribe {
                session: v.get("session").and_then(Value::as_str).map(str::to_string),
                participant: ParticipantId(str_field("participant")?),
                track: TrackId(str_field("track")?),
            },
            "unsubscribe" => ClientFrame::Unsubscribe {
                session: v.get("session").and_then(Value::as_str).map(str::to_string),
                participant: ParticipantId(str_field("participant")?),
                track: TrackId(str_field("track")?),
            },
            "offer" => ClientFrame::Offer {
                session: MediaSessionId(str_field("session")?),
                sdp: str_field("sdp")?,
            },
            "trickle" => ClientFrame::Trickle {
                session: MediaSessionId(str_field("session")?),
                candidate: v
                    .get("candidate")
                    .cloned()
                    .ok_or_else(|| DecodeError::Malformed("missing candidate".into()))?,
            },
            "leave" => ClientFrame::Leave {
                session: v.get("session").and_then(Value::as_str).map(str::to_string),
            },
            "ping" => ClientFrame::Ping,
            other => return Err(DecodeError::UnknownType(other.to_string())),
        })
    }
}
