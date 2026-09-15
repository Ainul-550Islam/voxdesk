//! Deterministic interval scheduler, the control-plane replacement for
//! `scripts/scheduler.py`.
//!
//! The Python worker runs a fixed set of named loops, each with its own
//! interval, and the one invariant that matters is that **a failing tick never
//! stops the loop** (a CRM outage is routine; a scheduler that exits because of
//! one is not). This module mirrors that exactly with a logical clock so the
//! cadence and the error isolation are both unit-testable without real time:
//!
//!   * a job has a name, an interval and a `next_run_at` timestamp;
//!   * `due_jobs(now)` returns the jobs whose time has come;
//!   * `run_job` executes the tick, records success/failure, and reschedules
//!     by the interval regardless of the outcome — the loop never dies.
//!
//! `default_jobs()` registers the same job set as `scripts/scheduler.py` with
//! the same intervals, so shadow-mode parity can compare due-cadence
//! side-by-side against the Python worker.

/// The intervals the Python scheduler uses (seconds). Kept as named constants
/// so `default_jobs()` and the tests cite the same source of truth.
pub const REMINDER_INTERVAL_SECONDS: u64 = 120;
pub const CAMPAIGN_INTERVAL_SECONDS: u64 = 60;
pub const KNOWLEDGE_INTERVAL_SECONDS: u64 = 15;
pub const CRM_SYNC_INTERVAL_SECONDS: u64 = 20;
pub const BILLING_INTERVAL_SECONDS: u64 = 3600;
pub const RETENTION_INTERVAL_SECONDS: u64 = 86_400;
pub const STUCK_SWEEP_INTERVAL_SECONDS: u64 = 60;

#[derive(Debug, Clone)]
pub struct Job {
    pub name: String,
    pub interval_seconds: u64,
    pub next_run_at_seconds: u64,
    pub runs: u64,
    pub failures: u64,
    pub last_run_ok: Option<bool>,
}

#[derive(Debug, Default)]
pub struct Scheduler {
    jobs: Vec<Job>,
}

impl Scheduler {
    pub fn new() -> Self {
        Scheduler { jobs: Vec::new() }
    }

    /// Registers a job due at `now` (its first tick runs immediately, matching
    /// the Python loops that fire once at startup) and then every
    /// `interval_seconds`. An interval of 0 is rejected — a zero-interval loop
    /// would starve every other job.
    pub fn add_job(
        &mut self,
        name: impl Into<String>,
        interval_seconds: u64,
        now_seconds: u64,
    ) -> Result<(), &'static str> {
        if interval_seconds == 0 {
            return Err("interval must be positive");
        }
        self.jobs.push(Job {
            name: name.into(),
            interval_seconds,
            next_run_at_seconds: now_seconds,
            runs: 0,
            failures: 0,
            last_run_ok: None,
        });
        Ok(())
    }

    pub fn job(&self, name: &str) -> Option<&Job> {
        self.jobs.iter().find(|j| j.name == name)
    }

    pub fn job_names(&self) -> Vec<&str> {
        self.jobs.iter().map(|j| j.name.as_str()).collect()
    }

    /// The names of the jobs whose next run is at or before `now_seconds`.
    pub fn due_jobs(&self, now_seconds: u64) -> Vec<&str> {
        self.jobs
            .iter()
            .filter(|j| j.next_run_at_seconds <= now_seconds)
            .map(|j| j.name.as_str())
            .collect()
    }

    /// Runs one tick of `name`. Returns `None` when the job is unknown,
    /// `Some(ok)` otherwise. A failing tick is recorded and the job is
    /// rescheduled exactly as a successful one — error isolation is the whole
    /// point, so `tick` returning `Err` must never stop the loop.
    pub fn run_job(
        &mut self,
        name: &str,
        now_seconds: u64,
        tick: &mut dyn FnMut() -> Result<(), String>,
    ) -> Option<bool> {
        let job = self.jobs.iter_mut().find(|j| j.name == name)?;
        let result = tick();
        let ok = result.is_ok();
        job.runs += 1;
        if !ok {
            job.failures += 1;
        }
        job.last_run_ok = Some(ok);
        job.next_run_at_seconds = now_seconds + job.interval_seconds;
        Some(ok)
    }
}

