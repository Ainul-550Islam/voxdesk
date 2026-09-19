#include "voxdesk/media/vad.hpp"

#include <algorithm>
#include <cmath>

namespace voxdesk::media {

Vad::Vad(double threshold_db, std::size_t hangover_frames,
         std::size_t window_frames)
    : threshold_db_(threshold_db),
      hangover_frames_(hangover_frames),
      window_size_(window_frames > 0 ? window_frames : 1),
      window_(window_size_, 0.0) {}

bool Vad::ProcessFrame(const float* frame, std::size_t count) {
  double energy = 0.0;
  for (std::size_t i = 0; i < count; ++i) {
    energy += static_cast<double>(frame[i]) * static_cast<double>(frame[i]);
  }

  // Minimum-statistics noise floor: keep a ring of the last window_size_
  // energies and take the minimum, so the floor tracks the quietest recent
  // stretch. The ring rolls over old values, letting the floor rise when the
  // room genuinely gets louder.
  if (filled_ < window_size_) {
    window_[filled_] = energy;
    ++filled_;
  } else {
    window_[pos_] = energy;
    pos_ = (pos_ + 1) % window_size_;
  }

  double floor = energy;
  for (std::size_t i = 0; i < filled_; ++i) {
    floor = std::min(floor, window_[i]);
  }
  noise_energy_ = floor;

  last_snr_db_ =
      10.0 * std::log10(energy / (floor > 0.0 ? floor : 1e-12));

  const bool voiced = last_snr_db_ >= threshold_db_;
  if (voiced) {
    remaining_hangover_ = hangover_frames_;
    return true;
  }
  if (remaining_hangover_ > 0) {
    --remaining_hangover_;
    return true;
  }
  return false;
}

}  // namespace voxdesk::media
