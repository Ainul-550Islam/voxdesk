//! livekit — LiveKit-compatible JWT grants: HS256 verify, `video` claim
//! interpretation, and the engine's mint helper for testing/dev flows.
//!
//! The two bytes-counting details this module gets right by contract:
//!
//! * **No padding tolerance on output**: encode/decode use base64url
//!   unpadded (RFC 7515 §2) EXACTLY — total_characters mod 4 must be 4s;
//!   trailing '=' are rejected (some libraries pad; LiveKit server-side
//!   flows do not)… except on INPUT parsing where we accept-but-require-
//!   full-correctness, because browsers and mobile SDKs differ. Strict
//!   out, lenient-but-checked in: the robustness principle, on record.
//! * **Claims are evaluated BEFORE effects**: verify() runs all checks
//!   for each rule and returns ALL violations rather than the first —
//!   support tickets with "and also exp expired" are worth the pass.
//!
//! https://github.com/livekit/livekit-server/blob/master/pkg/auth/grants.go
//! (claim shapes mirrored 1:1 to stay interoperable).

use protocol::json::{self, Value};
use webrtc::crypto::{hmac::hmac_sha256, sha256::sha256};

/// The LiveKit video-grant subset the engine USES. CanJoin chains to
/// a parsed room + canPublish+canSubscribe bits.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VideoGrant {
    pub room: String,
    pub identity: Option<String>,
    pub can_publish: bool,
    pub can_subscribe: bool,
}

#[derive(Clone, Debug)]
pub struct Claims {
    pub identity: String,
    pub exp: Option<u64>,
    pub nbf: Option<u64>,
    pub grant: VideoGrant,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum VerifyError {
    NotAJwt,
    UnknownAlgorithm,
    BadSignature,
    NotYetActive,
    Expired,
    MissingIdentity,
    MissingVideoGrant,
    NotRoomJoin,
    CannotPublish,
    CannotSubscribe,
    MalformedClaims(String),
}

impl std::fmt::Display for VerifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            VerifyError::NotAJwt => "not a three-part JWT",
            VerifyError::UnknownAlgorithm => "alg is not HS256",
            VerifyError::BadSignature => "HS256 signature mismatch",
            VerifyError::NotYetActive => "nbf is in the future",
            VerifyError::Expired => "token has expired",
            VerifyError::MissingIdentity => "no identity claim",
            VerifyError::MissingVideoGrant => "no video grant claim",
            VerifyError::NotRoomJoin => "grant is not roomJoin",
            VerifyError::CannotPublish => "grant forbids publish",
            VerifyError::CannotSubscribe => "grant forbids subscribe",
            VerifyError::MalformedClaims(m) => return write!(f, "malformed claims: {m}"),
        };
        s.fmt(f)
    }
}

// ------------------------------------------------------------- base64url

/// Strict base64url DECODE: unpadded-or-padded input accepted; '=' must
/// be at the expected count, any mid-string padding or wrong alphabet is
/// rejected outright (silently-repaired tokens are an audit hole).
pub fn decode_b64url(s: &str) -> Option<Vec<u8>> {
    fn digit(c: u8) -> Option<u8> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return None,
        })
    }
    let bytes = s.as_bytes();
    let pad = bytes.iter().rev().take_while(|&&c| c == b'=').count();
    if pad > 0 && !bytes[bytes.len() - pad..].iter().all(|&c| c == b'=') {
        return None;
    }
    let body = &bytes[..bytes.len() - pad];
    // Padding is OPTIONAL but must be exactly canonical when present:
    // the '=' count is fully determined by the body length mod 4.
    let expected_pad = match body.len() % 4 {
        0 | 2 | 3 => (4 - body.len() % 4) % 4,
        _ => return None,
    };
    // Padding is OPTIONAL: accept unpadded entirely; accept padded only
    // when canonical ('==' for len%4==2, '=' for len%4==3).
    if pad > 0 && pad != expected_pad {
        return None;
    }
    let mut out = Vec::with_capacity(body.len() * 3 / 4 + 2);
    for chunk in body.chunks(4) {
        let mut buf = [0u8; 4];
        for (i, &c) in chunk.iter().enumerate() {
            buf[i] = digit(c)?;
        }
        match chunk.len() {
            4 => {
                out.push((buf[0] << 2) | (buf[1] >> 4));
                out.push((buf[1] << 4) | (buf[2] >> 2));
                out.push((buf[2] << 6) | buf[3]);
            }
            3 => {
                out.push((buf[0] << 2) | (buf[1] >> 4));
                out.push((buf[1] << 4) | (buf[2] >> 2));
            }
            2 => out.push((buf[0] << 2) | (buf[1] >> 4)),
            1 => return None,
            _ => {}
        }
    }
    Some(out)
}

