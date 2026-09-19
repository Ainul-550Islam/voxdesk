//! STUN (RFC 5389) messages: the outer packet shape every ICE
//! connectivity check is carved into. Implements bind request/response
//! construction and parsing with the two sanity-critical attributes —
//! MESSAGE-INTEGRITY (HMAC-SHA1 over everything through the MI attribute
//! itself) and FINGERPRINT (CRC-32 XORed with the cookie constant).
//!
//! We deliberately do NOT implement TURN or TCP-RFC4571 framing here:
//! the UDP relay is speakable-but-optional, and this module's contract
//! says exactly that in `looks_like_stun`'s docs.

use super::crypto::hmac::hmac_sha1;

pub const COOKIE: u32 = 0x2112_A442;

pub mod types {
    pub const BINDING_REQUEST: u16 = 0x0001;
    pub const BINDING_SUCCESS: u16 = 0x0101;
    pub const BINDING_ERROR: u16 = 0x0111;
}

pub mod attrs {
    pub const USERNAME: u16 = 0x0006;
    pub const MESSAGE_INTEGRITY: u16 = 0x0008;
    pub const ERROR_CODE: u16 = 0x0009;
    pub const UNKNOWN_ATTRIBUTES: u16 = 0x000A;
    pub const PRIORITY: u16 = 0x0024;
    pub const USE_CANDIDATE: u16 = 0x0025;
    pub const FINGERPRINT: u16 = 0x8028;
    pub const ICE_CONTROLLED: u16 = 0x8029;
    pub const ICE_CONTROLLING: u16 = 0x802A;
    pub const XOR_MAPPED_ADDRESS: u16 = 0x0020;
}

/// Which side of an ICE transaction the local agent occupies in STUN.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IceRole {
    Controlling,
    Controlled,
}

/// A parsed STUN message: borrowed views into the received datagram.
#[derive(Clone, Debug)]
pub struct StunMessage {
    pub msg_type: u16,
    pub transaction_id: [u8; 12],
    /// Type → value bytes (non-padded). Attrs appear in wire order —
    /// critical for integrity (which is computed over the bytes THROUGH
    /// the MI attribute, so replaying attribute order is required).
    pub attrs: Vec<(u16, Vec<u8>)>,
    /// True if a FINGERPRINT was present AND correct.
    pub fingerprint_ok: bool,
    /// True if a MESSAGE-INTEGRITY was present; verified by the caller
    /// who owns the password (verify_integrity method).
    pub has_integrity: bool,
    /// Byte length consumed by the message THROUGH the integrity attr —
    /// needed for integrity verification.
    pub integrity_span: usize,
    /// Full raw length consumed from the datagram.
    pub raw_len: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StunError {
    TooShort,
    BadCookie,
    TruncatedAttribute,
    LengthMismatch,
    NotStun,
}

/// Cheap gate before full parsing: the browser-facing UDP loop checks
/// this first (first two bits of channel data would collide otherwise —
/// RFC 7983's demux table: STUN's top bits are 00, RTP's are 10).
pub fn looks_like_stun(data: &[u8]) -> bool {
    data.len() >= 20
        && data[0] & 0xC0 == 0
        && u32::from_be_bytes([data[4], data[5], data[6], data[7]]) == COOKIE
}

impl StunMessage {
    pub fn parse(data: &[u8]) -> Result<StunMessage, StunError> {
        if data.len() < 20 {
            return Err(StunError::TooShort);
        }
        if !looks_like_stun(data) {
            return Err(StunError::NotStun);
        }
        let msg_type = u16::from_be_bytes([data[0], data[1]]);
        let declared_len = u16::from_be_bytes([data[2], data[3]]) as usize;
        if !declared_len.is_multiple_of(4) {
            return Err(StunError::LengthMismatch);
        }
        let end = 20 + declared_len;
        if end > data.len() {
            return Err(StunError::TruncatedAttribute);
        }
        let buf = &data[..end];
        let mut transaction_id = [0u8; 12];
        transaction_id.copy_from_slice(&buf[8..20]);

        let mut attrs = Vec::new();
        let mut fingerprint_ok = false;
        let mut has_integrity = false;
        let mut integrity_span = 0usize;

        let mut p = 20usize;
        while p + 4 <= end {
            let attr_type = u16::from_be_bytes([buf[p], buf[p + 1]]);
            let attr_len = u16::from_be_bytes([buf[p + 2], buf[p + 3]]) as usize;
            let padded = attr_len.next_multiple_of(4);
            if p + 4 + padded > end {
                return Err(StunError::TruncatedAttribute);
            }
            let value = &buf[p + 4..p + 4 + attr_len];
            if attr_type == attrs::MESSAGE_INTEGRITY {
                has_integrity = true;
                integrity_span = p + 4 + 20; // through the 20-byte MI value
            }
            if attr_type == attrs::FINGERPRINT && attr_len == 4 {
                // FINGERPRINT covers the whole message THROUGH the FP attr
                // header; its own value is excluded, and the length field
                // must claim the 8 bytes of the FP attribute itself.
                let mut framed = buf[..p].to_vec();
                let len_with_fp = (p - 20 + 8) as u16;
                framed[2..4].copy_from_slice(&len_with_fp.to_be_bytes());
                let expect = crc32(&framed) ^ 0x5354_554E;
                fingerprint_ok = value == expect.to_be_bytes();
            }
            attrs.push((attr_type, value.to_vec()));
            p += 4 + padded;
        }

        Ok(StunMessage {
            msg_type,
            transaction_id,
            attrs,
            fingerprint_ok,
            has_integrity,
            integrity_span,
            raw_len: end,
        })
    }

