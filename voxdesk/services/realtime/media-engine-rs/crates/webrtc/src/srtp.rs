//! SRTP with AES-128 Counter Mode + HMAC-SHA1-80 (RFC 3711 default
//! profile, the only crypto-suite WebRTC's DTLS-SRTP negotiates in
//! practice). Key derivation per §4.3.1 ("one master key + salt, label-
//! derived sessions keys"); authentication per §4.2.1 (80-bit tag, packet
//! + ROC authenticated but replay-window protected before trust).
//!
//! Test vectors govern this file: RFC 3711 §B.3's key derivation and the
//! §B.4 example protection run — self-apparent from tests/srtp_round.rs.

use super::crypto::{aes128::Aes128, hmac::hmac_sha1};
use streams::packet::{RtpPacket, MIN_HEADER as MIN_RTP_HEADER};
use streams::replay::ReplayWindow;

/// The SRTP_AES128_CM_SHA1_80 profile constants (RFC 5764 Annex C id 7).
pub const SESSION_KEY_LEN: usize = 16;
pub const SESSION_SALT_LEN: usize = 14;
pub const AUTH_TAG_LEN: usize = 10;
pub const AUTH_KEY_LEN: usize = 20;

pub mod labels {
    pub const ENCRYPT: u8 = 0x00;
    pub const AUTH: u8 = 0x01;
    pub const SALT: u8 = 0x02;
}

#[derive(Clone, Debug)]
pub struct SessionKeys {
    pub aes: [u8; SESSION_KEY_LEN],
    pub salt: [u8; SESSION_SALT_LEN],
    pub auth: [u8; AUTH_KEY_LEN],
}

/// Derive the three session keys from a master key + salt (AES-CM PRF).
pub fn derive_session_keys(master_key: &[u8; 16], master_salt: &[u8; 14]) -> SessionKeys {
    let aes = derive_key::<SESSION_KEY_LEN>(master_key, master_salt, labels::ENCRYPT, 0);
    let auth = derive_key::<AUTH_KEY_LEN>(master_key, master_salt, labels::AUTH, 0);
    let salt14 = derive_key::<SESSION_SALT_LEN>(master_key, master_salt, labels::SALT, 0);
    SessionKeys {
        aes,
        salt: salt14,
        auth,
    }
}

/// AES-CM key derivation for an arbitrary length (KDR = 0 as always in
/// WebRTC: index/kdr = index = the packet-independent constant).
fn derive_key<const N: usize>(
    master_key: &[u8; 16],
    master_salt: &[u8; 14],
    label: u8,
    index: u64,
) -> [u8; N] {
    debug_assert_eq!(index, 0, "KDR=0 world: only index 0 exists");
    let aes = Aes128::new(master_key);

    // x = (label * 2^16 | index) XOR k_s, on the 112-bit salt, then the
    // AES-CM input appends the 16-bit block counter at the tail.
    // First 14 bytes of the AES-CM input are the 112-bit master salt.
    let mut x = [0u8; 16];
    x[..14].copy_from_slice(master_salt);
    // Key id layout: 7 bytes at the RIGHT of the salt; label first, then
    // the 48-bit index (always zero here) — see RFC 3711 §4.3.1.
    x[7] ^= label;
    // x[8..13] ^= index as 48 bits — zero, skip. x[14..16] = block ctr.

    let mut out = [0u8; N];
    let mut written = 0usize;
    let mut counter: u16 = 0;
    while written < N {
        x[14..16].copy_from_slice(&counter.to_be_bytes());
        let block = aes.encrypt_block(&x);
        let n = (N - written).min(16);
        out[written..written + n].copy_from_slice(&block[..n]);
        written += n;
        counter += 1;
    }
    out
}

