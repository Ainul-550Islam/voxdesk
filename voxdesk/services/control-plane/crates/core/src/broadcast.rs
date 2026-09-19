//! Bounded fan-out with backpressure-drop.
//!
//! One producer, many consumers. Publishing is non-blocking: a consumer whose
//! buffer is full silently misses the message (backpressure-drop), and a
//! consumer whose receiver has been dropped is pruned. This is the delivery
//! semantic a signaling fan-out needs — a slow client must never be able to
//! stall the room.

use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone)]
pub struct Subscriber<T: Clone> {
    rx: Arc<Mutex<Receiver<T>>>,
}

impl<T: Clone> Subscriber<T> {
    pub fn try_recv(&self) -> Option<T> {
        self.rx.lock().unwrap().try_recv().ok()
    }

    pub fn recv(&self) -> Result<T, std::sync::mpsc::RecvError> {
        self.rx.lock().unwrap().recv()
    }
}

#[derive(Debug)]
pub struct BoundedBroadcast<T: Clone> {
    capacity: usize,
    senders: Mutex<Vec<SyncSender<T>>>,
}

impl<T: Clone> BoundedBroadcast<T> {
    pub fn new(capacity: usize) -> Self {
        assert!(capacity > 0, "capacity must be positive");
        BoundedBroadcast {
            capacity,
            senders: Mutex::new(Vec::new()),
        }
    }

    pub fn subscribe(&self) -> Subscriber<T> {
        let (tx, rx) = sync_channel(self.capacity);
        self.senders.lock().unwrap().push(tx);
        Subscriber {
            rx: Arc::new(Mutex::new(rx)),
        }
    }

    /// Deliver one item to every live subscriber without blocking. Returns the
    /// number of subscribers that actually received the item. Subscribers
    /// whose buffer is full miss this item; subscribers whose receiver has
    /// been dropped are pruned.
    pub fn publish(&self, item: &T) -> usize {
        let mut senders = self.senders.lock().unwrap();
        let mut delivered = 0;
        senders.retain(|tx| match tx.try_send(item.clone()) {
            Ok(()) => {
                delivered += 1;
                true
            }
            Err(TrySendError::Full(_)) => true, // alive but slow: drop this copy
            Err(TrySendError::Disconnected(_)) => false, // receiver gone: prune
        });
        delivered
    }

    pub fn subscriber_count(&self) -> usize {
        self.senders.lock().unwrap().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn broadcast_reaches_all_subscribers() {
        let bus: BoundedBroadcast<i32> = BoundedBroadcast::new(4);
        let a = bus.subscribe();
        let b = bus.subscribe();
        assert_eq!(bus.publish(&7), 2);
        assert_eq!(a.try_recv(), Some(7));
        assert_eq!(b.try_recv(), Some(7));
    }

    #[test]
    fn slow_subscriber_misses_messages_but_stays() {
        let bus: BoundedBroadcast<i32> = BoundedBroadcast::new(1);
        let fast = bus.subscribe();
        let slow = bus.subscribe();
        // First message fills both buffers.
        assert_eq!(bus.publish(&1), 2);
        // The fast subscriber drains; the slow one does not.
        assert_eq!(fast.try_recv(), Some(1));
        // Second message: fast's buffer is empty so it receives; slow's buffer
        // is full so it misses — but stays subscribed.
        assert_eq!(bus.publish(&2), 1);
        assert_eq!(slow.try_recv(), Some(1));
        assert_eq!(slow.try_recv(), None);
        assert_eq!(fast.try_recv(), Some(2));
        assert_eq!(bus.subscriber_count(), 2);
    }

    #[test]
    fn dropped_subscriber_is_pruned() {
        let bus: BoundedBroadcast<i32> = BoundedBroadcast::new(4);
        let a = bus.subscribe();
        drop(a);
        // The disconnected sender is removed during publish.
        assert_eq!(bus.publish(&1), 0);
        assert_eq!(bus.subscriber_count(), 0);
    }
}
