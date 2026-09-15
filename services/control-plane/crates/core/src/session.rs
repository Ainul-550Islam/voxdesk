//! Call-session state machine, mirroring `app/telephony/call_state.py` and
//! `app/telephony/transfer_service.py` exactly.
//!
//! The Python modules are the single source of truth for call lifecycle, and
//! this file is their Rust shadow for the Phase 2 parity gate. Every rule
//! below is a verbatim re-statement of a Python rule, with the Python source
//! cited in the doc comment:
//!
//!   * the call-status graph (`ALLOWED_TRANSITIONS`), where TRANSFERRED is
//!     deliberately **not** terminal — a transferred call is still up, with a
//!     human on it, and must still reach COMPLETED/FAILED when the human hangs
//!     up;
//!   * the two-phase transfer (`request_transfer` → provider redirect →
//!     DIALING → callback → CONNECTED/FAILED), where `CallStatus.TRANSFERRED`
//!     is set at DIALING — once the provider has accepted the redirect — never
//!     from REQUESTED;
//!   * idempotency keyed on `transfer_state` (`TRANSFER_IN_FLIGHT`): a second
//!     request while one is in flight is a no-op, never a second dial;
//!   * the "inferred failure" correction rule: a provider-reported failure is
//!     authoritative, but a failure we *guessed* at (`"inferred: "` prefix)
//!     may be corrected by a later "connected" callback.

/// Mirrors `app.db.models.CallStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CallStatus {
    Ringing,
    InProgress,
    Completed,
    Failed,
    NoAnswer,
    Transferred,
}

/// Mirrors `app.db.models.TransferState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TransferState {
    None,
    Requested,
    Dialing,
    Connected,
    Failed,
}

/// Mirrors `call_state.ALLOWED_TRANSITIONS`. Every transition the application
/// considers legal, including the two people forget:
///
/// * RINGING -> COMPLETED — the provider reports completion straight from
///   ringing when the caller hangs up during ringback;
/// * TRANSFERRED -> COMPLETED | FAILED — the human hung up, or the transfer
///   leg died after connecting.
pub fn allowed_transitions(status: CallStatus) -> &'static [CallStatus] {
    match status {
        CallStatus::Ringing => &[
            CallStatus::InProgress,
            CallStatus::NoAnswer,
            CallStatus::Failed,
            CallStatus::Completed,
        ],
        CallStatus::InProgress => &[
            CallStatus::Transferred,
            CallStatus::Completed,
            CallStatus::Failed,
        ],
        CallStatus::Transferred => &[CallStatus::Completed, CallStatus::Failed],
        // Terminal.
        CallStatus::Completed | CallStatus::Failed | CallStatus::NoAnswer => &[],
    }
}

/// Mirrors `call_state.TERMINAL_STATUSES`. TRANSFERRED is deliberately absent.
pub fn is_terminal(status: CallStatus) -> bool {
    matches!(
        status,
        CallStatus::Completed | CallStatus::Failed | CallStatus::NoAnswer
    )
}

/// Mirrors `call_state.can_transition`: a transition to the same state is
/// always allowed (it is a no-op).
pub fn can_transition(current: CallStatus, target: CallStatus) -> bool {
    if current == target {
        return true;
    }
    allowed_transitions(current).contains(&target)
}

/// What `apply_status` did (mirrors `call_state.TransitionResult`). Never
/// raises; always reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransitionResult {
    pub applied: bool,
    pub previous: CallStatus,
    pub current: CallStatus,
}

/// Mirrors `transfer_service.TransferError`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferError {
    NoDestination,
    InvalidDestination,
    CallAlreadyEnded,
    CallNotTransferable,
    ProviderError,
    TenantMismatch,
}

/// Mirrors `transfer_service.TransferOutcome`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferOutcome {
    TransferStarted,
    TransferCompleted,
    TransferFailed,
    AlreadyTransferred,
}

/// Mirrors `transfer_service.TransferResult`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransferResult {
    pub outcome: TransferOutcome,
    pub state: TransferState,
    pub error: Option<TransferError>,
    pub destination: Option<String>,
}