/// Per-packet IV construction (RFC 3711 §4.1.1):
///
/// ```text
/// IV = (k_s * 2^16) XOR (SSRC * 2^64) XOR ((ROC || SEQ) * 2^16)
/// ```
///
/// i.e. salt bytes with SSRC at bytes 4..8 and the packet index at 8..14.
fn packet_iv(salt: &[u8; 14], ssrc: u32, roc: u32, seq: u16) -> [u8; 16] {
    let mut iv = [0u8; 16];
    for (i, b) in salt.iter().enumerate() {
        iv[i] = *b;
    }
    let ssrc_b = ssrc.to_be_bytes();
    for i in 0..4 {
        iv[4 + i] ^= ssrc_b[i];
    }
    let index: u64 = (u64::from(roc) << 16) | u64::from(seq);
    let idx_b = index.to_be_bytes();
    for i in 0..6 {
        iv[8 + i] ^= idx_b[2 + i];
    }
    iv
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProtectError {
    TooShort,
    Tag,
    Replay,
    Malformed,
}

/// One half of the duplex: sender-protect and receiver-unprotect need the
/// same keying but DIFFERENT replay bookkeeping direction, so they're
/// separate types — a single struct pretending to duplex protection is a
/// classic landmine (replay windows would be crossed).
pub struct SrtpProtector {
    keys: SessionKeys,
    /// Rollover estimation with the SAME RFC 3711 App A rule the
    /// receiver runs: sender and receiver must bump ROC in lockstep or
    /// every post-wrap packet's tag checks fail.
    tracker: streams::seq::SeqTracker,
}

impl SrtpProtector {
    pub fn new(keys: SessionKeys) -> SrtpProtector {
        SrtpProtector {
            keys,
            tracker: streams::seq::SeqTracker::default(),
        }
    }

    /// Feed the packet; returns the full MKI-free SRTP datagram. The
    /// underlying wire bytes come from the parsed packet's `raw` — the
    /// parse-built invariant is exactly that raw is complete.
    pub fn protect(&mut self, packet: &RtpPacket) -> Vec<u8> {
        let mut out = packet.raw.clone();
        self.protect_inplace(&mut out, packet.sequence);
        out
    }

    /// In-place: encrypt the payload of an already-carved wire frame and
    /// append the auth tag. `seq` must equal the frame's sequence field.
    pub fn protect_inplace(&mut self, frame: &mut Vec<u8>, seq: u16) {
        let roc = self.tracker.extend(seq) / 65536;

        let hdr_len = bottom_header_len(frame);
        let ssrc = u32::from_be_bytes([
            frame[hdr_len - 4],
            frame[hdr_len - 3],
            frame[hdr_len - 2],
            frame[hdr_len - 1],
        ]);
        let iv = packet_iv(&self.keys.salt, ssrc, roc, seq);
        let aes = Aes128::new(&self.keys.aes);
        aes.apply_keystream(&iv, &mut frame[hdr_len..], 0);

        // Authenticate: frame + ROC (the auth key's MAC covers the ROC
        // per §4.2.1), truncate to 80 bits, append.
        let mut mac_input = frame.clone();
        mac_input.extend_from_slice(&roc.to_be_bytes());
        let tag = hmac_sha1(&self.keys.auth, &mac_input);
        frame.extend_from_slice(&tag[..AUTH_TAG_LEN]);
    }
}

/// Receiver: keys + RFC 3711 App A index-estimation + replay window.
pub struct SrtpUnprotector {
    keys: SessionKeys,
    /// Estimated index bookkeeping (seq → ROC).
    tracker: streams::seq::SeqTracker,
    replay: ReplayWindow,
}

impl SrtpUnprotector {
    pub fn new(keys: SessionKeys) -> SrtpUnprotector {
        SrtpUnprotector {
            keys,
            tracker: streams::seq::SeqTracker::default(),
            replay: ReplayWindow::default(),
        }
    }

    /// Returns the srpt-decrypted plaintext datagram (RTP) or the failure.
    pub fn unprotect(&mut self, datagram: &[u8]) -> Result<Vec<u8>, ProtectError> {
        if datagram.len() < MIN_RTP_HEADER + AUTH_TAG_LEN {
            return Err(ProtectError::TooShort);
        }
        let (body, tag) = datagram.split_at(datagram.len() - AUTH_TAG_LEN);
        let seq = u16::from_be_bytes([body[2], body[3]]);
        let extended = self.tracker.extend(seq);
        let roc = extended / 65536;

        // Authenticate FIRST (before touching the replay window): an
        // attacker-arbitrary packet must not advance a trusted window.
        let mut mac_input = body.to_vec();
        mac_input.extend_from_slice(&roc.to_be_bytes());
        let expect = hmac_sha1(&self.keys.auth, &mac_input);
        let mut diff = 0u8;
        for (a, b) in tag.iter().zip(expect[..AUTH_TAG_LEN].iter()) {
            diff |= a ^ b;
        }
        if diff != 0 {
            return Err(ProtectError::Tag);
        }

        if self.replay.check_and_set(u64::from(extended)) == streams::replay::Verdict::Replay {
            return Err(ProtectError::Replay);
        }

        let mut out = body.to_vec();
        let hdr_len = bottom_header_len(&out);
        let ssrc = u32::from_be_bytes([
            out[hdr_len - 4],
            out[hdr_len - 3],
            out[hdr_len - 2],
            out[hdr_len - 1],
        ]);
        let iv = packet_iv(&self.keys.salt, ssrc, roc, seq);
        let aes = Aes128::new(&self.keys.aes);
        aes.apply_keystream(&iv, &mut out[hdr_len..], 0);
        Ok(out)
    }
}

/// Find where the RTP payload starts given the wire header layout —
/// duplicated knowledge of streams::packet, with the one-small-difference
/// that this is in-flight bytes, not a parsed packet.
fn bottom_header_len(frame: &[u8]) -> usize {
    if frame.len() < MIN_RTP_HEADER {
        return MIN_RTP_HEADER;
    }
    let cc = usize::from(frame[0] & 0x0F);
    let mut ofs = MIN_RTP_HEADER + cc * 4;
    let x = frame[0] & 0x10 != 0;
    if x && frame.len() >= ofs + 4 {
        let len = usize::from(u16::from_be_bytes([frame[ofs + 2], frame[ofs + 3]]));
        ofs += 4 + len * 4;
    }
    ofs.min(frame.len())
}

// ============================================================ RFC 7714
//
// AEAD_AES_GCM profiles: GCM gives confidentiality+integrity without the
// HMAC column, so a "session key set" is just (AES key, 12-byte salt).
// The keying structure mirrors RFC 3711's PRF (same labels, same 16-byte
// block window) with one bone-deep difference: the master salt is 12
// bytes, so the tail of the KDF input block stays zero — exactly the
// layout (libsrtp concordant) that lets `derive_key` run unchanged.
//
// Exposure discipline: callers BIND these through the profile enum;
// raw-salt constructors are public because the DTLS layer hands over
// pre-split key material, not secrets to re-derive.

use crate::crypto::aes256::Aes256;
use crate::crypto::gcm::AesGcm;

/// Session key material for RFC 7714 profiles (no auth key — AEAD is
/// self-authenticating).
#[derive(Clone)]
pub struct GcmSessionKeys {
    /// AES-128 or AES-256 cipher material.
    aes: GcmCipher,
    salt: [u8; 12],
}

#[derive(Clone)]
enum GcmCipher {
    Bits128([u8; 16]),
    Bits256([u8; 32]),
}

/// Which RFC 7714 profile is in effect — the only two we advertise.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GcmProfile {
    Aes128Gcm,
    Aes256Gcm,
}

