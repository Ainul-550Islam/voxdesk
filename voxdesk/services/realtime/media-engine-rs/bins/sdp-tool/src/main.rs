//! sdp-tool — the SFU dev's SDP sidekick:
//!
//! * `sdp-tool answer --offer FILE [--ip A.B.C.D --port N --fingerprint "AA:BB:.."]`
//!   parses the offer and prints the answer the SFU would emit (same code
//!   path the signaling core exercises — debugging a UA's echo starts here).
//! * `sdp-tool parse --offer FILE` shows the parsed anatomy for a human.
//! * `sdp-tool fingerprint TEXT` prints the sha-256 fingerprint label.

fn usage() -> ! {
    eprintln!(
        "sdp-tool <command>:
  answer --offer FILE [--ip A.B.C.D --port N --fingerprint HEX:PAIRS]
  parse  --offer FILE
  fingerprint TEXT"
    );
    std::process::exit(2);
}

fn flag_value(args: &[String], name: &str) -> Option<String> {
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == name {
            return it.next().cloned();
        }
    }
    None
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("answer") => answer(&args[1..]),
        Some("parse") => parse(&args[1..]),
        Some("fingerprint") => {
            let Some(text) = args.get(1) else { usage() };
            println!("{}", livekit::sha256_fingerprint_label(text));
        }
        _ => usage(),
    }
}

fn answer(args: &[String]) {
    let offer_text = read_offer(args);
    let ip = flag_value(args, "--ip")
        .map(|s| webrtc::ice::parse_ipv4(&s).expect("bad --ip"))
        .unwrap_or([203, 0, 113, 1]);
    let port: u16 = flag_value(args, "--port")
        .map(|s| s.parse().expect("--port u16"))
        .unwrap_or(5000);
    let fingerprint = flag_value(args, "--fingerprint")
        .unwrap_or_else(|| livekit::sha256_fingerprint_label("voxdesk-sdp-tool"));

    let offer = webrtc::sdp::parse_offer(&offer_text).unwrap_or_else(|e| {
        eprintln!("offer rejected: {e:?}");
        std::process::exit(1);
    });
    let answer = webrtc::sdp::build_answer(
        &offer,
        &webrtc::sdp::AnswerContext {
            local_ufrag: "sdpqfx".into(),
            local_pwd: "devPwd24charsLongEnough0!".into(),
            fingerprint_sha256: fingerprint,
            public_ip: ip,
            public_port: port,
            external_ip_label: "sdp-tool".into(),
        },
    );
    print!("{answer}");
}

fn parse(args: &[String]) {
    let offer_text = read_offer(args);
    match webrtc::sdp::parse_offer(&offer_text) {
        Ok(offer) => {
            println!("session ufrag: {:?}", offer.session_ufrag);
            println!("session pwd:   {:?}", offer.session_pwd);
            println!("fingerprint:   {:?}", offer.session_fingerprint);
            for (i, m) in offer.media.iter().enumerate() {
                println!(
                    "m[{i}] kind={} transport={} mid={}",
                    m.kind, m.transport, m.mid
                );
                println!("      formats: {:?}", m.formats);
                println!(
                    "      rtcp_mux={} direction={:?} setup={:?}",
                    m.rtcp_mux, m.direction, m.setup
                );
                println!("      candidates: {}", m.candidates.len());
                for c in &m.candidates {
                    println!(
                        "        - {} typ={} prio={} {}.{}.{}.{}:{}",
                        c.protocol, c.typ, c.priority, c.ip[0], c.ip[1], c.ip[2], c.ip[3], c.port
                    );
                }
                for (pt, codec) in &m.rtpmap {
                    println!("        rtpmap {pt} {codec}");
                }
            }
        }
        Err(e) => {
            eprintln!("parse error: {e:?}");
            std::process::exit(1);
        }
    }
}

fn read_offer(args: &[String]) -> String {
    let Some(path) = flag_value(args, "--offer") else {
        usage()
    };
    std::fs::read_to_string(&path).unwrap_or_else(|e| {
        eprintln!("read {path}: {e}");
        std::process::exit(1);
    })
}
