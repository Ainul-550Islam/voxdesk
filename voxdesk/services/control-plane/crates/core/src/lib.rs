//! Concurrency + session-state core for the VoxDesk control plane.
//!
//! Every module here is std-only and single-crate testable, so the invariants
//! that matter (tenant isolation, exactly-once side effects, bounded fan-out
//! with backpressure, a call lifecycle that mirrors the Python enums) are
//! enforced by plain unit tests before any network layer is involved.

pub mod broadcast;
pub mod idempotency;
pub mod ratelimit;
pub mod registry;
pub mod retry;
pub mod scheduler;
pub mod session;
pub mod usage;
