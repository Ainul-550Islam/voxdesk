//! DTLS-SRTP (RFC 5764) termination for the media engine — a thin,
//! auditable shim over `dimpl` (Sans-IO, sync, WebRTC-targeted). The
//! engine owns all sockets; this crate owns only protocol state.
//!
//! Flow per session (browser ⇔ engine, ICE-lite already nominated):
//!   1. SDP offer arrives → `Endpoint::new(identity, active, peer_fp, now)`
//!      (`active` mirrors the answer's `a=setup:` decision: we are the
//!      DTLS client only when the offer said `passive`).
//!   2. Engine demux hands us every DTLS datagram → `handle_packet`.
//!   3. We emit `Output::Packet`s back on the same 5-tuple, remember the
//!      retransmit timeout, and surface `Event::Established` once.
//!   4. The negotiated SRTP profile + role-correct key split travel in
//!      the event; the ENGINE decides what its crypto can bind (today:
//!      `Negotiated::aes128_cm()` only — GCM is exported correctly but
//!      rejected at bind time, loudly).
//!
//! Deliberate hardening:
//!   * Peer certificate is SHA-256-fingerprint-checked against the SDP
//!     `a=fingerprint` BEFORE keys are surfaced — a mismatched cert never
//!     reaches the media path.
//!   * Key layout is RFC 5764 §4.1.1/§4.2-exact for every profile dimpl
//!     negotiates (CM and both AEAD-GCM profiles), role-aware at split.

use std::fmt;
use std::sync::Arc;
use std::time::Instant;

use dimpl::{Config, Dtls, Output, SrtpProfile};

/// Re-export so callers bind the exact negotiated profile type without a
/// second dependency edge.
pub use dimpl::DtlsCertificate;

/// Default MTU-safe buffer for one poll cycle; resized on demand
/// (`Output::BufferTooSmall`) — never sized by guesswork alone.
const DRIVE_BUF: usize = 2048;
/// Hard cap on poll iterations per drive; the Sans-IO contract says a
/// cycle ends at `Timeout`, but a non-exhaustive future variant must not
/// hang the media loop.
const DRIVE_CAP: usize = 4096;

/// Per-boot engine identity: the self-signed ECDSA P-256 certificate the
/// engine answers with, plus its SDP-formatted SHA-256 fingerprint.
#[derive(Clone)]
pub struct Identity {
    config: Arc<Config>,
    cert: DtlsCertificate,
    fingerprint: String,
}

impl Identity {
    /// Generate the engine's self-signed WebRTC identity. Fails only if
    /// the crypto provider / OS CSPRNG is unavailable.
    /// Production identity: every profile the engine has REAL crypto for
    /// — RFC 7714 GCM in both widths first, with the RFC 3711 CM
    /// baseline as the universal floor. dimpl's server picks in ITS
    /// preference order over this exact list.
    pub fn generate() -> Result<Identity, DtlsError> {
        Self::generate_for_profiles(vec![
            SrtpProfile::AEAD_AES_256_GCM,
            SrtpProfile::AEAD_AES_128_GCM,
            SrtpProfile::AES128_CM_SHA1_80,
        ])
    }

    /// Identity generator with an explicit USE_SRTP profile list — the
    /// loopback tests pin CM-only here; production uses `generate()`.
    pub fn generate_for_profiles(profiles: Vec<SrtpProfile>) -> Result<Identity, DtlsError> {
        let cert = dimpl::certificate::generate_self_signed_certificate()
            .map_err(|_| DtlsError::CertificateGenerate)?;
        let fingerprint = cert.fingerprint_str();
        // The profile list comes straight from the caller through the
        // vendored-dimpl knob (workspace Cargo.toml [patch.crates-io] +
        // PROMPT2-DESIGN.md Amendment (d)) — the SAME list drives both
        // what the server accepts and what clients offer.
        let config = Config::builder()
            .srtp_profiles(profiles)
            .build()
            .map_err(|_| DtlsError::UnsupportedSrtpProfile("srtp config build".into()))?;
        Ok(Identity {
            config: Arc::new(config),
            cert,
            fingerprint,
        })
    }

    /// `a=fingerprint:sha-256 <this>` value, "AA:BB:.."-formatted — the
    /// exact string that must appear in every SDP answer this engine emits.
    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }
}

impl fmt::Debug for Identity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Identity")
            .field("fingerprint", &self.fingerprint)
            .finish()
    }
}

