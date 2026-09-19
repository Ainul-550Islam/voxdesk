//! A small, correct JSON subset codec: the media plane's control channel
//! speaks JSON (frames to/from the signaling edge, LiveKit-compatible
//! grants, telemetry payloads), and hand-rolling keeps the workspace
//! dependency-free. This is not a general-purpose JSON library — it
//! implements exactly RFC 8259's data model with strict UTF-8 handling via
//! Rust strings (we parse from &str, so invalid UTF-8 is impossible by
//! construction) and conservative number handling (f64, matching the
//! gateway-go side's encoding/json semantics).

use std::collections::BTreeMap;
use std::fmt;

/// A JSON value. Object keys are kept in a BTreeMap so serialization is
/// deterministic (golden tests, wire stability) rather than hash-order.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(BTreeMap<String, Value>),
}

impl Value {
    pub fn obj() -> Value {
        Value::Obj(BTreeMap::new())
    }

    /// Inserts a key into an object value; panics on a non-object —
    /// builders construct one shape, so a type error here is a bug, not a
    /// runtime case.
    pub fn set(&mut self, key: &str, v: Value) {
        match self {
            Value::Obj(map) => {
                map.insert(key.to_string(), v);
            }
            other => panic!("json set on non-object: {:?}", kind_of(other)),
        }
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(map) => map.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Num(n) => Some(*n),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Num(n) if n.fract() == 0.0 && n.abs() <= i64::MAX as f64 => Some(*n as i64),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        self.as_i64().and_then(|n| u64::try_from(n).ok())
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_arr(&self) -> Option<&[Value]> {
        match self {
            Value::Arr(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_obj(&self) -> Option<&BTreeMap<String, Value>> {
        match self {
            Value::Obj(map) => Some(map),
            _ => None,
        }
    }

    /// Serializes compactly (no whitespace) — the wire form.
    pub fn to_string_compact(&self) -> String {
        let mut out = String::new();
        write_value(&mut out, self);
        out
    }
}

fn kind_of(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Num(_) => "number",
        Value::Str(_) => "string",
        Value::Arr(_) => "array",
        Value::Obj(_) => "object",
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_string_compact())
    }
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Num(n) => write_num(out, *n),
        Value::Str(s) => write_str(out, s),
        Value::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Obj(map) => {
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, k);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

/// Numbers serialize like encoding/json's common case: integers without a
/// fraction print without one ("3", not "3.0"), non-finite values must
/// never reach the wire (callers validate payloads), everything else uses
/// Rust's shortest-round-trip float formatting.
fn write_num(out: &mut String, n: f64) {
    debug_assert!(n.is_finite(), "non-finite JSON number on the wire");
    if n.fract() == 0.0 && n.abs() < 9.0e15 {
        out.push_str(&format!("{}", n as i64));
    } else {
        out.push_str(&format!("{}", n));
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Parse one JSON document; trailing non-whitespace is an error.
pub fn parse(input: &str) -> Result<Value, ParseError> {
    let mut p = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(p.err("trailing characters"));
    }
    Ok(v)
}

/// A parse failure with byte position — callers map it to a 400-class
/// refusal, never a panic.
#[derive(Clone, Debug, PartialEq)]
pub struct ParseError {
    pub at: usize,
    pub message: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "json parse error at byte {}: {}", self.at, self.message)
    }
}

impl std::error::Error for ParseError {}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl Parser<'_> {
    fn err(&self, message: &str) -> ParseError {
        ParseError {
            at: self.pos,
            message: message.to_string(),
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn bump(&mut self) -> Option<u8> {
        let b = self.peek();
        if b.is_some() {
            self.pos += 1;
        }
        b
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, b: u8) -> Result<(), ParseError> {
        if self.bump() == Some(b) {
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", b as char)))
        }
    }

    fn parse_value(&mut self) -> Result<Value, ParseError> {
        match self.peek() {
            Some(b'n') => self.parse_lit("null", Value::Null),
            Some(b't') => self.parse_lit("true", Value::Bool(true)),
            Some(b'f') => self.parse_lit("false", Value::Bool(false)),
            Some(b'"') => self.parse_string().map(Value::Str),
            Some(b'[') => self.parse_array(),
            Some(b'{') => self.parse_object(),
            Some(b'-' | b'0'..=b'9') => self.parse_number(),
            _ => Err(self.err("expected a JSON value")),
        }
    }

    fn parse_lit(&mut self, lit: &str, value: Value) -> Result<Value, ParseError> {
        for &want in lit.as_bytes() {
            if self.bump() != Some(want) {
                return Err(self.err(&format!("expected '{}'", lit)));
            }
        }
        Ok(value)
    }

    /// Strings: parses escapes INCLUDING \uXXXX pairs (surrogate pairs
    /// joined) — a codec that silently drops escape handling corrupts
    /// agent names and error messages, which is exactly where they'd show.
    fn parse_string(&mut self) -> Result<String, ParseError> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            match self.bump() {
                None => return Err(self.err("unterminated string")),
                Some(b'"') => return Ok(out),
                Some(b'\\') => match self.bump() {
                    Some(b'"') => out.push('"'),
                    Some(b'\\') => out.push('\\'),
                    Some(b'/') => out.push('/'),
                    Some(b'b') => out.push('\u{08}'),
                    Some(b'f') => out.push('\u{0c}'),
                    Some(b'n') => out.push('\n'),
                    Some(b'r') => out.push('\r'),
                    Some(b't') => out.push('\t'),
                    Some(b'u') => {
                        let hi = self.parse_hex4()?;
                        if (0xD800..0xDC00).contains(&hi) {
                            // High surrogate: REQUIRE the low half.
                            if self.bump() != Some(b'\\') || self.bump() != Some(b'u') {
                                return Err(self.err("lone high surrogate"));
                            }
                            let lo = self.parse_hex4()?;
                            if !(0xDC00..0xE000).contains(&lo) {
                                return Err(self.err("invalid low surrogate"));
                            }
                            let combined =
                                0x10000 + (((hi - 0xD800) as u32) << 10) + (lo - 0xDC00) as u32;
                            out.push(
                                char::from_u32(combined)
                                    .ok_or_else(|| self.err("surrogate pair out of range"))?,
                            );
                        } else if (0xDC00..0xE000).contains(&hi) {
                            return Err(self.err("lone low surrogate"));
                        } else {
                            out.push(char::from_u32(hi as u32).unwrap_or('\u{FFFD}'));
                        }
                    }
                    _ => return Err(self.err("invalid escape")),
                },
                Some(b) if b < 0x20 => return Err(self.err("control byte in string")),
                Some(b) => {
                    // Input is &str, so ASCII bytes are whole chars and a
                    // >=0x80 byte starts a multi-byte char: decode it from the
                    // original slice for correctness.
                    if b < 0x80 {
                        out.push(b as char);
                    } else {
                        let start = self.pos - 1;
                        let ch = std::str::from_utf8(&self.bytes[start..])
                            .ok()
                            .and_then(|s| s.chars().next())
                            .ok_or_else(|| self.err("invalid utf-8 in string"))?;
                        out.push(ch);
                        self.pos = start + ch.len_utf8();
                    }
                }
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u16, ParseError> {
        let mut v: u16 = 0;
        for _ in 0..4 {
            let b = self.bump().ok_or_else(|| self.err("eof in \\u escape"))?;
            let d = match b {
                b'0'..=b'9' => b - b'0',
                b'a'..=b'f' => b - b'a' + 10,
                b'A'..=b'F' => b - b'A' + 10,
                _ => return Err(self.err("bad hex digit in \\u escape")),
            };
            v = (v << 4) | d as u16;
        }
        Ok(v)
    }

    fn parse_array(&mut self) -> Result<Value, ParseError> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.parse_value()?);
            self.skip_ws();
            match self.bump() {
                Some(b',') => continue,
                Some(b']') => return Ok(Value::Arr(items)),
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
    }

    fn parse_object(&mut self) -> Result<Value, ParseError> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Obj(map));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            self.expect(b':')?;
            self.skip_ws();
            map.insert(key, self.parse_value()?);
            self.skip_ws();
            match self.bump() {
                Some(b',') => continue,
                Some(b'}') => return Ok(Value::Obj(map)),
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
    }

    fn parse_number(&mut self) -> Result<Value, ParseError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(self.err("bad number")),
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.err("fraction needs a digit"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.err("exponent needs a digit"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("number not utf-8"))?;
        text.parse::<f64>()
            .map(Value::Num)
            .map_err(|_| self.err("number out of range"))
    }
}