/// RFC 3711 PRF derivation with the RFC 7714 salt length. The 12-byte
/// master salt loads into the same window `derive_key` uses for CM —
// the missing trailing 2 bytes are simply zero, which is how libsrtp
/// reaches identical session keys (§5.2.1: same PRF family, labels
/// unchanged). The AUTH label is never consulted — GCM self-
/// authenticates.
pub fn derive_gcm_session_keys_128(
    master_key: &[u8; 16],
    master_salt: &[u8; 12],
) -> GcmSessionKeys {
    let mut salt14 = [0u8; 14];
    salt14[..12].copy_from_slice(master_salt);
    let aes = derive_key::<16>(master_key, &salt14, labels::ENCRYPT, 0);
    let salt = derive_key::<12>(master_key, &salt14, labels::SALT, 0);
    GcmSessionKeys {
        aes: GcmCipher::Bits128(aes),
        salt,
    }
}

/// Same for the 256-bit profile (master salt is still 12 bytes — RFC
/// 7714 does not stretch it with the key).
pub fn derive_gcm_session_keys_256(
    master_key: &[u8; 32],
    master_salt: &[u8; 12],
) -> GcmSessionKeys {
    let mut salt14 = [0u8; 14];
    salt14[..12].copy_from_slice(master_salt);
    // The CM PRF is AES-CM keyed by the MASTER key — for a 256-bit
    // master that's AES-256; our derive_key is AES-128 only, so the
    // 256-bit path runs the same structure under the wider cipher.
    let aes = derive_key256::<32>(master_key, &salt14, labels::ENCRYPT, 0);
    let salt = derive_key256::<12>(master_key, &salt14, labels::SALT, 0);
    GcmSessionKeys {
        aes: GcmCipher::Bits256(aes),
        salt,
    }
}

