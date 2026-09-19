//! Anti-replay and duplicate detection over EXTENDED sequence numbers via
//! the classic 64-bit sliding window (the SRTP replay list's exact shape,
//! RFC 3711 §3.3.2): a bitset of the newest 64 extended indices. Accepting
//! marks the bit; a set bit or an index below the window is a replay.
//!
//! Two callers share it: the SRTP layer (a replayed datagram is an
//! ATTACK, not a loss event) and LossStats (a duplicate is neither loss
//! nor a fresh receipt).

/// The window's width in bits — RFC 3711's minimum for SRTP.
pub const WINDOW: u64 = 64;

#[derive(Clone, Debug)]
pub struct ReplayWindow {
    highest: u64, // highest extended index accepted (window anchor)
    bits: u64,    // bit i = (highest - i) has been seen
    initialized: bool,
}

/// What the window says about an index.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// First sighting; the bit has been set. Proceed.
    Fresh,
    /// Index already seen (bit set) or older than the window.
    Replay,
}

impl ReplayWindow {
    pub fn new() -> ReplayWindow {
        ReplayWindow {
            highest: 0,
            bits: 0,
            initialized: false,
        }
    }

    /// Check AND record one extended index.
    pub fn check_and_set(&mut self, index: u64) -> Verdict {
        if !self.initialized {
            self.initialized = true;
            self.highest = index;
            self.bits = 1;
            return Verdict::Fresh;
        }
        if index > self.highest {
            let shift = index - self.highest;
            if shift >= WINDOW {
                self.bits = 1;
            } else {
                self.bits = (self.bits << shift) | 1;
            }
            self.highest = index;
            return Verdict::Fresh;
        }
        let delta = self.highest - index;
        if delta >= WINDOW {
            return Verdict::Replay; // older than the window entirely
        }
        let mask = 1u64 << delta;
        if self.bits & mask != 0 {
            return Verdict::Replay;
        }
        self.bits |= mask;
        Verdict::Fresh
    }

    /// Read-only check for LossStats' duplicate probe.
    pub fn seen(&self, index: u64) -> bool {
        if !self.initialized || index > self.highest {
            return false;
        }
        let delta = self.highest - index;
        delta < WINDOW && (self.bits & (1u64 << delta)) != 0
    }
}

impl Default for ReplayWindow {
    fn default() -> Self {
        Self::new()
    }
}