/// The job registry the Python scheduler runs, with identical intervals.
/// Mirrors `scripts/scheduler.py`: reminders, campaigns, knowledge ingestion,
/// CRM sync, billing reconciliation, retention, and the stuck-side-effect
/// sweep.
pub fn default_jobs(now_seconds: u64) -> Scheduler {
    let mut s = Scheduler::new();
    // Interval 0 is rejected, so a bug here surfaces immediately rather than
    // registering a hot loop.
    for (name, interval) in [
        ("reminders", REMINDER_INTERVAL_SECONDS),
        ("campaigns", CAMPAIGN_INTERVAL_SECONDS),
        ("knowledge_ingestion", KNOWLEDGE_INTERVAL_SECONDS),
        ("crm_sync", CRM_SYNC_INTERVAL_SECONDS),
        ("billing_reconciliation", BILLING_INTERVAL_SECONDS),
        ("retention", RETENTION_INTERVAL_SECONDS),
        ("stuck_sweep", STUCK_SWEEP_INTERVAL_SECONDS),
    ] {
        let _ = s.add_job(name, interval, now_seconds);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn job_fires_immediately_then_on_interval() {
        let mut s = Scheduler::new();
        s.add_job("a", 10, 0).unwrap();
        assert_eq!(s.due_jobs(0), vec!["a"]);
        assert!(s.run_job("a", 0, &mut || Ok(())).unwrap());
        assert!(s.due_jobs(0).is_empty());
        assert!(s.due_jobs(9).is_empty());
        assert_eq!(s.due_jobs(10), vec!["a"]);
    }

    #[test]
    fn zero_interval_is_rejected() {
        let mut s = Scheduler::new();
        assert!(s.add_job("a", 0, 0).is_err());
        assert!(s.due_jobs(0).is_empty());
    }

    #[test]
    fn failing_tick_is_isolated_and_rescheduled() {
        let mut s = Scheduler::new();
        s.add_job("flaky", 10, 0).unwrap();
        s.add_job("steady", 10, 0).unwrap();

        assert!(!s.run_job("flaky", 0, &mut || Err("boom".into())).unwrap());
        // The failure is recorded but the loop continues: flaky is rescheduled.
        assert_eq!(s.job("flaky").unwrap().failures, 1);
        assert_eq!(s.job("flaky").unwrap().next_run_at_seconds, 10);
        // The other job is untouched and runs fine.
        assert!(s.run_job("steady", 0, &mut || Ok(())).unwrap());
        assert_eq!(s.job("steady").unwrap().failures, 0);
        // flaky runs again at t=10 and can succeed.
        assert!(s.run_job("flaky", 10, &mut || Ok(())).unwrap());
        assert_eq!(s.job("flaky").unwrap().runs, 2);
        assert_eq!(s.job("flaky").unwrap().failures, 1);
    }

    #[test]
    fn unknown_job_is_a_noop() {
        let mut s = Scheduler::new();
        assert_eq!(s.run_job("nope", 0, &mut || Ok(())), None);
    }

    #[test]
    fn default_registry_matches_python_scheduler() {
        let s = default_jobs(0);
        assert_eq!(s.job_names().len(), 7);
        assert_eq!(s.job("reminders").unwrap().interval_seconds, 120);
        assert_eq!(s.job("campaigns").unwrap().interval_seconds, 60);
        assert_eq!(s.job("knowledge_ingestion").unwrap().interval_seconds, 15);
        assert_eq!(s.job("crm_sync").unwrap().interval_seconds, 20);
        assert_eq!(
            s.job("billing_reconciliation").unwrap().interval_seconds,
            3600
        );
        assert_eq!(s.job("retention").unwrap().interval_seconds, 86_400);
        assert_eq!(s.job("stuck_sweep").unwrap().interval_seconds, 60);
    }

    #[test]
    fn multiple_jobs_due_together_are_all_reported() {
        let mut s = Scheduler::new();
        s.add_job("a", 10, 0).unwrap();
        s.add_job("b", 20, 0).unwrap();
        assert_eq!(s.due_jobs(0).len(), 2);
        s.run_job("a", 0, &mut || Ok(())).unwrap();
        s.run_job("b", 0, &mut || Ok(())).unwrap();
        // b next at 20, a next at 10.
        assert_eq!(s.due_jobs(10), vec!["a"]);
        assert!(s.due_jobs(19).contains(&"a"));
        assert!(!s.due_jobs(19).contains(&"b"));
        assert_eq!(s.due_jobs(20).len(), 2);
    }
}