/// Master key + salt for one DTLS-SRTP direction. Lengths follow the
/// negotiated profile (RFC 5764 §4.1.1 Table 1):
/// AES128_CM_SHA1_80 → 16/14, AEAD_AES_128_GCM → 16/12,
/// AEAD_AES_256_GCM → 32/12.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MasterPair {
    pub key: Vec<u8>,
    pub salt: Vec<u8>,
}

/// The typed CM pair the engine's RFC-3711 crypto actually binds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CmKeys {
    /// Master key + salt protecting traffic the PEER writes to us.
    pub inbound_key: [u8; 16],
    pub inbound_salt: [u8; 14],
    /// Master key + salt protecting traffic WE write to the peer.
    pub outbound_key: [u8; 16],
    pub outbound_salt: [u8; 14],
}

/// Typed GCM pair (RFC 7714): salt is 12 bytes, key is 16 or 32
/// depending on the negotiated profile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GcmKeys {
    pub inbound_key: Vec<u8>,
    pub inbound_salt: [u8; 12],
    pub outbound_key: Vec<u8>,
    pub outbound_salt: [u8; 12],
}

/// Profile-typed session keys: the ONE shape the engine binds. CM and
/// GCM paths diverge HERE — never inside the media hot path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SessionKeys {
    Cm(CmKeys),
    Gcm128(GcmKeys),
    Gcm256(GcmKeys),
}

/// Everything one successful handshake produced: the negotiated profile,
/// plus the RFC 5764 §4.2 split already mapped for OUR role (inbound is
/// always "what the peer writes", outbound "what we write").
#[derive(Clone, Debug)]
pub struct Negotiated {
    pub profile: SrtpProfile,
    pub inbound: MasterPair,
    pub outbound: MasterPair,
}

impl Negotiated {
    /// Typed CM view for the engine's RFC-3711 crypto. Any other profile
    /// is a REAL negotiation outcome that this engine cannot bind — the
    /// caller must fail the session closed (log + teardown), never guess
    /// at another cipher suite.
    pub fn aes128_cm(&self) -> Result<CmKeys, DtlsError> {
        if self.profile != SrtpProfile::AES128_CM_SHA1_80 {
            return Err(DtlsError::UnsupportedSrtpProfile(self.profile.to_string()));
        }
        let want = |pair: &MasterPair| (pair.key.len(), pair.salt.len());
        if want(&self.inbound) != (16, 14) || want(&self.outbound) != (16, 14) {
            return Err(DtlsError::BadKeyingMaterialLength(
                self.inbound.key.len() * 2 + self.inbound.salt.len() * 2,
            ));
        }
        Ok(CmKeys {
            inbound_key: copy16(&self.inbound.key),
            inbound_salt: copy14(&self.inbound.salt),
            outbound_key: copy16(&self.outbound.key),
            outbound_salt: copy14(&self.outbound.salt),
        })
    }

    /// Profile-general session keys — the bind gate REPLACEMENT. Only
    /// profiles this engine has real crypto for cross this gate; every
    /// other profile is a real negotiation outcome worth a loud refusal,
    /// never a guess.
    pub fn session_keys(&self) -> Result<SessionKeys, DtlsError> {
        match self.profile {
            SrtpProfile::AES128_CM_SHA1_80 => Ok(SessionKeys::Cm(self.aes128_cm()?)),
            SrtpProfile::AEAD_AES_128_GCM => Ok(SessionKeys::Gcm128(self.gcm_keys(16)?)),
            SrtpProfile::AEAD_AES_256_GCM => Ok(SessionKeys::Gcm256(self.gcm_keys(32)?)),
            other => Err(DtlsError::UnsupportedSrtpProfile(other.to_string())),
        }
    }

    fn gcm_keys(&self, key_len: usize) -> Result<GcmKeys, DtlsError> {
        let want = |pair: &MasterPair| (pair.key.len(), pair.salt.len());
        if want(&self.inbound) != (key_len, 12) || want(&self.outbound) != (key_len, 12) {
            return Err(DtlsError::BadKeyingMaterialLength(
                self.inbound.key.len() * 2 + self.inbound.salt.len() * 2,
            ));
        }
        Ok(GcmKeys {
            inbound_key: self.inbound.key.clone(),
            inbound_salt: copy12(&self.inbound.salt),
            outbound_key: self.outbound.key.clone(),
            outbound_salt: copy12(&self.outbound.salt),
        })
    }
}

