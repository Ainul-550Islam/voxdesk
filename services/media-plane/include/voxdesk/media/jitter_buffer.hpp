#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace voxdesk::media {

// One audio frame: 20 ms of encoded audio (Opus today; the buffer itself is
// codec-agnostic). `seq` is the RTP sequence number (16-bit) and is the
// ordering authority; `timestamp` is the RTP media timestamp carried along for
// the downstream codec layer.
struct AudioFrame {
  std::uint16_t seq = 0;
  std::uint32_t timestamp = 0;
  std::vector<std::uint8_t> payload;
  // Monotonic wall-clock at arrival, in milliseconds. Reserved for jitter
  // statistics; never used for ordering.
  std::int64_t arrival_ms = 0;
};

enum class InsertResult {
  Buffered,    // accepted, waiting to be popped in order
  Duplicate,   // same sequence number already buffered; dropped
  Late,        // older than the next expected frame; cannot be reordered
  DroppedFull, // buffer at capacity; newest frame dropped
};

enum class PopResult {
  Ok,        // a frame was written to `out`
  Underflow, // next expected frame has not arrived yet
  Overflow,  // the expected frame is declared lost; caller should conceal one
};

// A bounded, sequence-ordered de-jitter buffer for a single audio stream.
//
// Semantics:
//   * Insert() accepts frames up to `reorder_window` ahead of the next
//     expected sequence number; anything older is Late, anything already seen
//     is Duplicate, and a full buffer drops the newest arrival.
//   * Pop() returns the next contiguous frame. If that frame has not arrived
//     but a later frame sits beyond the reorder window, the missing frame(s)
//     are declared lost (Overflow) and playback advances to the oldest
//     buffered frame, so one lost packet never stalls the stream forever.
//   * Sequence comparison is RTP 16-bit wrap aware, so the wrap from 65535 to
//     0 is handled correctly.
//
// This class is deliberately single-threaded per stream (one owner). A media
// plane hands each stream its own buffer behind a lock/queue; baking a mutex
// in here would hide the ownership question rather than answer it.
//
// The playout base is the caller's responsibility: `initial_seq` must be the
// stream's negotiated RTP sequence (from SDP/PLI/RTCP). The buffer never
// guesses a starting sequence, so a caller that omits `initial_seq` is
// declaring the stream begins at sequence 0.
class JitterBuffer {
 public:
  // `max_frames` bounds memory; `reorder_window` is how many frames ahead of
  // the expected sequence number the buffer will hold before declaring the
  // expected frame lost. `initial_seq` seeds the expected sequence number so a
  // leg that joins a stream mid-call starts from the negotiated sequence.
  explicit JitterBuffer(std::size_t max_frames, std::uint32_t reorder_window,
                        std::uint16_t initial_seq = 0);

  ~JitterBuffer();
  JitterBuffer(const JitterBuffer&) = delete;
  JitterBuffer& operator=(const JitterBuffer&) = delete;
  JitterBuffer(JitterBuffer&&) noexcept;
  JitterBuffer& operator=(JitterBuffer&&) noexcept;

  InsertResult Insert(const AudioFrame& frame);
  PopResult Pop(AudioFrame* out);

  std::size_t Size() const;
  std::size_t max_frames() const { return max_frames_; }
  std::uint32_t reorder_window() const { return reorder_window_; }
  std::uint16_t expected_seq() const { return expected_seq_; }

  // Observability counters — monotonic, never reset, safe to export as metrics.
  std::uint64_t late_dropped() const { return late_dropped_; }
  std::uint64_t duplicate_dropped() const { return duplicate_dropped_; }
  std::uint64_t full_dropped() const { return full_dropped_; }
  std::uint64_t overflow_declared() const { return overflow_declared_; }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::size_t max_frames_;
  std::uint32_t reorder_window_;
  std::uint16_t expected_seq_;
  std::uint64_t late_dropped_ = 0;
  std::uint64_t duplicate_dropped_ = 0;
  std::uint64_t full_dropped_ = 0;
  std::uint64_t overflow_declared_ = 0;
};

}  // namespace voxdesk::media
