#include "voxdesk/media/fft.hpp"

#include <cmath>
#include <stdexcept>
#include <utility>

namespace voxdesk::media {

namespace {

constexpr double kTwoPi = 6.283185307179586476925286766559;

bool IsPowerOfTwo(std::size_t n) { return n >= 2 && (n & (n - 1)) == 0; }

}  // namespace

Fft::Fft(std::size_t size) : size_(size) {
  if (!IsPowerOfTwo(size)) {
    throw std::invalid_argument("FFT size must be a power of two >= 2");
  }

  std::size_t log2n = 0;
  while ((std::size_t{1} << log2n) < size) {
    ++log2n;
  }

  bitrev_.resize(size);
  for (std::size_t i = 0; i < size; ++i) {
    std::size_t rev = 0;
    for (std::size_t bit = 0; bit < log2n; ++bit) {
      if ((i & (std::size_t{1} << bit)) != 0) {
        rev |= std::size_t{1} << (log2n - 1 - bit);
      }
    }
    bitrev_[i] = rev;
  }

  twiddles_.resize(size / 2);
  for (std::size_t k = 0; k < size / 2; ++k) {
    const double angle =
        -kTwoPi * static_cast<double>(k) / static_cast<double>(size);
    twiddles_[k] = {std::cos(angle), std::sin(angle)};
  }
}

void Fft::Forward(std::complex<double>* data) const {
  // Bit-reversal permutation; each pair is swapped exactly once.
  for (std::size_t i = 0; i < size_; ++i) {
    const std::size_t j = bitrev_[i];
    if (i < j) {
      std::swap(data[i], data[j]);
    }
  }

  // Decimation-in-time butterflies. Twiddle w for stage `len` at offset `j` is
  // twiddles_[j * (size_ / len)] == exp(-2 pi i j / len).
  for (std::size_t len = 2; len <= size_; len <<= 1) {
    const std::size_t half = len >> 1;
    const std::size_t step = size_ / len;
    for (std::size_t i = 0; i < size_; i += len) {
      for (std::size_t j = 0; j < half; ++j) {
        const std::complex<double> w = twiddles_[j * step];
        const std::complex<double> u = data[i + j];
        const std::complex<double> v = data[i + j + half] * w;
        data[i + j] = u + v;
        data[i + j + half] = u - v;
      }
    }
  }
}

void Fft::Inverse(std::complex<double>* data) const {
  // IFFT(x) = conj(FFT(conj(x))) / N, which reuses the forward twiddles and
  // keeps the inverse exactly the adjoint of the forward transform.
  for (std::size_t i = 0; i < size_; ++i) {
    data[i] = std::conj(data[i]);
  }
  Forward(data);
  const double scale = 1.0 / static_cast<double>(size_);
  for (std::size_t i = 0; i < size_; ++i) {
    data[i] = std::conj(data[i]) * scale;
  }
}

}  // namespace voxdesk::media
