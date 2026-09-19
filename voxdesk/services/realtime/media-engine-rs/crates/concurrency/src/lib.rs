//! concurrency — the engine's thread discipline as named, tested types.
//!
//! A media plane is a threads-and-channels program, and the failure modes
//! are always the same: a leaked task that outlives its session, a full
//! channel that blocks the fan-out loop, a shutdown that half the threads
//! never hear. This crate pins the three contracts:
//!
//! * [`Supervisor`]: every task is spawned through it, every task hears
//!   shutdown through one broadcast, and `stop()` joins ALL of them —
//!   the server's whole teardown budget is one method call.
//! * [`channel`]: bounded mpsc with a drop-counting try_send (the media
//!   plane's backpressure rule: a full leg drops a PACKET, never the loop).
//! * [`Sequencer`]: process-wide monotonically increasing id source for
//!   sessions/roc epochs (cheaper than random ids where ordering helps
//!   debugging, and collision-free by construction).

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, SyncSender, TrySendError};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};

// ---------------------------------------------------------------------------
// Supervisor
// ---------------------------------------------------------------------------

/// Shared shutdown signal: a flag + condvar broadcast. Workers wait on it
/// with a timeout (they usually have their own poll cadence); `signal`
/// wakes ALL of them immediately — no polling lag in teardown.
#[derive(Clone)]
pub struct Shutdown {
    inner: Arc<(Mutex<bool>, Condvar)>,
}

impl Shutdown {
    pub fn new() -> Shutdown {
        Shutdown {
            inner: Arc::new((Mutex::new(false), Condvar::new())),
        }
    }

    /// Trip the flag and wake every waiter.
    pub fn signal(&self) {
        let (lock, cvar) = &*self.inner;
        *lock.lock().unwrap_or_else(|p| p.into_inner()) = true;
        cvar.notify_all();
    }

    pub fn tripped(&self) -> bool {
        *self.inner.0.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Waits until shutdown or timeout; returns true if shutdown arrived.
    pub fn wait(&self, timeout: std::time::Duration) -> bool {
        let (lock, cvar) = &*self.inner;
        let guard = lock.lock().unwrap_or_else(|p| p.into_inner());
        if *guard {
            return true;
        }
        let (g, _) = cvar
            .wait_timeout(guard, timeout)
            .unwrap_or_else(|p| p.into_inner());
        *g
    }
}

impl Default for Shutdown {
    fn default() -> Self {
        Self::new()
    }
}

/// What one task reported at exit.
#[derive(Debug)]
pub struct TaskReport {
    pub name: String,
    pub outcome: Result<(), String>, // Err(payload) if it panicked
}

/// Owns every engine task. Uses ONLY std threads (the engine's async-free
/// design: blocking io with small thread counts is a feature — RTP legs
/// are CPU-bound-forwarding, not connection-farming).
pub struct Supervisor {
    shutdown: Shutdown,
    handles: Mutex<Vec<(String, JoinHandle<TaskReport>)>>,
    /// Reject spawn-after-stop loudly rather than leaking a task nobody
    /// will join.
    stopped: AtomicBool,
}

impl Supervisor {
    pub fn new() -> Supervisor {
        Supervisor {
            shutdown: Shutdown::new(),
            handles: Mutex::new(Vec::new()),
            stopped: AtomicBool::new(false),
        }
    }

    pub fn shutdown_handle(&self) -> Shutdown {
        self.shutdown.clone()
    }

    /// Spawn one task. `f` receives the Shutdown handle and MUST poll it
    /// (the discipline this type exists to enforce: a task that never
    /// checks cannot be joined). A task that returns gets reported with
    /// its name; a panicking task is CAUGHT and reported — one dying leg
    /// must never take the engine down and must never be silent either.
    pub fn spawn<F>(&self, name: &str, f: F) -> Result<(), SupervisorStopped>
    where
        F: FnOnce(Shutdown) + Send + 'static,
    {
        if self.stopped.load(Ordering::SeqCst) {
            return Err(SupervisorStopped);
        }
        let shutdown = self.shutdown.clone();
        let owned = name.to_string();
        let report_name = owned.clone();
        let handle = thread::Builder::new()
            .name(owned)
            .spawn(move || {
                let outcome =
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(shutdown))).map_err(
                        |payload| {
                            payload
                                .downcast_ref::<&str>()
                                .map(|s| s.to_string())
                                .or_else(|| payload.downcast_ref::<String>().cloned())
                                .unwrap_or_else(|| "non-string panic payload".to_string())
                        },
                    );
                TaskReport {
                    name: report_name,
                    outcome,
                }
            })
            .map_err(|_| SupervisorStopped)?; // OS refused a thread: same class
        self.handles
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push((name.to_string(), handle));
        Ok(())
    }

    pub fn tasks(&self) -> usize {
        self.handles.lock().unwrap_or_else(|p| p.into_inner()).len()
    }

    /// Signal shutdown and join every task, collecting their reports.
    /// Panicking tasks surface in the return value, not in stderr noise:
    /// the engine's log line is "task X died with Y", once.
    pub fn stop(self) -> Vec<TaskReport> {
        self.stopped.store(true, Ordering::SeqCst);
        self.shutdown.signal();
        let handles = std::mem::take(&mut *self.handles.lock().unwrap_or_else(|p| p.into_inner()));
        let mut reports = Vec::with_capacity(handles.len());
        for (name, handle) in handles {
            match handle.join() {
                Ok(report) => reports.push(report),
                Err(_) => reports.push(TaskReport {
                    name,
                    outcome: Err("task panicked outside its report boundary".to_string()),
                }),
            }
        }
        reports
    }
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SupervisorStopped;

