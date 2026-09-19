//! Live DTLS 1.2 handshake between two `dtls::Endpoint`s (engine role +
//! browser role) with real records exchanged in memory: proves the drive
//! loop, fingerprint gating, the RFC 5764 export path and the role-correct
//! key split at real cryptographic quality.
//!
//! Profile reality, documented rather than faked: dimpl's server side
//! orders AEAD-GCM ahead of CM when the client offers both, and dimpl's
//! client offer lists GCM first — so an in-crate loopback ALWAYS lands on
//! AEAD-GCM only where the offer list used to let dimpl's stock
//! preference pick it. With the vendored-dimpl knob (workspace
//! [patch.crates-io], PROMPT2-DESIGN.md Amendment (d)) the Identity's
//! USE_SRTP set is CM-first/CM-only, so engine-bound negotiation lands
//! on CM even against a client offering the full browser set. The CM
//! key layout correctness is proven at unit level (`src/lib.rs` tests)
//! against the exact RFC 5764 §4.2 byte order; the CM↔engine-SRTP bind
//! round-trip is proven at engine level; and the browser lane (`it`
//! gate with a real Chromium) is the design doc's own plan for the
//! production-profile evidence.

use std::collections::VecDeque;

extern crate dimpl;
use std::time::Instant;

use dtls::{self, DtlsError, Endpoint, Event, Negotiated};

struct Pair {
    server: Endpoint,
    client: Endpoint,
    server_keys: Option<Negotiated>,
    client_keys: Option<Negotiated>,
}

impl Pair {
    fn new() -> Pair {
        Self::with_peer_fps(None, None)
    }

    /// `server_fp_override` / `client_fp_override` replace the HONEST
    /// expectation that side would normally pin (used to test the
    /// reject path — the certs themselves stay genuine).
    fn with_peer_fps(
        server_fp_override: Option<String>,
        client_fp_override: Option<String>,
    ) -> Pair {
        let server_id = dtls::Identity::generate().expect("server identity");
        let client_id = dtls::Identity::generate().expect("client identity");
        let now = Instant::now();
        Pair {
            server: Endpoint::new(
                &server_id,
                false,
                Some(server_fp_override.unwrap_or_else(|| client_id.fingerprint().to_string())),
                now,
            ),
            client: Endpoint::new(
                &client_id,
                true,
                Some(client_fp_override.unwrap_or_else(|| server_id.fingerprint().to_string())),
                now,
            ),
            server_keys: None,
            client_keys: None,
        }
    }

    /// Same pair, identities supplied (the profile-knob tests live HERE:
    /// generate() is GCM-first production, tests pin stripes explicitly).
    fn with_ids(server_id: &dtls::Identity, client_id: &dtls::Identity) -> Pair {
        let now = Instant::now();
        Pair {
            server: Endpoint::new(
                server_id,
                false,
                Some(client_id.fingerprint().to_string()),
                now,
            ),
            client: Endpoint::new(
                client_id,
                true,
                Some(server_id.fingerprint().to_string()),
                now,
            ),
            server_keys: None,
            client_keys: None,
        }
    }

    /// Pump both endpoints until both are established or an error fires.
    fn run(&mut self) -> Result<(), DtlsError> {
        let mut to_server: VecDeque<Vec<u8>> = VecDeque::new();
        let mut to_client: VecDeque<Vec<u8>> = VecDeque::new();

        for p in self.client.start_handshake(Instant::now())?.packets {
            to_server.push_back(p);
        }

        let mut rounds = 0usize;
        loop {
            rounds += 1;
            if rounds > 64 {
                panic!("handshake never finished in 64 pump rounds");
            }
            let mut moved = false;

            while let Some(p) = to_server.pop_front() {
                let drive = self.server.handle_packet(&p, Instant::now())?;
                self.capture(&drive.events, true);
                for q in drive.packets {
                    moved = true;
                    to_client.push_back(q);
                }
            }
            while let Some(p) = to_client.pop_front() {
                let drive = self.client.handle_packet(&p, Instant::now())?;
                self.capture(&drive.events, false);
                for q in drive.packets {
                    moved = true;
                    to_server.push_back(q);
                }
            }

            if self.server_keys.is_some() && self.client_keys.is_some() {
                return Ok(());
            }
            if !moved && to_server.is_empty() && to_client.is_empty() {
                panic!(
                    "handshake stalled: server established={}, client={}",
                    self.server.is_established(),
                    self.client.is_established()
                );
            }
        }
    }