fn copy16(v: &[u8]) -> [u8; 16] {
    let mut out = [0u8; 16];
    out.copy_from_slice(&v[..16]);
    out
}
fn copy14(v: &[u8]) -> [u8; 14] {
    let mut out = [0u8; 14];
    out.copy_from_slice(&v[..14]);
    out
}
fn copy12(v: &[u8]) -> [u8; 12] {
    let mut out = [0u8; 12];
    out.copy_from_slice(&v[..12]);
    out
}

/// The one terminal event the engine consumes.
#[derive(Clone, Debug)]
pub enum Event {
    /// Handshake completed, peer cert verified (when a fingerprint was
    /// expected), keying material exported and role-split. Fired once.
    Established(Negotiated),
}

#[derive(Debug)]
pub enum DtlsError {
    /// The OS could not mint our identity (`rcgen`/provider failure).
    CertificateGenerate,
    /// A datagram or timer made the protocol state machine fail — the
    /// endpoint must be scrapped; the alert (if any) is in the drive.
    Handshake(dimpl::Error),
    /// Handshake negotiated an SRTP profile the engine's crypto cannot
    /// bind (not AES128_CM_SHA1_80). WebRTC clients always offer CM;
    /// reaching this is a non-WebRTC client or misconfiguration — the
    /// session must fail closed.
    UnsupportedSrtpProfile(String),
    /// Exported keying material inconsistent with the negotiated
    /// profile's layout (paranoia bound — dimpl derives length itself).
    BadKeyingMaterialLength(usize),
    /// SHA-256 of the peer's DER cert does not match the SDP-pinned
    /// fingerprint. Connection is untrusted; fail closed.
    FingerprintMismatch { expected: String, got: String },
    /// A single `poll_output` drive exceeded the spin cap — the Sans-IO
    /// contract (cycle always ends at `Timeout`) was violated upstream.
    DriveDidNotTerminate,
}

impl fmt::Display for DtlsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DtlsError::CertificateGenerate => write!(f, "self-signed identity generation failed"),
            DtlsError::Handshake(e) => write!(f, "dtls handshake: {e}"),
            DtlsError::UnsupportedSrtpProfile(p) => {
                write!(f, "negotiated SRTP profile {p} is not usable by the engine")
            }
            DtlsError::BadKeyingMaterialLength(n) => {
                write!(
                    f,
                    "keying material length {n} inconsistent with profile layout"
                )
            }
            DtlsError::FingerprintMismatch { expected, got } => {
                write!(
                    f,
                    "peer fingerprint mismatch: expected {expected}, got {got}"
                )
            }
            DtlsError::DriveDidNotTerminate => {
                write!(f, "poll_output drive exceeded spin cap {DRIVE_CAP}")
            }
        }
    }
}

impl std::error::Error for DtlsError {}

/// Everything one drive cycle produced, ready for the engine to act on.
#[derive(Debug, Default)]
pub struct Drive {
    /// DTLS records to send back on the same 5-tuple (order matters).
    pub packets: Vec<Vec<u8>>,
    /// Terminal events (at most one Established per endpoint lifetime).
    pub events: Vec<Event>,
}

/// One DTLS association bound to one media session's nominated 5-tuple.
pub struct Endpoint {
    dtls: Dtls,
    /// Our role: true = we are the DTLS client (`a=setup:active` answer).
    active: bool,
    /// SDP-pinned peer fingerprint, case-normalised. `None` skips the
    /// check — reached only by fixture-grade offers; every browser offer
    /// carries `a=fingerprint`.
    expected_fingerprint: Option<String>,
    /// Last `Output::Timeout` — the engine's sweep triggers
    /// `handle_timeout` at (or after) this instant.
    timeout: Option<Instant>,
    established: bool,
}

impl fmt::Debug for Endpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Endpoint")
            .field("active", &self.active)
            .field("established", &self.established)
            .finish()
    }
}

impl Endpoint {
    /// `identity` is the engine's per-boot cert. `active` is our DTLS
    /// role (dimpl defaults to server, matching `a=setup:passive`; the
    /// answer builder chooses the role from the offer's `a=setup`).
    pub fn new(
        identity: &Identity,
        active: bool,
        expected_fingerprint: Option<String>,
        now: Instant,
    ) -> Endpoint {
        let mut dtls = Dtls::new_12(identity.config.clone(), identity.cert.clone(), now);
        if active {
            dtls.set_active(true);
        }
        Endpoint {
            dtls,
            active,
            expected_fingerprint: expected_fingerprint.map(|f| f.to_ascii_uppercase()),
            timeout: None,
            established: false,
        }
    }

    pub fn is_established(&self) -> bool {
        self.established
    }

