//! Usage metering and rating pipeline, mirroring `app/billing/metering.py`
//! and `app/billing/plans.py` exactly.
//!
//! The invariants the Python side enforces and this module re-states:
//!
//!   * **Append-only.** Usage is an event log; a correction is a *new* event
//!     with a negative quantity (manual adjustment), never a rewrite.
//!   * **Idempotency-keyed.** The key is derived from the business fact
//!     (`"{metric}:{entity}"` plus an optional discriminator), never a random
//!     id per attempt, so a retried callback cannot double-bill.
//!   * **Integer smallest units.** Voice is stored in seconds, everything else
//!     in its discrete unit; no float ledger.
//!   * **Rounding happens once, at the boundary.** Voice overage is rounded up
//!     to whole minutes on the *period total*, not per call: 620 minutes and
//!     1 second against 500 minutes is 121 billable overage minutes.

use std::collections::HashSet;

/// Mirrors `app.db.models.UsageMetric`; the `as_str` values are the wire names
/// from the published contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum UsageMetric {
    VoiceMinute,
    SmsSegment,
    LlmToken,
    TtsCharacter,
}

impl UsageMetric {
    pub fn as_str(self) -> &'static str {
        match self {
            UsageMetric::VoiceMinute => "voice_minute",
            UsageMetric::SmsSegment => "sms_segment",
            UsageMetric::LlmToken => "llm_token",
            UsageMetric::TtsCharacter => "tts_character",
        }
    }

    /// The event type that normally produces this metric (mirrors
    /// `_DEFAULT_EVENT_TYPE`).
    pub fn default_event_type(self) -> UsageEventType {
        match self {
            UsageMetric::VoiceMinute => UsageEventType::VoiceMinuteUsed,
            UsageMetric::SmsSegment => UsageEventType::SmsSegmentUsed,
            UsageMetric::LlmToken => UsageEventType::LlmTokenUsed,
            UsageMetric::TtsCharacter => UsageEventType::TtsCharacterUsed,
        }
    }

    /// Smallest units per "included" plan unit. Voice is written in minutes
    /// but stored in seconds; everything else is already its own unit.
    pub fn units_per_included(self) -> i64 {
        match self {
            UsageMetric::VoiceMinute => 60,
            _ => 1,
        }
    }
}

/// Mirrors `app.db.models.UsageEventType`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UsageEventType {
    VoiceMinuteUsed,
    SmsSegmentUsed,
    LlmTokenUsed,
    TtsCharacterUsed,
    ManualAdjustment,
}

impl UsageEventType {
    pub fn as_str(self) -> &'static str {
        match self {
            UsageEventType::VoiceMinuteUsed => "voice_minute_used",
            UsageEventType::SmsSegmentUsed => "sms_segment_used",
            UsageEventType::LlmTokenUsed => "llm_token_used",
            UsageEventType::TtsCharacterUsed => "tts_character_used",
            UsageEventType::ManualAdjustment => "manual_adjustment",
        }
    }
}

/// One plan in the seed catalogue (mirrors `SeedPlan`). Prices are integer
/// minor units; overage rates are in millicents.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Plan {
    pub code: String,
    pub name: String,
    pub monthly_price_cents: i64,
    pub annual_price_cents: Option<i64>,
    pub included_voice_minutes: i64,
    pub included_sms_segments: i64,
    pub included_llm_tokens: i64,
    pub included_tts_characters: i64,
    pub overage_voice_minute_millicents: i64,
    pub overage_sms_millicents: i64,
    pub overage_llm_token_millicents: i64,
    pub overage_tts_character_millicents: i64,
    pub overage_enabled: bool,
    pub trial_days: i64,
    pub display_order: i64,
}

impl Plan {
    /// The included allowance in the metric's smallest unit (mirrors
    /// `included_units`). Voice minutes x 60 -> seconds.
    pub fn included_units(&self, metric: UsageMetric) -> i64 {
        let field = match metric {
            UsageMetric::VoiceMinute => self.included_voice_minutes,
            UsageMetric::SmsSegment => self.included_sms_segments,
            UsageMetric::LlmToken => self.included_llm_tokens,
            UsageMetric::TtsCharacter => self.included_tts_characters,
        };
        field * metric.units_per_included()
    }

    /// The overage rate for `metric` in millicents (mirrors
    /// `overage_rate_millicents`).
    pub fn overage_rate_millicents(&self, metric: UsageMetric) -> i64 {
        match metric {
            UsageMetric::VoiceMinute => self.overage_voice_minute_millicents,
            UsageMetric::SmsSegment => self.overage_sms_millicents,
            UsageMetric::LlmToken => self.overage_llm_token_millicents,
            UsageMetric::TtsCharacter => self.overage_tts_character_millicents,
        }
    }
}

