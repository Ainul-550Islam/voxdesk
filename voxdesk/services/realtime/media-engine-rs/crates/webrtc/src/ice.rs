//! ICE-lite (RFC 8445 §2.7): the SFU-side half of ICE. We do NOT gather
//! STUN server candidates (we claim exactly one public host candidate)
//! and we do NOT send checks — we ACCEPT them, answer them, and remember
//! which remote candidate won, per RFC 8445's "lite" profile.
//!
//! Concurrency contract: one `LiteAgent` per session, owned by that
//! session's IO thread; no locks inside (the Supervisor owns sharing).

use super::stun::{attrs as stun_attrs, types as stun_types, IceRole, StunBuilder, StunMessage};

/// A remote candidate parsed from `a=candidate:...` (RFC 8839 §5.1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemoteCandidate {
    pub foundation: u32,
    pub component: u16,
    pub protocol: String, // "udp" (tcp candidates are noted but never paired)
    pub priority: u32,
    pub ip: [u8; 4],
    pub port: u16,
    pub typ: String, // "host" | "srflx" | "relay"
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IceError {
    Malformed(String),
    NotUdp,
}

impl RemoteCandidate {
    /// Parses the attribute body WITHOUT the leading "candidate:" — or
    /// with it if a transport layer passed the raw attribute; trimming
    /// here once keeps every caller a one-liner.
    pub fn parse(text: &str) -> Result<RemoteCandidate, IceError> {
        let text = text.strip_prefix("candidate:").unwrap_or(text).trim();
        let mut it = text.split_whitespace();

        let foundation: u32 = next("foundation", &mut it)?
            .parse()
            .map_err(|_| IceError::Malformed("foundation not u32".into()))?;
        let component: u16 = next("component", &mut it)?
            .parse()
            .map_err(|_| IceError::Malformed("component not u16".into()))?;
        let protocol = next("protocol", &mut it)?.to_ascii_lowercase();
        let priority: u32 = next("priority", &mut it)?
            .parse()
            .map_err(|_| IceError::Malformed("priority not u32".into()))?;
        let ip_text = next("ip", &mut it)?;
        let ip: [u8; 4] = parse_ipv4(ip_text).ok_or_else(|| {
            IceError::Malformed(format!(
                "v4 address expected for the SFU path, got {ip_text}"
            ))
        })?;
        let port: u16 = next("port", &mut it)?
            .parse()
            .map_err(|_| IceError::Malformed("port not u16".into()))?;
        // "typ <type> [raddr ...] [rport ...] [tcptype...]" — take the
        // type; related address is cosmetic for our pair table.
        if next("typ", &mut it)? != "typ" {
            return Err(IceError::Malformed("missing typ token".into()));
        }
        let typ = next("type", &mut it)?.to_ascii_lowercase();
        if protocol != "udp" {
            return Err(IceError::NotUdp);
        }
        Ok(RemoteCandidate {
            foundation,
            component,
            protocol,
            priority,
            ip,
            port,
            typ,
        })
    }

    pub fn endpoint(&self) -> (u16, [u8; 4]) {
        (self.port, self.ip)
    }
}

fn next<'a>(what: &str, it: &mut impl Iterator<Item = &'a str>) -> Result<&'a str, IceError> {
    it.next()
        .ok_or_else(|| IceError::Malformed(format!("{what} missing")))
}

/// Strict dotted-quad parse (no leading-zero or shorthand weirdness,
/// because browsers' candidate strings are already canonical; passing
/// something else is a signaling bug we surface, not forgive).
pub fn parse_ipv4(text: &str) -> Option<[u8; 4]> {
    let mut out = [0u8; 4];
    let mut parts = text.split('.');
    for slot in &mut out {
        let piece = parts.next()?;
        if piece.len() > 3 || (piece.len() > 1 && piece.starts_with('0')) {
            return None;
        }
        *slot = piece.parse().ok()?;
    }
    if parts.next().is_some() {
        return None;
    }
    Some(out)
}

/// RFC 5245/8839 §4.1.2.1 priority: (2^24)(pref) + (2^8)(local) + (256-comp).
pub fn candidate_priority(type_pref: u32, local_pref: u32, component: u16) -> u32 {
    (1 << 24) * type_pref + (1 << 8) * local_pref + (256 - u32::from(component))
}

