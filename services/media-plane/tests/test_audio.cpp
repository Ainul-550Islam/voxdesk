// Self-contained unit tests for the audio/video processing modules (Phase 3,
// increment 2): FFT, spectral denoiser, G.711 codec, and the energy VAD.
// Same ground rules as test_main.cpp — no external framework, a tiny CHECK
// macro, non-zero exit on failure.

#include "voxdesk/media/fft.hpp"
#include "voxdesk/media/g711.hpp"
#include "voxdesk/media/spectral_denoise.hpp"
#include "voxdesk/media/vad.hpp"

#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdint>
#include <vector>

namespace {

int g_checks = 0;
int g_failures = 0;

void Report(bool ok, const char* file, int line, const char* expr) {
  ++g_checks;
  if (!ok) {
    ++g_failures;
    std::printf("FAIL %s:%d: %s\n", file, line, expr);
  }
}

}  // namespace

#define CHECK(expr) Report((expr), __FILE__, __LINE__, #expr)

namespace {

constexpr double kTwoPi = 6.283185307179586476925286766559;
constexpr double kEps = 1e-9;
constexpr double kLoose = 1e-3;

// ---------------------------------------------------------------------------
// FFT
// ---------------------------------------------------------------------------

void TestFftImpulseIsFlat() {
  voxdesk::media::Fft fft(16);
  std::vector<std::complex<double>> x(16, {0.0, 0.0});
  x[0] = {1.0, 0.0};
  fft.Forward(x.data());
  for (const auto& v : x) {
    CHECK(std::abs(v - std::complex<double>(1.0, 0.0)) < kEps);
  }
}

void TestFftRoundTrip() {
  voxdesk::media::Fft fft(64);
  std::vector<std::complex<double>> x(64);
  std::uint64_t state = 0x9e3779b97f4a7c15ULL;
  for (auto& v : x) {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    const double a = static_cast<double>((state >> 33) & 0x7FFFFFFF) /
                     2147483648.0 - 0.5;
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    const double b = static_cast<double>((state >> 33) & 0x7FFFFFFF) /
                     2147483648.0 - 0.5;
    v = {a, b};
  }
  const auto original = x;
  fft.Forward(x.data());
  fft.Inverse(x.data());
  for (std::size_t i = 0; i < x.size(); ++i) {
    CHECK(std::abs(x[i] - original[i]) < kEps);
  }
}

void TestFftParseval() {
  voxdesk::media::Fft fft(32);
  std::vector<std::complex<double>> x(32);
  for (std::size_t i = 0; i < x.size(); ++i) {
    x[i] = {std::sin(kTwoPi * 3.0 * i / 32.0), std::cos(kTwoPi * 5.0 * i / 32.0)};
  }
  double time_energy = 0.0;
  for (const auto& v : x) {
    time_energy += std::norm(v);
  }
  fft.Forward(x.data());
  double freq_energy = 0.0;
  for (const auto& v : x) {
    freq_energy += std::norm(v);
  }
  CHECK(std::abs(freq_energy - 32.0 * time_energy) < 1e-7);
}

void TestFftLinearity() {
  voxdesk::media::Fft fft(16);
  std::vector<std::complex<double>> a(16), b(16), sum(16);
  for (std::size_t i = 0; i < 16; ++i) {
    a[i] = {static_cast<double>(i % 5) - 2.0, static_cast<double>(i % 3) - 1.0};
    b[i] = {static_cast<double>(i % 7) - 3.0, static_cast<double>(i % 2) - 0.5};
    sum[i] = a[i] + b[i];
  }
  fft.Forward(a.data());
  fft.Forward(b.data());
  fft.Forward(sum.data());
  for (std::size_t i = 0; i < 16; ++i) {
    CHECK(std::abs(sum[i] - (a[i] + b[i])) < kEps);
  }
}

// ---------------------------------------------------------------------------
// Spectral denoiser
// ---------------------------------------------------------------------------

void TestDenoiserPassThrough() {
  // With zero calibration frames there is no noise estimate, so every gain is
  // 1 and the denoiser must reconstruct the input (this also exercises the
  // FFT + Hann + overlap-add path end to end).
  voxdesk::media::SpectralDenoiser denoiser(256, 0);
  const std::size_t n = 3000;
  std::vector<float> in(n);
  for (std::size_t i = 0; i < n; ++i) {
    in[i] = static_cast<float>(0.7 * std::sin(kTwoPi * 13.0 * i / 256.0) +
                               0.2 * std::cos(kTwoPi * 40.0 * i / 256.0));
  }
  const auto out = denoiser.Denoise(in.data(), n);
  CHECK(out.size() == n);
  double max_diff = 0.0;
  for (std::size_t i = 0; i < n; ++i) {
    const double d = std::abs(static_cast<double>(out[i]) - in[i]);
    if (d > max_diff) {
      max_diff = d;
    }
  }
  CHECK(max_diff < kLoose);
}

double RegionEnergy(const std::vector<float>& signal, std::size_t begin,
                    std::size_t end) {
  double e = 0.0;
  for (std::size_t i = begin; i < end; ++i) {
    e += static_cast<double>(signal[i]) * signal[i];
  }
  return e;
}

void TestDenoiserSuppressesNoiseKeepsTone() {
  // Calibrate on 32 frames of white noise, then add a strong bin-aligned tone
  // on top of the same noise. The denoiser must crush the noise energy in the
  // post-calibration region while leaving the tone's energy essentially
  // intact.
  const std::size_t frame = 256;
  const std::size_t calibrate_frames = 32;
  const std::size_t noise_len = 8192;
  const std::size_t tone_len = 2048;
  const std::size_t total = noise_len + tone_len;

  std::uint64_t state = 0x123456789abcdefULL;
  auto next_noise = [&state]() {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    return (static_cast<double>((state >> 11) & 0xFFFFF) / 1048575.0 - 0.5) *
           0.1;  // [-0.05, 0.05]
  };

  std::vector<float> in(total);
  for (std::size_t i = 0; i < total; ++i) {
    in[i] = static_cast<float>(next_noise());
  }
  const double tone_bin = 64.0;
  for (std::size_t i = noise_len; i < total; ++i) {
    in[i] += static_cast<float>(0.5 * std::sin(kTwoPi * tone_bin * (i - noise_len) / frame));
  }

  voxdesk::media::SpectralDenoiser denoiser(frame, calibrate_frames);
  const auto out = denoiser.Denoise(in.data(), total);

  // Post-calibration pure-noise region (frames 39..62) vs the tone region
  // (frames 68..79). Both are beyond the 32 calibration frames.
  const double noise_in = RegionEnergy(in, 5000, 8000);
  const double noise_out = RegionEnergy(out, 5000, 8000);
  const double tone_in = RegionEnergy(in, 8700, 10200);
  const double tone_out = RegionEnergy(out, 8700, 10200);

  CHECK(noise_in > 0.0);
  CHECK(noise_out < 0.3 * noise_in);   // noise crushed
  CHECK(tone_out > 0.5 * tone_in);     // tone preserved
}

// ---------------------------------------------------------------------------
// G.711
// ---------------------------------------------------------------------------

void TestG711ReferenceDecode() {
  using namespace voxdesk::media;
  // mu-law: 0xFF and 0x7F are the two zero codes; 0x80 / 0x00 are the maxima.
  CHECK(DecodeUlLaw(0xFF) == 0);
  CHECK(DecodeUlLaw(0x7F) == 0);
  CHECK(DecodeUlLaw(0x80) == 32124);
  CHECK(DecodeUlLaw(0x00) == -32124);
  // A-law: sign bit = 1 is positive; 0xD5/0x55 are the near-zero codes.
  CHECK(DecodeALaw(0xD5) == 8);
  CHECK(DecodeALaw(0x55) == -8);
  CHECK(DecodeALaw(0xAA) == 32256);
  CHECK(DecodeALaw(0x2A) == -32256);
}

void TestG711ReferenceEncode() {
  using namespace voxdesk::media;
  CHECK(EncodeUlLaw(0) == 0xFF);
  CHECK(EncodeUlLaw(32767) == 0x80);
  CHECK(EncodeUlLaw(-32768) == 0x00);
  CHECK(EncodeALaw(0) == 0xD5);
  CHECK(EncodeALaw(32767) == 0xAA);
  CHECK(EncodeALaw(-32768) == 0x2A);
}

void TestG711RoundTripBound() {
  using namespace voxdesk::media;
  int mu_max_err = 0;
  int a_max_err = 0;
  for (int x = -32768; x <= 32767; ++x) {
    const std::int16_t sample = static_cast<std::int16_t>(x);
    const int mu_err = std::abs(static_cast<int>(DecodeUlLaw(EncodeUlLaw(sample))) - x);
    const int a_err = std::abs(static_cast<int>(DecodeALaw(EncodeALaw(sample))) - x);
    if (mu_err > mu_max_err) {
      mu_max_err = mu_err;
    }
    if (a_err > a_max_err) {
      a_max_err = a_err;
    }
    // Sign must be preserved away from the degenerate near-zero region.
    if (x > 256) {
      CHECK(DecodeUlLaw(EncodeUlLaw(sample)) > 0);
      CHECK(DecodeALaw(EncodeALaw(sample)) > 0);
    } else if (x < -256) {
      CHECK(DecodeUlLaw(EncodeUlLaw(sample)) < 0);
      CHECK(DecodeALaw(EncodeALaw(sample)) < 0);
    }
  }
  // Measured on this implementation: mu-law <= 644, A-law <= 519. Allow a
  // little headroom so the assertion documents the codec, not the rounding.
  CHECK(mu_max_err <= 660);
  CHECK(a_max_err <= 560);
}

void TestG711BufferHelpers() {
  using namespace voxdesk::media;
  const std::int16_t pcm[4] = {0, 100, -100, 32124};
  const auto u = EncodeUlLaw(pcm, 4);
  CHECK(u.size() == 4);
  const auto back = DecodeUlLaw(u.data(), u.size());
  CHECK(back.size() == 4);
  for (std::size_t i = 0; i < 4; ++i) {
    CHECK(back[i] == DecodeUlLaw(EncodeUlLaw(pcm[i])));
  }
  const auto a = EncodeALaw(pcm, 4);
  const auto aback = DecodeALaw(a.data(), a.size());
  CHECK(aback.size() == 4);
  for (std::size_t i = 0; i < 4; ++i) {
    CHECK(aback[i] == DecodeALaw(EncodeALaw(pcm[i])));
  }
}

// ---------------------------------------------------------------------------
// VAD
// ---------------------------------------------------------------------------

void TestVadSilenceThenSpeech() {
  voxdesk::media::Vad vad(9.0, 5);
  const std::size_t n = 160;
  std::vector<float> noise(n), tone(n), silence(n, 0.0f);

  std::uint64_t state = 0xabcdef123456789ULL;
  for (std::size_t i = 0; i < n; ++i) {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    noise[i] = static_cast<float>(
        (static_cast<double>((state >> 11) & 0xFFFFF) / 1048575.0 - 0.5) * 0.02);
    tone[i] = static_cast<float>(0.5 * std::sin(kTwoPi * 40.0 * i / n));
  }

  // Establish the noise floor on quiet frames.
  for (int f = 0; f < 20; ++f) {
    CHECK(!vad.ProcessFrame(noise.data(), n));
  }
  // A loud tone is clearly voiced.
  CHECK(vad.ProcessFrame(tone.data(), n));
  // Hangover keeps the decision on for `hangover_frames` silent frames.
  for (std::size_t f = 0; f < 5; ++f) {
    CHECK(vad.ProcessFrame(silence.data(), n));
  }
  // And then drops.
  CHECK(!vad.ProcessFrame(silence.data(), n));
}

void TestVadAdaptsToLouderNoise() {
  // Minimum-statistics floor must rise once the room gets louder: after the
  // quiet frames fall out of the window, the louder ambient noise is no
  // longer classified as speech — but a genuinely loud tone still is.
  voxdesk::media::Vad vad(9.0, 0, 100);
  const std::size_t n = 160;
  std::vector<float> quiet(n, 0.005f);
  std::vector<float> louder(n, 0.02f);
  std::vector<float> tone(n);
  for (std::size_t i = 0; i < n; ++i) {
    tone[i] = static_cast<float>(0.5 * std::sin(kTwoPi * 40.0 * i / n));
  }

  // Establish a low floor.
  for (int f = 0; f < 150; ++f) {
    CHECK(!vad.ProcessFrame(quiet.data(), n));
  }
  // Feed louder noise long enough for the 100-frame window to roll over.
  for (int f = 0; f < 150; ++f) {
    vad.ProcessFrame(louder.data(), n);
  }
  // The last stretch of louder noise is now at (or under) the raised floor:
  // it must not be classified as speech.
  for (int f = 0; f < 30; ++f) {
    CHECK(!vad.ProcessFrame(louder.data(), n));
  }
  // A real tone still exceeds the raised floor by far more than 9 dB.
  CHECK(vad.ProcessFrame(tone.data(), n));
}

}  // namespace

int main() {
  TestFftImpulseIsFlat();
  TestFftRoundTrip();
  TestFftParseval();
  TestFftLinearity();
  TestDenoiserPassThrough();
  TestDenoiserSuppressesNoiseKeepsTone();
  TestG711ReferenceDecode();
  TestG711ReferenceEncode();
  TestG711RoundTripBound();
  TestG711BufferHelpers();
  TestVadSilenceThenSpeech();
  TestVadAdaptsToLouderNoise();

  std::printf("%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
