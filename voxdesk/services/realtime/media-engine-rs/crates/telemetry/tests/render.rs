use std::collections::BTreeMap;
use telemetry::Registry;

#[test]
fn counters_gauges_and_exposition_shape() {
    let reg = Registry::new();
    Registry::inc(&reg.packets_received);
    Registry::add(&reg.packets_received, 4);
    Registry::add(&reg.bytes_received, 1200);
    Registry::set(&reg.rooms_current, 3);
    Registry::inc(&reg.ice_pairs_selected);

    let mut extra = BTreeMap::new();
    extra.insert("voxdesk_media_udp_sockets_current".to_string(), 2);
    let text = reg.render(&extra);

    for line in [
        "# TYPE voxdesk_media_packets_received_total counter",
        "voxdesk_media_packets_received_total 5",
        "voxdesk_media_bytes_received_total 1200",
        "voxdesk_media_rooms_current 3",
        "voxdesk_media_ice_pairs_selected_total 1",
        "voxdesk_media_udp_sockets_current 2",
        "# TYPE voxdesk_media_participants_current gauge",
    ] {
        assert!(text.contains(line), "exposition missing {line:?}:\n{text}");
    }
    // HELP immediately precedes TYPE for one of the counters.
    let adja = "# HELP voxdesk_media_packets_forwarded_total Media datagrams enqueued to a subscriber leg.\n# TYPE voxdesk_media_packets_forwarded_total counter\n";
    assert!(text.contains(adja), "HELP/TYPE adjacency broken:\n{text}");
}

#[test]
fn gauges_tuple_and_default() {
    let reg = Registry::default();
    Registry::set(&reg.rooms_current, 2);
    Registry::set(&reg.participants_current, 7);
    Registry::set(&reg.tracks_current, 9);
    assert_eq!(reg.gauges(), (2, 7, 9));
}
