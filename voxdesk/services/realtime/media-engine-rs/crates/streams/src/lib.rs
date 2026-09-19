//! streams — everything between "a datagram arrived" and "media frames in
//! sequence": the RTP packet model, sequence-space arithmetic, the
//! anti-replay window, jitter estimation + reorder buffering, and RTCP.
//!
//! Layer rule: this crate knows bytes and sequence numbers, NEVER sockets
//! (transport's job), participants (sessions'), or codecs (audio/media).

pub mod jitter;
pub mod packet;
pub mod replay;
pub mod rtcp;
pub mod seq;

pub use jitter::{JitterEstimator, ReorderBuffer};
pub use packet::{looks_like_rtp, RtpPacket};
pub use replay::{ReplayWindow, Verdict};
pub use rtcp::{build_receiver_report, parse as parse_rtcp, ReportBlock, Rtcp};
pub use seq::{forward_distance, is_newer, LossStats, SeqTracker, CYCLE};
