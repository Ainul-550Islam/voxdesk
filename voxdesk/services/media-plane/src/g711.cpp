#include "voxdesk/media/g711.hpp"

namespace voxdesk::media {

namespace {

constexpr std::uint8_t kSignBit = 0x80;
constexpr std::uint8_t kQuantMask = 0x0f;
constexpr int kSegShift = 4;
constexpr std::uint8_t kSegMask = 0x70;
constexpr int kMuBias = 0x84;

// Segment upper bounds, in the 16-bit biased-linear domain. Both mu-law and
// A-law use the same table in the canonical reference: the A-law encoder
// offsets the magnitude by -8 before the search, which shifts the segment
// boundaries into the same grid.
const std::int16_t kSegEnd[8] = {0xFF,   0x1FF,  0x3FF,  0x7FF,
                                 0xFFF,  0x1FFF, 0x3FFF, 0x7FFF};

// First table index whose upper bound is >= val, or 8 if val exceeds all.
int Search(int val) {
  for (int i = 0; i < 8; ++i) {
    if (val <= kSegEnd[i]) {
      return i;
    }
  }
  return 8;
}

}  // namespace

std::uint8_t EncodeUlLaw(std::int16_t sample) {
  int magnitude = sample;
  int mask;
  if (magnitude < 0) {
    magnitude = kMuBias - magnitude;
    mask = 0x7F;
  } else {
    magnitude += kMuBias;
    mask = 0xFF;
  }

  const int seg = Search(magnitude);
  if (seg >= 8) {
    // Out of range: return the maximum-magnitude code for this sign.
    return static_cast<std::uint8_t>(0x7F ^ mask);
  }
  const std::uint8_t uval = static_cast<std::uint8_t>(
      (seg << kSegShift) | ((magnitude >> (seg + 3)) & kQuantMask));
  return static_cast<std::uint8_t>(uval ^ mask);
}

std::int16_t DecodeUlLaw(std::uint8_t code) {
  const int u = static_cast<std::uint8_t>(~code);
  int t = ((u & kQuantMask) << 3) + kMuBias;
  t <<= (u & kSegMask) >> kSegShift;
  return static_cast<std::int16_t>((u & kSignBit) ? (kMuBias - t)
                                                  : (t - kMuBias));
}

std::uint8_t EncodeALaw(std::int16_t sample) {
  int magnitude = sample;
  int mask;
  if (magnitude >= 0) {
    mask = 0xD5;  // sign bit = 1 for positive A-law
  } else {
    mask = 0x55;
    magnitude = -magnitude - 8;
  }

  const int seg = Search(magnitude);
  if (seg >= 8) {
    return static_cast<std::uint8_t>(0x7F ^ mask);
  }

  std::uint8_t aval = static_cast<std::uint8_t>(seg << kSegShift);
  if (seg < 2) {
    aval = static_cast<std::uint8_t>(aval | ((magnitude >> 4) & kQuantMask));
  } else {
    aval = static_cast<std::uint8_t>(
        aval | ((magnitude >> (seg + 3)) & kQuantMask));
  }
  return static_cast<std::uint8_t>(aval ^ mask);
}

std::int16_t DecodeALaw(std::uint8_t code) {
  const int a = code ^ 0x55;
  int t = (a & kQuantMask) << 4;
  const int seg = (a & kSegMask) >> kSegShift;
  switch (seg) {
    case 0:
      t += 8;
      break;
    case 1:
      t += 0x108;
      break;
    default:
      t += 0x108;
      t <<= seg - 1;
      break;
  }
  return static_cast<std::int16_t>((a & kSignBit) ? t : -t);
}

std::vector<std::uint8_t> EncodeUlLaw(const std::int16_t* pcm,
                                      std::size_t count) {
  std::vector<std::uint8_t> out(count);
  for (std::size_t i = 0; i < count; ++i) {
    out[i] = EncodeUlLaw(pcm[i]);
  }
  return out;
}

std::vector<std::int16_t> DecodeUlLaw(const std::uint8_t* code,
                                      std::size_t count) {
  std::vector<std::int16_t> out(count);
  for (std::size_t i = 0; i < count; ++i) {
    out[i] = DecodeUlLaw(code[i]);
  }
  return out;
}

std::vector<std::uint8_t> EncodeALaw(const std::int16_t* pcm,
                                     std::size_t count) {
  std::vector<std::uint8_t> out(count);
  for (std::size_t i = 0; i < count; ++i) {
    out[i] = EncodeALaw(pcm[i]);
  }
  return out;
}

std::vector<std::int16_t> DecodeALaw(const std::uint8_t* code,
                                     std::size_t count) {
  std::vector<std::int16_t> out(count);
  for (std::size_t i = 0; i < count; ++i) {
    out[i] = DecodeALaw(code[i]);
  }
  return out;
}

}  // namespace voxdesk::media
