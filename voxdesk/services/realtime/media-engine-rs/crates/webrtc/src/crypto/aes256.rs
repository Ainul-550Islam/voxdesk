//! AES-256 (FIPS-197 §5) — 14-round complement to `aes128`, with the
//! same column-major state layout and table-free implementation. Needed
//! for RFC 7714's AEAD_AES_256_GCM SRTP profile. Correctness is pinned
//! by the NIST/FIPS-197 AES-256 vectors in `mod tests` below.

use super::aes128;

/// Shared single-round machinery lives in `aes128` (pub(crate) struct
/// lanes); this module reuses them via small exposed helpers so the two
/// key schedules diverge ONLY where FIPS-197 differs (rot/sub schedule).
pub struct Aes256 {
    round_keys: [[u8; 16]; 15],
}

impl Aes256 {
    pub fn new(key: &[u8; 32]) -> Aes256 {
        // FIPS-197 §5.2: 60 words; SubWord on words i ≡ 4 (mod 8) plus
        // the RotSubRcon branch on i ≡ 0 (mod 8).
        let mut w = [[0u8; 4]; 60];
        for i in 0..8 {
            w[i] = [key[4 * i], key[4 * i + 1], key[4 * i + 2], key[4 * i + 3]];
        }
        for i in 8..60 {
            let mut t = w[i - 1];
            if i % 8 == 0 {
                t = [t[1], t[2], t[3], t[0]];
                t = [sbox(t[0]), sbox(t[1]), sbox(t[2]), sbox(t[3])];
                t[0] ^= RCON[i / 8 - 1];
            } else if i % 8 == 4 {
                t = [sbox(t[0]), sbox(t[1]), sbox(t[2]), sbox(t[3])];
            }
            for j in 0..4 {
                w[i][j] = w[i - 8][j] ^ t[j];
            }
        }
        let mut round_keys = [[0u8; 16]; 15];
        for (r, rk) in round_keys.iter_mut().enumerate() {
            for col in 0..4 {
                rk[col * 4..col * 4 + 4].copy_from_slice(&w[r * 4 + col]);
            }
        }
        Aes256 { round_keys }
    }

    /// Encrypt one 16-byte block (same state semantics as Aes128).
    pub fn encrypt_block(&self, input: &[u8; 16]) -> [u8; 16] {
        let mut state = *input;
        aes128::add_round_key(&mut state, &self.round_keys[0]);
        for round in 1..14 {
            aes128::sub_bytes(&mut state);
            aes128::shift_rows(&mut state);
            aes128::mix_columns(&mut state);
            aes128::add_round_key(&mut state, &self.round_keys[round]);
        }
        aes128::sub_bytes(&mut state);
        aes128::shift_rows(&mut state);
        aes128::add_round_key(&mut state, &self.round_keys[14]);
        state
    }
}

fn sbox(x: u8) -> u8 {
    aes128::sbox(x)
}

const RCON: [u8; 7] = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fips197_aes256_known_answer() {
        // FIPS-197 Appendix B, AES-256 example.
        let key = hex32("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4");
        let plain = hex16("6bc1bee22e409f96e93d7e117393172a");
        let expect = hex16("f3eed1bdb5d2a03c064b5a7e3db181f8");
        let ct = Aes256::new(&key).encrypt_block(&plain);
        assert_eq!(ct, expect);
    }

    fn hex16(s: &str) -> [u8; 16] {
        let b = hex(s);
        let mut out = [0u8; 16];
        out.copy_from_slice(&b);
        out
    }

    fn hex32(s: &str) -> [u8; 32] {
        let b = hex(s);
        let mut out = [0u8; 32];
        out.copy_from_slice(&b);
        out
    }

    fn hex(s: &str) -> Vec<u8> {
        (0..s.len() / 2)
            .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap())
            .collect()
    }
}
