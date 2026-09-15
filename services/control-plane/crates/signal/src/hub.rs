//! The signaling hub: connection lifecycle, tenant scoping, room membership
//! and fan-out delivery.

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex};
use tokio_tungstenite::tungstenite::Message;
use uuid::Uuid;

use crate::protocol::{ClientMessage, ServerMessage};

/// A room is keyed by (tenant_id, room) — tenant scoping is structural, not
/// a check that can be forgotten.
type RoomKey = (String, String);
type SessionId = String;

struct Room {
    peers: HashSet<SessionId>,
}

struct SessionMeta {
    tenant_id: Option<String>,
    rooms: HashSet<String>,
    outgoing: mpsc::Sender<ServerMessage>,
}

#[derive(Default)]
struct HubState {
    rooms: HashMap<RoomKey, Room>,
    sessions: HashMap<SessionId, SessionMeta>,
}

/// Cloneable handle to the shared hub state.
#[derive(Clone, Default)]
pub struct Hub {
    state: Arc<Mutex<HubState>>,
}

impl Hub {
    pub fn new() -> Self {
        Hub {
            state: Arc::new(Mutex::new(HubState::default())),
        }
    }

    /// Accept connections until the listener is closed. Each connection is
    /// handled by its own task.
    pub async fn serve(&self, listener: TcpListener) -> std::io::Result<()> {
        loop {
            let (stream, addr) = listener.accept().await?;
            let hub = self.clone();
            tokio::spawn(async move {
                if let Err(err) = hub.handle_connection(stream, addr).await {
                    tracing_lite(addr, &err.to_string());
                }
            });
        }
    }

    async fn handle_connection(
        &self,
        stream: TcpStream,
        addr: SocketAddr,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        tracing_lite(addr, "connected");
        let ws = tokio_tungstenite::accept_async(stream).await?;
        let (mut sender, mut receiver) = ws.split();

        let session_id = Uuid::new_v4().to_string();
        let (tx, mut rx) = mpsc::channel::<ServerMessage>(64);

        {
            let mut state = self.state.lock().await;
            state.sessions.insert(
                session_id.clone(),
                SessionMeta {
                    tenant_id: None,
                    rooms: HashSet::new(),
                    outgoing: tx.clone(),
                },
            );
        }
        let _ = tx
            .send(ServerMessage::Welcome {
                session_id: session_id.clone(),
            })
            .await;

        loop {
            tokio::select! {
                outgoing = rx.recv() => {
                    match outgoing {
                        Some(message) => {
                            let text = serde_json::to_string(&message).unwrap_or_default();
                            if sender.send(Message::Text(text.into())).await.is_err() {
                                break;
                            }
                        }
                        None => break,
                    }
                }
                incoming = receiver.next() => {
                    match incoming {
                        Some(Ok(Message::Text(text))) => {
                            if let Ok(message) = serde_json::from_str::<ClientMessage>(&text) {
                                self.dispatch(&session_id, message).await;
                            } else {
                                let _ = tx.send(ServerMessage::Error {
                                    code: "bad_message".into(),
                                    message: "unrecognized message shape".into(),
                                }).await;
                            }
                        }
                        Some(Ok(Message::Ping(_))) => {
                            let _ = sender
                                .send(Message::Pong(tokio_tungstenite::tungstenite::Bytes::new()))
                                .await;
                        }
                        Some(Ok(Message::Close(_))) | None => break,
                        Some(Ok(_)) => {}
                        Some(Err(_)) => break,
                    }
                }
                _ = tokio::time::sleep(Duration::from_secs(60)) => {
                    // Idle timeout: no traffic for 60s reaps the connection.
                    break;
                }
            }
        }

        self.remove_session(&session_id).await;
        Ok(())
    }

    async fn dispatch(&self, session_id: &str, message: ClientMessage) {
        match message {
            ClientMessage::Hello { tenant_id } => self.hello(session_id, tenant_id).await,
            ClientMessage::Subscribe { room } => self.subscribe(session_id, room).await,
            ClientMessage::Unsubscribe { room } => self.unsubscribe(session_id, room).await,
            ClientMessage::Publish { room, payload } => {
                self.publish(session_id, room, payload).await
            }
            ClientMessage::Ping => {
                self.send(session_id, ServerMessage::Pong).await;
            }
        }
    }