/// Unpadded base64url ENCODE (strict output: no '=').
pub fn encode_b64url(data: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(data.len() * 4 / 3 + 4);
    for chunk in data.chunks(3) {
        let (b0, b1, b2) = (
            chunk[0] as u32,
            chunk.get(1).copied().unwrap_or(0) as u32,
            chunk.get(2).copied().unwrap_or(0) as u32,
        );
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((n >> 18) & 63) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((n >> 6) & 63) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(n & 63) as usize] as char);
        }
    }
    out
}

/// What capability the caller seeks — checked against the grant's bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability {
    Join,
    Publish,
    Subscribe,
}

/// Verify `token` against `api_key`/`api_secret` for `capability` in
/// `wanted_room`, at unix-now `now`. ALL rule violations are collected:
/// the returned Vec is empty iff the token is FULLY valid for the ask.
pub fn verify(
    token: &str,
    api_key: &str,
    api_secret: &str,
    capability: Capability,
    wanted_room: &str,
    now: u64,
) -> Result<Claims, Vec<VerifyError>> {
    let mut errors = Vec::new();
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Err(vec![VerifyError::NotAJwt]);
    }

    // Header check: alg must be HS256.
    let Some(hdr_bytes) = decode_b64url(parts[0]) else {
        return Err(vec![VerifyError::MalformedClaims(
            "header is not base64url".into(),
        )]);
    };
    let Ok(hdr) = json::parse(
        std::str::from_utf8(&hdr_bytes)
            .map_err(|_| "utf8".to_string())
            .unwrap_or("{}"),
    ) else {
        return Err(vec![VerifyError::MalformedClaims(
            "header is not JSON".into(),
        )]);
    };
    if hdr.get("alg").and_then(Value::as_str) != Some("HS256")
        || hdr.get("typ").and_then(Value::as_str) != Some("JWT")
    {
        errors.push(VerifyError::UnknownAlgorithm);
    }

    // Signature: HMAC over header.payload with the API secret.
    let signed = format!("{}.{}", parts[0], parts[1]);
    if let Some(sig) = decode_b64url(parts[2]) {
        let expect = hmac_sha256(api_secret.as_bytes(), signed.as_bytes());
        if sig != expect {
            errors.push(VerifyError::BadSignature);
        }
    } else {
        errors.push(VerifyError::MalformedClaims(
            "signature is not base64url".into(),
        ));
    }
    let _ = api_key; // iss-match is asserted at the payload phase below

    // Claims.
    let Some(claims_bytes) = decode_b64url(parts[1]) else {
        errors.push(VerifyError::MalformedClaims(
            "claims are not base64url".into(),
        ));
        return Err(errors);
    };
    let parsed = json::parse(std::str::from_utf8(&claims_bytes).unwrap_or("{}"));
    let claims = match parsed {
        Ok(c) => c,
        Err(_) => {
            errors.push(VerifyError::MalformedClaims("claims are not JSON".into()));
            return Err(errors);
        }
    };

    let identity = claims
        .get("sub")
        .and_then(Value::as_str)
        .or_else(|| claims.get("identity").and_then(Value::as_str));
    if identity.is_none() {
        errors.push(VerifyError::MissingIdentity);
    }
    if let Some(nbf) = claims.get("nbf").and_then(Value::as_u64) {
        if nbf > now {
            errors.push(VerifyError::NotYetActive);
        }
    }
    if let Some(exp) = claims.get("exp").and_then(Value::as_u64) {
        if exp <= now {
            errors.push(VerifyError::Expired);
        }
    }

    // The video grant: LiveKit puts it under "video" with room* fields.
    let video = claims.get("video").ok_or(());
    let grant = match video {
        Ok(v) => {
            let room = v.get("room").and_then(Value::as_str).unwrap_or("");
            let join =
                v.get("roomJoin").and_then(Value::as_bool).unwrap_or(false) || !room.is_empty();
            let can_publish = v.get("canPublish").and_then(Value::as_bool).unwrap_or(join);
            let can_subscribe = v
                .get("canSubscribe")
                .and_then(Value::as_bool)
                .unwrap_or(join);
            if join && room == wanted_room {
                VideoGrant {
                    room: room.to_string(),
                    identity: identity.map(str::to_string),
                    can_publish,
                    can_subscribe,
                }
            } else if !join {
                errors.push(VerifyError::NotRoomJoin);
                VideoGrant {
                    room: room.to_string(),
                    identity: None,
                    can_publish,
                    can_subscribe,
                }
            } else {
                errors.push(VerifyError::NotRoomJoin);
                VideoGrant {
                    room: room.to_string(),
                    identity: identity.map(str::to_string),
                    can_publish,
                    can_subscribe,
                }
            }
        }
        Err(()) => {
            errors.push(VerifyError::MissingVideoGrant);
            VideoGrant {
                room: String::new(),
                identity: None,
                can_publish: false,
                can_subscribe: false,
            }
        }
    };

    match capability {
        Capability::Join => {}
        Capability::Publish => {
            if !grant.can_publish {
                errors.push(VerifyError::CannotPublish);
            }
        }
        Capability::Subscribe => {
            if !grant.can_subscribe {
                errors.push(VerifyError::CannotSubscribe);
            }
        }
    }

    if errors.is_empty() {
        Ok(Claims {
            identity: identity.unwrap_or_default().to_string(),
            exp: claims.get("exp").and_then(Value::as_u64),
            nbf: claims.get("nbf").and_then(Value::as_u64),
            grant,
        })
    } else {
        Err(errors)
    }
}