    pub const fn timeout(&self) -> Option<Instant> {
        self.timeout
    }

    /// Kick off / progress the association without inbound bytes. For an
    /// ACTIVE endpoint this emits the ClientHello; for a passive one it
    /// is a timer-checked no-op (dimpl arms flights on `handle_timeout`;
    /// premature ticks are filtered by the armed instants). Safe to call
    /// spuriously.
    pub fn start_handshake(&mut self, now: Instant) -> Result<Drive, DtlsError> {
        self.dtls
            .handle_timeout(now)
            .map_err(DtlsError::Handshake)?;
        self.drain()
    }

    /// Feed one inbound DTLS datagram (demux-decoded by the transport).
    /// `now` doubles as the flight-arming tick (see `start_handshake`).
    pub fn handle_packet(&mut self, packet: &[u8], now: Instant) -> Result<Drive, DtlsError> {
        self.dtls
            .handle_packet(packet)
            .map_err(DtlsError::Handshake)?;
        self.dtls
            .handle_timeout(now)
            .map_err(DtlsError::Handshake)?;
        self.drain()
    }

    /// Retransmission timer fired (engine sweep cadence).
    pub fn handle_timeout(&mut self, now: Instant) -> Result<Drive, DtlsError> {
        self.dtls
            .handle_timeout(now)
            .map_err(DtlsError::Handshake)?;
        self.drain()
    }

    /// Drain the Sans-IO output queue until `Timeout` (the contract's
    /// cycle terminator); grow the buffer only if asked.
    fn drain(&mut self) -> Result<Drive, DtlsError> {
        let mut drive = Drive::default();
        let mut buf = vec![0u8; DRIVE_BUF];
        let mut spins = 0usize;
        loop {
            spins += 1;
            if spins > DRIVE_CAP {
                // Sans-IO contract broken by a future variant; bail loud
                // rather than spin the media loop.
                return Err(DtlsError::DriveDidNotTerminate);
            }
            match self.dtls.poll_output(&mut buf) {
                Output::Packet(p) => drive.packets.push(p.to_vec()),
                Output::BufferTooSmall { needed } => {
                    buf.resize(needed.max(buf.len() * 2), 0);
                }
                Output::Connected => {}
                Output::PeerCert(der) => {
                    if let Some(expected) = &self.expected_fingerprint {
                        let got = dimpl::certificate::format_fingerprint(
                            &dimpl::certificate::calculate_fingerprint(der),
                        );
                        if &got != expected {
                            return Err(DtlsError::FingerprintMismatch {
                                expected: expected.clone(),
                                got,
                            });
                        }
                    }
                }
                Output::KeyingMaterial(km, profile) => {
                    let negotiated = self.split(&km, profile)?;
                    self.established = true;
                    drive.events.push(Event::Established(negotiated));
                }
                Output::Timeout(when) => {
                    self.timeout = Some(when);
                    return Ok(drive);
                }
                Output::ApplicationData(_) | Output::CloseNotify => {}
                // Non-exhaustive: unknown future variants end the cycle
                // defensively with what we have — packets/events stay valid.
                _ => return Ok(drive),
            }
        }
    }

