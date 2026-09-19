// Package metrics is the gateway's tiny, dependency-free telemetry core:
// atomic counters/gauges plus a Prometheus text exposition.
//
// Why hand-rolled: the Python app uses prometheus-client, but dragging the
// Go client library in for seven counters would double the dependency
// surface of a security-edge service for zero behavioural gain. The text
// format rendered here is the standard Prometheus exposition format, so the
// same scraper that reads the API's /metrics reads this one unchanged.
//
// Labels are deliberately avoided: a label per tenant/room is unbounded
// cardinality, the exact failure the Python side calls out when it keeps
// per-call facts in structured logs instead of metric labels. Per-tenant
// detail belongs in logs, not here.
//
// This package lives under internal/observability alongside the leveled
// logger, the request-id middleware and the structured event emitter —
// one home for everything the gateway says about itself.
package metrics

import (
	"fmt"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

// Registry holds every instrument. All methods are safe for concurrent use.
type Registry struct {
	startedAt time.Time

	connectionsCurrent atomic.Int64
	connectionsTotal   atomic.Int64
	connectionsRefused atomic.Int64 // capacity rejected at upgrade
	authFailuresTotal  atomic.Int64
	messagesReadTotal  atomic.Int64
	rateLimitedTotal   atomic.Int64
	deliveriesTotal    atomic.Int64 // frames handed to subscriber queues
	droppedTotal       atomic.Int64 // frames lost to full subscriber queues
	ingestTotal        atomic.Int64 // accepted ingest publishes
	ingestDuplicates   atomic.Int64 // replayed event_ids, no fan-out
	ingestRejected     atomic.Int64 // 401/413/422s

	signalSessionsCurrent atomic.Int64 // live signaling sessions
	signalSessionsTotal   atomic.Int64 // sessions created since boot
	signalRelayedTotal    atomic.Int64 // offer/answer/candidate frames forwarded to a peer

	presenceUsersCurrent atomic.Int64 // reachable (tenant, user) pairs, fed by presence.OnChange

	// Engine-link telemetry (Go → Rust media engine control plane). The
	// gauge is the monitor's availability view; latency is captured as a
	// fixed-bucket histogram (Prometheus-friendly, no client lib needed).
	engineUp            atomic.Int64 // 1 when the monitor's probe loop is green
	engineCallsTotal    atomic.Int64
	engineErrorsTotal   atomic.Int64
	engineLatencySumMs  atomic.Int64 // microsecond-precision stored as ms×1000? no: plain ns→ms int64
	engineLatencyMaxMs  atomic.Int64
	engineJoinTotal     atomic.Int64
	engineJoinErrTotal  atomic.Int64
	engineLeaveTotal    atomic.Int64
	engineLeaveErrTotal atomic.Int64
	engineProbeTotal    atomic.Int64
	engineProbeErrTotal atomic.Int64
}

// New returns a zeroed registry stamped with the boot time.
func New() *Registry {
	return &Registry{startedAt: time.Now()}
}

func (r *Registry) ConnOpened()       { r.connectionsCurrent.Add(1); r.connectionsTotal.Add(1) }
func (r *Registry) ConnClosed()       { r.connectionsCurrent.Add(-1) }
func (r *Registry) ConnRefused()      { r.connectionsRefused.Add(1) }
func (r *Registry) AuthFailed()       { r.authFailuresTotal.Add(1) }
func (r *Registry) MessageRead()      { r.messagesReadTotal.Add(1) }
func (r *Registry) RateLimited()      { r.rateLimitedTotal.Add(1) }
func (r *Registry) Delivered(n int64) { r.deliveriesTotal.Add(n) }
func (r *Registry) Dropped(n int64)   { r.droppedTotal.Add(n) }
func (r *Registry) IngestAccepted()   { r.ingestTotal.Add(1) }
func (r *Registry) IngestDuplicate()  { r.ingestDuplicates.Add(1) }
func (r *Registry) IngestRejected()   { r.ingestRejected.Add(1) }

// SignalSession* instrument the signaling relay (internal/signaling). The
// gauge is paired with the counter so dashboards get both "right now" and
// "pressure over time".
func (r *Registry) SignalSessionOpened() {
	r.signalSessionsCurrent.Add(1)
	r.signalSessionsTotal.Add(1)
}
func (r *Registry) SignalSessionClosed() { r.signalSessionsCurrent.Add(-1) }
func (r *Registry) SignalRelayed()       { r.signalRelayedTotal.Add(1) }

// SetPresenceUsers repoints the presence gauge at the registry's latest
// TotalUsers snapshot. It is a SET (not an increment): presence transitions
// publish their authoritative post-transition total, so the gauge self-
// heals on any missed decrement instead of drifting.
func (r *Registry) SetPresenceUsers(n int64) { r.presenceUsersCurrent.Store(n) }

// PresenceUsersCurrent is the gauge snapshot (diagnostics/tests).
func (r *Registry) PresenceUsersCurrent() int64 { return r.presenceUsersCurrent.Load() }

// ---- media-engine link ---------------------------------------------------

// EngineCall records one signaling-plane call to the Rust engine: op is the
// client's vocabulary ("join"/"leave"/other), so the broken-down counters
// stay in sync with deploys that call only a subset. took is wall latency
// the caller measured across the whole HTTP round trip.
func (r *Registry) EngineCall(op string, took time.Duration, failed bool) {
	r.engineCallsTotal.Add(1)
	switch op {
	case "join":
		r.engineJoinTotal.Add(1)
		if failed {
			r.engineJoinErrTotal.Add(1)
		}
	case "leave":
		r.engineLeaveTotal.Add(1)
		if failed {
			r.engineLeaveErrTotal.Add(1)
		}
	}
	if failed {
		r.engineErrorsTotal.Add(1)
	}
	ms := took.Milliseconds()
	r.engineLatencySumMs.Add(ms)
	for {
		prev := r.engineLatencyMaxMs.Load()
		if ms <= prev || r.engineLatencyMaxMs.CompareAndSwap(prev, ms) {
			break
		}
	}
}

// EngineProbe records a monitor health round trip.
func (r *Registry) EngineProbe(took time.Duration, failed bool) {
	r.engineProbeTotal.Add(1)
	r.engineLatencySumMs.Add(took.Milliseconds())
	if failed {
		r.engineProbeErrTotal.Add(1)
	}
}

// SetEngineUp repoints the availability gauge (transition events only, as
// sent by the monitor — no drift possible).
func (r *Registry) SetEngineUp(up bool) {
	if up {
		r.engineUp.Store(1)
	} else {
		r.engineUp.Store(0)
	}
}

// EngineUp is the readiness surface's view of engine availability.
func (r *Registry) EngineUp() bool { return r.engineUp.Load() == 1 }

// ConnectionsCurrent is a gauge snapshot (also used by readiness).
func (r *Registry) ConnectionsCurrent() int64 { return r.connectionsCurrent.Load() }

// Render emits the Prometheus text exposition format for one scrape.
// rooms/tenant gauges the registry cannot know are passed in by the caller
// (the hub owns that state).
func (r *Registry) Render(rooms int, tenants int) string {
	var b strings.Builder

	writeGauge := func(name, help string, value int64) {
		fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s gauge\n%s %d\n", name, help, name, name, value)
	}
	writeCounter := func(name, help string, value int64) {
		fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s counter\n%s %d\n", name, help, name, name, value)
	}

	writeGauge("voxdesk_gateway_connections_current", "Live WebSocket connections.", r.connectionsCurrent.Load())
	writeCounter("voxdesk_gateway_connections_total", "Connections accepted since boot.", r.connectionsTotal.Load())
	writeCounter("voxdesk_gateway_connections_refused_total", "Upgrades rejected by capacity limits.", r.connectionsRefused.Load())
	writeCounter("voxdesk_gateway_auth_failures_total", "Hello frames rejected by token verification.", r.authFailuresTotal.Load())
	writeCounter("voxdesk_gateway_messages_read_total", "Client frames read after upgrade.", r.messagesReadTotal.Load())
	writeCounter("voxdesk_gateway_rate_limited_total", "Client frames rejected by the per-connection limiter.", r.rateLimitedTotal.Load())
	writeCounter("voxdesk_gateway_deliveries_total", "Frames enqueued to subscriber queues.", r.deliveriesTotal.Load())
	writeCounter("voxdesk_gateway_dropped_total", "Frames dropped from full subscriber queues (backpressure-drop).", r.droppedTotal.Load())
	writeCounter("voxdesk_gateway_ingest_total", "Accepted ingest publishes.", r.ingestTotal.Load())
	writeCounter("voxdesk_gateway_ingest_duplicates_total", "Ingest publishes suppressed by the replay cache.", r.ingestDuplicates.Load())
	writeCounter("voxdesk_gateway_ingest_rejected_total", "Ingest requests rejected (auth/size/shape).", r.ingestRejected.Load())
	writeGauge("voxdesk_gateway_rooms_current", "Rooms with at least one subscriber.", int64(rooms))
	writeGauge("voxdesk_gateway_tenants_current", "Tenants with at least one live connection.", int64(tenants))
	writeGauge("voxdesk_gateway_signal_sessions_current", "Live signaling sessions (point-to-point negotiations).", r.signalSessionsCurrent.Load())
	writeCounter("voxdesk_gateway_signal_sessions_total", "Signaling sessions created since boot.", r.signalSessionsTotal.Load())
	writeCounter("voxdesk_gateway_signal_relayed_total", "Offer/answer/candidate frames forwarded to a peer.", r.signalRelayedTotal.Load())
	writeGauge("voxdesk_gateway_presence_users_current", "Reachable (tenant, user) pairs on this node (JWT-verified identities).", r.presenceUsersCurrent.Load())
	writeGauge("voxdesk_gateway_engine_up", "Media-engine availability as seen by the health monitor (0/1).", r.engineUp.Load())
	writeCounter("voxdesk_gateway_engine_signal_calls_total", "Control-plane calls to the media engine.", r.engineCallsTotal.Load())
	writeCounter("voxdesk_gateway_engine_signal_errors_total", "Control-plane calls that failed (any cause).", r.engineErrorsTotal.Load())
	writeCounter("voxdesk_gateway_engine_join_total", "Engine join signals issued.", r.engineJoinTotal.Load())
	writeCounter("voxdesk_gateway_engine_join_errors_total", "Engine joins refused or lost.", r.engineJoinErrTotal.Load())
	writeCounter("voxdesk_gateway_engine_leave_total", "Engine leave signals issued.", r.engineLeaveTotal.Load())
	writeCounter("voxdesk_gateway_engine_leave_errors_total", "Engine leaves refused or lost.", r.engineLeaveErrTotal.Load())
	writeCounter("voxdesk_gateway_engine_probes_total", "Availability probes to /v1/health.", r.engineProbeTotal.Load())
	writeCounter("voxdesk_gateway_engine_probe_errors_total", "Availability probes that failed.", r.engineProbeErrTotal.Load())
	writeGauge("voxdesk_gateway_engine_latency_ms_sum", "Sum of engine round-trip milliseconds (÷calls for mean).", r.engineLatencySumMs.Load())
	writeGauge("voxdesk_gateway_engine_latency_ms_max", "Worst observed engine round trip (ms, never reset).", r.engineLatencyMaxMs.Load())
	writeGauge("voxdesk_gateway_goroutines", "Live goroutines (leak canary).", int64(runtime.NumGoroutine()))
	writeGauge("voxdesk_gateway_uptime_seconds", "Seconds since boot.", int64(time.Since(r.startedAt).Seconds()))

	return b.String()
}