impl TransferResult {
    /// Mirrors `TransferResult.ok`.
    pub fn ok(&self) -> bool {
        matches!(
            self.outcome,
            TransferOutcome::TransferStarted
                | TransferOutcome::TransferCompleted
                | TransferOutcome::AlreadyTransferred
        )
    }
}

/// Mirrors `transfer_service.INFERRED_PREFIX`: a failure we guessed at rather
/// than one the provider reported. The provider always wins over the guess.
pub const INFERRED_PREFIX: &str = "inferred: ";

/// Mirrors `transfer_service`'s field caps (`.transfer_error` detail at 300,
/// `.transfer_reason` at 400).
pub const MAX_TRANSFER_ERROR_CHARS: usize = 300;
pub const MAX_TRANSFER_REASON_CHARS: usize = 400;

/// Mirrors `app.db.models.TRANSFER_IN_FLIGHT`. States in which a transfer is
/// already under way or finished; a second request in one of these is a no-op.
pub fn transfer_in_flight(state: TransferState) -> bool {
    matches!(
        state,
        TransferState::Requested | TransferState::Dialing | TransferState::Connected
    )
}

/// The outcome of `Session::request_transfer`: either validation passed and a
/// provider redirect must be performed now, or the request was refused with
/// the reason in the result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RequestOutcome {
    Dial,
    Refused(TransferResult),
}

/// The mutable state of one call session. `call_id` is the internal UUID (as
/// text); the provider SID is carried separately at the transport layer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Session {
    pub call_id: String,
    pub status: CallStatus,
    pub transfer: TransferState,
    pub transfer_destination: Option<String>,
    pub transfer_reason: Option<String>,
    pub transfer_attempts: u32,
    pub transfer_error: Option<String>,
    /// Mirrors `Call.escalated`: a transfer was attempted; kept visible even
    /// after a failure.
    pub escalated: bool,
}

fn truncate(s: &str, max: usize) -> Option<String> {
    if s.is_empty() {
        return None;
    }
    if s.chars().count() <= max {
        return Some(s.to_string());
    }
    Some(s.chars().take(max).collect())
}

impl Session {
    pub fn new(call_id: impl Into<String>) -> Self {
        Session {
            call_id: call_id.into(),
            status: CallStatus::Ringing,
            transfer: TransferState::None,
            transfer_destination: None,
            transfer_reason: None,
            transfer_attempts: 0,
            transfer_error: None,
            escalated: false,
        }
    }

    /// Mirrors `call_state.apply_status`: idempotent, never-raising. Returns
    /// whether the status actually changed.
    pub fn apply_status(&mut self, target: CallStatus) -> TransitionResult {
        let previous = self.status;
        if previous == target {
            return TransitionResult {
                applied: false,
                previous,
                current: target,
            };
        }
        if can_transition(previous, target) {
            self.status = target;
            return TransitionResult {
                applied: true,
                previous,
                current: target,
            };
        }
        TransitionResult {
            applied: false,
            previous,
            current: previous,
        }
    }

    /// Mirrors the validation ladder in `transfer_service.request_transfer`
    /// (steps 1–4: idempotency, terminal, transferable, destination, then
    /// record intent). `destination` is the tenant's already-resolved
    /// escalation number — in Python it always comes from the tenant row,
    /// never from a request body; `None` mirrors an unconfigured number.
    ///
    /// Returns `Dial` when a provider redirect must be performed now (intent
    /// recorded: REQUESTED, attempts + 1), or `Refused` with the reason.
    pub fn request_transfer(&mut self, destination: Option<&str>, reason: &str) -> RequestOutcome {
        // 1. Idempotency: in-flight means never a second dial.
        if transfer_in_flight(self.transfer) {
            let outcome = if self.transfer == TransferState::Connected {
                TransferOutcome::TransferCompleted
            } else {
                TransferOutcome::AlreadyTransferred
            };
            return RequestOutcome::Refused(TransferResult {
                outcome,
                state: self.transfer,
                error: None,
                destination: self.transfer_destination.clone(),
            });
        }

        // 2. The call must still be up, and must be able to reach TRANSFERRED.
        if is_terminal(self.status) {
            return RequestOutcome::Refused(TransferResult {
                outcome: TransferOutcome::TransferFailed,
                state: self.transfer,
                error: Some(TransferError::CallAlreadyEnded),
                destination: None,
            });
        }
        if !can_transition(self.status, CallStatus::Transferred) {
            return RequestOutcome::Refused(TransferResult {
                outcome: TransferOutcome::TransferFailed,
                state: self.transfer,
                error: Some(TransferError::CallNotTransferable),
                destination: None,
            });
        }

        // 3. Destination. A missing destination is recorded as a failure
        //    (transfer_state = FAILED) exactly like `_record_failure`.
        let dest = match destination {
            Some(d) => d,
            None => {
                self.record_failure(TransferError::NoDestination);
                return RequestOutcome::Refused(TransferResult {
                    outcome: TransferOutcome::TransferFailed,
                    state: self.transfer,
                    error: Some(TransferError::NoDestination),
                    destination: None,
                });
            }
        };

        // 4. Record intent (REQUESTED) before touching the provider, so that
        //    if the process dies mid-redirect we still know a transfer was
        //    attempted.
        self.escalated = true;
        self.transfer = TransferState::Requested;
        self.transfer_destination = Some(dest.to_string());
        self.transfer_reason = truncate(reason, MAX_TRANSFER_REASON_CHARS);
        self.transfer_attempts += 1;
        self.transfer_error = None;
        RequestOutcome::Dial
    }

