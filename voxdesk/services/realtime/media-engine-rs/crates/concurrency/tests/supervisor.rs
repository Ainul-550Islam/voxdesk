use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use concurrency::{channel, Sequencer, Supervisor};

#[test]
fn supervisor_runs_tasks_and_joins_all_on_stop() {
    let sup = Supervisor::new();
    let counter = Arc::new(AtomicUsize::new(0));
    for _ in 0..4 {
        let c = Arc::clone(&counter);
        sup.spawn("worker", move |shutdown| {
            // Add BEFORE the shutdown check each iteration: join()
            // guarantees every worker was scheduled, so every worker
            // contributes at least one increment no matter when stop()
            // fires — the assertion stays race-free.
            loop {
                c.fetch_add(1, Ordering::Relaxed);
                if shutdown.wait(Duration::from_millis(1)) {
                    break;
                }
            }
        })
        .unwrap();
    }
    assert_eq!(sup.tasks(), 4);
    let reports = sup.stop();
    assert_eq!(reports.len(), 4, "every task must be joined and reported");
    assert!(reports.iter().all(|r| r.outcome.is_ok()));
    assert!(
        counter.load(Ordering::Relaxed) > 0,
        "workers must have spun"
    );
}

#[test]
fn panicking_task_is_reported_not_propagated() {
    let sup = Supervisor::new();
    sup.spawn("doomed", |_| panic!("boom {ita}", ita = 42))
        .unwrap();
    sup.spawn("polite", |shutdown| {
        shutdown.wait(Duration::from_millis(50));
    })
    .unwrap();
    let reports = sup.stop();
    let doomed = reports.iter().find(|r| r.name == "doomed").unwrap();
    assert!(doomed.outcome.as_ref().unwrap_err().contains("boom"));
    assert!(reports
        .iter()
        .find(|r| r.name == "polite")
        .unwrap()
        .outcome
        .is_ok());
}

#[test]
fn shutdown_handle_given_to_workers_trips_every_listener() {
    let sup = Supervisor::new();
    let master = sup.shutdown_handle();

    let woke = Arc::new(AtomicUsize::new(0));
    let w = Arc::clone(&woke);
    sup.spawn("listener", move |shutdown| {
        while !shutdown.wait(Duration::from_millis(2)) {}
        assert!(
            shutdown.tripped(),
            "wait() returning true ⇒ flag must read true"
        );
        w.fetch_add(1, Ordering::SeqCst);
    })
    .unwrap();

    // External trip (NOT through stop): the worker must also wake — the
    // stop() path and the signal path are the same broadcast.
    master.signal();
    let reports = sup.stop();
    assert_eq!(woke.load(Ordering::SeqCst), 1);
    assert_eq!(reports.len(), 1);
}

#[test]
fn wait_with_timeout_returns_false_without_shutdown() {
    let sup = Supervisor::new();
    let handle = sup.shutdown_handle();
    assert!(
        !handle.wait(Duration::from_millis(5)),
        "timeout without signal is false"
    );
    let _ = sup.stop();
}

#[test]
fn bounded_channel_drops_and_counts_but_never_blocks() {
    let (tx, rx) = channel::<u32>(2);
    assert!(tx.try_send(1).is_ok());
    assert!(tx.try_send(2).is_ok());
    let returned = tx.try_send(3);
    assert_eq!(returned, Err(3), "overflow must hand the item BACK");
    assert_eq!(rx.recv_timeout(Duration::from_millis(10)).unwrap(), 1);
    assert!(tx.try_send(4).is_ok());
    let stats = tx.stats();
    assert_eq!(stats.sent, 3);
    assert_eq!(stats.dropped, 1);
    assert_eq!(rx.stats().received, 1);
    // FIFO preserved around the drop.
    assert_eq!(rx.try_recv().unwrap(), 2);
    assert_eq!(rx.try_recv().unwrap(), 4);
}

#[test]
fn sequencer_is_monotonic_and_starts_at_one() {
    let seq = Sequencer::new();
    let a = seq.next();
    let b = seq.next();
    assert_eq!(a, 1);
    assert_eq!(b, 2);
    assert_eq!(seq.current(), 2);
}
