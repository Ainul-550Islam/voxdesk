use audio::g711::*;
use audio::{Frame, LevelMeter, Mixer};

#[test]
fn ulaw_known_points() {
    // Canonical G.711 points: silence encodes to 0xFF and decodes to 0.
    assert_eq!(ulaw_encode(0), 0xFF);
    assert_eq!(ulaw_decode(0xFF), 0);
    // Sign symmetry around zero: same magnitude bits, different sign bit.
    let pos = ulaw_encode(1000);
    let neg = ulaw_encode(-1000);
    assert_eq!(
        pos & 0x7F,
        neg & 0x7F,
        "opposite signs share magnitude bits"
    );
    assert_ne!(
        pos & 0x80,
        neg & 0x80,
        "opposite signs differ in the sign bit"
    );
    // Monotonic magnitude ordering (companding's whole point).
    let mut prev: Option<i32> = None;
    for s in (0..=30000).step_by(1000) {
        let d = ulaw_decode(ulaw_encode(s)) as i32;
        if let Some(p) = prev {
            assert!(d >= p - 1, "decode not monotone at {s}: {d} < {p} - 1");
        }
        prev = Some(d);
    }
}

#[test]
fn ulaw_round_trip_within_quantization() {
    // µ-law's relative error shrinks with segment; assert the codec's own
    // guarantee band: |roundtrip - x| <= max(segment step/2, 2 LSB).
    for s in (-30000..=30000).step_by(997) {
        let rt = i32::from(ulaw_decode(ulaw_encode(s)));
        let err = (rt - i32::from(s)).abs();
        let magnitude = i32::from(s).abs();
        let tolerance = (magnitude / 32).max(16) + 16;
        assert!(
            err <= tolerance,
            "µ-law error {err} at {s} exceeds {tolerance}"
        );
    }
}

#[test]
fn alaw_known_points() {
    // Canonical: 8 (PCM) ⇄ 0xD5 (A-law) per the reference tables.
    assert_eq!(alaw_decode(0xD5), 8);
    assert_eq!(alaw_encode(8), 0xD5);
    // A-law zero-adjacent behavior differs from µ-law by design (no dead zone).
    assert_eq!(alaw_encode(0), 0xD5, "0 falls in the same quant cell as 8");
    // Saturation: extremes hit the maximum code, not a wrap.
    let max_pos = alaw_encode(32000);
    let max_neg = alaw_encode(-32000);
    assert!(alaw_decode(max_pos) > 30000);
    assert!(alaw_decode(max_neg) < -30000);
}

#[test]
fn alaw_round_trip_within_quantization() {
    for s in (-30000..=30000).step_by(991) {
        let rt = i32::from(alaw_decode(alaw_encode(s)));
        let err = (rt - i32::from(s)).abs();
        let magnitude = i32::from(s).abs();
        let tolerance = (magnitude / 32).max(16) + 32;
        assert!(
            err <= tolerance,
            "A-law error {err} at {s} exceeds {tolerance}"
        );
    }
}

#[test]
fn payload_helpers_are_elementwise() {
    let payload = [0xFFu8, 0xD5, 0x00, 0x7F];
    let mut samples = Vec::new();
    decode_ulaw_payload(&payload, &mut samples);
    assert_eq!(samples.len(), payload.len());
    let mut back = Vec::new();
    encode_ulaw_payload(&samples, &mut back);
    for (orig, rt) in payload.iter().zip(back.iter()) {
        assert_eq!(
            ulaw_decode(*orig),
            ulaw_decode(*rt),
            "element drift {orig} vs {rt}"
        );
    }
}

#[test]
fn level_meter_rms_peak_and_dbfs() {
    let mut m = LevelMeter::new();
    let loud = Frame {
        samples: vec![16000; 160],
        sample_rate: 8000,
    };
    m.add(&loud);
    assert_eq!(m.peak(), 16000);
    assert!(
        (m.rms() - 16000.0).abs() < 1e-6,
        "constant frame: rms == value"
    );
    // Full scale: 0 dBFS; half (-32768/2): about -6 dB.
    let mut m2 = LevelMeter::new();
    m2.add(&Frame {
        samples: vec![16384; 160],
        sample_rate: 8000,
    });
    assert!(
        (m2.dbfs() + 6.0).abs() < 0.2,
        "half-scale should be ≈ -6 dBFS, got {}",
        m2.dbfs()
    );
    m2.reset();
    assert_eq!(m2.dbfs(), -96.0, "silence reports the -96 floor");
}

#[test]
fn mixer_sums_with_saturation_and_divisor() {
    let mut mix = Mixer::new();
    let mk = |v: i16| {
        Some(Frame {
            samples: vec![v; 4],
            sample_rate: 8000,
        })
    };
    // One contributor: divisor ⇒ ≈ its own value (activity starts at 1).
    let out = mix.mix(&[mk(1000)], 4, 8000);
    assert_eq!(out.samples, vec![1000; 4]);
    // Saturation is a CLIP, never a wrap: a fresh mixer (divisor 1.0)
    // fed three hot streams must stay inside i16 bounds.
    let mut hot = Mixer::new();
    let out = hot.mix(&[mk(30000), mk(30000), mk(30000)], 4, 8000);
    assert!(
        out.samples.iter().all(|&s| (0..=i16::MAX).contains(&s)),
        "clip, not wrap: {:?}",
        out.samples
    );
    // Drive to steady state: repeated mixing must converge near unclipped sum/3.
    let mut last = Frame::silence(4, 8000);
    for _ in 0..200 {
        last = hot.mix(&[mk(30000), mk(30000), mk(30000)], 4, 8000);
    }
    assert!(
        last.samples.iter().all(|&s| (29000..=30100).contains(&s)),
        "steady-state divisor wrong: {:?}",
        last.samples
    );
}

#[test]
fn concealment_decays_to_silence_in_three_steps() {
    let last = Frame {
        samples: vec![10000; 160],
        sample_rate: 8000,
    };
    let s0 = Mixer::conceal(&last, 0);
    let s1 = Mixer::conceal(&last, 1);
    let s2 = Mixer::conceal(&last, 2);
    let s3 = Mixer::conceal(&last, 3);
    assert_eq!(s0.samples[0], 6000);
    assert_eq!(s1.samples[0], 3500);
    assert_eq!(s2.samples[0], 1500);
    assert_eq!(s3.samples[0], 0);
}