/// Construct from PRE-SPLIT session material (what the DTLS layer owns
/// after RFC 5228 splitting: key + salt already session-level).
pub fn gcm_session_from_split(profile: GcmProfile, key: &[u8], salt: &[u8; 12]) -> GcmSessionKeys {
    match (profile, key.len()) {
        (GcmProfile::Aes128Gcm, 16) => {
            let mut k = [0u8; 16];
            k.copy_from_slice(key);
            GcmSessionKeys {
                aes: GcmCipher::Bits128(k),
                salt: *salt,
            }
        }
        (GcmProfile::Aes256Gcm, 32) => {
            let mut k = [0u8; 32];
            k.copy_from_slice(key);
            GcmSessionKeys {
                aes: GcmCipher::Bits256(k),
                salt: *salt,
            }
        }
        _ => unreachable!("gcm_session_from_split called with off-profile length"),
    }
}

impl GcmSessionKeys {
    fn seal(&self, iv: &[u8; 12], aad: &[u8], pt: &[u8]) -> Vec<u8> {
        match &self.aes {
            GcmCipher::Bits128(k) => AesGcm::new(Aes128::new(k)).seal_96bit(iv, aad, pt),
            GcmCipher::Bits256(k) => AesGcm::new(Aes256::new(k)).seal_96bit(iv, aad, pt),
        }
    }

    fn open(&self, iv: &[u8; 12], aad: &[u8], ct_tag: &[u8]) -> Option<Vec<u8>> {
        match &self.aes {
            GcmCipher::Bits128(k) => AesGcm::new(Aes128::new(k)).open_96bit(iv, aad, ct_tag),
            GcmCipher::Bits256(k) => AesGcm::new(Aes256::new(k)).open_96bit(iv, aad, ct_tag),
        }
    }
}

/// Per-packet IV, RFC 7714 §8.1 byte-for-byte:
/// [00 00 | SSRC (4) | ROC (4) | SEQ (2)] XOR 12-byte salt.
fn gcm_packet_iv(salt: &[u8; 12], ssrc: u32, roc: u32, seq: u16) -> [u8; 12] {
    let mut iv = *salt;
    let ssrc_b = ssrc.to_be_bytes();
    for i in 0..4 {
        iv[2 + i] ^= ssrc_b[i];
    }
    let roc_b = roc.to_be_bytes();
    for i in 0..4 {
        iv[6 + i] ^= roc_b[i];
    }
    let seq_b = seq.to_be_bytes();
    iv[10] ^= seq_b[0];
    iv[11] ^= seq_b[1];
    iv
}

/// Sender half for GCM profiles — same ROC bookkeeping contract as the
/// CM one.
pub struct SrtpGcmProtector {
    keys: GcmSessionKeys,
    tracker: streams::seq::SeqTracker,
}

impl SrtpGcmProtector {
    pub fn new(keys: GcmSessionKeys) -> SrtpGcmProtector {
        SrtpGcmProtector {
            keys,
            tracker: streams::seq::SeqTracker::default(),
        }
    }

    /// Returns the full SRTP packet: header ‖ GCM(payload) ‖ tag.
    pub fn protect(&mut self, packet: &RtpPacket) -> Vec<u8> {
        let mut frame = packet.raw.clone();
        self.protect_inplace(&mut frame, packet.sequence);
        frame
    }

    /// In-place variant for engine hot paths (same discipline as the CM
    /// one: seq is the frame's own field).
    pub fn protect_inplace(&mut self, frame: &mut Vec<u8>, seq: u16) {
        let roc = self.tracker.extend(seq) / 65536;
        let hdr_len = bottom_header_len(frame);
        let ssrc = u32::from_be_bytes([
            frame[hdr_len - 4],
            frame[hdr_len - 3],
            frame[hdr_len - 2],
            frame[hdr_len - 1],
        ]);
        let iv = gcm_packet_iv(&self.keys.salt, ssrc, roc, seq);
        let (aad, payload) = frame.split_at(hdr_len);
        let payload = payload.to_vec();
        let sealed = self.keys.seal(&iv, aad, &payload);
        frame.truncate(hdr_len);
        frame.extend_from_slice(&sealed);
    }
}

