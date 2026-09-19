//! HMAC (RFC 2104) over the crate's owned digests. STUN integrity and
//! SRTP tags use HMAC-SHA1; LiveKit tokens use HMAC-SHA256.

use super::{sha1, sha256};

const BLOCK: usize = 64;

fn hmac<const OUT: usize>(digest: fn(&[u8]) -> [u8; OUT], key: &[u8], message: &[u8]) -> [u8; OUT] {
    // Keys longer than the block are hashed down (RFC 2104 §2).
    let owned;
    let key = if key.len() > BLOCK {
        owned = digest(key);
        &owned[..]
    } else {
        key
    };

    let mut ipad = [0x36u8; BLOCK];
    let mut opad = [0x5cu8; BLOCK];
    for (k, (i, o)) in key.iter().zip(ipad.iter_mut().zip(opad.iter_mut())) {
        *i ^= k;
        *o ^= k;
    }

    let mut inner = Vec::with_capacity(BLOCK + message.len());
    inner.extend_from_slice(&ipad);
    inner.extend_from_slice(message);
    let inner_digest = digest(&inner);

    let mut outer = Vec::with_capacity(BLOCK + OUT);
    outer.extend_from_slice(&opad);
    outer.extend_from_slice(&inner_digest);
    digest(&outer)
}

/// HMAC-SHA1 (RFC 2202 test vectors govern this module).
pub fn hmac_sha1(key: &[u8], message: &[u8]) -> [u8; 20] {
    hmac(sha1::sha1, key, message)
}

/// HMAC-SHA256 (RFC 4231 test vectors govern this module).
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    hmac(sha256::sha256, key, message)
}