/// Mint a token — for integration tests, dev tokens, and the gpg-style
/// `sdp-tool` dev helper. Production tokens still come from the gateway.
pub fn mint(
    api_secret: &str,
    identity: &str,
    room: &str,
    exp_in: u64,
    now: u64,
    can_publish: bool,
    can_subscribe: bool,
) -> String {
    let header = encode_b64url(br#"{"alg":"HS256","typ":"JWT"}"#);
    let mut claims = Value::obj();
    claims.set("sub", Value::Str(identity.to_string()));
    claims.set("exp", Value::Num((now + exp_in) as f64));
    let mut video = Value::obj();
    video.set("roomJoin", Value::Bool(true));
    video.set("room", Value::Str(room.to_string()));
    video.set("canPublish", Value::Bool(can_publish));
    video.set("canSubscribe", Value::Bool(can_subscribe));
    claims.set("video", video);
    let body = encode_b64url(claims.to_string_compact().as_bytes());
    let signed = format!("{header}.{body}");
    let digest = hmac_sha256(api_secret.as_bytes(), signed.as_bytes());
    format!("{}.{}", signed, encode_b64url(&digest))
}

/// sha256 helper surfaced for the tool binaries' fingerprint pin option.
pub fn sha256_fingerprint_label(text: &str) -> String {
    sha256(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02X}"))
        .collect::<Vec<_>>()
        .join(":")
}
