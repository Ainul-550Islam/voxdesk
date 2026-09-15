#include "voxdesk/media/jitter_buffer.hpp"

#include <map>
#include <utility>

namespace voxdesk::media {

namespace {

// RTP 16-bit wrap-aware ordering: a is "before" b iff the signed 16-bit
// interpretation of (a - b) is negative. This treats 65534, 65535, 0, 1 as a
// contiguous ascending run, which plain unsigned comparison does not.
inline bool SeqLess(std::uint16_t a, std::uint16_t b) {
  return static_cast<std::int16_t>(static_cast<std::uint16_t>(a - b)) < 0;
}

}  // namespace

struct JitterBuffer::Impl {
  // Ordered by sequence number (wrapped). A std::map keeps the "oldest
  // buffered frame" operation O(1) and is far less error-prone than a
  // hand-rolled ring; a media plane under real load can swap this for a
  // preallocated ring without changing the public contract.
  std::map<std::uint16_t, AudioFrame> frames;
};

JitterBuffer::JitterBuffer(std::size_t max_frames, std::uint32_t reorder_window,
                           std::uint16_t initial_seq)
    : impl_(std::make_unique<Impl>()),
      max_frames_(max_frames),
      reorder_window_(reorder_window),
      expected_seq_(initial_seq) {}

JitterBuffer::~JitterBuffer() = default;
JitterBuffer::JitterBuffer(JitterBuffer&&) noexcept = default;
JitterBuffer& JitterBuffer::operator=(JitterBuffer&&) noexcept = default;

InsertResult JitterBuffer::Insert(const AudioFrame& frame) {
  if (SeqLess(frame.seq, expected_seq_)) {
    ++late_dropped_;
    return InsertResult::Late;
  }
  if (impl_->frames.find(frame.seq) != impl_->frames.end()) {
    ++duplicate_dropped_;
    return InsertResult::Duplicate;
  }
  if (impl_->frames.size() >= max_frames_) {
    ++full_dropped_;
    return InsertResult::DroppedFull;
  }
  impl_->frames.emplace(frame.seq, frame);
  return InsertResult::Buffered;
}

PopResult JitterBuffer::Pop(AudioFrame* out) {
  const auto it = impl_->frames.find(expected_seq_);
  if (it != impl_->frames.end()) {
    if (out != nullptr) {
      *out = std::move(it->second);
    }
    impl_->frames.erase(it);
    ++expected_seq_;
    return PopResult::Ok;
  }
  if (impl_->frames.empty()) {
    return PopResult::Underflow;
  }
  const std::uint16_t oldest = impl_->frames.begin()->first;
  const std::uint16_t gap =
      static_cast<std::uint16_t>(oldest - expected_seq_);
  if (gap > reorder_window_) {
    // The expected frame is gone and the oldest buffered frame is beyond the
    // reorder window: declare the missing frames lost, advance playback to the
    // oldest buffered frame, and let the caller conceal one frame.
    expected_seq_ = oldest;
    ++overflow_declared_;
    return PopResult::Overflow;
  }
  return PopResult::Underflow;
}

std::size_t JitterBuffer::Size() const { return impl_->frames.size(); }

}  // namespace voxdesk::media