    /// Mirrors the success branch of `transfer_service.request_transfer`
    /// (step 6): the provider accepted the redirect, so — now and only now —
    /// the transfer is DIALING and the call status becomes TRANSFERRED.
    pub fn on_redirect_ok(&mut self) -> TransferResult {
        self.transfer = TransferState::Dialing;
        self.apply_status(CallStatus::Transferred);
        TransferResult {
            outcome: TransferOutcome::TransferStarted,
            state: TransferState::Dialing,
            error: None,
            destination: self.transfer_destination.clone(),
        }
    }

    /// Mirrors the provider-failure branch of
    /// `transfer_service.request_transfer` (step 5): record the failure, keep
    /// the call alive.
    pub fn on_redirect_error(&mut self, detail: &str) -> TransferResult {
        self.record_failure_detail(detail);
        TransferResult {
            outcome: TransferOutcome::TransferFailed,
            state: TransferState::Failed,
            error: Some(TransferError::ProviderError),
            destination: self.transfer_destination.clone(),
        }
    }

    /// Mirrors `transfer_service.mark_transfer_connected`. Returns true when
    /// this changed anything, so a caller can tell a real event from a
    /// retried webhook. A failure we inferred (prefix `inferred: `) may be
    /// corrected here; a provider-reported failure may not. Does not touch
    /// the call status — the call stays TRANSFERRED.
    pub fn mark_transfer_connected(&mut self) -> bool {
        if self.transfer == TransferState::Connected {
            return false;
        }
        let recoverable = self.transfer == TransferState::Failed
            && self
                .transfer_error
                .as_deref()
                .is_some_and(|e| e.starts_with(INFERRED_PREFIX));
        if !matches!(
            self.transfer,
            TransferState::Requested | TransferState::Dialing
        ) && !recoverable
        {
            return false;
        }
        if recoverable {
            self.transfer_error = None;
        }
        self.transfer = TransferState::Connected;
        true
    }

    /// Mirrors `transfer_service.mark_transfer_failed`. Returns true when this
    /// changed anything. A late failure for an already-connected leg must not
    /// rewrite the successful transfer, and the call itself stays alive (the
    /// TwiML falls through to voicemail).
    pub fn mark_transfer_failed(&mut self, reason: &str) -> bool {
        if self.transfer == TransferState::Failed {
            return false;
        }
        if self.transfer == TransferState::Connected {
            return false;
        }
        self.record_failure_detail(reason);
        true
    }

    /// Mirrors `transfer_service._record_failure` (minus the transcript
    /// event): FAILED + truncated detail + keep `escalated` visible.
    fn record_failure_detail(&mut self, detail: &str) {
        self.transfer = TransferState::Failed;
        self.transfer_error = truncate(detail, MAX_TRANSFER_ERROR_CHARS);
        self.escalated = true;
    }

