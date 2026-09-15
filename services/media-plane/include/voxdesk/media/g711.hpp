#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace voxdesk::media {

// G.711 — the companded voice codec carried by classic VoIP (PCMU/PCMA,
// RFC 3551 payload types 0 and 8). A voice packet holds 8-bit samples at
// 8 kHz; decoding turns them back into 16-bit linear PCM.
//
// The encode/decode pairs implement the canonical G.711 segment algorithms
// (the Sun Microsystems reference, in the public domain since 1994): encode
// biases the linear magnitude, finds its segment and 4-bit quantization, and
// complements the code word for transmission; decode extracts the segment and
// quantization, shifts up, and removes the bias. No lookup tables are used, so
// the quantization is the codec's own, not a table transcription. The
// round-trip error is bounded by the segment step sizes (<= 644 for mu-law,
// <= 560 for A-law over the 16-bit range); tests assert the exact ITU-T
// reference points.
//
// Sign conventions (ITU-T): mu-law bit 7 == 1 is positive; A-law bit 7 == 1 is
// positive. Both codecs decode 0xFF / 0x55 / 0xD5 as their (near-)zero codes.

// PCM (16-bit) -> mu-law (8-bit).
std::uint8_t EncodeUlLaw(std::int16_t sample);

// mu-law (8-bit) -> PCM (16-bit).
std::int16_t DecodeUlLaw(std::uint8_t code);

// PCM (16-bit) -> A-law (8-bit).
std::uint8_t EncodeALaw(std::int16_t sample);

// A-law (8-bit) -> PCM (16-bit).
std::int16_t DecodeALaw(std::uint8_t code);

// Whole-buffer convenience helpers (one sample in, one sample out).
std::vector<std::uint8_t> EncodeUlLaw(const std::int16_t* pcm,
                                      std::size_t count);
std::vector<std::int16_t> DecodeUlLaw(const std::uint8_t* code,
                                      std::size_t count);
std::vector<std::uint8_t> EncodeALaw(const std::int16_t* pcm,
                                     std::size_t count);
std::vector<std::int16_t> DecodeALaw(const std::uint8_t* code,
                                     std::size_t count);

}  // namespace voxdesk::media