/// One half of the pair table.
#[derive(Clone, Debug)]
pub struct CandidatePair {
    pub remote: RemoteCandidate,
    pub nominated: bool,
    pub governing_pair_id: u64,
}

/// The agent's lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AgentState {
    /// Checks haven't seen any valid binding request yet.
    Gathering,
    /// Peer nominated (USE-CANDIDATE) at least once.
    Nominated,
    /// Nominated pair confirmed and in plain use.
    Selected,
}

/// ICE-lite agent: local ufrag/pwd are OUR identity (sent in the SDP
/// answer); remote credentials come from the offer (and later trickles).
pub struct LiteAgent {
    pub local_ufrag: String,
    pub local_pwd: String,
    pub local_fingerprint: [u8; 32],
    remote_ufrag: String,
    remote_pwd: String,
    pairs: Vec<CandidatePair>,
    selected: Option<usize>, // index into pairs
    state: AgentState,
    pub stats_frames: u64,
    /// Tiebreaker: only used when we CONTROLLED-reject; retained for
    /// diagnostics and mixed into transaction-id generation.
    pub tiebreaker: u64,
    /// Monotonic transaction-id counter: RFC 5389 requires transaction
    /// ids unique per request within a ufrag — a compile-time constant
    /// (an earlier draft's sin) would let a 5-tuple race collide two
    /// outstanding checks.
    txn_seq: std::sync::atomic::AtomicU64,
}

/// What `handle_binding` tells the session to DO next.
#[derive(Clone, Debug)]
pub struct BindOutcome {
    /// Datagram to send back (already integrity+fingerprint framed), if any.
    pub response: Option<Vec<u8>>,
    /// Remote endpoint nominated by this packet (USE-CANDIDATE), if that
    /// happened here — the datagram sink's route table keys on this.
    pub nominated: Option<(u16, [u8; 4])>,
    /// Peer-controlled-knockout: (443 role conflict) for the audit log.
    pub role_conflict: bool,
}

impl LiteAgent {
    pub fn new(local_ufrag: String, local_pwd: String, local_fingerprint: [u8; 32]) -> LiteAgent {
        LiteAgent {
            local_ufrag,
            local_pwd,
            local_fingerprint,
            remote_ufrag: String::new(),
            remote_pwd: String::new(),
            pairs: Vec::new(),
            selected: None,
            state: AgentState::Gathering,
            stats_frames: 0,
            tiebreaker: 0x4F6F_5241_4755_4D45,
            txn_seq: std::sync::atomic::AtomicU64::new(0),
        }
    }

    /// Offer-side credentials + candidates, harvested from the SDP
    /// (adoption is per relation to the OFFER's claim; trickles after it
    /// go through add_remote_candidate).
    pub fn adopt_remote(&mut self, ufrag: &str, pwd: &str, candidates: Vec<RemoteCandidate>) {
        self.remote_ufrag = ufrag.to_string();
        self.remote_pwd = pwd.to_string();
        self.pairs.clear();
        self.pairs
            .extend((1u64..).zip(candidates).map(|(id, remote)| CandidatePair {
                remote,
                nominated: false,
                governing_pair_id: id,
            }));
        self.pairs
            .sort_by_key(|a| std::cmp::Reverse(a.remote.priority));
        self.state = AgentState::Gathering;
        self.selected = None;
    }

    /// One trickled candidate, validated and deduplicated into the pair
    /// table. Returns Ok(false) for a re-asserted duplicate (browsers
    /// re-trickle during restarts; SAME endpoint+foundation is a no-op,
    /// not an error) and Ok(true) for a registered-new endpoint.
    pub fn add_remote_candidate(&mut self, c: RemoteCandidate) -> Result<bool, IceError> {
        if c.port == 0 || c.priority == 0 {
            return Err(IceError::Malformed("port/priority must be non-zero".into()));
        }
        if self.remote_ufrag.is_empty() {
            // A candidate before credentials: RFC 8445 §5.3 pairs it at
            // the checklist level, which we do not have yet — persist
            // anyway under zero creds (the offer may carry none; full
            // trickling session).
        }
        if self.pairs.iter().any(|p| {
            p.remote.endpoint() == c.endpoint()
                && (p.remote.foundation == c.foundation || p.remote.protocol == c.protocol)
        }) {
            return Ok(false);
        }
        let id = self.pairs.len() as u64 + 1;
        self.pairs.push(CandidatePair {
            remote: c,
            nominated: false,
            governing_pair_id: id,
        });
        self.pairs
            .sort_by_key(|a| std::cmp::Reverse(a.remote.priority));
        Ok(true)
    }

