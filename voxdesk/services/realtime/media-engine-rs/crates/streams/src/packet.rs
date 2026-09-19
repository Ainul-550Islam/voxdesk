//! RTP packet parse/serialize per RFC 3550 §5.1: the fixed 12-byte header,
//! CSRC list, one extension header (skipped by the forwarding path but
//! ACCOUNTED for — a payload offset that ignores it corrupts every frame),
//! and the payload slice.
//!
//! Packets are owned (Vec<u8>): the engine's hot path copies ONCE on
//! receipt, then shares via Arc between legs — zero per-subscriber
//! re-serialization, same contract as gateway-go's hub fan-out (frames
//! marshalled once).

/// Minimum RTP header: 12 bytes without CSRCs/extensions.
pub const MIN_HEADER: usize = 12;

/// A parsed RTP packet. Parsing VALIDATES header structure only; payload
/// contents are the codec's business (mirroring the gateway's "sized,
/// never parsed" SDP discipline).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RtpPacket {
    pub padding: bool,
    pub marker: bool,
    pub payload_type: u8,
    pub sequence: u16,
    pub timestamp: u32,
    pub ssrc: u32,
    /// Byte range of the payload within `raw` (after header + CSRCs +
    /// extension, before any padding).
    payload_start: usize,
    payload_end: usize,
    pub raw: Vec<u8>,
}

/// Why a datagram is not an RTP packet.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PacketError {
    TooShort { got: usize, need: usize },
    BadVersion(u8),
    TruncatedCsrc,
    TruncatedExtension,
    PaddingExceedsPayload,
}

impl std::fmt::Display for PacketError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PacketError::TooShort { got, need } => write!(f, "datagram {got}B < minimum {need}B"),
            PacketError::BadVersion(v) => write!(f, "RTP version {v}, want 2"),
            PacketError::TruncatedCsrc => write!(f, "header truncated inside CSRC list"),
            PacketError::TruncatedExtension => write!(f, "header truncated inside extension"),
            PacketError::PaddingExceedsPayload => write!(f, "padding count exceeds payload"),
        }
    }
}

impl std::error::Error for PacketError {}

impl RtpPacket {
    /// Parse one datagram.
    pub fn parse(raw: Vec<u8>) -> Result<RtpPacket, PacketError> {
        if raw.len() < MIN_HEADER {
            return Err(PacketError::TooShort {
                got: raw.len(),
                need: MIN_HEADER,
            });
        }
        let version = raw[0] >> 6;
        if version != 2 {
            return Err(PacketError::BadVersion(version));
        }
        let padding = raw[0] & 0x20 != 0;
        let extension = raw[0] & 0x10 != 0;
        let cc = (raw[0] & 0x0f) as usize;
        let marker = raw[1] & 0x80 != 0;
        let payload_type = raw[1] & 0x7f;
        let sequence = u16::from_be_bytes([raw[2], raw[3]]);
        let timestamp = u32::from_be_bytes([raw[4], raw[5], raw[6], raw[7]]);
        let ssrc = u32::from_be_bytes([raw[8], raw[9], raw[10], raw[11]]);

        let mut offset = MIN_HEADER + cc * 4;
        if raw.len() < offset {
            return Err(PacketError::TruncatedCsrc);
        }
        if extension {
            // RFC 3550: 16-bit profile + 16-bit length in 32-bit words.
            if raw.len() < offset + 4 {
                return Err(PacketError::TruncatedExtension);
            }
            let ext_words = u16::from_be_bytes([raw[offset + 2], raw[offset + 3]]) as usize;
            offset += 4 + ext_words * 4;
            if raw.len() < offset {
                return Err(PacketError::TruncatedExtension);
            }
        }

        let mut payload_end = raw.len();
        if padding {
            let pad = *raw.last().unwrap() as usize;
            if pad == 0 || pad > payload_end.saturating_sub(offset) {
                return Err(PacketError::PaddingExceedsPayload);
            }
            payload_end -= pad;
        }

        Ok(RtpPacket {
            padding,
            marker,
            payload_type,
            sequence,
            timestamp,
            ssrc,
            payload_start: offset,
            payload_end,
            raw,
        })
    }

    /// The payload slice (between header/extensions and padding).
    pub fn payload(&self) -> &[u8] {
        &self.raw[self.payload_start..self.payload_end]
    }

    /// Builds a packet with no CSRCs/extensions/padding (engine-originated
    /// media — forwarded packets arrive already serialized via parse()).
    pub fn build(
        payload_type: u8,
        sequence: u16,
        timestamp: u32,
        ssrc: u32,
        marker: bool,
        payload: &[u8],
    ) -> RtpPacket {
        let mut raw = Vec::with_capacity(MIN_HEADER + payload.len());
        raw.push(0x80); // V=2
        raw.push(if marker {
            0x80 | payload_type
        } else {
            payload_type
        });
        raw.extend_from_slice(&sequence.to_be_bytes());
        raw.extend_from_slice(&timestamp.to_be_bytes());
        raw.extend_from_slice(&ssrc.to_be_bytes());
        raw.extend_from_slice(payload);
        let payload_end = raw.len();
        RtpPacket {
            padding: false,
            marker,
            payload_type,
            sequence,
            timestamp,
            ssrc,
            payload_start: MIN_HEADER,
            payload_end,
            raw,
        }
    }
}

/// The cheap, non-owning sniff used by the transport demux: is this
/// datagram plausibly RTP (vs STUN/DTLS/RTCP)? RFC 5764 demultiplexing
/// says: STUN starts 0b00, DTLS 20-63, RTP first byte 128-191 with payload
/// types outside the RTCP range. FULL disambiguation lives in transport;
/// this helper only answers the RTP leg.
pub fn looks_like_rtp(raw: &[u8]) -> bool {
    raw.len() >= MIN_HEADER && (raw[0] >> 6) == 2
}