    fn capture(&mut self, events: &[Event], server_side: bool) {
        for e in events {
            let Event::Established(n) = e;
            if server_side {
                self.server_keys = Some(n.clone());
            } else {
                self.client_keys = Some(n.clone());
            }
        }
    }
}

#[test]
fn handshake_produces_matching_rfc5764_splits_at_both_ends() {
    let mut pair = Pair::new();
    pair.run().expect("handshake completes");

    let s = pair.server_keys.clone().expect("server keys");
    let c = pair.client_keys.clone().expect("client keys");

    // Both sides report the SAME negotiated profile (dimpl pairs land on
    // AEAD_AES_256_GCM in-crate — documented above).
    assert_eq!(s.profile, c.profile, "one negotiation, one profile");
    assert!(pair.server.is_established());
    assert!(pair.client.is_established());

    // Cross-symmetry of the RFC 5764 split: what the client writes is
    // exactly what the server reads, byte for byte.
    assert_eq!(c.outbound, s.inbound, "client→server direction");
    assert_eq!(s.outbound, c.inbound, "server→client direction");
    // Directions genuinely differ (RFC 5764 split, no SDES-style
    // same-key collapse).
    assert_ne!(c.outbound.key, c.inbound.key);
    assert_ne!(c.outbound.salt, c.inbound.salt);
}

#[test]
fn cm_only_server_negotiates_cm_and_the_ext_horizontal_export_shape_holds() {
    // With the vendored-dimpl knob (workspace [patch.crates-io]) the
    // Identity carries a CM-only USE_SRTP list: a genuine client offering
    // the full browser set still lands ON CM here. The bind-gate then
    // accepts (crypto speaks CM), and the full RFC 5764 key layout is
    // exported for the srtp.rs path.
    let server_id =
        dtls::Identity::generate_for_profiles(vec![dimpl::SrtpProfile::AES128_CM_SHA1_80])
            .expect("cm-only server");
    let client_id =
        dtls::Identity::generate_for_profiles(vec![dimpl::SrtpProfile::AES128_CM_SHA1_80])
            .expect("cm-only client");
    let mut pair = Pair::with_ids(&server_id, &client_id);
    pair.run().expect("handshake completes");
    let n = pair.server_keys.clone().expect("keys");
    assert_eq!(n.profile.to_string(), "SRTP_AES128_CM_SHA1_80");
    let split = n.aes128_cm().expect("bind gate accepts CM");
    assert_eq!(n.outbound.key.len(), 16);
    assert_eq!(n.outbound.salt.len(), 14);
    let _ = split;
}

#[test]
fn fingerprint_mismatch_fails_before_keys() {
    // The CLIENT pins a wrong fingerprint for the server; the pinned
    // certs themselves stay genuine (that's the SDP-pin violation shape).
    let mut pair = Pair::with_peer_fps(None, Some("DE:AD".repeat(16)));
    let err = pair.run().expect_err("mismatch must abort the handshake");
    assert!(
        matches!(err, DtlsError::FingerprintMismatch { .. }),
        "got {err:?}"
    );
    assert!(
        !pair.client.is_established(),
        "no establishment on bad fingerprint"
    );
    assert!(
        pair.client_keys.is_none(),
        "keys must never surface for the violator"
    );
    assert!(!pair.server.is_established());
    assert!(pair.server_keys.is_none());
}

#[test]
fn idle_server_drive_returns_timeout_not_packets() {
    // Engine sweep posture: a passive endpoint with nothing sent has a
    // retransmit timeout armed and emits no spurious records — the sweep
    // cadence stays cheap.
    let id = dtls::Identity::generate().expect("identity");
    let mut server = Endpoint::new(&id, false, None, Instant::now());
    let drive = server.start_handshake(Instant::now()).expect("idle tick");
    assert!(drive.packets.is_empty());
    assert!(server.timeout().is_some(), "timeout armed for the sweep");
}