    /// Snapshot of the pair table (control-plane introspection for the
    /// Go gateway's session ledger via the engine's HTTP surface).
    pub fn remote_candidates(&self) -> Vec<RemoteCandidate> {
        self.pairs.iter().map(|p| p.remote.clone()).collect()
    }

    /// How many endpoints are known but not yet nominated (backstop for
    /// the control surface's diagnostics).
    pub fn unnominated_pair_count(&self) -> usize {
        self.pairs.iter().filter(|p| !p.nominated).count()
    }

    pub fn state(&self) -> AgentState {
        self.state
    }

    /// Selected 5-tuple for the media path — None until nomination.
    pub fn selected_endpoint(&self) -> Option<(u16, [u8; 4])> {
        self.selected.map(|i| self.pairs[i].remote.endpoint())
    }

    /// Handle an INBOUND binding request. Any STUN that arrived but was
    /// not a bind request is an error-response (400) opportunity, surfaced
    /// as `response` anyway — RFC 8445 §7.3.1.4's "unsupported requests".
    pub fn handle_stun(
        &mut self,
        msg: &StunMessage,
        original: &[u8],
        from: (u16, [u8; 4]),
    ) -> BindOutcome {
        self.stats_frames += 1;
        if msg.msg_type != stun_types::BINDING_REQUEST {
            let response = StunBuilder::new(stun_types::BINDING_ERROR, msg.transaction_id)
                .attr(
                    stun_attrs::ERROR_CODE,
                    &[
                        0, 0, 4, 20, b'U', b'n', b's', b'u', b'p', b'p', b'o', b'r', b't', b'e',
                        b'd', b' ',
                    ],
                )
                .build_with_integrity(&self.local_pwd);
            return BindOutcome {
                response: Some(response),
                nominated: None,
                role_conflict: false,
            };
        }

        // 1. USERNAME must address us ("remoteUfrag:localUfrag").
        let Some(username) = msg.attr(stun_attrs::USERNAME) else {
            return self.unauthenticated(msg, 0x01, b"no USERNAME", original);
        };
        let expected = format!("{}:{}", self.local_ufrag, self.remote_ufrag);
        if username != expected.as_bytes() {
            // RFC 8445 §7.3.1: unknown ufrag → 401, never a media path.
            return self.unauthenticated(msg, 0x02, b"ufrag mismatch", original);
        }

        // 2. Integrity check with the LOCAL password (we verify the peer's
        //    knowledge of our secret).
        if !msg.verify_integrity(&self.local_pwd, original) {
            return self.unauthenticated(msg, 0x03, b"integrity failed", original);
        }

        // 3. Fingerprint must check out if present.
        if !msg.fingerprint_ok && msg.attr(stun_attrs::FINGERPRINT).is_some() {
            return self.unauthenticated(msg, 0x04, b"bad fingerprint", original);
        }

        // 4. Role conflict handling: a FULL agent is always CONTROLLING
        //    and we're always CONTROLLED by them; only a peer-REFLEXIVE
        //    request would conflict, which our 401 path covers only when
        //    credentials fail — no tie break needed.
        let role_conflict = false;

        // 5. Locate-or-learn the pair with the arrival endpoint. A bind
        //    to an UNSEEN endpoint is a probing bug AND a legal "peer-
        //    reflexive" learning event (RFC 8445 §7.3.1.5).
        let endpoint_idx = self.pairs.iter().position(|p| p.remote.endpoint() == from);

        let nominated = if msg.use_candidate() {
            match endpoint_idx {
                Some(i) => {
                    self.pairs[i].nominated = true;
                    // Lite agent per §6.2: honor the request, remember pair.
                    self.selected = Some(i);
                    self.state = AgentState::Selected;
                    Some(from)
                }
                None => {
                    // USE-CANDIDATE on an unknown endpoint: honor anyway —
                    // an early nomination before candidate exchange lands.
                    let pair = CandidatePair {
                        remote: RemoteCandidate {
                            foundation: 0,
                            component: 1,
                            protocol: "udp".into(),
                            priority: candidate_priority(127, 65535, 1),
                            ip: from.1,
                            port: from.0,
                            typ: "prflx".into(),
                        },
                        nominated: true,
                        governing_pair_id: self.pairs.len() as u64 + 1,
                    };
                    self.pairs.push(pair);
                    let idx = self.pairs.len() - 1;
                    self.selected = Some(idx);
                    self.state = AgentState::Nominated;
                    Some(from)
                }
            }
        } else if let AgentState::Gathering = self.state {
            self.state = AgentState::Gathering; // checks flowing; still pre-nomination
            None
        } else {
            None
        };

        let response = StunBuilder::response_to(msg.transaction_id)
            .xor_mapped_ipv4(from.0, from.1)
            .username(&format!("{}:{}", self.remote_ufrag, self.local_ufrag))
            .ice_role(IceRole::Controlled, self.tiebreaker)
            .build_with_integrity(&self.local_pwd);
        BindOutcome {
            response: Some(response),
            nominated,
            role_conflict,
        }
    }