    pub fn attr(&self, want: u16) -> Option<&[u8]> {
        self.attrs
            .iter()
            .find(|(t, _)| *t == want)
            .map(|(_, v)| v.as_slice())
    }

    pub fn use_candidate(&self) -> bool {
        self.attr(attrs::USE_CANDIDATE).is_some()
    }

    /// Verify MESSAGE-INTEGRITY against `password` (the remote ufrag's
    /// password). Returned bool doesn't shortcut: the whole MAC compare
    /// runs regardless (constant-time about where a mismatch lies).
    pub fn verify_integrity(&self, password: &str, original: &[u8]) -> bool {
        if !self.has_integrity || self.integrity_span > original.len() {
            return false;
        }
        let Some(stored) = self.attr(attrs::MESSAGE_INTEGRITY) else {
            return false;
        };
        if stored.len() != 20 {
            return false;
        }
        // RFC 8489 §14.5 (and the pion/stun wire reality the it-pion
        // lane forced us to confront): the HMAC input runs through the
        // attribute PRECEDING MESSAGE-INTEGRITY only — MI's own header
        // is NOT hashed — with the length field patched to point at the
        // end of the MI value.
        let mi_start = self.integrity_span - 24; // MI attr header(4) + value(20)
        let mut framed = original[..mi_start].to_vec();
        let declared = (mi_start - 20 + 24) as u16; // body-before-MI + MI TLV
        framed[2..4].copy_from_slice(&declared.to_be_bytes());
        let expect = hmac_sha1(password.as_bytes(), &framed);
        if std::env::var_os("VOXDESK_STUN_DEBUG").is_some() {
            eprintln!(
                "verify-internal span={} framed_len={} stored={} expect={}",
                self.integrity_span,
                framed.len(),
                stored
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>(),
                expect
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            );
        }
        // Constant-tolerance compare: loop every byte, no early break.
        let mut diff = 0u8;
        for (a, b) in stored.iter().zip(expect.iter()) {
            diff |= a ^ b;
        }
        diff == 0
    }
}

/// Builder. Attribute order is fixed by policy: everything protocol, then
/// integrity, then fingerprint (RFC 5389 mandates MI before FP).
pub struct StunBuilder {
    msg_type: u16,
    transaction_id: [u8; 12],
    attrs: Vec<(u16, Vec<u8>)>,
}

impl StunBuilder {
    pub fn new(msg_type: u16, transaction_id: [u8; 12]) -> StunBuilder {
        StunBuilder {
            msg_type,
            transaction_id,
            attrs: Vec::new(),
        }
    }

    pub fn response_to(tid: [u8; 12]) -> StunBuilder {
        StunBuilder::new(types::BINDING_SUCCESS, tid)
    }

    pub fn username(mut self, u: &str) -> StunBuilder {
        self.attrs.push((attrs::USERNAME, u.as_bytes().to_vec()));
        self
    }

    pub fn ice_role(self, role: IceRole, tiebreaker: u64) -> StunBuilder {
        match role {
            IceRole::Controlling => self.attr(attrs::ICE_CONTROLLING, &tiebreaker.to_be_bytes()),
            IceRole::Controlled => self.attr(attrs::ICE_CONTROLLED, &tiebreaker.to_be_bytes()),
        }
    }

