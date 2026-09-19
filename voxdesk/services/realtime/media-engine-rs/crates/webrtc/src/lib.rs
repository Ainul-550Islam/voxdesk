//! webrtc — the ET2 path's browser-facing protocol half:
//!
//! * `crypto` — owned primitives (AES-128, SHA-1, SHA-256, HMAC) per the
//!   offline-build rule; test-vector-anchored.
//! * `stun` — RFC 5389 framing + MESSAGE-INTEGRITY + FINGERPRINT.
//! * `ice`  — RFC 8445 ICE-lite agent (SFU side).
//! * `sdp`  — RFC 8866 offer parse / answer build.
//! * `srtp` — RFC 3711 AES-128-CM SHA1-80 protect/unprotect.
//!
//! The DTLS server handshake is deliberately out of scope (that is a TLS
//! stack); the DTLS-SRTP key extraction happens in `sessions` via our
//! SRTP `derive_session_keys` once keying material is in hand.
//!
//! ```no_run
//! # use webrtc::srtp::{SessionKeys, derive_session_keys, SrtpProtector};
//! let master_key = [0u8; 16];
//! let master_salt = [0u8; 14];
//! let keys: SessionKeys = derive_session_keys(&master_key, &master_salt);
//! let mut protector = SrtpProtector::new(keys);
//! let _ = &mut protector;
//! ```

pub mod crypto;
pub mod ice;
pub mod sdp;
pub mod srtp;
pub mod stun;
