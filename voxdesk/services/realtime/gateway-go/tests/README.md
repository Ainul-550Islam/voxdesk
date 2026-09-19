# gateway-go/tests

Cross-package tests that do not belong to any single `internal/` package.
Package-internal unit and e2e tests stay next to their packages (Go
convention); only tests whose subject IS the seam between packages live
here.

| Directory    | What it pins                                                                 | Runs with `go test ./...`? |
|--------------|------------------------------------------------------------------------------|-----------------------------|
| `protocol/`  | Byte-exact wire format goldens (`.golden` files). A diff here is a **wire-format change**: regenerate only deliberately with `go test ./tests/protocol -update` and review the golden diff in code review. | yes |
| `integration/` | The assembled system across package boundaries (config → auth → hub → server → protocol → presence → observability), including the X-Request-ID trace continuing API → gateway → event stream. | yes |
| `load/`      | Fan-out smoke harness (hundreds of sockets, tens of thousands of frames). Behind the `load` build tag: `go test -tags load ./tests/load/ -v -timeout 120s`. | **no** — timing-sensitive by nature |

Golden files live in `protocol/testdata/` — one file per server frame, so a
format change shows up as a one-line diff next to the struct change that
caused it.