    /// Convenience over `record_failure_detail` for a `TransferError` code.
    fn record_failure(&mut self, error: TransferError) {
        let detail = match error {
            TransferError::NoDestination => "no_destination",
            TransferError::InvalidDestination => "invalid_destination",
            TransferError::CallAlreadyEnded => "call_already_ended",
            TransferError::CallNotTransferable => "call_not_transferable",
            TransferError::ProviderError => "provider_error",
            TransferError::TenantMismatch => "tenant_mismatch",
        };
        self.record_failure_detail(detail);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ------------------------------------------------------------------
    // Differential tests: each one encodes a Python rule verbatim, so the
    // same inputs produce the same observable outcomes as the Python path.
    // ------------------------------------------------------------------

    #[test]
    fn differential_ringing_completed_is_legal() {
        // call_state.ALLOWED_TRANSITIONS[RINGING] contains COMPLETED (hang up
        // during ringback).
        let mut s = Session::new("c");
        let r = s.apply_status(CallStatus::Completed);
        assert!(r.applied);
        assert_eq!(s.status, CallStatus::Completed);
    }

    #[test]
    fn differential_transferred_is_not_terminal() {
        // TRANSFERRED -> COMPLETED and TRANSFERRED -> FAILED are legal (the
        // human hung up / the leg died); TRANSFERRED -> IN_PROGRESS is not.
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.apply_status(CallStatus::Transferred);
        assert!(!is_terminal(CallStatus::Transferred));
        assert!(can_transition(
            CallStatus::Transferred,
            CallStatus::Completed
        ));
        assert!(can_transition(CallStatus::Transferred, CallStatus::Failed));
        assert!(!can_transition(
            CallStatus::Transferred,
            CallStatus::InProgress
        ));
        assert!(s.apply_status(CallStatus::Completed).applied);
    }

    #[test]
    fn differential_same_state_is_an_idempotent_noop() {
        // call_state.can_transition: same state is always allowed (no-op).
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        let r = s.apply_status(CallStatus::InProgress);
        assert!(!r.applied);
        assert_eq!(r.previous, CallStatus::InProgress);
        assert_eq!(r.current, CallStatus::InProgress);
    }

    #[test]
    fn differential_terminal_never_moves() {
        for terminal in [
            CallStatus::Completed,
            CallStatus::Failed,
            CallStatus::NoAnswer,
        ] {
            assert!(is_terminal(terminal));
            assert!(allowed_transitions(terminal).is_empty());
        }
        let mut s = Session::new("c");
        s.apply_status(CallStatus::Completed);
        let r = s.apply_status(CallStatus::InProgress);
        assert!(!r.applied);
        assert_eq!(s.status, CallStatus::Completed);
    }

    #[test]
    fn differential_happy_path_sets_transferred_at_dialing() {
        // transfer_service: TRANSFERRED is set at DIALING, never REQUESTED.
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);

        match s.request_transfer(Some("+15551234567"), "human please") {
            RequestOutcome::Dial => {}
            _ => panic!("expected a dial"),
        }
        assert_eq!(s.transfer, TransferState::Requested);
        assert_eq!(s.transfer_attempts, 1);
        assert!(s.escalated);
        assert_eq!(s.status, CallStatus::InProgress); // NOT transferred yet
        assert_eq!(s.transfer_error, None);

        let result = s.on_redirect_ok();
        assert_eq!(result.outcome, TransferOutcome::TransferStarted);
        assert!(result.ok());
        assert_eq!(s.transfer, TransferState::Dialing);
        assert_eq!(s.status, CallStatus::Transferred); // set at DIALING
    }

    #[test]
    fn differential_missing_destination_records_failure() {
        // resolve_destination -> NO_DESTINATION -> _record_failure (FAILED).
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        match s.request_transfer(None, "human") {
            RequestOutcome::Refused(r) => {
                assert_eq!(r.outcome, TransferOutcome::TransferFailed);
                assert_eq!(r.error, Some(TransferError::NoDestination));
            }
            _ => panic!("expected refusal"),
        }
        assert_eq!(s.transfer, TransferState::Failed);
        assert_eq!(s.transfer_error.as_deref(), Some("no_destination"));
        assert!(s.escalated);
        assert_eq!(s.transfer_attempts, 0); // never dialed
    }

