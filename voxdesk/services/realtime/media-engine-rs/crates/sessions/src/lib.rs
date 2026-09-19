//! sessions — the transport-independent session registry: who is known
//! to the engine, in what lifecycle state, and how stale writers are
//! fenced (epoch fencing, same rule the gateway's router applies with
//! its epoch counter on the session map).
//!
//! State machine (strictly forward, with Close as a sink; re-Join of the
//! same participant creates a fresh epoch):
//!
//! ```text
//!   New → Negotiating → Connected → Draining → Closed
//!     ╰─────╯         (any failure → AbortedClosed directly)
//! ```
//!
//! Concurrency: one RwLock over BTreeMaps, epoch snapshots fetched
//! atomically with the Arc — the "fetch, validate, commit" pattern
//! that's deadlock-safe for sweeps (never mutate inside a read-map
//! borrow).

use protocol::{ParticipantId, RoomId};
use std::collections::BTreeMap;
use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Instant;

/// Wall-clock-free time source: tests drive it, prod reads the monotonic
/// clock. Same discipline as gateway's injectable now().
pub trait ClockSource: Send + Sync {
    fn now(&self) -> Instant;
}

pub struct RealClock;
impl ClockSource for RealClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum SessionState {
    New = 0,
    Negotiating,
    Connected,
    Draining,
    Closed,
}

impl fmt::Display for SessionState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        repr_names(*self).fmt(f)
    }
}

fn repr_names(s: SessionState) -> &'static str {
    match s {
        SessionState::New => "new",
        SessionState::Negotiating => "negotiating",
        SessionState::Connected => "connected",
        SessionState::Draining => "draining",
        SessionState::Closed => "closed",
    }
}

/// Allowed transitions — the table IS the spec: fs.exec tests enumerate
/// every row.
pub fn legal_transition(from: SessionState, to: SessionState) -> bool {
    use SessionState::*;
    matches!(
        (from, to),
        (New, Negotiating)
            | (Negotiating, Connected)
            | (Negotiating, Closed)
            | (Connected, Draining)
            | (Connected, Closed)
            | (Draining, Closed)
    )
}

/// One session's public, epoch-tagged record. The `epoch` is bumped on
/// every structural change; a captured (epoch, Arc) pair is how callers
/// fence stale writes (CAS your change only if epoch agrees).
#[derive(Debug)]
pub struct Session {
    pub id: u64,
    pub room: RoomId,
    pub participant: ParticipantId,
    epoch: AtomicU64,
    state: RwLock<SessionState>,
    pub created_at: Instant,
    last_active: RwLock<Instant>,
}

impl Clone for Session {
    fn clone(&self) -> Self {
        Session {
            id: self.id,
            room: self.room.clone(),
            participant: self.participant.clone(),
            epoch: AtomicU64::new(self.epoch.load(Ordering::SeqCst)),
            state: RwLock::new(*self.state.read().unwrap_or_else(|p| p.into_inner())),
            created_at: self.created_at,
            last_active: RwLock::new(*self.last_active.read().unwrap_or_else(|p| p.into_inner())),
        }
    }
}

impl Session {
    fn new(id: u64, room: RoomId, participant: ParticipantId, now: Instant) -> Session {
        Session {
            id,
            room,
            participant,
            epoch: AtomicU64::new(0),
            state: RwLock::new(SessionState::New),
            created_at: now,
            last_active: RwLock::new(now),
        }
    }

    pub fn epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }

    pub fn state(&self) -> SessionState {
        *self.state.read().unwrap_or_else(|p| p.into_inner())
    }

    pub fn last_active(&self) -> Instant {
        *self.last_active.read().unwrap_or_else(|p| p.into_inner())
    }

    pub fn touch(&self, now: Instant) {
        *self.last_active.write().unwrap_or_else(|p| p.into_inner()) = now;
    }

    /// Attempt a state transition; fails (returns current state) when the
    /// move is not in `legal_transition`'s table. Never panics on a
    /// policy violation: session lifecycle errors are worth surfaces, not
    /// crashes.
    pub fn advance(&self, to: SessionState) -> Result<SessionState, SessionState> {
        let mut s = self.state.write().unwrap_or_else(|p| p.into_inner());
        if legal_transition(*s, to) {
            *s = to;
            self.epoch.fetch_add(1, Ordering::SeqCst);
            Ok(*s)
        } else {
            Err(*s)
        }
    }

    pub fn close(&self, now: Instant) {
        self.touch(now);
        let mut s = self.state.write().unwrap_or_else(|p| p.into_inner());
        *s = SessionState::Closed; // close is legal from anywhere (shutdown path certainty)
        self.epoch.fetch_add(1, Ordering::SeqCst);
    }
}

