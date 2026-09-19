//! G.711 µ-law and A-law companding (ITU-T G.711): the payload codec of
//! PSTN-facing legs (RTP payload types 0 and 8).
//!
//! Implemented from the standard reference algorithms (the same arithmetic
//! published by Sun Microsystems as the G.711 reference and reproduced in
//! ITU-T G.191's test material), at a consistent 16-bit operating point:
//!
//! * decode: companded byte → signed 16-bit linear PCM
//! * encode: signed 16-bit linear PCM → companded byte (lossy quantization:
//!   round-trip error stays within the codec's design step size, which the
//!   conformance tests assert rather than pretending bit-exactness exists)
//!
//! Mixing happens in linear PCM (mixer.rs), so these functions are the
//! boundary of every companded leg.

// ---------------------------------------------------------------------------
// µ-law (North America / Japan; RTP payload type 0)
// ---------------------------------------------------------------------------

/// µ-law's bias and linear clip point at the 16-bit operating point, and
/// the segment upper-bound table (0xFF, 0x1FF, … 0x7FFF).
const ULAW_BIAS: i32 = 0x84; // 132
const ULAW_CLIP: i32 = 32635;
const ULAW_SEG_END: [i32; 8] = [0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF];

/// Decode one µ-law byte to 16-bit linear PCM (reference arithmetic:
/// t = ((mantissa << 3) + BIAS) << segment; sample = t - BIAS, signed).
pub fn ulaw_decode(u_val: u8) -> i16 {
    let u = !u_val; // ones' complement per G.711
    let sign = u & 0x80;
    let segment = i32::from((u >> 4) & 0x07);
    let mantissa = i32::from(u & 0x0F);
    let t = (((mantissa << 3) + ULAW_BIAS) << segment) - ULAW_BIAS;
    if sign != 0 {
        -t as i16
    } else {
        t as i16
    }
}

/// Encode one 16-bit linear PCM sample to µ-law (reference arithmetic:
/// bias, clip, segment search, then (seg<<4)|quant with the polarity
/// mask folded in).
pub fn ulaw_encode(sample: i16) -> u8 {
    let s = i32::from(sample);
    let mask: u8;
    let mut pcm = if s < 0 {
        mask = 0x7F;
        ULAW_BIAS - s // note: -s, not s — the bias subtraction is the reference's convention
    } else {
        mask = 0xFF;
        s + ULAW_BIAS
    };
    if pcm > ULAW_CLIP {
        pcm = ULAW_CLIP;
    }
    let segment = ULAW_SEG_END.iter().position(|&end| pcm <= end).unwrap_or(8) as i32;
    if segment >= 8 {
        return 0x7F ^ mask; // out of range: maximum magnitude code
    }
    let quant = ((pcm >> (segment + 3)) & 0x0F) as u8;
    ((segment as u8) << 4 | quant) ^ mask
}

// ---------------------------------------------------------------------------
// A-law (Europe / rest of world; RTP payload type 8)
// ---------------------------------------------------------------------------

/// Segment upper bounds at the 13-bit scale: 0x1F, 0x3F, 0x7F, …
const ALAW_SEG_END: [i32; 8] = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF];

/// Decode one A-law byte to 16-bit linear PCM.
pub fn alaw_decode(a_val: u8) -> i16 {
    let a = a_val ^ 0x55; // even-bit inversion per G.711
    let sign = a & 0x80;
    let segment = i32::from((a >> 4) & 0x07);
    let mut t = i32::from(a & 0x0F) << 4; // to 16-bit scale
    match segment {
        0 => t += 8,
        1 => t += 0x108,
        _ => {
            t += 0x108;
            t <<= segment - 1;
        }
    }
    if sign != 0 {
        t as i16
    } else {
        -(t as i16)
    }
}

/// Encode one 16-bit linear PCM sample to A-law.
pub fn alaw_encode(sample: i16) -> u8 {
    let s = i32::from(sample) >> 3; // to the 13-bit scale
    let mask: u8;
    let mut pcm13 = if s >= 0 {
        mask = 0xD5;
        s
    } else {
        mask = 0x55;
        -s - 1
    };
    let segment = match ALAW_SEG_END.iter().position(|&end| pcm13 <= end) {
        Some(seg) => seg as i32,
        None => return 0x7F ^ mask, // out of range: maximum magnitude
    };
    let _ = &mut pcm13;
    let quant = if segment < 2 {
        ((pcm13 >> 1) & 0x0F) as u8
    } else {
        ((pcm13 >> segment) & 0x0F) as u8
    };
    ((segment as u8) << 4 | quant) ^ mask
}

// ---------------------------------------------------------------------------
// Payload-wise helpers (RTP payloads are byte strings, not sample slices)
// ---------------------------------------------------------------------------

pub fn decode_ulaw_payload(payload: &[u8], out: &mut Vec<i16>) {
    out.extend(payload.iter().map(|&b| ulaw_decode(b)));
}

pub fn encode_ulaw_payload(samples: &[i16], out: &mut Vec<u8>) {
    out.extend(samples.iter().map(|&s| ulaw_encode(s)));
}

pub fn decode_alaw_payload(payload: &[u8], out: &mut Vec<i16>) {
    out.extend(payload.iter().map(|&b| alaw_decode(b)));
}

pub fn encode_alaw_payload(samples: &[i16], out: &mut Vec<u8>) {
    out.extend(samples.iter().map(|&s| alaw_encode(s)));
}
