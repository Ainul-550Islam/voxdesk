//! routing — the forwarding graph: which participant publishes what, who
//! subscribed to it, and therefore which legs an inbound datagram fans
//! out to. The hot path's query is ONE method (`legs_for`) returning an
//! already-materialized, allocation-light answer — the UDP loop cannot
//! afford lock-holding or map iteration per packet.
//!
//! Design notes:
//! * The table is a single RwLock over small BTreeMaps. Reads (route
//!   lookup per packet) take the READ lock; structural changes take the
//!   write lock. Rooms are cloned-snapshot per query instead — see
//!   `RouteCache`: per-room routing snapshots invalidated by structural
//!   change, so the per-packet path is a version-checked snapshot read,
//!   never a lock convoy.
//! * Caps are enforced at the structural boundary (join/publish), not on
//!   the packet path, mirroring the gateway's upgrade/hello-time policy.

use protocol::{MediaKind, ParticipantId, RoomId, TrackId};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};

/// Structural caps per room (config-fed, boot-validated).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RoomLimits {
    pub max_participants: usize,
    pub max_tracks_per_participant: usize,
    pub max_subscriptions_per_participant: usize,
}

impl Default for RoomLimits {
    fn default() -> Self {
        RoomLimits {
            max_participants: 64,
            max_tracks_per_participant: 4,
            max_subscriptions_per_participant: 64,
        }
    }
}

/// Structural-change failures — the caller maps them to wire error codes
/// (OverLimit / RoomUnknown …); this crate stays transport-free.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteError {
    RoomFull,
    TrackLimit,
    SubscriptionLimit,
    NoSuchParticipant,
    NoSuchTrack,
    DuplicateTrack,
}

/// One participant's structural record.
#[derive(Clone, Debug)]
pub struct ParticipantEntry {
    pub id: ParticipantId,
    pub joined_seq: u64,
    /// Tracks this participant PUBLISHES: track id → media kind.
    pub published: BTreeMap<TrackId, MediaKind>,
    /// Tracks this participant RECEIVES: (publisher, track) pairs.
    pub subscribed: BTreeMap<(ParticipantId, TrackId), ()>,
}

/// One room's full structure.
#[derive(Clone, Debug, Default)]
pub struct RoomEntry {
    pub participants: BTreeMap<ParticipantId, ParticipantEntry>,
}

/// A query answer: the destination participants for one (publisher,
/// track) flow, snapshot-materialized.
#[derive(Clone, Debug, Default)]
pub struct LegSet {
    pub legs: Vec<ParticipantId>,
    pub version: u64,
}

/// The route table with per-room snapshot caching.
pub struct RouteTable {
    rooms: RwLock<BTreeMap<RoomId, RoomEntry>>,
    limits: RoomLimits,
    /// Structural version: bumped by EVERY mutation; snapshots carry the
    /// version they were built at, so a hot-path reader can cheaply detect
    /// a stale snapshot and rebuild it (read-lock) exactly once per change.
    version: Arc<AtomicU64>,
    snapshot: RwLock<BTreeMap<RoomId, Arc<CachedRoomRoutes>>>,
}

struct CachedRoomRoutes {
    version: u64,
    /// (publisher, track) → subscriber leg list. Public to routing only.
    routes: BTreeMap<(ParticipantId, TrackId), Arc<Vec<ParticipantId>>>,
}

impl RouteTable {
    pub fn new(limits: RoomLimits) -> RouteTable {
        RouteTable {
            rooms: RwLock::new(BTreeMap::new()),
            limits,
            version: Arc::new(AtomicU64::new(0)),
            snapshot: RwLock::new(BTreeMap::new()),
        }
    }

