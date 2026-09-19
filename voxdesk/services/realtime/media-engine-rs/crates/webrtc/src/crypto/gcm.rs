//! AES-GCM (NIST SP 800-38D) built on the workspace's owned AES-128/256
//! — the mechanical heart RFC 7714 needs. The unusual single-known-good
//! vector here is NIST GCM test case 2 (and the RFC pillars stand on
//! the full vector round through `crate::srtp`'s AES_128_GCM KAT).
//!
//! The ground rules this implementation lives by:
//! * GHASH multiply is a translate-table-less shift/XOR reference
//!   implementation — correctness over speed, and constant in data
//!   shape only (not timing-classified inputs; non-goals documented).
//! * 96-bit IVs only — everything SRTP produces is 12 octets; the
//!   variable-IV J0 construction ("IV hashed by GHASH" path) is OUT of
//!   scope and never reachable through the `_96bit` entry point names.

use super::aes128::Aes128;
use super::aes256::Aes256;

/// Anything that can encrypt one 16-byte block — both our Aes cores.
pub trait BlockEncrypt {
    fn encrypt_block(&self, input: &[u8; 16]) -> [u8; 16];
}

impl BlockEncrypt for Aes128 {
    fn encrypt_block(&self, input: &[u8; 16]) -> [u8; 16] {
        Aes128::encrypt_block(self, input)
    }
}

impl BlockEncrypt for Aes256 {
    fn encrypt_block(&self, input: &[u8; 16]) -> [u8; 16] {
        Aes256::encrypt_block(self, input)
    }
}

/// One direction of an AES-GCM context: the cipher and the GHASH hash
/// key H = E(K, 0^128) precomputed once.
pub struct AesGcm<C: BlockEncrypt> {
    cipher: C,
    h: [u8; 16],
}

impl<C: BlockEncrypt> AesGcm<C> {
    pub fn new(cipher: C) -> Self {
        let h = cipher.encrypt_block(&[0u8; 16]);
        AesGcm { cipher, h }
    }

    /// AEAD seal with a 96-bit IV: returns ciphertext ‖ tag(16).
    pub fn seal_96bit(&self, iv: &[u8; 12], aad: &[u8], plaintext: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(plaintext.len() + 16);
        out.resize(plaintext.len(), 0u8);
        out.copy_from_slice(plaintext);
        self.apply_ctr(iv, &mut out[..]);

        let tag = self.authentication_tag(iv, aad, &out[..]);
        out.extend_from_slice(&tag);
        out
    }

    /// AEAD open: verifies first (no plaintext leaks on a bad tag —
    /// secrecy AND integrity before decoding).
    pub fn open_96bit(
        &self,
        iv: &[u8; 12],
        aad: &[u8],
        ciphertext_with_tag: &[u8],
    ) -> Option<Vec<u8>> {
        if ciphertext_with_tag.len() < 16 {
            return None;
        }
        let (ct, tag) = ciphertext_with_tag.split_at(ciphertext_with_tag.len() - 16);
        let expect = self.authentication_tag(iv, aad, ct);
        // Compare without an early exit.
        let mut diff = 0u8;
        for (a, b) in tag.iter().zip(expect.iter()) {
            diff |= a ^ b;
        }
        if diff != 0 {
            return None;
        }
        let mut out = ct.to_vec();
        self.apply_ctr(iv, &mut out[..]);
        Some(out)
    }

    /// SP 800-38D §6.4: GCTR over the counter initialized at J0+1 — for
    /// 96-bit IVs J0 = IV ‖ 0x00000001. The SAME stream is used for
    /// encryption and decryption; the tag's base encryption E(K,J0)
    /// stays isolated from it.
    fn apply_ctr(&self, iv: &[u8; 12], data: &mut [u8]) {
        let mut ctr = [0u8; 16];
        ctr[..12].copy_from_slice(iv);
        ctr[15] = 1; // J0 = IV ‖ 0x00000001
        for block in data.chunks_mut(16) {
            inc32(&mut ctr);
            let keystream = self.cipher.encrypt_block(&ctr);
            for (b, k) in block.iter_mut().zip(keystream.iter()) {
                *b ^= *k;
            }
        }
    }