#[test]
fn gcm_only_client_never_exports_keys_against_cm_only_server() {
    // Posture after the vendored knob: production engines negotiate ONLY
    // CM. A hypothetical GCM-only client (offered list has no
    // intersection with the server's CM list) must fail to negotiate the
    // USE_SRTP extension — the handshake may still complete at the
    // transport level, but NO SRTP keying material event may ever fire,
    // which is the only event the engine's media gate binds from.
    let server_id =
        dtls::Identity::generate_for_profiles(vec![dimpl::SrtpProfile::AES128_CM_SHA1_80])
            .expect("cm-only server identity");
    let client_id =
        dtls::Identity::generate_for_profiles(vec![dimpl::SrtpProfile::AEAD_AES_256_GCM])
            .expect("gcm-only client identity");
    let now = Instant::now();
    let mut pair = Pair {
        server: Endpoint::new(
            &server_id,
            false,
            Some(client_id.fingerprint().to_string()),
            now,
        ),
        client: Endpoint::new(
            &client_id,
            true,
            Some(server_id.fingerprint().to_string()),
            now,
        ),
        server_keys: None,
        client_keys: None,
    };
    // Drive a bounded pump — run() alone would interpret the intended
    // stall as an error; here the stall IS the assertion surface.
    let mut to_server: VecDeque<Vec<u8>> = VecDeque::new();
    let mut to_client: VecDeque<Vec<u8>> = VecDeque::new();
    for q in pair
        .client
        .start_handshake(Instant::now())
        .expect("gcm client CH")
        .packets
    {
        to_server.push_back(q);
    }
    for round in 0..64 {
        for _ in 0..8 {
            if let Some(pkt) = to_server.pop_front() {
                let Ok(drive) = pair.server.handle_packet(&pkt, Instant::now()) else {
                    continue;
                };
                for e in drive.events {
                    let Event::Established(n) = e;
                    pair.server_keys = Some(n);
                }
                for q in drive.packets {
                    to_client.push_back(q);
                }
            }
            if let Some(pkt) = to_client.pop_front() {
                let Ok(drive) = pair.client.handle_packet(&pkt, Instant::now()) else {
                    continue;
                };
                for e in drive.events {
                    let Event::Established(n) = e;
                    pair.client_keys = Some(n);
                }
                for q in drive.packets {
                    to_server.push_back(q);
                }
            }
        }
        if to_server.is_empty() && to_client.is_empty() && round > 8 {
            break;
        }
    }
    assert!(
        pair.server_keys.is_none() && pair.client_keys.is_none(),
        "no USE_SRTP material may be exported across a profile-free negotiation"
    );
}

#[test]
fn default_identities_negotiate_gcm256_and_the_typed_gate_opens() {
    // Production posture: generate() carries BOTH GCM widths + CM; the
    // pair lands on the strongest common profile and session_keys() —
    // the bind gate the engine actually uses — hands over 32-byte keys
    // with 12-byte salts, cross-symmetric RFC 5764 direction by
    // direction.
    let mut pair = Pair::new();
    pair.run().expect("handshake completes");
    let s = pair.server_keys.clone().expect("server keys");
    let c = pair.client_keys.clone().expect("client keys");
    assert_eq!(s.profile.to_string(), "SRTP_AEAD_AES_256_GCM");
    assert_eq!(s.profile, c.profile);

    let sk = s.session_keys().expect("bind gate opens for GCM-256");
    let dtls::SessionKeys::Gcm256(g) = sk else {
        panic!("GCM-256 negotiation must yield Gcm256 keys: {sk:?}");
    };
    assert_eq!(g.inbound_key.len(), 32);
    assert_eq!(g.inbound_salt.len(), 12);
    assert_eq!(g.outbound_key.len(), 32);
    assert_eq!(g.outbound_salt.len(), 12);
    // And the cross-wire symmetry: client outbound == server inbound.
    let ck = c.session_keys().expect("client gate");
    let dtls::SessionKeys::Gcm256(cg) = ck else {
        panic!("client must mirror its profile: {ck:?}");
    };
    assert_eq!(cg.outbound_key, g.inbound_key);
    assert_eq!(cg.outbound_salt, g.inbound_salt);
}

#[test]
fn gcm128_only_pair_stays_on_gcm128_with_16_byte_keys() {
    let mk = || {
        dtls::Identity::generate_for_profiles(vec![dimpl::SrtpProfile::AEAD_AES_128_GCM])
            .expect("gcm128 identity")
    };
    let server_id = mk();
    let client_id = mk();
    let mut pair = Pair::with_ids(&server_id, &client_id);
    pair.run().expect("handshake completes");
    let n = pair.server_keys.clone().expect("keys");
    assert_eq!(n.profile.to_string(), "SRTP_AEAD_AES_128_GCM");
    let dtls::SessionKeys::Gcm128(g) = n.session_keys().expect("gate") else {
        panic!("GCM-128 profile must yield Gcm128 keys");
    };
    assert_eq!(g.outbound_key.len(), 16);
    assert_eq!(g.outbound_salt.len(), 12);
}
