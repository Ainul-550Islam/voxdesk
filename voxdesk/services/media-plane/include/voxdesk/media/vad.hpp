#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace voxdesk::media {

// Energy-based voice-activity detector over mono PCM float frames in [-1, 1].
//
// The noise floor is the minimum frame energy over a sliding window
// (minimum-statistics tracking): it adapts downward within one frame and
// upward within `window_frames` frames once the old quiet frames fall out of
// the window, so a room that gets louder stops being classified as speech.
// A frame is voiced when its energy exceeds the floor by `threshold_db`. A
// hangover of `hangover_frames` keeps the decision on after the last voiced
// frame, so word-final unvoiced tails and inter-word gaps are not clipped.
//
// The detector is the cheap, deterministic complement to the spectral denoiser:
// the same noise-floor concept decides *whether* a frame carries voice, and the
// denoiser decides *what* to keep. The caller must feed a consistent frame
// length (e.g. 160 samples = 20 ms @ 8 kHz), since the floor is a sum of
// squares and the decision compares energies on a log scale.
class Vad {
 public:
  // `threshold_db` is the speech decision margin above the noise floor;
  // `hangover_frames` extends each voiced run (5 is a typical start);
  // `window_frames` is the noise-floor memory (~100 = 2 s at 50 fps).
  explicit Vad(double threshold_db = 9.0, std::size_t hangover_frames = 5,
               std::size_t window_frames = 100);

  // Returns true when the most recent `count` samples are voiced.
  bool ProcessFrame(const float* frame, std::size_t count);

  double threshold_db() const { return threshold_db_; }
  std::size_t hangover_frames() const { return hangover_frames_; }
  std::size_t window_frames() const { return window_size_; }

  // Observability: the current noise-floor energy estimate (sum of squares),
  // and the last frame's energy in dB above that floor. Safe to export.
  double noise_energy() const { return noise_energy_; }
  double last_snr_db() const { return last_snr_db_; }

 private:
  double threshold_db_;
  std::size_t hangover_frames_;
  std::size_t remaining_hangover_ = 0;
  std::size_t window_size_;
  std::vector<double> window_;  // ring of recent frame energies
  std::size_t pos_ = 0;
  std::size_t filled_ = 0;
  double noise_energy_ = 0.0;  // 0 == not yet estimated
  double last_snr_db_ = 0.0;
};

}  // namespace voxdesk::media