// ---------------------------------------------------------------------------
// Bounded, drop-counting channel
// ---------------------------------------------------------------------------

/// Statistics a producer/consumer pair publishes about itself.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ChannelStats {
    pub sent: u64,
    pub dropped: u64, // try_send on a full channel — the backpressure counter
    pub received: u64,
}

/// Sender half: try_send NEVER blocks (a full leg sheds one packet and
/// counts it — mirroring gateway-go's backpressure.Queue).
pub struct Sender<T> {
    tx: SyncSender<T>,
    stats: Arc<ChannelCounters>,
}

struct ChannelCounters {
    sent: AtomicU64,
    dropped: AtomicU64,
    received: AtomicU64,
}

pub struct Receiver<T> {
    rx: mpsc::Receiver<T>,
    stats: Arc<ChannelCounters>,
}

/// Bounded channel with shared stats.
pub fn channel<T>(capacity: usize) -> (Sender<T>, Receiver<T>) {
    let (tx, rx) = mpsc::sync_channel(capacity.max(1));
    let stats = Arc::new(ChannelCounters {
        sent: AtomicU64::new(0),
        dropped: AtomicU64::new(0),
        received: AtomicU64::new(0),
    });
    (
        Sender {
            tx,
            stats: stats.clone(),
        },
        Receiver { rx, stats },
    )
}

impl<T> Sender<T> {
    /// Non-blocking offer. Full channel → the ITEM IS RETURNED to the
    /// caller (unlike mpsc, which would have the item be lost-in-error:
    /// the forwarding loop needs to decide what to do with it) and the
    /// drop counted.
    pub fn try_send(&self, item: T) -> Result<(), T> {
        match self.tx.try_send(item) {
            Ok(()) => {
                self.stats.sent.fetch_add(1, Ordering::Relaxed);
                Ok(())
            }
            Err(TrySendError::Full(item)) => {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                Err(item)
            }
            Err(TrySendError::Disconnected(item)) => Err(item),
        }
    }

    /// Blocking send for CONTROL paths (control is allowed to wait; media
    /// legs use try_send exclusively).
    pub fn send(&self, item: T) -> Result<(), mpsc::SendError<T>> {
        self.stats.sent.fetch_add(1, Ordering::Relaxed);
        self.tx.send(item)
    }

    pub fn stats(&self) -> ChannelStats {
        ChannelStats {
            sent: self.stats.sent.load(Ordering::Relaxed),
            dropped: self.stats.dropped.load(Ordering::Relaxed),
            received: self.stats.received.load(Ordering::Relaxed),
        }
    }
}

impl<T> Clone for Sender<T> {
    fn clone(&self) -> Self {
        Sender {
            tx: self.tx.clone(),
            stats: self.stats.clone(),
        }
    }
}

impl<T> Receiver<T> {
    pub fn try_recv(&self) -> Result<T, mpsc::TryRecvError> {
        let item = self.rx.try_recv();
        if item.is_ok() {
            self.stats.received.fetch_add(1, Ordering::Relaxed);
        }
        item
    }

    pub fn recv_timeout(&self, timeout: std::time::Duration) -> Result<T, mpsc::RecvTimeoutError> {
        let item = self.rx.recv_timeout(timeout)?;
        self.stats.received.fetch_add(1, Ordering::Relaxed);
        Ok(item)
    }

    pub fn stats(&self) -> ChannelStats {
        ChannelStats {
            sent: self.stats.sent.load(Ordering::Relaxed),
            dropped: self.stats.dropped.load(Ordering::Relaxed),
            received: self.stats.received.load(Ordering::Relaxed),
        }
    }
}

// ---------------------------------------------------------------------------
// Sequencer
// ---------------------------------------------------------------------------

/// Process-wide monotonically increasing ids. Where a UUID's randomness
/// buys nothing (internal session indexes, ROC epochs, per-node message
/// numbers) a cheap, ALLOCATOR-only counter wins — and ordering tells you
/// which of two objects is older in a debugger, which a random id never
/// does.
pub struct Sequencer(AtomicU64);

impl Sequencer {
    pub const fn new() -> Sequencer {
        Sequencer(AtomicU64::new(0))
    }

    /// The next id, starting at 1 (0 stays a sentinel "unset" value).
    pub fn next(&self) -> u64 {
        self.0.fetch_add(1, Ordering::Relaxed) + 1
    }

    pub fn current(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}

impl Default for Sequencer {
    fn default() -> Self {
        Self::new()
    }
}
