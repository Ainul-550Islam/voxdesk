#!/bin/bash
# restore-tooling.sh — the sandbox's snapshot prunes installed toolchains
# (under .rustup/toolchains and ~/toolchains) between turns. This script is
# idempotent: run it whenever `cargo` or `go` answers "command not found".
# It restores ONLY the toolchains, never project state.
set -u

# --- Rust (rustup stable, minimal profile) -------------------------------
RUST_HOME="$HOME/.rustup"
mkdir -p "$RUST_HOME"
if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  chmod +x "$HOME/.cargo/bin/rustup" 2>/dev/null
  export PATH="$HOME/.cargo/bin:$PATH"
  if ! rustup toolchain list 2>/dev/null | grep -q stable; then
    rustup toolchain install stable --profile minimal
  fi
  rustup default stable >/dev/null
fi
export PATH="$HOME/.cargo/bin:$PATH"

# --- Go 1.27.1 -------------------------------------------------------------
GO_ROOT="$HOME/toolchains/go"
if [ ! -x "$GO_ROOT/bin/go" ]; then
  mkdir -p "$HOME/toolchains"
  tmp=$(mktemp -d)
  curl -sL -o "$tmp/go.tgz" https://go.dev/dl/go1.27.1.linux-amd64.tar.gz
  tar -C "$HOME/toolchains" -xzf "$tmp/go.tgz"
  rm -rf "$tmp"
fi
export GOROOT="$GO_ROOT"
export GOPATH="$HOME/go-work"
export PATH="$GO_ROOT/bin:$PATH"

echo "rust: $(cargo --version 2>&1) | go: $(go version 2>&1)"
