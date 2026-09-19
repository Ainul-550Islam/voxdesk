//! SDP (RFC 8866) — parse what a WebRTC offerer UAs produce, emit what
//! our SFU answers with. We implement the SUBSET the BUNDLE/ICE/DTLS
//! path actually reads:
//!
//! * session-level: o=, a=group:BUNDLE, a=msid-semantic
//! * media-level: m= audio/video, a=mid, a=rtpmap, a=ice-ufrag/pwd,
//!   a=fingerprint sha-256, a=setup, a=candidate, a=rtcp-mux,
//!   a=sendrecv et al. directions
//!
//! Anything else (ptime, fmtp, codec parameters we gate on the engine
//! side) is stored BAG-style so the answer can round-trip nondestructive-
//! ly: parse → augment → still carry the pieces a UA entrenches on.

use super::ice::RemoteCandidate;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaDirection {
    SendRecv,
    SendOnly,
    RecvOnly,
    Inactive,
}

/// One parsed media (=) body worth caring about.
#[derive(Clone, Debug)]
pub struct MediaBody {
    pub kind: String,      // "audio" | "video" per m=
    pub transport: String, // "UDP/TLS/RTP/SAVPF"
    pub formats: Vec<u8>,  // payload types on the m= line
    pub mid: String,
    pub rtcp_mux: bool,
    pub ice_ufrag: Option<String>,
    pub ice_pwd: Option<String>,
    pub fingerprint_sha256: Option<String>,
    pub setup: Option<String>, // "actpass" | "active" | "passive"
    pub candidates: Vec<RemoteCandidate>,
    pub rtpmap: Vec<(u8, String)>, // pt → "opus/48000/2"
    pub direction: MediaDirection,
    /// Everything else, verbatim: "ptime:20", "fmtp:111 ...", etc.
    pub leftovers: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Offer {
    pub session_ufrag: Option<String>,
    pub session_pwd: Option<String>,
    pub session_fingerprint: Option<String>,
    pub media: Vec<MediaBody>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SdpError {
    Empty,
    LineSyntax(String),
    NoMedia,
}

pub fn parse_offer(text: &str) -> Result<Offer, SdpError> {
    if text.trim().is_empty() {
        return Err(SdpError::Empty);
    }
    let mut offer = Offer {
        session_ufrag: None,
        session_pwd: None,
        session_fingerprint: None,
        media: Vec::new(),
    };
    let mut current: Option<MediaBody> = None;

    for raw_line in text.lines() {
        let line = raw_line.trim_end_matches('\r');
        if line.len() < 2 || &line[1..2] != "=" {
            // Unknown shortifiers: tolerate (o=- etc all have 2 prefixes).
            if line.is_empty() {
                continue;
            }
            return Err(SdpError::LineSyntax(line.to_string()));
        }
        let (prefix, body) = line.split_at(2);
        let body = body.trim();

        match prefix {
            "m=" => {
                if let Some(done) = current.take() {
                    offer.media.push(done);
                }
                let mut parts = body.split_whitespace();
                let kind = parts.next().unwrap_or("").to_string();
                let _port = parts.next();
                let transport = parts.next().unwrap_or("").to_string();
                let formats: Vec<u8> = parts.filter_map(|f| f.parse().ok()).collect();
                current = Some(MediaBody {
                    kind,
                    transport,
                    formats,
                    mid: String::new(),
                    rtcp_mux: false,
                    ice_ufrag: None,
                    ice_pwd: None,
                    fingerprint_sha256: None,
                    setup: None,
                    candidates: Vec::new(),
                    rtpmap: Vec::new(),
                    direction: MediaDirection::Inactive,
                    leftovers: Vec::new(),
                });
            }
            "a=" => {
                if let Some(socket) = body.strip_prefix("ice-ufrag:") {
                    match &mut current {
                        Some(m) => m.ice_ufrag = Some(socket.to_string()),
                        None => offer.session_ufrag = Some(socket.to_string()),
                    }
                } else if let Some(socket) = body.strip_prefix("ice-pwd:") {
                    match &mut current {
                        Some(m) => m.ice_pwd = Some(socket.to_string()),
                        None => offer.session_pwd = Some(socket.to_string()),
                    }
                } else if let Some(fp) = body.strip_prefix("fingerprint:sha-256") {
                    let fp = fp.trim().to_string();
                    match &mut current {
                        Some(m) => m.fingerprint_sha256 = Some(fp),
                        None => offer.session_fingerprint = Some(fp),
                    }
                } else if let Some(socket) = body.strip_prefix("setup:") {
                    if let Some(m) = &mut current {
                        m.setup = Some(socket.to_string());
                    }
                } else if let Some(socket) = body.strip_prefix("mid:") {
                    if let Some(m) = &mut current {
                        m.mid = socket.to_string();
                    }
                } else if body == "rtcp-mux" {
                    if let Some(m) = &mut current {
                        m.rtcp_mux = true;
                    }
                } else if let Some(socket) = body.strip_prefix("candidate:") {
                    if let Some(m) = &mut current {
                        if let Ok(c) = RemoteCandidate::parse(socket) {
                            m.candidates.push(c);
                        }
                    }
                } else if let Some(socket) = body.strip_prefix("rtpmap:") {
                    if let Some(m) = &mut current {
                        let mut it = socket.split(' ');
                        if let (Some(pt), Some(codec)) = (it.next(), it.next()) {
                            if let Ok(pt) = pt.parse::<u8>() {
                                m.rtpmap.push((pt, codec.to_string()));
                            }
                        }
                    }
                } else if body == "sendrecv" {
                    set_direction(&mut current, MediaDirection::SendRecv);
                } else if body == "sendonly" {
                    set_direction(&mut current, MediaDirection::SendOnly);
                } else if body == "recvonly" {
                    set_direction(&mut current, MediaDirection::RecvOnly);
                } else if body == "inactive" {
                    set_direction(&mut current, MediaDirection::Inactive);
                } else if let Some(m) = &mut current {
                    // Session-level groups, extmaps, everything we didn't
                    // explicitly read: preserve for round-trip.
                    m.leftovers.push(body.to_string());
                }
            }
            _ => {}
        }
    }
    if let Some(last) = current.take() {
        offer.media.push(last);
    }
    if offer.media.is_empty() {
        return Err(SdpError::NoMedia);
    }
    Ok(offer)
}

fn set_direction(current: &mut Option<MediaBody>, dir: MediaDirection) {
    if let Some(m) = current {
        m.direction = dir;
    }
}

/// Which of the offer's media bodies we ACCEPT in the answer — our SFU
/// accepts everything the client bundles (BUNDLE emits as one set of
/// ice lines; we echo per-body mids to keep Chrome's logger happy).
#[derive(Clone, Debug)]
pub struct AnswerContext {
    pub local_ufrag: String,
    pub local_pwd: String,
    pub fingerprint_sha256: String, // "AA:BB:.." hex pairs
    pub public_ip: [u8; 4],
    pub public_port: u16,
    pub external_ip_label: String,
}

/// Build the SDP answer to `offer`.
pub fn build_answer(offer: &Offer, ctx: &AnswerContext) -> String {
    let pwd = ctx.local_pwd.clone();

    let mut out = String::with_capacity(1024);
    out.push_str("v=0\r\n");
    out.push_str("o=- 1 1 IN IP4 0.0.0.1\r\n");
    out.push_str("s=-\r\n");
    out.push_str("t=0 0\r\n");
    let mids: Vec<&str> = offer.media.iter().map(|m| m.mid.as_str()).collect();
    if !mids.is_empty() {
        out.push_str(&format!("a=group:BUNDLE {}\r\n", mids.join(" ")));
    }
    out.push_str("a=msid-semantic: WMS *\r\n");
    out.push_str(&format!("a=ice-ufrag:{}\r\n", ctx.local_ufrag));
    out.push_str(&format!("a=ice-pwd:{pwd}\r\n"));
    out.push_str(&format!(
        "a=fingerprint:sha-256 {}\r\n",
        ctx.fingerprint_sha256
    ));

    for m in &offer.media {
        out.push_str(&format!(
            "m={} 9 {} {}\r\n",
            m.kind,
            m.transport,
            m.formats
                .iter()
                .map(|f| f.to_string())
                .collect::<Vec<_>>()
                .join(" ")
        ));
        out.push_str("c=IN IP4 0.0.0.0\r\n");
        out.push_str(&format!("a=mid:{}\r\n", m.mid));
        // We accept every offered codec for the mirroring simplicity —
        // codec negotiation is the engine's pomegranate, not SDP's.
        for (pt, codec) in &m.rtpmap {
            out.push_str(&format!("a=rtpmap:{pt} {codec}\r\n"));
        }
        out.push_str("a=rtcp-mux\r\n");
        out.push_str("a=ice-lite\r\n");
        // RFC 5763 §5 role settlement: the answer MUST complement the
        // offer's setup — `passive` offerers force us into the DTLS
        // client role; anything else (actpass/active/none) makes us the
        // DTLS server who's fine being reconnected to.
        let answered_setup = match m.setup.as_deref() {
            Some("passive") => "active",
            _ => "passive",
        };
        out.push_str(&format!("a=setup:{answered_setup}\r\n"));
        let direction = match m.direction {
            MediaDirection::SendOnly => "recvonly",
            MediaDirection::RecvOnly => "sendonly",
            MediaDirection::SendRecv => "sendrecv",
            MediaDirection::Inactive => "inactive",
        };
        out.push_str(&format!("a={direction}\r\n"));
        out.push_str(&format!(
            "a=candidate:1 1 udp 2128609535 {}.{}.{}.{} {} typ host\r\n",
            ctx.public_ip[0], ctx.public_ip[1], ctx.public_ip[2], ctx.public_ip[3], ctx.public_port
        ));
        // End-of-candidates signals the offerer we are done gathering
        // (RFC 8839 §4.1).
        out.push_str("a=end-of-candidates\r\n");
    }
    out
}