    /// Auth tag T = GHASH(H; AAD ‖ pad ‖ CT ‖ pad ‖ (bit-lengths)) ⊕
    /// E(K, J0).
    fn authentication_tag(&self, iv: &[u8; 12], aad: &[u8], ct: &[u8]) -> [u8; 16] {
        let mut x = [0u8; 16];
        ghash_accumulate(&mut x, &self.h, aad);
        ghash_accumulate(&mut x, &self.h, ct);
        let mut lens = [0u8; 16];
        lens[..8].copy_from_slice(&((aad.len() as u64) * 8).to_be_bytes());
        lens[8..].copy_from_slice(&((ct.len() as u64) * 8).to_be_bytes());
        ghash_accumulate(&mut x, &self.h, &lens);

        let mut j0 = [0u8; 16];
        j0[..12].copy_from_slice(iv);
        j0[15] = 1;
        let s = self.cipher.encrypt_block(&j0);
        for i in 0..16 {
            x[i] ^= s[i];
        }
        x
    }
}

/// GHASH over data padded to the block boundary with zeroes.
fn ghash_accumulate(x: &mut [u8; 16], h: &[u8; 16], data: &[u8]) {
    for chunk in data.chunks(16) {
        let mut block = [0u8; 16];
        block[..chunk.len()].copy_from_slice(chunk);
        for i in 0..16 {
            x[i] ^= block[i];
        }
        *x = gf128_mul(x, h);
    }
}

/// Carryless multiply in GF(2^128) per SP 800-38D §6.3: the shift-and-
/// conditional-reduce bit path (R = 0xe1 << 120).
fn gf128_mul(x: &[u8; 16], y: &[u8; 16]) -> [u8; 16] {
    const R: u128 = 0xE100_0000_0000_0000_0000_0000_0000_0000u128;
    let mut z: u128 = 0;
    let mut v = u128::from_be_bytes(*y);
    let xv = u128::from_be_bytes(*x);
    for i in 0..128 {
        if (xv >> (127 - i)) & 1 == 1 {
            z ^= v;
        }
        if v & 1 == 1 {
            v = (v >> 1) ^ R;
        } else {
            v >>= 1;
        }
    }
    z.to_be_bytes()
}

/// The 32-bit big-endian increment of the counter's LOW word — the GCTR
/// rule (wraps by construction, per the standard).
fn inc32(ctr: &mut [u8; 16]) {
    let mut c = 1u32;
    for byte in ctr[12..].iter_mut().rev() {
        c += u32::from(*byte);
        *byte = c as u8;
        c >>= 8;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hx(s: &str) -> Vec<u8> {
        (0..s.len() / 2)
            .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn nist_gcm_case2_aead_roundtrip() {
        // NIST GCM S1: K=0, P=0^128, IV=0^96 → CT + known tag.
        let k12 = [0u8; 16];
        let g = AesGcm::new(Aes128::new(&k12));
        let iv = [0u8; 12];
        let pt = [0u8; 16];
        let out = g.seal_96bit(&iv, &[], &pt);
        assert_eq!(
            out,
            hx("0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf")
        );
        let back = g.open_96bit(&iv, &[], &out).expect("tag verifies");
        assert_eq!(back, pt.to_vec());
    }

    #[test]
    fn aad_only_affects_the_tag() {
        let g = AesGcm::new(Aes128::new(&[7u8; 16]));
        let iv = [3u8; 12];
        let pt = *b"payload-under-aad-alive";
        let a = g.seal_96bit(&iv, b"", &pt);
        let b = g.seal_96bit(&iv, b"x", &pt);
        assert_eq!(&a[..pt.len()], &b[..pt.len()], "aad never touches ct");
        assert_ne!(&a[pt.len()..], &b[pt.len()..], "aad shifts the tag");
        assert!(g.open_96bit(&iv, b"", &b).is_none());
        assert!(g.open_96bit(&iv, b"x", &b).is_some());
    }

    #[test]
    fn aes256_gcm_rfc7748_case() {
        // NIST GCM S3 for 256-bit: key 0^256, IV 0^96, PT 16 zeroes.
        let g = AesGcm::new(Aes256::new(&[0u8; 32]));
        let out = g.seal_96bit(&[0u8; 12], &[], &[0u8; 16]);
        assert_eq!(&out[..16], &hx("cea7403d4d606b6e074ec5d3baf39d18")[..]);
        assert_eq!(&out[16..], &hx("d0d1c8a799996bf0265b98b5d48ab919")[..]);
    }

    #[test]
    fn tampered_tag_is_quietly_refused() {
        let g = AesGcm::new(Aes128::new(&[9u8; 16]));
        let iv = [1u8; 12];
        let mut out = g.seal_96bit(&iv, b"hdr", b"p");
        let last = out.len() - 1;
        out[last] ^= 0x80;
        assert!(g.open_96bit(&iv, b"hdr", &out).is_none());
        // Cipher tamper flips the tag just the same.
        let mut out = g.seal_96bit(&iv, b"hdr", b"p");
        out[0] ^= 0x01;
        assert!(g.open_96bit(&iv, b"hdr", &out).is_none());
    }
}
