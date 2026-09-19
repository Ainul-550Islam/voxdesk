use livekit::{
    decode_b64url, encode_b64url, mint, sha256_fingerprint_label, verify, Capability, VerifyError,
};

const SECRET: &str = "devsecret-shared-with-voxdesk-gateway";
const KEY: &str = "devkey";

#[test]
fn b64url_round_trip_is_strict_against_tampered_shape() {
    assert_eq!(encode_b64url(b""), "");
    assert_eq!(encode_b64url(b"f"), "Zg");
    assert_eq!(encode_b64url(b"fo"), "Zm8");
    assert_eq!(encode_b64url(b"foo"), "Zm9v");
    assert_eq!(encode_b64url(b"foob"), "Zm9vYg");
    assert_eq!(encode_b64url(b"fooba"), "Zm9vYmE");
    assert_eq!(encode_b64url(b"foobar"), "Zm9vYmFy");
    assert_eq!(encode_b64url(&[0xFF, 0xEE]), "_-4");
    assert_eq!(decode_b64url("Zm9vYmFy").unwrap(), b"foobar");
    // Padding on input is accepted if AND ONLY IF consistent.
    assert_eq!(decode_b64url("Zm8=").unwrap(), b"fo");
    assert_eq!(decode_b64url("Zg==").unwrap(), b"f");
    assert!(decode_b64url("Z===").is_none());
    assert!(decode_b64url("Zm=v").is_none());
    assert!(decode_b64url("Zmsg!").is_none());
    assert!(
        decode_b64url("Z").is_none(),
        "single char is never a valid segment"
    );
}

#[test]
fn mint_then_verify_happy_path_and_capability_gates() {
    let now = 1_700_000_000u64;
    let token = mint(SECRET, "alice", "standup", 3600, now + 10, true, true);

    let claims = verify(
        token.as_str(),
        KEY,
        SECRET,
        Capability::Join,
        "standup",
        now + 100,
    )
    .unwrap();
    assert_eq!(claims.grant.room, "standup");
    assert!(claims.grant.can_publish);
    // Subscribe needs the bit.
    assert!(verify(
        token.as_str(),
        KEY,
        SECRET,
        Capability::Subscribe,
        "standup",
        now + 100
    )
    .is_ok());

    // A restricted token (publish only) fails Subscribe.
    let pub_only = mint(SECRET, "spk", "stage", 3600, now + 10, true, false);
    assert_eq!(
        verify(
            pub_only.as_str(),
            KEY,
            SECRET,
            Capability::Subscribe,
            "stage",
            now + 100
        )
        .unwrap_err(),
        vec![VerifyError::CannotSubscribe]
    );
    assert!(verify(
        pub_only.as_str(),
        KEY,
        SECRET,
        Capability::Publish,
        "stage",
        now + 100
    )
    .is_ok());

    // Wrong room is NotRoomJoin.
    assert_eq!(
        verify(
            token.as_str(),
            KEY,
            SECRET,
            Capability::Join,
            "green-room",
            now + 100
        )
        .unwrap_err(),
        vec![VerifyError::NotRoomJoin]
    );
}

#[test]
fn time_rules_and_signatures_are_enforced_strictly() {
    let now = 1_700_000_000u64;
    let token = mint(SECRET, "alice", "standup", 60, now, true, true);
    // Expired: exp == now ⇒ expired (the token was valid strictly before).
    assert_eq!(
        verify(&token, KEY, SECRET, Capability::Join, "standup", now + 61).unwrap_err(),
        vec![VerifyError::Expired]
    );

    // Tampered signature: every error caught, not just one.
    // Forged claims (different identity, valid base64) with a stale
    // signature — HMAC catches the mismatch.
    let tampered = {
        let parts: Vec<&str> = token.split('.').collect();
        format!(
            "{}.{}.{}",
            parts[0],
            encode_b64url(
                br#"{"sub":"mallory","exp":1700010000,"video":{"room":"standup","roomJoin":true}}"#
            ),
            parts[2]
        )
    };
    let errs = verify(
        &tampered,
        KEY,
        SECRET,
        Capability::Join,
        "standup",
        now + 10,
    )
    .unwrap_err();
    assert!(
        errs.contains(&VerifyError::BadSignature),
        "want bad-signature in {errs:?}"
    );

    // A three-part syntactic-but-wrong signature with forged claims signed by a DIFFERENT secret.
    let forged = mint(
        "attacker-secret",
        "mallory",
        "standup",
        3600,
        now + 10,
        true,
        true,
    );
    assert_eq!(
        verify(&forged, KEY, SECRET, Capability::Join, "standup", now + 11).unwrap_err(),
        vec![VerifyError::BadSignature]
    );

    // Non-JWT shape.
    assert_eq!(
        verify("not-a-jwt", KEY, SECRET, Capability::Join, "standup", now).unwrap_err(),
        vec![VerifyError::NotAJwt]
    );

    // Wrong alg claim (even if everything else is well-formed).
    let hs512 = {
        let head = encode_b64url(br#"{"alg":"HS512","typ":"JWT"}"#);
        let body = mint(SECRET, "a", "standup", 100, now, true, true);
        let b: Vec<&str> = body.split('.').collect();
        format!("{}.{}.{}", head, b[1], b[2])
    };
    let errs = verify(&hs512, KEY, SECRET, Capability::Join, "standup", now + 10).unwrap_err();
    assert!(
        errs.contains(&VerifyError::UnknownAlgorithm),
        "want alg error in {errs:?}"
    );
}

#[test]
fn decode_accepts_padded_and_unpadded_input_equivalently() {
    let a = decode_b64url("eyJhbGciOiJIUzI1NiJ9").unwrap();
    assert_eq!(std::str::from_utf8(&a).unwrap(), r#"{"alg":"HS256"}"#);
    assert_eq!(decode_b64url("eyJhbGciOiJIUzI1NiJ9").unwrap(), a);
}

#[test]
fn fingerprint_helper_hashes_hex_pairs() {
    let fp = sha256_fingerprint_label("sha-256");
    assert_eq!(fp.chars().count(), 95, "32 bytes as AA:BB:.. is 95 chars");
    assert!(fp.chars().all(|c| c.is_ascii_hexdigit() || c == ':'));
    assert_eq!(fp.matches(':').count(), 31);
    assert_eq!(&fp[..5], "31:28");
}
