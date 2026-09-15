#pragma once

#include <complex>
#include <cstddef>
#include <vector>

namespace voxdesk::media {

// Iterative radix-2 complex FFT for power-of-two sizes.
//
// The bit-reversal permutation and the forward twiddle factors are computed
// once in the constructor, so repeated transforms of the same size do not
// allocate. `Forward` and `Inverse` are exact inverses (`Inverse` includes the
// 1/N scaling), which the round-trip and linearity tests assert.
//
// This is the frequency-domain primitive the spectral-subtraction denoiser is
// built on. It is std-only and deterministic, like the rest of the media
// plane.
class Fft {
 public:
  // `size` must be a power of two >= 2.
  explicit Fft(std::size_t size);

  // In-place transforms. `data` must point to `size()` elements.
  void Forward(std::complex<double>* data) const;
  void Inverse(std::complex<double>* data) const;

  std::size_t size() const { return size_; }

 private:
  std::size_t size_;
  std::vector<std::size_t> bitrev_;
  std::vector<std::complex<double>> twiddles_;
};

}  // namespace voxdesk::media