    #[test]
    fn differential_second_request_in_flight_is_a_noop() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        assert!(matches!(
            s.request_transfer(Some("+1"), "r"),
            RequestOutcome::Dial
        ));
        assert_eq!(s.transfer_attempts, 1);
        // In-flight: refused, attempts unchanged, no second dial.
        match s.request_transfer(Some("+1"), "again") {
            RequestOutcome::Refused(r) => {
                assert_eq!(r.outcome, TransferOutcome::AlreadyTransferred);
                assert!(r.ok()); // not an error from the caller's perspective
            }
            _ => panic!("expected refusal"),
        }
        assert_eq!(s.transfer_attempts, 1);
    }

    #[test]
    fn differential_request_while_connected_reports_completed() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        assert!(s.mark_transfer_connected());
        match s.request_transfer(Some("+1"), "again") {
            RequestOutcome::Refused(r) => {
                assert_eq!(r.outcome, TransferOutcome::TransferCompleted);
            }
            _ => panic!("expected refusal"),
        }
    }

    #[test]
    fn differential_request_when_terminal_is_rejected() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::Completed);
        match s.request_transfer(Some("+1"), "r") {
            RequestOutcome::Refused(r) => {
                assert_eq!(r.error, Some(TransferError::CallAlreadyEnded));
                assert_eq!(r.outcome, TransferOutcome::TransferFailed);
            }
            _ => panic!("expected refusal"),
        }
        // No state change: still NONE, never escalated.
        assert_eq!(s.transfer, TransferState::None);
        assert!(!s.escalated);
    }

    #[test]
    fn differential_request_while_ringing_is_not_transferable() {
        // RINGING cannot transition to TRANSFERRED.
        let mut s = Session::new("c"); // starts RINGING
        match s.request_transfer(Some("+1"), "r") {
            RequestOutcome::Refused(r) => {
                assert_eq!(r.error, Some(TransferError::CallNotTransferable));
            }
            _ => panic!("expected refusal"),
        }
        assert_eq!(s.transfer, TransferState::None);
    }

    #[test]
    fn differential_provider_error_records_failure_keeps_call_alive() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        let result = s.on_redirect_error("21212: busy");
        assert_eq!(result.outcome, TransferOutcome::TransferFailed);
        assert_eq!(result.error, Some(TransferError::ProviderError));
        assert_eq!(s.transfer, TransferState::Failed);
        assert_eq!(s.status, CallStatus::InProgress); // call still up
        assert_eq!(s.transfer_error.as_deref(), Some("21212: busy"));
    }

    #[test]
    fn differential_connected_from_dialing_keeps_status() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        assert!(s.mark_transfer_connected());
        assert_eq!(s.transfer, TransferState::Connected);
        assert_eq!(s.status, CallStatus::Transferred); // unchanged by connect
                                                       // Duplicate callback is a no-op.
        assert!(!s.mark_transfer_connected());
    }

    #[test]
    fn differential_inferred_failure_can_be_corrected() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        // The parent ended while the human's phone was ringing: we guess.
        assert!(s.mark_transfer_failed("inferred: parent call ended"));
        assert_eq!(s.transfer, TransferState::Failed);
        // The authoritative callback arrives: the guess is corrected.
        assert!(s.mark_transfer_connected());
        assert_eq!(s.transfer, TransferState::Connected);
        assert_eq!(s.transfer_error, None);
    }

    #[test]
    fn differential_reported_failure_is_never_corrected() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        // Provider-reported (no prefix) — authoritative.
        assert!(s.mark_transfer_failed("no-answer"));
        assert!(!s.mark_transfer_connected());
        assert_eq!(s.transfer, TransferState::Failed);
        assert_eq!(s.transfer_error.as_deref(), Some("no-answer"));
    }

    #[test]
    fn differential_late_failure_does_not_rewrite_connected() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        assert!(s.mark_transfer_connected());
        // A late failure callback for an earlier leg must not rewrite.
        assert!(!s.mark_transfer_failed("no-answer"));
        assert_eq!(s.transfer, TransferState::Connected);
    }

    #[test]
    fn differential_duplicate_failure_is_a_noop() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_ok();
        assert!(s.mark_transfer_failed("busy"));
        assert!(!s.mark_transfer_failed("busy"));
    }

    #[test]
    fn differential_retry_after_failed_is_allowed() {
        // FAILED is not in TRANSFER_IN_FLIGHT, so a retry dials again.
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        s.request_transfer(Some("+1"), "r");
        s.on_redirect_error("500");
        assert_eq!(s.transfer_attempts, 1);
        match s.request_transfer(Some("+1"), "retry") {
            RequestOutcome::Dial => {}
            _ => panic!("expected a retry dial"),
        }
        assert_eq!(s.transfer_attempts, 2);
        assert_eq!(s.transfer, TransferState::Requested);
        assert_eq!(s.transfer_error, None);
    }

    #[test]
    fn differential_reason_is_capped_at_400() {
        let mut s = Session::new("c");
        s.apply_status(CallStatus::InProgress);
        let long_reason = "x".repeat(500);
        s.request_transfer(Some("+1"), &long_reason);
        assert_eq!(s.transfer_reason.as_deref().unwrap().chars().count(), 400);
    }

    // ------------------------------------------------------------------
    // Property-based test: random operation sequences must never violate the
    // invariants the Python state machine guarantees.
    // ------------------------------------------------------------------

    struct Lcg(u64);
    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0 >> 33
        }
        fn below(&mut self, n: u64) -> usize {
            (self.next() % n) as usize
        }
    }

    /// `apply_status` models provider callbacks, which — per the
    /// `call_state.provider status map` — never report TRANSFERRED; the only
    /// way to reach TRANSFERRED is `on_redirect_ok`, mirroring the Python
    /// transfer service.
    const PROVIDER_STATUSES: [CallStatus; 5] = [
        CallStatus::Ringing,
        CallStatus::InProgress,
        CallStatus::Completed,
        CallStatus::Failed,
        CallStatus::NoAnswer,
    ];

    #[test]
    fn property_random_sequences_preserve_invariants() {
        let mut rng = Lcg(0x9e3779b97f4a7c15);
        for _case in 0..3000 {
            let mut s = Session::new("call");
            let mut terminal: Option<CallStatus> = None;
            for _step in 0..80 {
                match rng.below(7) {
                    0 | 1 => {
                        let target = PROVIDER_STATUSES[rng.below(5)];
                        let before = s.status;
                        let r = s.apply_status(target);
                        if r.applied {
                            assert!(can_transition(before, target));
                            assert_eq!(r.previous, before);
                            assert_eq!(r.current, target);
                            if is_terminal(target) {
                                terminal = Some(target);
                            }
                        } else {
                            assert_eq!(r.current, before);
                        }
                    }
                    2 => {
                        let before = s.transfer_attempts;
                        let dest = if rng.below(2) == 0 {
                            Some("+15551234567")
                        } else {
                            None
                        };
                        match s.request_transfer(dest, "reason") {
                            RequestOutcome::Dial => {
                                assert_eq!(s.transfer, TransferState::Requested);
                                assert_eq!(s.transfer_attempts, before + 1);
                                assert!(s.escalated);
                            }
                            RequestOutcome::Refused(_) => {
                                assert_eq!(s.transfer_attempts, before);
                            }
                        }
                    }
                    3 => {
                        if s.transfer == TransferState::Requested {
                            s.on_redirect_ok();
                        }
                    }
                    4 => {
                        if s.transfer == TransferState::Requested {
                            s.on_redirect_error("provider: 500");
                        }
                    }
                    5 => {
                        s.mark_transfer_connected();
                    }
                    _ => {
                        s.mark_transfer_failed("inferred: parent ended");
                    }
                }

                // Invariants, checked after every step:
                if let Some(t) = terminal {
                    assert_eq!(s.status, t, "terminal status must never move");
                }
                if s.status == CallStatus::Transferred {
                    assert!(
                        s.transfer != TransferState::None,
                        "TRANSFERRED implies a transfer was attempted"
                    );
                }
                if matches!(
                    s.transfer,
                    TransferState::Requested | TransferState::Dialing | TransferState::Connected
                ) {
                    assert_eq!(
                        s.transfer_error, None,
                        "an in-flight or connected transfer has no failure recorded"
                    );
                }
                assert!(s.escalated || s.transfer == TransferState::None);
            }
        }
    }
}
