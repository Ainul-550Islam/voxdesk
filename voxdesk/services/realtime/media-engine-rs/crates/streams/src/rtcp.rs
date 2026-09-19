//! RTCP, the engine-relevant subset of RFC 3550: parse Sender/Receiver
//! Reports (inbound SSRC/loss/jitter truth) and BUILD Receiver Reports
//! (what the engine sends upstream about an RTP source it forwards).
//!
//! Compound packets are walked type-by-type; unknown types are SKIPPED by
//! length (the RTCP extension rule), never fatal — one unknown report must
//! not blind us to the ones we do read.

/// RTCP packet types we model (the rest parse as Unknown and are skipped).
pub const PT_SR: u8 = 200;
pub const PT_RR: u8 = 201;
pub const PT_SDES: u8 = 202;
pub const PT_BYE: u8 = 203;

/// One parsed report.
#[derive(Clone, Debug, PartialEq)]
pub enum Rtcp {
    SenderReport {
        ssrc: u32,
        ntp_msw: u32,
        ntp_lsw: u32,
        rtp_timestamp: u32,
        packet_count: u32,
        octet_count: u32,
    },
    ReceiverReport {
        ssrc: u32,
        reports: Vec<ReportBlock>,
    },
    /// Anything we don't model, retained by type/length for skipping.
    Unknown { packet_type: u8, payload_len: usize },
}

/// One report block (RFC 3550 §6.4.1) — the per-source statistics a
/// receiver asserts about a sender.
#[derive(Clone, Debug, PartialEq)]
pub struct ReportBlock {
    pub ssrc: u32,
    pub fraction_lost: u8,
    pub cumulative_lost: u32, // 24-bit on the wire; parsed into u32
    pub highest_seq_ext: u32,
    pub jitter: u32,
    pub lsr: u32,
    pub dlsr: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RtcpError {
    TooShort,
    BadVersion(u8),
    Truncated { packet_type: u8 },
}

impl std::fmt::Display for RtcpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RtcpError::TooShort => write!(f, "RTCP datagram shorter than one header"),
            RtcpError::BadVersion(v) => write!(f, "RTCP version {v}, want 2"),
            RtcpError::Truncated { packet_type } => {
                write!(f, "compound truncated inside type {packet_type}")
            }
        }
    }
}

impl std::error::Error for RtcpError {}

/// Parse every report in one (possibly compound) datagram.
pub fn parse(raw: &[u8]) -> Result<Vec<Rtcp>, RtcpError> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < raw.len() {
        if raw.len() - pos < 4 {
            return Err(RtcpError::TooShort);
        }
        let version = raw[pos] >> 6;
        if version != 2 {
            return Err(RtcpError::BadVersion(version));
        }
        let rc = (raw[pos] & 0x1f) as usize;
        let packet_type = raw[pos + 1];
        // Length is in 32-bit words MINUS one, including the header.
        let len_bytes = (u16::from_be_bytes([raw[pos + 2], raw[pos + 3]]) as usize + 1) * 4;
        if raw.len() - pos < len_bytes {
            return Err(RtcpError::Truncated { packet_type });
        }
        let body = &raw[pos..pos + len_bytes];
        match packet_type {
            PT_SR if len_bytes >= 28 => {
                out.push(Rtcp::SenderReport {
                    ssrc: be32(body, 4)?,
                    ntp_msw: be32(body, 8)?,
                    ntp_lsw: be32(body, 12)?,
                    rtp_timestamp: be32(body, 16)?,
                    packet_count: be32(body, 20)?,
                    octet_count: be32(body, 24)?,
                });
            }
            PT_RR if len_bytes >= 8 + rc * 24 => {
                let mut reports = Vec::with_capacity(rc);
                for i in 0..rc {
                    let base = 8 + i * 24;
                    reports.push(ReportBlock {
                        ssrc: be32(body, base)?,
                        fraction_lost: body[base + 4],
                        cumulative_lost: be24(body, base + 5)?,
                        highest_seq_ext: be32(body, base + 8)?,
                        jitter: be32(body, base + 12)?,
                        lsr: be32(body, base + 16)?,
                        dlsr: be32(body, base + 20)?,
                    });
                }
                out.push(Rtcp::ReceiverReport {
                    ssrc: be32(body, 4)?,
                    reports,
                });
            }
            other => {
                out.push(Rtcp::Unknown {
                    packet_type: other,
                    payload_len: len_bytes - 4,
                });
            }
        }
        pos += len_bytes;
    }
    Ok(out)
}

fn be32(raw: &[u8], off: usize) -> Result<u32, RtcpError> {
    if raw.len() < off + 4 {
        return Err(RtcpError::Truncated {
            packet_type: raw.get(1).copied().unwrap_or(0),
        });
    }
    Ok(u32::from_be_bytes([
        raw[off],
        raw[off + 1],
        raw[off + 2],
        raw[off + 3],
    ]))
}

fn be24(raw: &[u8], off: usize) -> Result<u32, RtcpError> {
    if raw.len() < off + 3 {
        return Err(RtcpError::Truncated {
            packet_type: raw.get(1).copied().unwrap_or(0),
        });
    }
    Ok(((raw[off] as u32) << 16) | ((raw[off + 1] as u32) << 8) | raw[off + 2] as u32)
}

/// Build a Receiver Report (SSRC of THIS reporter, then the blocks). RC
/// field carries min(blocks, 31) per RFC; the engine emits at most a
/// handful of sources per report.
pub fn build_receiver_report(reporter_ssrc: u32, blocks: &[ReportBlock]) -> Vec<u8> {
    let rc = blocks.len().min(31);
    let words = 1 /*header*/ + 1 /*ssrc*/ + rc * 6;
    let mut out = Vec::with_capacity(words * 4);
    out.push(0x80 | rc as u8); // V=2, RC=count
    out.push(PT_RR);
    out.extend_from_slice(&((words - 1) as u16).to_be_bytes());
    out.extend_from_slice(&reporter_ssrc.to_be_bytes());
    for b in &blocks[..rc] {
        out.extend_from_slice(&b.ssrc.to_be_bytes());
        out.push(b.fraction_lost);
        let lost = b.cumulative_lost.min(0x007f_ffff); // 24-bit magnitude; sign extension is the sender's concern for RR purposes
        out.extend_from_slice(&lost.to_be_bytes()[1..]);
        out.extend_from_slice(&b.highest_seq_ext.to_be_bytes());
        out.extend_from_slice(&b.jitter.to_be_bytes());
        out.extend_from_slice(&b.lsr.to_be_bytes());
        out.extend_from_slice(&b.dlsr.to_be_bytes());
    }
    out
}

/// Cheap demux sniff: RTCP type range per RFC 5764 (64..95 second byte is
/// DTLS-or-RTCP; RTCP payload types 192..223 overlap RTP — the transport
/// disambiguates via the second byte: 200..204 here).
pub fn looks_like_rtcp(raw: &[u8]) -> bool {
    raw.len() >= 4 && (raw[0] >> 6) == 2 && (192..=223).contains(&raw[1])
}
