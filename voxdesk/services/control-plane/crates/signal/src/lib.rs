//! Real-time WebSocket signaling hub.
//!
//! This is the Phase 2 control-plane boundary for signaling: it accepts
//! WebSocket connections, requires each client to declare its tenant in a
//! `hello` message, and then scopes every room by `(tenant_id, room)` so no
//! client can ever subscribe to, or publish into, another tenant's room.
//!
//! Delivery is non-blocking (backpressure-drop) so one slow client can never
//! stall a room, and the read loop enforces an idle timeout so dead sockets
//! are reaped.

pub mod hub;
pub mod protocol;