/// The default catalogue, identical to `SEED_PLANS` in `app/billing/plans.py`.
pub fn seed_plans() -> Vec<Plan> {
    vec![
        Plan {
            code: "trial".into(),
            name: "Trial".into(),
            monthly_price_cents: 0,
            annual_price_cents: None,
            included_voice_minutes: 60,
            included_sms_segments: 50,
            included_llm_tokens: 200_000,
            included_tts_characters: 100_000,
            overage_voice_minute_millicents: 0,
            overage_sms_millicents: 0,
            overage_llm_token_millicents: 0,
            overage_tts_character_millicents: 0,
            overage_enabled: false,
            trial_days: 14,
            display_order: 0,
        },
        Plan {
            code: "starter".into(),
            name: "Starter".into(),
            monthly_price_cents: 19_900,
            annual_price_cents: Some(199_000),
            included_voice_minutes: 500,
            included_sms_segments: 500,
            included_llm_tokens: 2_000_000,
            included_tts_characters: 1_000_000,
            overage_voice_minute_millicents: 1_200,
            overage_sms_millicents: 200,
            overage_llm_token_millicents: 0,
            overage_tts_character_millicents: 0,
            overage_enabled: true,
            trial_days: 0,
            display_order: 1,
        },
        Plan {
            code: "pro".into(),
            name: "Pro".into(),
            monthly_price_cents: 49_900,
            annual_price_cents: Some(499_000),
            included_voice_minutes: 2_000,
            included_sms_segments: 2_500,
            included_llm_tokens: 10_000_000,
            included_tts_characters: 5_000_000,
            overage_voice_minute_millicents: 1_000,
            overage_sms_millicents: 150,
            overage_llm_token_millicents: 0,
            overage_tts_character_millicents: 0,
            overage_enabled: true,
            trial_days: 0,
            display_order: 2,
        },
        Plan {
            code: "enterprise".into(),
            name: "Enterprise".into(),
            monthly_price_cents: 149_900,
            annual_price_cents: Some(1_499_000),
            included_voice_minutes: 10_000,
            included_sms_segments: 10_000,
            included_llm_tokens: 50_000_000,
            included_tts_characters: 25_000_000,
            overage_voice_minute_millicents: 800,
            overage_sms_millicents: 100,
            overage_llm_token_millicents: 0,
            overage_tts_character_millicents: 0,
            overage_enabled: true,
            trial_days: 0,
            display_order: 3,
        },
    ]
}

/// The plan a tenant with no subscription falls back to (mirrors
/// `DEFAULT_PLAN_CODE`).
pub fn default_plan_code() -> &'static str {
    "starter"
}

/// The stable identity of one unit of consumption (mirrors
/// `usage_idempotency_key`): `"{metric}:{entity}"` with an optional
/// `":{discriminator}"` suffix. Deterministic and legible, never hashed or
/// timestamped, so a retried callback computes the same key.
pub fn usage_idempotency_key(metric: UsageMetric, entity_id: &str, discriminator: &str) -> String {
    let base = format!("{}:{}", metric.as_str(), entity_id);
    if discriminator.is_empty() {
        base
    } else {
        format!("{}:{}", base, discriminator)
    }
}

/// A call's duration as whole billable seconds (mirrors `billable_seconds`):
/// floors rather than rounds, applies a one-second minimum, and treats
/// non-positive durations as zero.
pub fn billable_seconds(duration_seconds: f64) -> i64 {
    if !duration_seconds.is_finite() || duration_seconds <= 0.0 {
        return 0;
    }
    let floored = duration_seconds as i64;
    floored.max(1)
}

/// Returns `(billable_units, cost_millicents)` for the period overage
/// (mirrors `compute_overage`). Voice overage is rounded up to whole minutes
/// once, on the period total; every other metric is already discrete.
pub fn compute_overage(
    used: i64,
    included: i64,
    metric: UsageMetric,
    rate_millicents: i64,
) -> (i64, i64) {
    let used = used.max(0);
    let included = included.max(0);
    let raw_overage = (used - included).max(0);
    if raw_overage == 0 {
        return (0, 0);
    }
    let billable = match metric {
        UsageMetric::VoiceMinute => (raw_overage + 59) / 60,
        _ => raw_overage,
    };
    (billable, billable * rate_millicents)
}

