//! Mixing in the linear-PCM domain: N contribution streams → one output
//! stream, with per-frame level metering and packet-loss concealment.
//!
//! The engine mixes exactly ONE thing well: telephone classroom audio —
//! a room where at most a few participants should be heard at once. The
//! mixer's structural answers, kept deliberately boring:
//!
//! * sum with saturation (i16 clip, not wrap — a wrap is a speaker-pop
//!   audible forty desks away);
//! * soft normalization: divide by a slow follower of the ACTIVE stream
//!   count so quiet rooms aren't dampened and loud rooms can't clip-crawl;
//! * PLC is a fade-to-silence on gaps (no interpolation voodoo that would
//!   have to be validated per codec; holes up to ~60 ms fade inaudibly).

/// One linear-PCM frame: mono, 8 kHz or 48 kHz, 20 ms by convention
/// (G.711 telephone = 160 samples @ 8 kHz; opus-side mixes use 960).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub samples: Vec<i16>, // mono interleave-free
    pub sample_rate: u32,
}

impl Frame {
    pub fn silence(len: usize, sample_rate: u32) -> Frame {
        Frame {
            samples: vec![0; len],
            sample_rate,
        }
    }

    pub fn duration_ms(&self) -> f64 {
        (self.samples.len() as f64) / (self.sample_rate as f64 / 1000.0)
    }
}

/// Peak/RMS level meter over a sliding set of frames — the feed for the
/// active-speaker decision in the media crate. Levels are per-frame, so a
/// talker's RECENT energy decays when a burst of frames stops arriving.
#[derive(Clone, Debug, Default)]
pub struct LevelMeter {
    peak: i32,
    energy: f64, // running RMS² over the window the caller folds in
    frames: u32,
}

impl LevelMeter {
    pub fn new() -> LevelMeter {
        LevelMeter::default()
    }

    pub fn add(&mut self, frame: &Frame) {
        if frame.samples.is_empty() {
            return;
        }
        let mut peak = 0i32;
        let mut sumsq = 0f64;
        for &s in &frame.samples {
            let v = i32::from(s).abs();
            if v > peak {
                peak = v;
            }
            let x = f64::from(s);
            sumsq += x * x;
        }
        self.peak = self.peak.max(peak);
        self.energy += sumsq / frame.samples.len() as f64;
        self.frames += 1;
    }

    /// RMS level over all folded frames, in PCM units (0..32767).
    pub fn rms(&self) -> f64 {
        if self.frames == 0 {
            0.0
        } else {
            (self.energy / self.frames as f64).sqrt()
        }
    }

    /// Decibels below full scale (dBFS). Silence → -inf as -96.
    pub fn dbfs(&self) -> f64 {
        let rms = self.rms();
        if rms <= 0.0 {
            -96.0
        } else {
            20.0 * (rms / 32768.0).max(1e-10).log10()
        }
    }

    pub fn peak(&self) -> i32 {
        self.peak
    }

    /// Reset the window (the caller drives window boundaries).
    pub fn reset(&mut self) {
        *self = LevelMeter::default();
    }
}

/// The summing mixer.
#[derive(Clone, Debug)]
pub struct Mixer {
    /// Slow follower of recent active-stream count (EWMA), the divisor
    /// that keeps loud rooms out of saturation. Minimum 1.0.
    activity: f64,
}

impl Mixer {
    pub fn new() -> Mixer {
        Mixer { activity: 1.0 }
    }

    /// Mix one output frame from however many contributors have a frame
    /// THIS tick (absent contributors contribute silence — buffer timing
    /// is the caller's, see streams::ReorderBuffer). Frames must share
    /// the output's sample rate and length; short frames are zero-padded,
    /// which never happens on a well-formed pipeline and fades a hole the
    /// same way silence would.
    pub fn mix(
        &mut self,
        contributions: &[Option<Frame>],
        frame_len: usize,
        sample_rate: u32,
    ) -> Frame {
        let active = contributions.iter().filter(|c| c.is_some()).count();
        // EWMA toward the current count: ~0.1 aggression keeps the divisor
        // from dancing per-tick while still catching a handover in ~300 ms.
        self.activity += 0.1 * ((active.max(1) as f64) - self.activity);
        let divisor = self.activity.max(1.0);

        let mut out = Frame::silence(frame_len, sample_rate);
        for slot in contributions {
            let Some(frame) = slot else { continue };
            for (i, dst) in out.samples.iter_mut().enumerate() {
                let s = frame.samples.get(i).copied().unwrap_or(0);
                let mixed = (f64::from(*dst) + f64::from(s) / divisor).round();
                *dst = mixed.clamp(f64::from(i16::MIN), f64::from(i16::MAX)) as i16;
            }
        }
        out
    }

    /// Packet-loss concealment for one missing contributor: the caller
    /// feeds `None` for ~3 ticks, and instead of instant silence (a
    /// dropout click) this produces a decaying echo of the LAST REAL
    /// frame — ear-tricking the hole down to the noise floor in ~60 ms
    /// at 20 ms frames.
    pub fn conceal(last_frame: &Frame, step: u32) -> Frame {
        let decay = match step {
            0 => 0.6,
            1 => 0.35,
            2 => 0.15,
            _ => 0.0,
        };
        Frame {
            samples: last_frame
                .samples
                .iter()
                .map(|&s| (f64::from(s) * decay).round() as i16)
                .collect(),
            sample_rate: last_frame.sample_rate,
        }
    }
}

impl Default for Mixer {
    fn default() -> Self {
        Self::new()
    }
}
