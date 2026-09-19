//! Wire-contract tests: JSON codec conformance plus frame round-trips.

use protocol::json::{self, Value};
use protocol::{
    ClientFrame, DecodeError, ErrorCode, MediaKind, MediaSessionId, ParticipantId, RoomId,
    ServerFrame, TrackId,
};

#[test]
fn json_round_trip_nested() {
    let src = r#"{"a":[1,2.5,true,null,"x\ny"],"b":{"c":"\u00e9","d":[]}}"#;
    let v = json::parse(src).unwrap();
    let a = v.get("a").unwrap().as_arr().unwrap();
    assert_eq!(a[0].as_f64(), Some(1.0));
    assert_eq!(a[4].as_str(), Some("x\ny"));
    assert_eq!(v.get("b").unwrap().get("c").unwrap().as_str(), Some("é"));
    // Deterministic, sorted-key, compact serialization.
    assert_eq!(
        v.to_string_compact(),
        r#"{"a":[1,2.5,true,null,"x\ny"],"b":{"c":"é","d":[]}}"#
    );
}

#[test]
fn json_surrogate_pairs_and_escapes() {
    let v = json::parse(r#""\uD83D\uDE00 ok""#).unwrap();
    assert_eq!(v.as_str(), Some("😀 ok"));
    assert!(
        json::parse(r#""\uD83Dlone""#).is_err(),
        "lone high surrogate must fail"
    );
    assert!(
        json::parse("\"\u{0001}\"").is_err(),
        "raw control byte must fail"
    );
}

#[test]
fn json_rejects_trailing_and_malformed() {
    assert!(json::parse("{} {}").is_err());
    assert!(json::parse("[1,]").is_err());
    assert!(json::parse("{\"a\":}").is_err());
    assert!(
        json::parse("01").is_err(),
        "leading zeros are not JSON numbers"
    );
    assert!(json::parse("1e").is_err());
    let err = json::parse("{\"a\": 1\"b\": 2}").unwrap_err();
    assert!(err.at > 0);
}

#[test]
fn json_numbers_serialize_like_encoding_json() {
    assert_eq!(Value::Num(3.0).to_string(), "3");
    assert_eq!(Value::Num(2.5).to_string(), "2.5");
    assert_eq!(Value::Num(-0.0).to_string(), "0");
    assert_eq!(Value::Num(1e21).as_f64(), Some(1e21));
}

#[test]
fn client_frames_decode() {
    let f = ClientFrame::decode(r#"{"type":"join","room":"room-1","participant":"p-7"}"#).unwrap();
    assert_eq!(
        f,
        ClientFrame::Join {
            room: RoomId("room-1".into()),
            participant: ParticipantId("p-7".into())
        }
    );

    let f = ClientFrame::decode(r#"{"type":"publish","track":"t-1","kind":"audio"}"#).unwrap();
    assert_eq!(
        f,
        ClientFrame::Publish {
            session: None,
            track: TrackId("t-1".into()),
            kind: MediaKind::Audio,
            ssrc: None
        }
    );

    match ClientFrame::decode(r#"{"type":"publish","track":"t-1","kind":"smell"}"#) {
        Err(DecodeError::Malformed(_)) => {}
        other => panic!("unknown kind must be malformed, not silent: {other:?}"),
    }
    match ClientFrame::decode(r#"{"type":"quantum.entangle"}"#) {
        Err(DecodeError::UnknownType(t)) => assert_eq!(t, "quantum.entangle"),
        other => panic!("unknown types must be distinguishable: {other:?}"),
    }
    assert!(ClientFrame::decode("not json").is_err());
    assert!(
        ClientFrame::decode(r#"{"type":"join","room":"r"}"#).is_err(),
        "missing participant"
    );
}

#[test]
fn server_frames_encode_exact_wire_shapes() {
    let f = ServerFrame::Ready {
        session: MediaSessionId("s-1".into()),
        participant: ParticipantId("p-1".into()),
        room: RoomId("r-1".into()),
        ice_ufrag: "abcd".into(),
        ice_pwd: "efgh".into(),
    };
    assert_eq!(
        f.encode(),
        r#"{"ice_pwd":"efgh","ice_ufrag":"abcd","participant":"p-1","room":"r-1","session":"s-1","type":"ready"}"#
    );

    let f = ServerFrame::TrackPublished {
        room: RoomId("r-1".into()),
        participant: ParticipantId("p-2".into()),
        track: TrackId("t-9".into()),
        kind: MediaKind::Video,
    };
    assert_eq!(
        f.encode(),
        r#"{"kind":"video","participant":"p-2","room":"r-1","track":"t-9","type":"track.published"}"#
    );

    let f = ServerFrame::Error {
        code: ErrorCode::WrongState,
        message: "offer already answered".into(),
    };
    assert_eq!(
        f.encode(),
        r#"{"code":"wrong_state","message":"offer already answered","type":"error"}"#
    );
}

#[test]
fn error_code_vocabulary_is_closed() {
    for code in [
        ErrorCode::BadMessage,
        ErrorCode::AuthFailed,
        ErrorCode::RoomUnknown,
        ErrorCode::TrackUnknown,
        ErrorCode::OverLimit,
        ErrorCode::WrongState,
        ErrorCode::ServerBusy,
    ] {
        assert_eq!(ErrorCode::parse(code.as_str()), Some(code));
    }
    assert_eq!(ErrorCode::parse("teapot"), None);
}
