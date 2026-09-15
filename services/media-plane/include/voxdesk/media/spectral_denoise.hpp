#pragma once

#include <cstddef>
#include <vector>

#include "voxdesk/media/fft.hpp"

namespace voxdesk::media {

// Spectral-subtraction denoiser for mono PCM float samples in [-1, 1].
//
// The signal is cut into `frame_size` (power of two) Hann-windowed frames with
// 50% overlap, transformed, and — after a calibration period that measures the
// per-bin noise magnitude — each bin's magnitude is reduced by
// `oversubtraction * noise`, floored at `spectral_floor * noise`, with the
// phase left untouched. Reconstruction is overlap-add; the Hann window
// satisfies the constant-overlap-add condition, so a clean signal passes
// through unchanged (the pass-through test asserts this at ~1e-4).
//
// Calibration: the first `calibrate_frames` frames are measured as noise and
// passed through unmodified. The noise estimate is the per-bin maximum
// magnitude seen in that window, which deliberately overestimates the noise
// and so biases the gate toward suppression rather than leakage. With
// `calibrate_frames == 0` there is no noise estimate, every gain is 1, and the
// denoiser is an exact identity (modulo float rounding) — that path doubles as
// the reconstruction test.
//
// This is a batch processor: `Denoise` consumes the whole buffer at once and
// zero-pads it internally to a frame boundary. A streaming wrapper (with the
// inherent half-frame latency) is a later increment; the frame-level internals
// are already structured for it.
class SpectralDenoiser {
 public:
  // `frame_size` must be a power of two >= 2. `calibrate_frames` is the number
  // of leading frames treated as pure noise (0 disables the noise estimate).
  SpectralDenoiser(std::size_t frame_size, std::size_t calibrate_frames,
                   double oversubtraction = 2.0, double spectral_floor = 0.02);

  // Denoises `count` mono samples and returns the denoised signal, exactly
  // `count` samples long. Calibrates on this call's leading frames (see class
  // comment). Not thread-safe; one denoiser per stream.
  std::vector<float> Denoise(const float* samples, std::size_t count);

  std::size_t frame_size() const { return frame_size_; }
  std::size_t calibrate_frames() const { return calibrate_frames_; }
  double oversubtraction() const { return oversubtraction_; }
  double spectral_floor() const { return spectral_floor_; }

  // Observability: the per-bin noise magnitude estimate after the last
  // Denoise() call, for exporting as a metric or reusing across calls.
  const std::vector<double>& noise_estimate() const { return noise_; }

 private:
  std::size_t frame_size_;
  std::size_t calibrate_frames_;
  double oversubtraction_;
  double spectral_floor_;

  Fft fft_;
  std::vector<double> window_;  // Hann analysis window, length frame_size_
  std::vector<double> noise_;   // per-bin noise magnitude from last Denoise()
};

}  // namespace voxdesk::media
