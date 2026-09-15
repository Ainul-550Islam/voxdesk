# Media plane — C++ audio/video processing (Phase 3)

The C++ half of the media plane. Deliberately dependency-free: everything
compiles with `g++` and `make` alone, and the tests run without a test
framework, so the module is verifiable on any machine before the real media
stack (DTLS-SRTP, Opus, libwebrtc-class transport) is introduced.

## What is here

| Component | Files | Purpose |
|---|---|---|
| De-jitter buffer | `include/voxdesk/media/jitter_buffer.hpp`, `src/jitter_buffer.cpp` | Sequence-ordered, RTP 16-bit-wrap-aware buffer per audio stream: reorders within a window, drops duplicates/late/full, and declares a gap lost instead of stalling forever. |
| Tenant router | `include/voxdesk/media/consistent_hash_router.hpp`, `src/consistent_hash_router.cpp` | Deterministic tenant → node assignment via a consistent-hash ring (FNV-1a + SplitMix64 finalizer), so a tenant's streams are owned by one node and adding/removing a node reshuffles minimally. |
| FFT | `include/voxdesk/media/fft.hpp`, `src/fft.cpp` | Iterative radix-2 complex FFT/IFFT (power-of-two), precomputed bit-reversal + twiddles. The frequency-domain primitive the denoiser is built on. |
| Spectral denoiser | `include/voxdesk/media/spectral_denoise.hpp`, `src/spectral_denoise.cpp` | Spectral-subtraction noise reduction: calibrates a per-bin noise floor from leading frames, then subtracts `oversubtraction × noise` (floored at `spectral_floor × noise`) with the phase untouched; Hann-windowed 50%-overlap-add reconstruction. With zero calibration it is an exact identity, which doubles as the reconstruction test. |
| G.711 codec | `include/voxdesk/media/g711.hpp`, `src/g711.cpp` | μ-law (PCMU) and A-law (PCMA) encode/decode — the classic voice-packet codecs. Canonical segment algorithms, no lookup tables; the tests assert the ITU-T reference points and a round-trip error bound. |
| VAD | `include/voxdesk/media/vad.hpp`, `src/vad.cpp` | Energy voice-activity detector with minimum-statistics noise-floor tracking and hangover. Complements the denoiser: VAD decides *whether* a frame is voice, the denoiser decides *what* to keep. |
| Tests | `tests/test_main.cpp`, `tests/test_audio.cpp` | Self-contained unit tests (own CHECK macro, non-zero exit on failure). |

## Build and test

```bash
make -C services/media-plane            # builds libvoxdesk_media.a + both test binaries
make -C services/media-plane test       # builds and runs both suites
make -C services/media-plane clean      # removes build/ and the archive
```

`test` runs `media_tests` (foundation) and `audio_tests` (audio/video
processing) and exits 0 only when every check passes. This is what CI runs
(`.github/workflows/polyglot.yml`, `media-plane` job).

## Design decisions

* **No third-party dependencies.** FFT, G.711, the denoiser and the VAD are
  std-only; a test framework would add a dependency for a few hundred
  assertions.
* **Single-threaded per stream, by design.** `JitterBuffer` and the DSP
  modules are owned by one stream; the media plane wraps them in a per-stream
  lock/queue where concurrency is introduced. Baking a mutex in here would
  hide the ownership question.
* **The denoiser is batch, not streaming.** `SpectralDenoiser::Denoise` takes
  a whole buffer, zero-pads to frame boundaries, and returns the same length.
  A streaming wrapper (with the inherent half-frame latency) is a later
  increment; the frame-level internals are already structured for it.
* **G.711 uses the canonical segment algorithms**, not a 256-entry table, so
  the quantization is the codec's own and the exact ITU-T reference points
  (μ-law `0xFF→0`, `0x80→+32124`, `0x00→−32124`; A-law `0xD5→+8`, `0x55→−8`,
  `0xAA→+32256`, `0x2A→−32256`) are asserted directly.
* **Consistent-hash invariants are tested, not assumed** — see the removal
  test: tenants not on a removed node keep their assignment.
* **Tenant isolation** is expressed here as "one tenant → one owning node",
  which the contract layer (`contracts/proto`) will also carry as the
  `TenantContext` on every media message.

## Next steps (not yet written)

* Wire the `MediaControl` RPCs (`contracts/proto/.../media.proto`) to these
  components.
* Streaming denoiser wrapper (half-frame latency, real-time feed).
* Opus decode/encode, DTLS-SRTP, congestion control — built on an established
  C++ base (roadmap decision gate in `docs/EXPANSION-ROADMAP.md`).
* A CMake build once the media stack lands (CMake is not required for this
  module; `make` is the verified build).