    async fn hello(&self, session_id: &str, tenant_id: String) {
        let mut state = self.state.lock().await;
        let Some(meta) = state.sessions.get_mut(session_id) else {
            return;
        };
        if meta.tenant_id.is_some() || tenant_id.trim().is_empty() {
            let _ = meta.outgoing.try_send(ServerMessage::Error {
                code: "hello_invalid".into(),
                message: "hello may be sent exactly once with a non-empty tenant_id".into(),
            });
            return;
        }
        meta.tenant_id = Some(tenant_id);
    }

    async fn subscribe(&self, session_id: &str, room: String) {
        let mut state = self.state.lock().await;
        // Read tenant + record membership while the session borrow is live.
        let (tenant_id, outgoing) = {
            let Some(meta) = state.sessions.get_mut(session_id) else {
                return;
            };
            let Some(tenant_id) = meta.tenant_id.clone() else {
                let _ = meta.outgoing.try_send(ServerMessage::Error {
                    code: "hello_required".into(),
                    message: "send hello before subscribing".into(),
                });
                return;
            };
            meta.rooms.insert(room.clone());
            (tenant_id, meta.outgoing.clone())
        };
        // Session borrow is released; mutate rooms freely.
        let room_state = state
            .rooms
            .entry((tenant_id, room.clone()))
            .or_insert_with(|| Room {
                peers: HashSet::new(),
            });
        room_state.peers.insert(session_id.to_string());
        let count = room_state.peers.len();
        let _ = outgoing.try_send(ServerMessage::Subscribed { room, peers: count });
    }

    async fn unsubscribe(&self, session_id: &str, room: String) {
        let mut state = self.state.lock().await;
        let (tenant_id, outgoing) = {
            let Some(meta) = state.sessions.get_mut(session_id) else {
                return;
            };
            let Some(tenant_id) = meta.tenant_id.clone() else {
                return;
            };
            meta.rooms.remove(&room);
            (tenant_id, meta.outgoing.clone())
        };
        let should_remove =
            if let Some(room_state) = state.rooms.get_mut(&(tenant_id.clone(), room.clone())) {
                room_state.peers.remove(session_id);
                room_state.peers.is_empty()
            } else {
                false
            };
        if should_remove {
            state.rooms.remove(&(tenant_id.clone(), room.clone()));
        }
        let _ = outgoing.try_send(ServerMessage::Unsubscribed { room });
    }

    async fn publish(&self, session_id: &str, room: String, payload: serde_json::Value) {
        let state = self.state.lock().await;
        let tenant_id = {
            let Some(meta) = state.sessions.get(session_id) else {
                return;
            };
            let Some(tenant_id) = meta.tenant_id.clone() else {
                let _ = meta.outgoing.try_send(ServerMessage::Error {
                    code: "hello_required".into(),
                    message: "send hello before publishing".into(),
                });
                return;
            };
            tenant_id
        };
        let Some(room_state) = state.rooms.get(&(tenant_id, room.clone())) else {
            return;
        };
        let peers: Vec<String> = room_state
            .peers
            .iter()
            .filter(|peer| peer.as_str() != session_id)
            .cloned()
            .collect();
        for peer in peers {
            if let Some(peer_meta) = state.sessions.get(&peer) {
                let _ = peer_meta.outgoing.try_send(ServerMessage::Delivery {
                    room: room.clone(),
                    from: session_id.to_string(),
                    payload: payload.clone(),
                });
            }
        }
    }

    async fn send(&self, session_id: &str, message: ServerMessage) {
        let state = self.state.lock().await;
        if let Some(meta) = state.sessions.get(session_id) {
            let _ = meta.outgoing.try_send(message);
        }
    }

    async fn remove_session(&self, session_id: &str) {
        let mut state = self.state.lock().await;
        let Some(meta) = state.sessions.remove(session_id) else {
            return;
        };
        let Some(tenant_id) = meta.tenant_id else {
            return;
        };
        for room in meta.rooms {
            let key = (tenant_id.clone(), room.clone());
            let should_remove = if let Some(room_state) = state.rooms.get_mut(&key) {
                room_state.peers.remove(session_id);
                room_state.peers.is_empty()
            } else {
                false
            };
            if should_remove {
                state.rooms.remove(&key);
            }
        }
    }

    /// Number of live sessions (useful for tests and health checks).
    pub async fn session_count(&self) -> usize {
        self.state.lock().await.sessions.len()
    }
}

fn tracing_lite(addr: SocketAddr, message: &str) {
    eprintln!("[signal] {addr}: {message}");
}