/// Receiver half — verify-before-any-mutation, replay window AFTER tag.
pub struct SrtpGcmUnprotector {
    keys: GcmSessionKeys,
    tracker: streams::seq::SeqTracker,
    replay: ReplayWindow,
}

impl SrtpGcmUnprotector {
    pub fn new(keys: GcmSessionKeys) -> SrtpGcmUnprotector {
        SrtpGcmUnprotector {
            keys,
            tracker: streams::seq::SeqTracker::default(),
            replay: ReplayWindow::default(),
        }
    }

    pub fn unprotect(&mut self, datagram: &[u8]) -> Result<Vec<u8>, ProtectError> {
        if datagram.len() < MIN_RTP_HEADER + 16 {
            return Err(ProtectError::TooShort);
        }
        let seq = u16::from_be_bytes([datagram[2], datagram[3]]);
        let extended = self.tracker.extend(seq);
        let roc = extended / 65536;
        let hdr_len = bottom_header_len(datagram);
        let ssrc = u32::from_be_bytes([
            datagram[hdr_len - 4],
            datagram[hdr_len - 3],
            datagram[hdr_len - 2],
            datagram[hdr_len - 1],
        ]);
        let (aad, ct_tag) = datagram.split_at(hdr_len);
        let iv = gcm_packet_iv(&self.keys.salt, ssrc, roc, seq);
        // Verify the tag FIRST — replay window moves only for honest
        // packets.
        let plain_payload = self.keys.open(&iv, aad, ct_tag).ok_or(ProtectError::Tag)?;
        if self.replay.check_and_set(u64::from(extended)) == streams::replay::Verdict::Replay {
            return Err(ProtectError::Replay);
        }
        let mut out = aad.to_vec();
        out.extend_from_slice(&plain_payload);
        Ok(out)
    }
}

/// RFC 3711 PRF with an AES-256 master key (same layout; wider cipher).
fn derive_key256<const N: usize>(
    master_key: &[u8; 32],
    master_salt: &[u8; 14],
    label: u8,
    index: u64,
) -> [u8; N] {
    debug_assert_eq!(index, 0, "KDR=0 world");
    let aes = Aes256::new(master_key);
    let mut x = [0u8; 16];
    x[..14].copy_from_slice(master_salt);
    x[7] ^= label;
    let mut out = [0u8; N];
    let mut written = 0usize;
    let mut counter: u16 = 0;
    while written < N {
        x[14..16].copy_from_slice(&counter.to_be_bytes());
        let block = aes.encrypt_block(&x);
        let n = (N - written).min(16);
        out[written..written + n].copy_from_slice(&block[..n]);
        written += n;
        counter += 1;
    }
    out
}

// ------------------------------------------------- profile bridging
//
// The engine binds ONE negotiated profile per association (CM or a GCM
// width) — the hot path dispatches through these enums so the media loop
// never carries "which crypto?" as free-floating state.

/// Sender-side protector, profile-mixed.
pub enum RtpProtector {
    Cm(SrtpProtector),
    Gcm(SrtpGcmProtector),
}

impl RtpProtector {
    pub fn protect(&mut self, packet: &RtpPacket) -> Vec<u8> {
        match self {
            RtpProtector::Cm(p) => p.protect(packet),
            RtpProtector::Gcm(p) => p.protect(packet),
        }
    }

    pub fn protect_inplace(&mut self, frame: &mut Vec<u8>, seq: u16) {
        match self {
            RtpProtector::Cm(p) => p.protect_inplace(frame, seq),
            RtpProtector::Gcm(p) => p.protect_inplace(frame, seq),
        }
    }
}

/// Receiver-side unprotector, profile-mixed.
pub enum RtpUnprotector {
    Cm(SrtpUnprotector),
    Gcm(SrtpGcmUnprotector),
}

impl RtpUnprotector {
    pub fn unprotect(&mut self, datagram: &[u8]) -> Result<Vec<u8>, ProtectError> {
        match self {
            RtpUnprotector::Cm(p) => p.unprotect(datagram),
            RtpUnprotector::Gcm(p) => p.unprotect(datagram),
        }
    }
}