/// The outcome of `Meter::record` — recording never errors for a duplicate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecordOutcome {
    NoEvent,
    Recorded,
    Duplicate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UsageError {
    NegativeUsageNotAdjustment,
}

/// The audit trail for a manual correction: who did it and why (mirrors the
/// `metadata={"reason": ..., "actor": ...}` carried by `record_adjustment`).
#[derive(Debug, Clone, Copy)]
pub struct Adjustment<'a> {
    pub reason: &'a str,
    pub actor: &'a str,
    pub reference: Option<&'a str>,
}

/// One append-only usage event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageEvent {
    pub tenant_id: String,
    pub metric: UsageMetric,
    pub event_type: UsageEventType,
    pub quantity: i64,
    pub idempotency_key: String,
    pub period: String,
}

/// Tenant-scoped, idempotent, append-only usage log. The Rust shadow of the
/// `UsageEvent` table: corrections are new rows, duplicates are no-ops, and
/// totals are summed (clamped at zero) exactly like `used_quantity`.
#[derive(Debug, Default)]
pub struct Meter {
    events: Vec<UsageEvent>,
    seen: HashSet<(String, String)>,
}

impl Meter {
    pub fn new() -> Self {
        Meter {
            events: Vec::new(),
            seen: HashSet::new(),
        }
    }

    /// Append one usage event. Idempotent per `(tenant, idempotency_key)`.
    /// Zero quantity is not an error and records nothing; a negative quantity
    /// is only valid for a manual adjustment.
    pub fn record(
        &mut self,
        tenant_id: &str,
        metric: UsageMetric,
        quantity: i64,
        idempotency_key: &str,
        period: &str,
        event_type: Option<UsageEventType>,
    ) -> Result<RecordOutcome, UsageError> {
        if quantity == 0 {
            return Ok(RecordOutcome::NoEvent);
        }
        if quantity < 0 && event_type != Some(UsageEventType::ManualAdjustment) {
            return Err(UsageError::NegativeUsageNotAdjustment);
        }
        let key = (tenant_id.to_string(), idempotency_key.to_string());
        if self.seen.contains(&key) {
            return Ok(RecordOutcome::Duplicate);
        }
        self.seen.insert(key);
        self.events.push(UsageEvent {
            tenant_id: tenant_id.to_string(),
            metric,
            event_type: event_type.unwrap_or_else(|| metric.default_event_type()),
            quantity,
            idempotency_key: idempotency_key.to_string(),
            period: period.to_string(),
        });
        Ok(RecordOutcome::Recorded)
    }

    /// A signed correction (mirrors `record_adjustment`): always a new row,
    /// keyed by reason and actor so a genuine second correction does not
    /// collide while a replay of the first does.
    pub fn record_adjustment(
        &mut self,
        tenant_id: &str,
        metric: UsageMetric,
        quantity: i64,
        adjustment: &Adjustment<'_>,
        period: &str,
    ) -> Result<RecordOutcome, UsageError> {
        let key = usage_idempotency_key(
            metric,
            &format!(
                "adjust:{}",
                adjustment.reference.unwrap_or(adjustment.reason)
            ),
            &format!("{}:{}", adjustment.actor, quantity),
        );
        self.record(
            tenant_id,
            metric,
            quantity,
            &key,
            period,
            Some(UsageEventType::ManualAdjustment),
        )
    }

