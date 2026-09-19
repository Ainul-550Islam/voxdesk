#include "voxdesk/media/spectral_denoise.hpp"

#include <algorithm>
#include <cmath>
#include <complex>

namespace voxdesk::media {

namespace {
constexpr double kTwoPi = 6.283185307179586476925286766559;
}  // namespace

SpectralDenoiser::SpectralDenoiser(std::size_t frame_size,
                                   std::size_t calibrate_frames,
                                   double oversubtraction,
                                   double spectral_floor)
    : frame_size_(frame_size),
      calibrate_frames_(calibrate_frames),
      oversubtraction_(oversubtraction),
      spectral_floor_(spectral_floor),
      fft_(frame_size) {
  // Periodic Hann window (cosine over [0, N)); it satisfies
  // w[n] + w[n + N/2] == 1, which is the constant-overlap-add condition for
  // the 50%-overlap reconstruction.
  window_.resize(frame_size_);
  for (std::size_t i = 0; i < frame_size_; ++i) {
    const double phase =
        kTwoPi * static_cast<double>(i) / static_cast<double>(frame_size_);
    window_[i] = 0.5 * (1.0 - std::cos(phase));
  }
  noise_.assign(frame_size_, 0.0);
}

std::vector<float> SpectralDenoiser::Denoise(const float* samples,
                                             std::size_t count) {
  if (count == 0) {
    noise_.assign(frame_size_, 0.0);
    return {};
  }

  const std::size_t n = frame_size_;
  const std::size_t hop = n / 2;

  // Zero-pad by `hop` samples before the signal so the first real sample gets
  // both of its overlapping-window contributions, and extend the frame run so
  // the last real sample does too. Every output sample then reconstructs from
  // exactly the two Hann windows that sum to 1 (the pass-through test asserts
  // the result is the input, not a half-weighted boundary).
  const std::size_t max_f = (hop + count - 1) / hop;
  const std::size_t frames = max_f + 1;
  const std::size_t padded = (frames - 1) * hop + n;

  std::vector<float> signal(padded, 0.0f);
  std::copy(samples, samples + count, signal.begin() + hop);

  // Overlap-add accumulator across all frames.
  std::vector<double> overlap(padded, 0.0);
  std::vector<std::complex<double>> buf(n);

  noise_.assign(n, 0.0);
  const std::size_t measuring_limit =
      std::min(calibrate_frames_, frames);

  for (std::size_t f = 0; f < frames; ++f) {
    const std::size_t start = f * hop;

    // Analysis: window then transform.
    for (std::size_t i = 0; i < n; ++i) {
      buf[i] = std::complex<double>(signal[start + i] * window_[i], 0.0);
    }
    fft_.Forward(buf.data());

    if (f < measuring_limit) {
      // Calibration frame: record the noise magnitude and pass through.
      for (std::size_t k = 0; k < n; ++k) {
        const double mag = std::abs(buf[k]);
        if (mag > noise_[k]) {
          noise_[k] = mag;
        }
      }
    } else {
      // Denoise frame: subtract the noise floor, keep the phase.
      for (std::size_t k = 0; k < n; ++k) {
        const double mag = std::abs(buf[k]);
        const double noise = noise_[k];
        if (noise <= 0.0 || mag <= 0.0) {
          continue;
        }
        double reduced = mag - oversubtraction_ * noise;
        const double floor = spectral_floor_ * noise;
        if (reduced < floor) {
          reduced = floor;
        }
        buf[k] *= reduced / mag;
      }
    }

    // Synthesis: invert and overlap-add.
    fft_.Inverse(buf.data());
    for (std::size_t i = 0; i < n; ++i) {
      overlap[start + i] += buf[i].real();
    }
  }

  std::vector<float> out(count);
  for (std::size_t i = 0; i < count; ++i) {
    out[i] = static_cast<float>(overlap[hop + i]);
  }
  return out;
}

}  // namespace voxdesk::media
