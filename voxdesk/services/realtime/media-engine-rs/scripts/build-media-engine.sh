#!/bin/sh
# Build the media-engine-rs binary for the Go gateway's live integration
# test (internal/engineclient/it_live_test.go, tag `it`):
#
#   ./scripts/build-media-engine.sh
#   cd ../gateway-go
#   VOXDESK_IT_ENGINE=1 \
#   VOXDESK_IT_ENGINE_BIN=../media-engine-rs/target/debug/media-engine \
#   go test -tags it -run TestLive ./internal/engineclient
set -eu
cd "$(dirname "$0")/.."
cargo build -p media-engine
echo "built: $(pwd)/target/debug/media-engine"