    /// Total consumption for `(tenant, metric)`, clamped at zero (mirrors
    /// `used_quantity`). This is the authoritative number; a negative total
    /// is stored truthfully and clamped only at the reporting boundary.
    pub fn used(&self, tenant_id: &str, metric: UsageMetric) -> i64 {
        let total: i64 = self
            .events
            .iter()
            .filter(|e| e.tenant_id == tenant_id && e.metric == metric)
            .map(|e| e.quantity)
            .sum();
        total.max(0)
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idempotency_key_is_legible_and_deterministic() {
        let k1 = usage_idempotency_key(UsageMetric::VoiceMinute, "call-1", "");
        assert_eq!(k1, "voice_minute:call-1");
        let k2 = usage_idempotency_key(UsageMetric::VoiceMinute, "call-1", "leg-2");
        assert_eq!(k2, "voice_minute:call-1:leg-2");
    }

    #[test]
    fn billable_seconds_floors_and_applies_minimum() {
        assert_eq!(billable_seconds(99.4), 99);
        assert_eq!(billable_seconds(0.0), 0);
        assert_eq!(billable_seconds(-5.0), 0);
        assert_eq!(billable_seconds(0.4), 1);
        assert_eq!(billable_seconds(120.0), 120);
    }

    #[test]
    fn voice_overage_rounds_up_once_on_period_total() {
        // 620 minutes + 1 second = 37201 seconds used against 500 min
        // (30000 s) -> 7201 s raw overage -> 121 billable minutes.
        let (billable, cost) = compute_overage(37_201, 30_000, UsageMetric::VoiceMinute, 1_200);
        assert_eq!(billable, 121);
        assert_eq!(cost, 121 * 1_200);
    }

    #[test]
    fn discrete_metric_overage_is_not_rounded() {
        let (billable, cost) = compute_overage(700, 500, UsageMetric::SmsSegment, 200);
        assert_eq!(billable, 200);
        assert_eq!(cost, 200 * 200);
    }

    #[test]
    fn no_overage_is_free() {
        assert_eq!(
            compute_overage(100, 500, UsageMetric::VoiceMinute, 1_200),
            (0, 0)
        );
        assert_eq!(
            compute_overage(500, 500, UsageMetric::SmsSegment, 200),
            (0, 0)
        );
    }

    #[test]
    fn included_units_converts_voice_minutes_to_seconds() {
        let starter = &seed_plans()[1];
        assert_eq!(starter.included_units(UsageMetric::VoiceMinute), 30_000);
        assert_eq!(starter.included_units(UsageMetric::SmsSegment), 500);
        assert_eq!(starter.included_units(UsageMetric::LlmToken), 2_000_000);
        assert_eq!(starter.included_units(UsageMetric::TtsCharacter), 1_000_000);
        assert_eq!(
            starter.overage_rate_millicents(UsageMetric::VoiceMinute),
            1_200
        );
    }

    #[test]
    fn seed_catalogue_matches_python() {
        let plans = seed_plans();
        assert_eq!(plans.len(), 4);
        assert_eq!(default_plan_code(), "starter");
        assert_eq!(plans[0].code, "trial");
        assert!(!plans[0].overage_enabled);
        assert_eq!(plans[1].code, "starter");
        assert_eq!(plans[1].monthly_price_cents, 19_900);
        assert_eq!(plans[2].code, "pro");
        assert_eq!(plans[2].included_voice_minutes, 2_000);
        assert_eq!(plans[3].code, "enterprise");
        assert_eq!(plans[3].overage_voice_minute_millicents, 800);
    }

    #[test]
    fn zero_quantity_records_nothing() {
        let mut m = Meter::new();
        assert_eq!(
            m.record("t", UsageMetric::VoiceMinute, 0, "k", "p", None),
            Ok(RecordOutcome::NoEvent)
        );
        assert!(m.is_empty());
    }

    #[test]
    fn negative_usage_only_valid_for_adjustment() {
        let mut m = Meter::new();
        assert_eq!(
            m.record("t", UsageMetric::VoiceMinute, -5, "k", "p", None),
            Err(UsageError::NegativeUsageNotAdjustment)
        );
        assert_eq!(
            m.record(
                "t",
                UsageMetric::VoiceMinute,
                -5,
                "k",
                "p",
                Some(UsageEventType::ManualAdjustment)
            ),
            Ok(RecordOutcome::Recorded)
        );
    }

    #[test]
    fn duplicate_key_is_a_noop_not_a_failure() {
        let mut m = Meter::new();
        assert_eq!(
            m.record("t", UsageMetric::VoiceMinute, 10, "k", "p", None),
            Ok(RecordOutcome::Recorded)
        );
        assert_eq!(
            m.record("t", UsageMetric::VoiceMinute, 10, "k", "p", None),
            Ok(RecordOutcome::Duplicate)
        );
        assert_eq!(m.len(), 1);
        assert_eq!(m.used("t", UsageMetric::VoiceMinute), 10);
    }

    #[test]
    fn totals_are_tenant_scoped_and_clamped() {
        let mut m = Meter::new();
        m.record("a", UsageMetric::VoiceMinute, 100, "a1", "p", None)
            .unwrap();
        m.record("b", UsageMetric::VoiceMinute, 999, "b1", "p", None)
            .unwrap();
        assert_eq!(m.used("a", UsageMetric::VoiceMinute), 100);
        assert_eq!(m.used("b", UsageMetric::VoiceMinute), 999);
        // A correction can push a tenant negative in storage, but the
        // reported total is clamped at zero.
        let adj = Adjustment {
            reason: "dispute",
            actor: "ops",
            reference: None,
        };
        m.record_adjustment("a", UsageMetric::VoiceMinute, -500, &adj, "p")
            .unwrap();
        assert_eq!(m.used("a", UsageMetric::VoiceMinute), 0);
    }
}
