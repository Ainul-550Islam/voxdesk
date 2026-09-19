//! Hub tests: tenant scoping, hello gating, room membership and delivery.

use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::MaybeTlsStream;

use voxdesk_signal::hub::Hub;
use voxdesk_signal::protocol::{ClientMessage, ServerMessage};

type TestSocket = tokio_tungstenite::WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;
type TestSink = futures_util::stream::SplitSink<TestSocket, Message>;
type TestStream = futures_util::stream::SplitStream<TestSocket>;

async fn spawn_hub() -> (Hub, String) {
    let hub = Hub::new();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let h = hub.clone();
    tokio::spawn(async move {
        let _ = h.serve(listener).await;
    });
    (hub, addr)
}

async fn connect(addr: &str) -> (TestSink, TestStream) {
    let (ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}"))
        .await
        .unwrap();
    ws.split()
}

async fn recv_text(
    rx: &mut (impl StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin),
) -> ServerMessage {
    loop {
        let msg = rx.next().await.unwrap().unwrap();
        if let Message::Text(text) = msg {
            return serde_json::from_str(&text).unwrap();
        }
    }
}

async fn send_json(tx: &mut TestSink, message: &ClientMessage) {
    let text = serde_json::to_string(message).unwrap();
    let _ = tx.send(Message::Text(text.into())).await;
}

#[tokio::test]
async fn publish_requires_hello() {
    let (hub, addr) = spawn_hub().await;
    let (mut tx, mut rx) = connect(&addr).await;
    let _ = recv_text(&mut rx).await; // Welcome
    send_json(
        &mut tx,
        &ClientMessage::Publish {
            room: "room-a".into(),
            payload: serde_json::json!({}),
        },
    )
    .await;
    match recv_text(&mut rx).await {
        ServerMessage::Error { code, .. } => assert_eq!(code, "hello_required"),
        other => panic!("expected error, got {other:?}"),
    }
    let _ = hub;
}

#[tokio::test]
async fn two_peers_in_same_room_deliver() {
    let (_hub, addr) = spawn_hub().await;
    let (mut a_tx, mut a_rx) = connect(&addr).await;
    let (mut b_tx, mut b_rx) = connect(&addr).await;

    for tx in [&mut a_tx, &mut b_tx] {
        send_json(
            tx,
            &ClientMessage::Hello {
                tenant_id: "tenant-1".into(),
            },
        )
        .await;
    }
    // Drain the welcome message.
    let _ = recv_text(&mut a_rx).await;
    let _ = recv_text(&mut b_rx).await;

    send_json(
        &mut a_tx,
        &ClientMessage::Subscribe {
            room: "room-x".into(),
        },
    )
    .await;
    send_json(
        &mut b_tx,
        &ClientMessage::Subscribe {
            room: "room-x".into(),
        },
    )
    .await;
    let _ = recv_text(&mut a_rx).await; // Subscribed
    let _ = recv_text(&mut b_rx).await; // Subscribed

    send_json(
        &mut a_tx,
        &ClientMessage::Publish {
            room: "room-x".into(),
            payload: serde_json::json!({"hello": "world"}),
        },
    )
    .await;

    match recv_text(&mut b_rx).await {
        ServerMessage::Delivery { room, payload, .. } => {
            assert_eq!(room, "room-x");
            assert_eq!(payload, serde_json::json!({"hello": "world"}));
        }
        other => panic!("expected delivery, got {other:?}"),
    }
}

#[tokio::test]
async fn rooms_are_tenant_scoped() {
    let (_hub, addr) = spawn_hub().await;
    let (mut a_tx, mut a_rx) = connect(&addr).await;
    let (mut b_tx, mut b_rx) = connect(&addr).await;

    send_json(
        &mut a_tx,
        &ClientMessage::Hello {
            tenant_id: "tenant-a".into(),
        },
    )
    .await;
    send_json(
        &mut b_tx,
        &ClientMessage::Hello {
            tenant_id: "tenant-b".into(),
        },
    )
    .await;
    let _ = recv_text(&mut a_rx).await;
    let _ = recv_text(&mut b_rx).await;

    send_json(
        &mut a_tx,
        &ClientMessage::Subscribe {
            room: "room-x".into(),
        },
    )
    .await;
    send_json(
        &mut b_tx,
        &ClientMessage::Subscribe {
            room: "room-x".into(),
        },
    )
    .await;
    let _ = recv_text(&mut a_rx).await;
    let _ = recv_text(&mut b_rx).await;

    // a publishes into tenant-a/room-x; b (tenant-b/room-x) must not receive it.
    send_json(
        &mut a_tx,
        &ClientMessage::Publish {
            room: "room-x".into(),
            payload: serde_json::json!({"secret": true}),
        },
    )
    .await;

    // Give any (illegal) cross-tenant delivery a moment to arrive, then assert
    // the b side only ever sees its own subscription ack.
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    let next = tokio::time::timeout(std::time::Duration::from_millis(200), b_rx.next())
        .await
        .ok();
    assert!(next.is_none(), "cross-tenant delivery must not happen");
}
