//! `signal-server` — the signaling hub as a standalone binary.
//!
//! Binds `0.0.0.0:VOXDESK_SIGNAL_PORT` (default 8765) and serves until the
//! process is terminated.

use std::net::SocketAddr;

use tokio::net::TcpListener;

use voxdesk_signal::hub::Hub;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let port: u16 = std::env::var("VOXDESK_SIGNAL_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(8765);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = TcpListener::bind(addr).await?;
    eprintln!("signal-server listening on {addr}");

    let hub = Hub::new();

    // Graceful shutdown on SIGINT/SIGTERM.
    tokio::spawn(async {
        let _ = tokio::signal::ctrl_c().await;
        std::process::exit(0);
    });

    hub.serve(listener).await?;
    Ok(())
}
