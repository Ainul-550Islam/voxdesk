//! AES-128 (FIPS-197), single-key schedule + block cipher. Only the
//! ENCRYPT direction is implemented — every crypto construction we need
//! (AES-CTR, AES-CM/SRTP PRF, DTLS check) is encrypt-only even for
//! "decryption", which eliminates the inverse-tables maintenance hazard.
//!
//! The S-box is CONSTRUCTED (log/exp tables in GF(2^8), const-evaluated)
//! rather than transcribed from a reference table: any transcription bug
//! would be silent; the constructed form is verified against the FIPS-197
//! Appendix A vectors in tests either way.

// ---------------------------------------------------------------- GF(2^8)

const fn gadd(a: u8, b: u8) -> u8 {
    a ^ b
}

/// Multiplication by x: shift + conditional reduction with 0x1B.
const fn xtime(a: u8) -> u8 {
    let shifted = a << 1;
    if a & 0x80 != 0 {
        shifted ^ 0x1B
    } else {
        shifted
    }
}

/// Full GF multiply via repeated doubling (Russian peasant, 8 rounds).
const fn gmul(mut a: u8, mut b: u8) -> u8 {
    let mut p = 0u8;
    while b != 0 {
        if b & 1 != 0 {
            p = gadd(p, a);
        }
        a = xtime(a);
        b >>= 1;
    }
    p
}

/// S-box: affine transform over the multiplicative inverse, per FIPS-197.
const fn build_sbox() -> [u8; 256] {
    // exp/log tables: generator 3.
    let mut exp = [0u8; 256];
    let mut log = [0u8; 256];
    let mut x = 1u8;
    let mut i = 0usize;
    while i < 255 {
        exp[i] = x;
        log[x as usize] = i as u8;
        // multiply x (current power) by 3 = 1 in exponent: x = x*3
        // via log/exp — but we build exp from scratch, so do it the
        // direct way: x = gmul(x, 3)
        x = gmul(x, 3);
        i += 1;
    }
    exp[255] = exp[0]; // wrap so discrete log handles 0→0
    let mut sbox = [0u8; 256];
    let mut v = 0usize;
    while v < 256 {
        // Multiplicative inverse with the convention 0 → 0.
        let inv = if v == 0 {
            0u8
        } else {
            exp[255 - (log[v] as usize) % 255]
        };
        // Affine: a ^ (a <<< 1) ^ (a <<< 2) ^ (a <<< 3) ^ (a <<< 4) ^ 0x63
        let rot1 = inv.rotate_left(1);
        let rot2 = inv.rotate_left(2);
        let rot3 = inv.rotate_left(3);
        let rot4 = inv.rotate_right(4);
        sbox[v] = inv ^ rot1 ^ rot2 ^ rot3 ^ rot4 ^ 0x63;
        v += 1;
    }
    sbox
}

const SBOX: [u8; 256] = build_sbox();

pub(crate) fn sbox(x: u8) -> u8 {
    SBOX[x as usize]
}

const RCON: [u8; 10] = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36];

// -------------------------------------------------------------- key style

/// AES-128 schedule: 11 round keys (176 bytes), words rhymes with FIPS "w".
pub struct Aes128 {
    round_keys: [[u8; 16]; 11],
}

impl Aes128 {
    pub fn new(key: &[u8; 16]) -> Aes128 {
        // FIPS-197 §5.2 key expansion: 44 words of 4 bytes.
        let mut w = [[0u8; 4]; 44];
        for i in 0..4 {
            w[i] = [key[4 * i], key[4 * i + 1], key[4 * i + 2], key[4 * i + 3]];
        }
        for i in 4..44 {
            let mut t = w[i - 1];
            if i % 4 == 0 {
                // RotWord, SubWord, Rcon.
                t = [t[1], t[2], t[3], t[0]];
                t = [
                    SBOX[t[0] as usize],
                    SBOX[t[1] as usize],
                    SBOX[t[2] as usize],
                    SBOX[t[3] as usize],
                ];
                t[0] ^= RCON[i / 4 - 1];
            }
            for j in 0..4 {
                w[i][j] = w[i - 4][j] ^ t[j];
            }
        }
        let mut round_keys = [[0u8; 16]; 11];
        for (r, rk) in round_keys.iter_mut().enumerate() {
            for col in 0..4 {
                rk[col * 4..col * 4 + 4].copy_from_slice(&w[r * 4 + col]);
            }
        }
        Aes128 { round_keys }
    }

    /// Encrypt one 16-byte block (FIPS-197 §5.1: state is COLUMN-major,
    /// all indexing here is col*4 + row).
    pub fn encrypt_block(&self, input: &[u8; 16]) -> [u8; 16] {
        let mut state = *input;
        add_round_key(&mut state, &self.round_keys[0]);
        for round in 1..10 {
            sub_bytes(&mut state);
            shift_rows(&mut state);
            mix_columns(&mut state);
            add_round_key(&mut state, &self.round_keys[round]);
        }
        sub_bytes(&mut state);
        shift_rows(&mut state);
        add_round_key(&mut state, &self.round_keys[10]);
        state
    }

    /// CTR keystream+apply over arbitrary bytes; `nonce` is the 16-byte
    /// initial counter block (SRTP's IV construction passes one).
    pub fn apply_keystream(&self, iv: &[u8; 16], data: &mut [u8], byte_offset: u64) {
        let mut counter = u128::from_be_bytes(*iv) + u128::from(byte_offset / 16);
        let mut block_ofs = (byte_offset % 16) as usize;
        let mut written = 0usize;
        while written < data.len() {
            let keystream = self.encrypt_block(&counter.to_be_bytes());
            let n = (16 - block_ofs).min(data.len() - written);
            for i in 0..n {
                data[written + i] ^= keystream[block_ofs + i];
            }
            written += n;
            block_ofs = 0;
            counter = counter.wrapping_add(1);
        }
    }
}

pub(crate) fn add_round_key(state: &mut [u8; 16], rk: &[u8; 16]) {
    for i in 0..16 {
        state[i] ^= rk[i];
    }
}

pub(crate) fn sub_bytes(state: &mut [u8; 16]) {
    for b in state.iter_mut() {
        *b = SBOX[*b as usize];
    }
}

pub(crate) fn shift_rows(state: &mut [u8; 16]) {
    // state index: col*4 + row. Row r rotates left by r columns.
    let src = *state;
    for row in 0..4 {
        for col in 0..4 {
            state[col * 4 + row] = src[((col + row) % 4) * 4 + row];
        }
    }
}

pub(crate) fn mix_columns(state: &mut [u8; 16]) {
    for col in 0..4 {
        let c = &mut state[col * 4..col * 4 + 4];
        let (a0, a1, a2, a3) = (c[0], c[1], c[2], c[3]);
        c[0] = gmul(a0, 2) ^ gmul(a1, 3) ^ a2 ^ a3;
        c[1] = a0 ^ gmul(a1, 2) ^ gmul(a2, 3) ^ a3;
        c[2] = a0 ^ a1 ^ gmul(a2, 2) ^ gmul(a3, 3);
        c[3] = gmul(a0, 3) ^ a1 ^ a2 ^ gmul(a3, 2);
    }
}