    /// RFC 5764 §4.2: the exporter gives client_write first, then
    /// server_write (all keys first, then both salts). Profile Table 1
    /// lengths: CM → 16/14 (60 total), GCM-128 → 16/12 (56),
    /// GCM-256 → 32/12 (88). OUR direction mapping flips on role: when
    /// we are the server, the client's writes are our inbound.
    fn split(&self, km: &[u8], profile: SrtpProfile) -> Result<Negotiated, DtlsError> {
        let (klen, slen) = match profile {
            SrtpProfile::AES128_CM_SHA1_80 => (16usize, 14usize),
            SrtpProfile::AEAD_AES_128_GCM => (16, 12),
            SrtpProfile::AEAD_AES_256_GCM => (32, 12),
            // dimpl's enum is closed today; guard against a future
            // profile whose layout we haven't verified here.
            _ => return Err(DtlsError::UnsupportedSrtpProfile(format!("{profile}"))),
        };
        if km.len() != 2 * (klen + slen) {
            return Err(DtlsError::BadKeyingMaterialLength(km.len()));
        }
        let ck = km[0..klen].to_vec();
        let sk = km[klen..2 * klen].to_vec();
        let cs = km[2 * klen..2 * klen + slen].to_vec();
        let ss = km[2 * klen + slen..].to_vec();
        let (inbound, outbound) = if self.active {
            // We are the DTLS client: WE write with the client's keys.
            (
                MasterPair { key: sk, salt: ss },
                MasterPair { key: ck, salt: cs },
            )
        } else {
            // DTLS server: the default WebRTC/SFU posture.
            (
                MasterPair { key: ck, salt: cs },
                MasterPair { key: sk, salt: ss },
            )
        };
        Ok(Negotiated {
            profile,
            inbound,
            outbound,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Crafted exporter output where byte i == i+1 so every field is
    /// position-identifiable. CM layout (60 bytes):
    /// client key 0x01..0x10, server key 0x11..0x20,
    /// client salt 0x21..0x2E, server salt 0x2F..0x3C.
    fn synthetic_cm() -> [u8; 60] {
        let mut km = [0u8; 60];
        for (i, b) in km.iter_mut().enumerate() {
            *b = (i + 1) as u8;
        }
        km
    }

    /// Same trick at the GCM-256 layout: client key 0x01..0x20,
    /// server key 0x21..0x40, client salt 0x41..0x4C, server 0x4D..0x58.
    fn synthetic_gcm256() -> [u8; 88] {
        let mut km = [0u8; 88];
        for (i, b) in km.iter_mut().enumerate() {
            *b = ((i + 1) & 0x7F) as u8;
        }
        km
    }

    fn endpoint(active: bool) -> Endpoint {
        let id = Identity::generate().expect("identity");
        Endpoint::new(&id, active, None, Instant::now())
    }

    #[test]
    fn rfc5764_split_server_role_inbound_is_client_write_cm() {
        // We are the DTLS server: the client writes toward us, so the
        // CLIENT's key/salt pair must become our INBOUND pair.
        let n = endpoint(false)
            .split(&synthetic_cm(), SrtpProfile::AES128_CM_SHA1_80)
            .unwrap();
        assert_eq!(n.inbound.key[15], 16, "client key tail");
        assert_eq!(n.inbound.salt[13], 46, "client salt tail");
        assert_eq!(n.outbound.key[15], 32, "server key tail");
        assert_eq!(n.outbound.salt[13], 60, "server salt tail");
        let cm = n.aes128_cm().expect("CM view");
        assert_eq!(cm.inbound_key[0], 1);
        assert_eq!(cm.outbound_salt[13], 60);
    }

    #[test]
    fn rfc5764_split_client_role_inbound_is_server_write_cm() {
        // We are the DTLS client: the SERVER writes toward us.
        let n = endpoint(true)
            .split(&synthetic_cm(), SrtpProfile::AES128_CM_SHA1_80)
            .unwrap();
        assert_eq!(n.inbound.key[15], 32, "server key tail");
        assert_eq!(n.inbound.salt[13], 60, "server salt tail");
        assert_eq!(n.outbound.key[15], 16, "client key tail");
        assert_eq!(n.outbound.salt[13], 46, "client salt tail");
    }

    #[test]
    fn split_layouts_gcm_exported_exactly() {
        let n = endpoint(false)
            .split(&synthetic_gcm256(), SrtpProfile::AEAD_AES_256_GCM)
            .unwrap();
        assert_eq!(n.inbound.key.len(), 32);
        assert_eq!(n.outbound.key.len(), 32);
        assert_eq!(n.inbound.salt.len(), 12);
        assert_eq!(n.outbound.key[31], 0x40, "server key tail");
        assert_eq!(n.outbound.salt[11], 0x58, "server salt tail");
        // ...but the engine crypto never pretends to bind it as CM.
        assert!(matches!(
            n.aes128_cm(),
            Err(DtlsError::UnsupportedSrtpProfile(_))
        ));
    }

    #[test]
    fn split_rejects_wrong_length() {
        let err = endpoint(false)
            .split(&[0u8; 59], SrtpProfile::AES128_CM_SHA1_80)
            .expect_err("59 bytes must not parse");
        assert!(matches!(err, DtlsError::BadKeyingMaterialLength(59)));
    }

    #[test]
    fn identity_fingerprint_is_sha256_hex_pairs() {
        let id = Identity::generate().expect("identity");
        let fp = id.fingerprint();
        // "AA:BB:.." over 32 bytes ⇒ 32 hex pairs, 31 colons.
        assert_eq!(fp.len(), 95);
        assert!(fp
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b':'));
        // Fresh identities differ (per-boot removability of identity pin).
        let id2 = Identity::generate().expect("identity 2");
        assert_ne!(fp, id2.fingerprint());
    }
}