/// Reconnect handling: re-binding an existing participant yields a NEW
/// record (higher id, fresh epoch) and the old one is fenced-out — any
/// code still holding the old Arc sees state → Closed.
pub enum JoinOutcome {
    New(Arc<Session>),
    /// Same participant re-joined: the returned session is fresh; the
    /// previous incarnation is Closed inside the store.
    Rebound {
        fresh: Arc<Session>,
        previous_id: u64,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SweepKind {
    /// New+Negotiating sessions that never reached Connected in time.
    NegotiationTimedOut,
    /// Connected/any sessions idle past the idle window.
    Idle,
}

pub struct Store {
    inner: RwLock<BTreeMap<u64, Arc<Session>>>,
    next_id: AtomicU64,
    pub negotiation_timeout: std::time::Duration,
    pub idle_timeout: std::time::Duration,
}

impl Default for Store {
    fn default() -> Self {
        Store::new()
    }
}

impl Store {
    pub fn new() -> Store {
        Store {
            inner: RwLock::new(BTreeMap::new()),
            next_id: AtomicU64::new(1),
            negotiation_timeout: std::time::Duration::from_secs(10),
            idle_timeout: std::time::Duration::from_secs(60),
        }
    }

    pub fn join(&self, room: &RoomId, participant: &ParticipantId, now: Instant) -> JoinOutcome {
        let mut inner = self.inner.write().unwrap_or_else(|p| p.into_inner());
        // Same participant re-joining: fence the old incarnation.
        let old: Vec<u64> = inner
            .values()
            .filter(|s| {
                s.room == *room
                    && s.participant == *participant
                    && s.state() != SessionState::Closed
            })
            .map(|s| s.id)
            .collect();
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let session = Arc::new(Session::new(id, room.clone(), participant.clone(), now));
        inner.insert(id, session.clone());
        match old.first() {
            Some(&old_id) => {
                if let Some(prev) = inner.get(&old_id) {
                    prev.close(now);
                }
                JoinOutcome::Rebound {
                    fresh: session,
                    previous_id: old_id,
                }
            }
            None => JoinOutcome::New(session),
        }
    }

    pub fn get(&self, id: u64) -> Option<Arc<Session>> {
        self.inner
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .get(&id)
            .cloned()
    }

    pub fn leave(&self, id: u64, now: Instant) -> Option<Arc<Session>> {
        let mut inner = self.inner.write().unwrap_or_else(|p| p.into_inner());
        let s = inner.remove(&id)?;
        s.close(now);
        Some(s)
    }

    /// Sweep pass: closes stale sessions, returns (kind, session) pairs
    /// so the engine can emit close events + release routing state.
    pub fn sweep(&self, now: Instant) -> Vec<(SweepKind, Arc<Session>)> {
        let mut inner = self.inner.write().unwrap_or_else(|p| p.into_inner());
        let mut gone = Vec::new();
        inner.retain(|_, s| {
            let state = s.state();
            if state == SessionState::Closed {
                return false; // already dead: purge the record
            }
            let negotiation_stale = state < SessionState::Connected
                && now.duration_since(s.created_at) > self.negotiation_timeout;
            let idle_stale = state >= SessionState::Negotiating
                && now.duration_since(s.last_active()) > self.idle_timeout;
            if negotiation_stale {
                s.close(now);
                gone.push((SweepKind::NegotiationTimedOut, s.clone()));
                false
            } else if idle_stale {
                s.close(now);
                gone.push((SweepKind::Idle, s.clone()));
                false
            } else {
                true
            }
        });
        gone
    }

    pub fn count(&self) -> usize {
        self.inner.read().unwrap_or_else(|p| p.into_inner()).len()
    }

    /// Snapshot of every session matching a predicate, WITHOUT holding
    /// the registry lock while callers act on the Arcs (engine-fan-out
    /// pattern: snapshot-clone, release lock, then iterate).
    pub fn snapshot_where(&self, mut f: impl FnMut(&Session) -> bool) -> Vec<Arc<Session>> {
        self.inner
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .values()
            .filter(|s| f(s))
            .cloned()
            .collect()
    }
}