    fn bump(&self) {
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    pub fn version(&self) -> u64 {
        self.version.load(Ordering::SeqCst)
    }

    // --------------------------------------------------------------- join

    /// Registers a participant in a room (creating the room). Returns the
    /// participant count after the join — the join ack's "room size".
    pub fn join(
        &self,
        room: &RoomId,
        participant: ParticipantId,
        seq: u64,
    ) -> Result<usize, RouteError> {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let entry = rooms.entry(room.clone()).or_default();
        if entry.participants.contains_key(&participant) {
            // Re-join of an existing participant: idempotent, not an error
            // (a reconnecting client re-asserts state; same discipline as
            // the gateway hub's re-bind).
            return Ok(entry.participants.len());
        }
        if entry.participants.len() >= self.limits.max_participants {
            return Err(RouteError::RoomFull);
        }
        entry.participants.insert(
            participant.clone(),
            ParticipantEntry {
                id: participant,
                joined_seq: seq,
                published: BTreeMap::new(),
                subscribed: BTreeMap::new(),
            },
        );
        let count = entry.participants.len();
        drop(rooms);
        self.bump();
        self.invalidate(room);
        Ok(count)
    }

    /// Removes a participant and every route involving it.
    pub fn leave(&self, room: &RoomId, participant: &ParticipantId) -> Option<ParticipantEntry> {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let entry = rooms.get_mut(room)?;
        let removed = entry.participants.remove(participant);
        if let Some(member) = removed.as_ref() {
            // Scrub subscriptions TO the departed participant's tracks.
            let gone_tracks: Vec<(ParticipantId, TrackId)> = member
                .published
                .keys()
                .map(|t| (participant.clone(), t.clone()))
                .collect();
            for other in entry.participants.values_mut() {
                for key in &gone_tracks {
                    other.subscribed.remove(key);
                }
            }
            if entry.participants.is_empty() {
                rooms.remove(room);
            }
            drop(rooms);
            self.bump();
            self.invalidate(room);
            return removed;
        }
        None
    }

    // ------------------------------------------------------------ publish

    pub fn publish(
        &self,
        room: &RoomId,
        participant: &ParticipantId,
        track: TrackId,
        kind: MediaKind,
    ) -> Result<(), RouteError> {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let entry = rooms.get_mut(room).ok_or(RouteError::NoSuchParticipant)?;
        let p = entry
            .participants
            .get_mut(participant)
            .ok_or(RouteError::NoSuchParticipant)?;
        if p.published.contains_key(&track) {
            return Err(RouteError::DuplicateTrack);
        }
        if p.published.len() >= self.limits.max_tracks_per_participant {
            return Err(RouteError::TrackLimit);
        }
        p.published.insert(track, kind);
        drop(rooms);
        self.bump();
        self.invalidate(room);
        Ok(())
    }

    pub fn unpublish(&self, room: &RoomId, participant: &ParticipantId, track: &TrackId) -> bool {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let Some(entry) = rooms.get_mut(room) else {
            return false;
        };
        let Some(p) = entry.participants.get_mut(participant) else {
            return false;
        };
        let had = p.published.remove(track).is_some();
        if had {
            let key = (participant.clone(), track.clone());
            for other in entry.participants.values_mut() {
                other.subscribed.remove(&key);
            }
            drop(rooms);
            self.bump();
            self.invalidate(room);
        }
        had
    }

    // ---------------------------------------------------------- subscribe

    pub fn subscribe(
        &self,
        room: &RoomId,
        subscriber: &ParticipantId,
        publisher: &ParticipantId,
        track: &TrackId,
    ) -> Result<(), RouteError> {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let entry = rooms.get_mut(room).ok_or(RouteError::NoSuchParticipant)?;
        // The target must really be published — a subscription to a hoped-
        // for track is a routing lie the engine would never fulfill.
        let target = entry
            .participants
            .get(publisher)
            .ok_or(RouteError::NoSuchParticipant)?;
        if !target.published.contains_key(track) {
            return Err(RouteError::NoSuchTrack);
        }
        let sub = entry
            .participants
            .get_mut(subscriber)
            .ok_or(RouteError::NoSuchParticipant)?;
        let key = (publisher.clone(), track.clone());
        if sub.subscribed.contains_key(&key) {
            return Ok(()); // idempotent re-assert
        }
        if sub.subscribed.len() >= self.limits.max_subscriptions_per_participant {
            return Err(RouteError::SubscriptionLimit);
        }
        sub.subscribed.insert(key, ());
        drop(rooms);
        self.bump();
        self.invalidate(room);
        Ok(())
    }

    pub fn unsubscribe(
        &self,
        room: &RoomId,
        subscriber: &ParticipantId,
        publisher: &ParticipantId,
        track: &TrackId,
    ) -> bool {
        let mut rooms = self.rooms.write().unwrap_or_else(|p| p.into_inner());
        let Some(entry) = rooms.get_mut(room) else {
            return false;
        };
        let Some(sub) = entry.participants.get_mut(subscriber) else {
            return false;
        };
        let had = sub
            .subscribed
            .remove(&(publisher.clone(), track.clone()))
            .is_some();
        if had {
            drop(rooms);
            self.bump();
            self.invalidate(room);
        }
        had
    }

    // ----------------------------------------------------------- queries

    fn invalidate(&self, room: &RoomId) {
        self.snapshot
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .remove(room);
    }

    /// THE hot-path query: subscriber legs of (room, publisher, track).
    /// Read-mostly: rebuilds the room's cached route snapshot only when
    /// the structural version advanced past the snapshot's.
    pub fn legs_for(
        &self,
        room: &RoomId,
        publisher: &ParticipantId,
        track: &TrackId,
    ) -> Arc<Vec<ParticipantId>> {
        let key = (publisher.clone(), track.clone());
        let version = self.version();

        // Fast path: snapshot is current.
        {
            let snaps = self.snapshot.read().unwrap_or_else(|p| p.into_inner());
            if let Some(cached) = snaps.get(room) {
                if cached.version == version {
                    if let Some(legs) = cached.routes.get(&key) {
                        return legs.clone();
                    }
                    return empty_legs();
                }
            }
        }
        // Slow path: rebuild the snapshot for this room.
        self.rebuild(room);
        let snaps = self.snapshot.read().unwrap_or_else(|p| p.into_inner());
        if let Some(cached) = snaps.get(room) {
            if let Some(legs) = cached.routes.get(&key) {
                return legs.clone();
            }
        }
        empty_legs()
    }

    /// Rebuild one room's route snapshot from the authoritative table.
    fn rebuild(&self, room: &RoomId) {
        let rooms = self.rooms.read().unwrap_or_else(|p| p.into_inner());
        let version = self.version();
        let mut routes: BTreeMap<(ParticipantId, TrackId), Arc<Vec<ParticipantId>>> =
            BTreeMap::new();
        if let Some(entry) = rooms.get(room) {
            // Invert the subscription graph: (publisher,track) → [subscribers].
            // Presorted by participant id: leg iteration is deterministic.
            let mut inverse: BTreeMap<(ParticipantId, TrackId), Vec<ParticipantId>> =
                BTreeMap::new();
            for p in entry.participants.values() {
                for (publisher, track) in p.subscribed.keys() {
                    inverse
                        .entry((publisher.clone(), track.clone()))
                        .or_default()
                        .push(p.id.clone());
                }
            }
            for (k, v) in inverse {
                routes.insert(k, Arc::new(v));
            }
        }
        drop(rooms);
        self.snapshot
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .insert(room.clone(), Arc::new(CachedRoomRoutes { version, routes }));
    }

    /// Structural counts for telemetry gauges.
    pub fn stats(&self) -> (usize, usize, usize) {
        let rooms = self.rooms.read().unwrap_or_else(|p| p.into_inner());
        let participants: usize = rooms.values().map(|r| r.participants.len()).sum();
        let tracks: usize = rooms
            .values()
            .flat_map(|r| r.participants.values())
            .map(|p| p.published.len())
            .sum();
        (rooms.len(), participants, tracks)
    }

    /// A participant's published tracks (signaling's TrackPublished fanfare).
    pub fn published_tracks(
        &self,
        room: &RoomId,
        participant: &ParticipantId,
    ) -> Vec<(TrackId, MediaKind)> {
        let rooms = self.rooms.read().unwrap_or_else(|p| p.into_inner());
        rooms
            .get(room)
            .and_then(|r| r.participants.get(participant))
            .map(|p| p.published.clone().into_iter().collect())
            .unwrap_or_default()
    }

    /// All published tracks of a room except the asking participant's
    /// (the "here's what you can subscribe to" listing on join).
    pub fn room_summary(
        &self,
        room: &RoomId,
        except: &ParticipantId,
    ) -> Vec<(ParticipantId, TrackId, MediaKind)> {
        let rooms = self.rooms.read().unwrap_or_else(|p| p.into_inner());
        let mut out = Vec::new();
        if let Some(entry) = rooms.get(room) {
            for p in entry.participants.values() {
                if p.id == *except {
                    continue;
                }
                for (t, k) in &p.published {
                    out.push((p.id.clone(), t.clone(), *k));
                }
            }
        }
        out
    }
}

fn empty_legs() -> Arc<Vec<ParticipantId>> {
    // A per-thread shared empty vector: legs_for never allocates for
    // "no subscribers" — the overwhelmingly common case for a track that
    // only just started.
    thread_local! {
        static SHARED_EMPTY: Arc<Vec<ParticipantId>> = Arc::new(Vec::new());
    }
    SHARED_EMPTY.with(|e| e.clone())
}
