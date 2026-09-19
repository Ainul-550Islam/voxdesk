//! audio — the codecs and mix math of the PSTN-facing leg: G.711 in both
//! laws at the companding boundary, a saturating sum mixer with an
//! activity-adapted divisor, per-frame level metering (the feed for
//! active-speaker selection), and packet-loss concealment for the holes a
//! jitter buffer can't fill.

pub mod g711;
pub mod mixer;

pub use g711::{alaw_decode, alaw_encode, ulaw_decode, ulaw_encode};
pub use mixer::{Frame, LevelMeter, Mixer};
