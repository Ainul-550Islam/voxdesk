//! Sequence-number arithmetic for 16-bit RTP sequence spaces, per
//! RFC 3550 Appendix A: cycle tracking (ROLLOVER), extended sequence
//! numbers, and reorder distance — all of it correct across the 65535→0
//! wrap, which is where naive comparisons in RTP code go to die.

/// One cycle of the 16-bit sequence space.
pub const CYCLE: u32 = 1 << 16;

/// Signed wrapped distance from a to b in sequence space: positive when b
/// is AHEAD of a (newer), negative when b lags. Half the space is "ahead"
/// by definition (RFC 3550's rule); exact half is declared behind
/// (RFC 3550 Appendix A's tie-break choice).
pub fn forward_distance(a: u16, b: u16) -> i32 {
    let d = (b as i32) - (a as i32);
    // Map into (-32768, 32767].
    ((d + 32768).rem_euclid(65536)) - 32768
}

/// True when b is ahead of a in sequence space.
pub fn is_newer(a: u16, b: u16) -> bool {
    forward_distance(a, b) > 0
}

/// Extended-sequence tracker: converts 16-bit on-wire sequence numbers
/// into 32-bit "roc-extended" numbers that increase monotonically across
/// wraps. Needed by SRTP (the rollover counter is part of the packet IV)
/// and by loss statistics alike.
#[derive(Clone, Debug)]
pub struct SeqTracker {
    roc: u32,          // rollover counter: how many wraps seen
    max_seq: u16,      // highest seq accepted so far (in current cycle)
    cycles_full: bool, // seen at least one full cycle (init validation per RFC 3711 §3.3.1)
    initialized: bool,
}

impl SeqTracker {
    pub fn new() -> SeqTracker {
        SeqTracker {
            roc: 0,
            max_seq: 0,
            cycles_full: false,
            initialized: false,
        }
    }

    /// Feed one observed sequence number; returns the extended value.
    /// Implements RFC 3711 Appendix A's index estimation EXACTLY:
    ///
    /// ```text
    /// if (s_l < 32768) { v = (seq - s_l > 32768) ? roc - 1 : roc }
    /// else             { v = (s_l - 32768 > seq) ? roc + 1 : roc }
    /// ```
    ///
    /// i.e. a wrap is only BELIEVED when the new value crosses the
    /// half-way point convincingly — small reorderings around the wrap
    /// neither bump nor decrement the rollover counter.
    pub fn extend(&mut self, seq: u16) -> u32 {
        if !self.initialized {
            self.initialized = true;
            self.max_seq = seq;
            return seq as u32;
        }
        let s_l = self.max_seq as i32;
        let s = seq as i32;
        let v = if self.max_seq < 32768 {
            if s - s_l > 32768 {
                self.roc.saturating_sub(1)
            } else {
                self.roc
            }
        } else if s_l - 32768 > s {
            self.roc + 1
        } else {
            self.roc
        };
        if forward_distance(self.max_seq, seq) > 0 {
            self.max_seq = seq;
            self.roc = v;
            if v > 0 {
                self.cycles_full = true;
            }
        }
        v * CYCLE + seq as u32
    }

    pub fn rollover_count(&self) -> u32 {
        self.roc
    }

    pub fn highest(&self) -> u16 {
        self.max_seq
    }
}

impl Default for SeqTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Running loss/duplicate accounting for one media stream (the receiver
/// side's view, exported into RTCP receiver reports).
///
/// Semantics, per RFC 3550 §6.4.1: expected = ext_highest - ext_initial +
/// 1, cumulative_lost = expected - received (reorders do NOT count as
/// loss — the slot is expected either way — but TRUE duplicates are
/// filtered first, else a duplicate inflates "received" past "expected").
#[derive(Clone, Debug, Default)]
pub struct LossStats {
    tracker: Option<SeqTracker>,
    initial_ext: Option<u32>,
    highest_ext: u32,
    received: u32,
    duplicated: u32,
    window: crate::replay::ReplayWindow,
}

impl LossStats {
    pub fn new() -> LossStats {
        LossStats::default()
    }

    pub fn record(&mut self, seq: u16) {
        let ext = match &mut self.tracker {
            None => {
                let mut t = SeqTracker::new();
                let ext = t.extend(seq);
                self.tracker = Some(t);
                ext
            }
            Some(t) => t.extend(seq),
        };
        if self.initial_ext.is_none() {
            self.initial_ext = Some(ext);
            self.highest_ext = ext;
        }
        // Duplicate or ancient: count it separately, do NOT re-count
        // receipt (the window's set bit is set by the first sighting).
        if self.window.seen(u64::from(ext))
            || self.window.check_and_set(u64::from(ext)) == crate::replay::Verdict::Replay
        {
            self.duplicated += 1;
            return;
        }
        if ext > self.highest_ext {
            self.highest_ext = ext;
        }
        self.received += 1;
    }

    pub fn expected(&self) -> u32 {
        match self.initial_ext {
            None => 0,
            Some(first) => self.highest_ext - first + 1,
        }
    }

    pub fn received(&self) -> u32 {
        self.received
    }

    pub fn duplicated(&self) -> u32 {
        self.duplicated
    }

    pub fn cumulative_lost(&self) -> u32 {
        self.expected().saturating_sub(self.received)
    }

    /// Fraction of packets lost in 8.8 fixed point (RTCP RR field shape),
    /// cumulative over the stream: lost/expected clamped to 255.
    pub fn fraction_lost8(&self) -> u8 {
        let expected = self.expected();
        if expected == 0 {
            return 0;
        }
        (((self.cumulative_lost() as u64) * 256) / expected as u64).min(255) as u8
    }

    pub fn highest_extended(&self) -> u32 {
        self.highest_ext
    }
}