    fn unauthenticated(
        &self,
        msg: &StunMessage,
        audit_code: u8,
        _why: &[u8],
        original: &[u8],
    ) -> BindOutcome {
        // Wire the 401 back with integrity (proof we hold the secret,
        // without revealing WHY it failed — RFC 8489 §9). audit_code is
        // local diagnostics only; the session layers log it.
        // Debugging aid (opt-in env): NEVER logs credentials — reason
        // plus the raw frame is enough to diagnose interop, and the
        // password stays out of logs even when debugging is enabled.
        if std::env::var_os("VOXDESK_STUN_DEBUG").is_some() {
            let _ = self.local_pwd;
            eprintln!(
                "stun-401 audit={audit_code} reason={} local_ufrag={} remote_ufrag={} req_hex={}",
                String::from_utf8_lossy(_why),
                self.local_ufrag,
                self.remote_ufrag,
                original
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            );
        }
        let _ = audit_code;
        let response = StunBuilder::new(stun_types::BINDING_ERROR, msg.transaction_id)
            .attr(stun_attrs::ERROR_CODE, b"\0\0\x04\x01Unauthorized")
            .build_with_integrity(&self.local_pwd);
        BindOutcome {
            response: Some(response),
            nominated: None,
            role_conflict: true,
        }
    }

    pub fn stun_looks_ours(msg_type: u16, peer_data: &[u8]) -> bool {
        let _ = peer_data;
        msg_type == stun_types::BINDING_REQUEST || msg_type == stun_types::BINDING_SUCCESS
    }

    /// A fresh 12-byte transaction id, unique per call within this agent:
    /// counter || tiebreaker LOW bytes — a UA can never beat it from the
    /// wire side, and a per-agent counter never repeats for that ufrag
    /// (RFC 5389 §6's uniqueness rule).
    pub fn next_transaction_id(&self) -> [u8; 12] {
        let n = self
            .txn_seq
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let mut out = [0u8; 12];
        out[0..4].copy_from_slice(&(self.tiebreaker.rotate_left(13) as u32).to_be_bytes());
        out[4..12].copy_from_slice(&n.to_be_bytes());
        out
    }

    /// RFC 8445 §6.1 local check-list helper — provided for testing and
    /// echo; the lite profile doesn't originate checks.
    pub fn build_bind_request(&self, to: (u16, [u8; 4])) -> Vec<u8> {
        let _ = to;
        let tid = self.next_transaction_id();
        StunBuilder::new(stun_types::BINDING_REQUEST, tid)
            .username(&format!("{}:{}", self.remote_ufrag, self.local_ufrag))
            .priority(candidate_priority(126, 65535, 1))
            .ice_role(IceRole::Controlled, self.tiebreaker)
            .use_candidate()
            .build_with_integrity(&self.remote_pwd)
    }
}

/// a=candidate builder for OUR single host candidate line — the SFU's
/// public socket. foundation is stable to make browser logs diffable.
pub fn our_candidate_line(ip: [u8; 4], port: u16) -> String {
    let priority = candidate_priority(126, 65535, 1);
    format!(
        "candidate:1 1 udp {priority} {}.{}.{}.{} {port} typ host",
        ip[0], ip[1], ip[2], ip[3]
    )
}