    pub fn priority(self, p: u32) -> StunBuilder {
        self.attr(attrs::PRIORITY, &p.to_be_bytes())
    }

    pub fn use_candidate(self) -> StunBuilder {
        self.attr(attrs::USE_CANDIDATE, &[])
    }

    pub fn xor_mapped_ipv4(self, port: u16, ip: [u8; 4]) -> StunBuilder {
        // RFC 5389 §15.2: port XOR cookie-hi, address XOR cookie.
        let mut v = vec![0u8, 0x01];
        v.extend_from_slice(&(port ^ (COOKIE >> 16) as u16).to_be_bytes());
        for (b, k) in ip.iter().zip(COOKIE.to_be_bytes().iter()) {
            v.push(b ^ k);
        }
        self.attr(attrs::XOR_MAPPED_ADDRESS, &v)
    }

    pub fn attr(mut self, t: u16, v: &[u8]) -> StunBuilder {
        self.attrs.push((t, v.to_vec()));
        self
    }

    fn frame_without_trailer(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(128);
        out.extend_from_slice(&self.msg_type.to_be_bytes());
        out.extend_from_slice(&[0, 0]); // length — patched in build()
        out.extend_from_slice(&COOKIE.to_be_bytes());
        out.extend_from_slice(&self.transaction_id);
        for (t, v) in &self.attrs {
            out.extend_from_slice(&t.to_be_bytes());
            out.extend_from_slice(&(v.len() as u16).to_be_bytes());
            out.extend_from_slice(v);
            out.resize(out.len() + v.len().next_multiple_of(4) - v.len(), 0); // 0..4 pad
        }
        out
    }

    /// Build WITHOUT trailers (for tests and exotic flows).
    pub fn build_plain(mut self) -> Vec<u8> {
        let mut out = self.frame_without_trailer();
        let len = (out.len() - 20) as u16;
        out[2..4].copy_from_slice(&len.to_be_bytes());
        let _ = &mut self;
        out
    }

    /// Build with MESSAGE-INTEGRITY + FINGERPRINT — the only build ICE
    /// requests/responses may use on the wire (RFC 8445 §11).
    pub fn build_with_integrity(self, password: &str) -> Vec<u8> {
        // RFC 8489 §14.5: hash the header (length = body + MI TLV(24))
        // plus the attributes BEFORE MI — not MI's own header.
        let mut out = self.frame_without_trailer();
        let len_to_mi = (out.len() - 20 + 24) as u16; // + attr header(4) + 20 value
        out[2..4].copy_from_slice(&len_to_mi.to_be_bytes());
        let mac = hmac_sha1(password.as_bytes(), &out);
        out.extend_from_slice(&attrs::MESSAGE_INTEGRITY.to_be_bytes());
        out.extend_from_slice(&20u16.to_be_bytes());
        out.extend_from_slice(&mac);

        // FINGERPRINT (RFC 5389 §15.5): CRC over the message THROUGH the
        // attribute PRECEDING the fingerprint — i.e. excluding the FP
        // attribute's own header and value — with the length field
        // already stating the full final length (as if FP were there).
        let final_len = (out.len() - 20 + 8) as u16;
        out[2..4].copy_from_slice(&final_len.to_be_bytes());
        let crc = crc32(&out) ^ 0x5354_554E;
        out.extend_from_slice(&attrs::FINGERPRINT.to_be_bytes());
        out.extend_from_slice(&4u16.to_be_bytes());
        out.extend_from_slice(&crc.to_be_bytes());
        out
    }
}

/// CRC-32 (IEEE, reflected, poly 0xEDB88320) — the FINGERPRINT function.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (if crc & 1 != 0 { 0xEDB8_8320 } else { 0 });
        }
    }
    !crc
}

/// Parse XOR-MAPPED-ADDRESS (IPv4 flavor only).
pub fn xor_mapped_ipv4(attr: &[u8], transaction_id: &[u8; 12]) -> Option<(u16, [u8; 4])> {
    if attr.len() < 8 || attr[1] != 0x01 {
        return None;
    }
    let port = u16::from_be_bytes([attr[2], attr[3]]) ^ (COOKIE >> 16) as u16;
    let mut ip = [0u8; 4];
    for i in 0..4 {
        ip[i] = attr[4 + i] ^ COOKIE.to_be_bytes()[i];
    }
    let _ = transaction_id; // IPv6 flavor would need it; not implemented.
    Some((port, ip))
}
