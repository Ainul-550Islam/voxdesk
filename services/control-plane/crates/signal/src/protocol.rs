//! Wire messages exchanged over the WebSocket, tagged-JSON for forwards
//! compatibility. Fields are minimal and never carry credentials: the tenant
//! id is declared once in `hello` and enforced server-side afterwards.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMessage {
    /// Declares the tenant this connection belongs to. Must be the first
    /// message; any other message before `hello` is rejected.
    Hello {
        tenant_id: String,
    },
    Subscribe {
        room: String,
    },
    Unsubscribe {
        room: String,
    },
    Publish {
        room: String,
        payload: Value,
    },
    Ping,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerMessage {
    Welcome {
        session_id: String,
    },
    Subscribed {
        room: String,
        peers: usize,
    },
    Unsubscribed {
        room: String,
    },
    Delivery {
        room: String,
        from: String,
        payload: Value,
    },
    Error {
        code: String,
        message: String,
    },
    Pong,
}

// ============================================================================
// Session-contract message shapes
//
// These mirror `contracts/proto/voxdesk/contracts/v1/session.proto`
// field-for-field (and `common.proto` for `TenantContext`), as JSON. They are
// what the control plane emits for the session/SIP-SDP boundary; the protobuf
// (tonic/prost) transport binds the same shapes once it lands, so the JSON
// here is the interim wire format rather than a different vocabulary.
//
// `Uuid` is serialized as its canonical RFC 4122 text (36 chars), which is
// exactly how the Python API already serializes `uuid.UUID` values in JSON.
// Timestamps are RFC 3339 strings, matching `google.protobuf.Timestamp`.
// ============================================================================

/// Mirrors `common.proto` `TenantContext`. Receivers take the tenant identity
/// from `tenant_id` alone and never from anywhere else in the message.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TenantContext {
    pub tenant_id: String,
    pub trace_id: String,
    pub actor: String,
}

/// Mirrors `session.proto` `SessionStarted`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SessionStarted {
    pub tenant: TenantContext,
    pub call_id: String,
    pub call_sid: String,
    pub direction: String,
    pub from_number: String,
    pub to_number: String,
    pub started_at: String,
}

/// Mirrors `session.proto` `SessionStatusChanged`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SessionStatusChanged {
    pub tenant: TenantContext,
    pub call_id: String,
    pub status: String,
    pub failure_reason: Option<String>,
    pub ended_at: Option<String>,
    pub duration_seconds: Option<f64>,
}

/// Mirrors `session.proto` `TransferRequested`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TransferRequested {
    pub tenant: TenantContext,
    pub call_id: String,
    pub destination: String,
    pub reason: String,
    pub attempts: i32,
}

/// Mirrors `session.proto` `TransferStateChanged`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TransferStateChanged {
    pub tenant: TenantContext,
    pub call_id: String,
    pub state: String,
    pub error: Option<String>,
    pub requested_at: Option<String>,
    pub started_at: Option<String>,
    pub completed_at: Option<String>,
    pub failed_at: Option<String>,
}

/// Mirrors `session.proto` `SessionAck`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SessionAck {
    pub tenant: TenantContext,
    pub call_id: String,
    pub accepted: bool,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> TenantContext {
        TenantContext {
            tenant_id: "8f14e45f-ceea-4f0a-9a4b-1f7b2e3d4a5b".into(),
            trace_id: "trace-1".into(),
            actor: "scheduler".into(),
        }
    }

    #[test]
    fn session_started_round_trips() {
        let msg = SessionStarted {
            tenant: ctx(),
            call_id: "c0ffee00-0000-0000-0000-000000000001".into(),
            call_sid: "CA1234".into(),
            direction: "inbound".into(),
            from_number: "+15551234567".into(),
            to_number: "+15559876543".into(),
            started_at: "2026-09-14T10:00:00Z".into(),
        };
        let json = serde_json::to_string(&msg).unwrap();
        let back: SessionStarted = serde_json::from_str(&json).unwrap();
        assert_eq!(back, msg);
    }

    #[test]
    fn status_changed_carries_optional_fields() {
        let json = r#"{
            "tenant": {"tenant_id": "t", "trace_id": "tr", "actor": "system"},
            "call_id": "c",
            "status": "transferred",
            "failure_reason": null,
            "ended_at": null,
            "duration_seconds": 12.5
        }"#;
        let msg: SessionStatusChanged = serde_json::from_str(json).unwrap();
        assert_eq!(msg.status, "transferred");
        assert_eq!(msg.failure_reason, None);
        assert_eq!(msg.duration_seconds, Some(12.5));
    }

    #[test]
    fn ack_mirrors_contract_fields() {
        let msg = SessionAck {
            tenant: ctx(),
            call_id: "c".into(),
            accepted: false,
            error_code: Some("call_already_ended".into()),
            error_message: Some("call is in a terminal state".into()),
        };
        let value = serde_json::to_value(&msg).unwrap();
        let obj = value.as_object().unwrap();
        // The contract vocabulary is the field names themselves.
        for key in [
            "tenant",
            "call_id",
            "accepted",
            "error_code",
            "error_message",
        ] {
            assert!(obj.contains_key(key), "missing contract field {key}");
        }
        let tenant = obj["tenant"].as_object().unwrap();
        assert!(tenant.contains_key("tenant_id"));
        assert!(tenant.contains_key("trace_id"));
        assert!(tenant.contains_key("actor"));
    }
}
