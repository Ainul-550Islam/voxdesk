package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// ingestTopic is THE fan-out topic: every accepted publish (this node's
// API POST or another replica's bus hop) travels it. Colon-namespaced the
// way redis channels conventionally are.
const ingestTopic = "voxdesk:ingest"

// publishMessage is the wire schema of an ingest envelope — the bytes that
// cross the broker. Payload rides as a raw message so the bus hop NEVER
// re-marshals the event body (forwarded-verbatim is the whole contract).
type publishMessage struct {
	TenantID string          `json:"tenant_id"`
	Room     string          `json:"room"`
	Kind     string          `json:"kind"`
	EventID  string          `json:"event_id,omitempty"`
	Payload  json.RawMessage `json:"payload"`
}

// ingestRequest is POST /ingest/v1/publish's body — the ONE shape the API
// uses to push realtime events to browsers:
//
//	{"tenant_id": "<uuid>",             — required, routing key
//	 "room":      "calls"|"metrics"|"call:<uuid>"|"campaign:<uuid>",
//	 "kind":      "call.updated",       — dotted event name
//	 "payload":   {...any JSON...},     — delivered verbatim to subscribers
//	 "event_id":  "<uuid>",             — optional, drives replay suppression
//	}
type ingestRequest struct {
	TenantID string          `json:"tenant_id"`
	Room     string          `json:"room"`
	Kind     string          `json:"kind"`
	Payload  json.RawMessage `json:"payload"`
	EventID  string          `json:"event_id,omitempty"`
}

// ingestResponse reports the fan-out. duplicate=true means a replayed
// event_id was suppressed — a 200 in that case is deliberate: publisher
// retries are EXPECTED (the API emits events from the same transactions as
// its state changes and will redeliver after a crash), and retrying a
// duplicate must converge, not amplify.
type ingestResponse struct {
	Delivered int  `json:"delivered"`
	Dropped   int  `json:"dropped"`
	Duplicate bool `json:"duplicate"`
}

// serveIngest handles POST /ingest/v1/publish. Verification order is
// deliberate: auth FIRST (cheapest, rejects forgeries before any parsing),
// then size, then shape, then replay, then fan-out.
func (s *Server) serveIngest(w http.ResponseWriter, r *http.Request) {
	// 1) Auth: shared ingest secret as a Bearer credential, constant-time
	//    (hashed first so even the secret's length does not leak). This
	//    endpoint can write into any tenant's rooms, so it is exactly as
	//    sensitive as an unauthenticated webhook would be on the API.
	if !ingestAuthorized(r.Header.Get("Authorization"), s.cfg.IngestSecret) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "auth")
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "invalid ingest credentials"})
		return
	}

	// 2) Size ceiling, enforced BEFORE decoding — a body has no business
	//    being bigger than the envelope plus the largest payload we fan out.
	r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxIngestPayloadBytes+4096)
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.registry.IngestRejected()
		var maxBytesErr *http.MaxBytesError
		if errors.As(err, &maxBytesErr) {
			s.emitIngestRejected(r, "payload_too_large")
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"detail": "payload too large"})
			return
		}
		s.emitIngestRejected(r, "not_json")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "body is not valid JSON"})
		return
	}

	// 3) Shape: every field that becomes a routing key is validated. An
	//    ingest that cannot name its tenant/room precisely is a bug in the
	//    publisher, and 422 tells the API's publisher-loop to stop retrying
	//    (the API's retry taxonomy already treats 422 as permanent).
	if !validate.IsUUID(req.TenantID) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "tenant_id_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "tenant_id must be a UUID"})
		return
	}
	if !validate.IsRoomName(req.Room) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "room_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "room must be calls, metrics, call:<uuid> or campaign:<uuid>"})
		return
	}
	if !validate.IsEventKind(req.Kind) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "kind_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "kind must be a dotted event name like call.updated"})
		return
	}
	trimmed := strings.TrimSpace(string(req.Payload))
	objectOrArray := len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[')
	if len(req.Payload) == 0 || !json.Valid(req.Payload) || !objectOrArray {
		// Deliveries fan out verbatim, and dashboards dispatch on payload
		// fields: a scalar would deserialize "successfully" into something
		// no client switch can handle. Objects and arrays only.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "payload_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "payload must be a JSON object or array"})
		return
	}
	if int64(len(req.Payload)) > s.cfg.MaxIngestPayloadBytes {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "payload_too_large")
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"detail": "payload too large"})
		return
	}
	if req.EventID != "" && !validate.IsUUID(req.EventID) {
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "event_id_shape")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "event_id must be a UUID when present"})
		return
	}
	tenantID := strings.ToLower(req.TenantID)

	// 4) Replay suppression. No event_id means the publisher opted out of
	//    dedupe (a deliberate choice for coarse metric ticks).
	if req.EventID != "" && s.replay.SeenBefore(tenantID, req.EventID, time.Now()) {
		s.registry.IngestDuplicate()
		s.emitIngest(r, "ingest.duplicate", "tenant", tenantID, "room", req.Room, "kind", req.Kind, "event_id", req.EventID)
		writeJSON(w, http.StatusOK, ingestResponse{Delivered: 0, Dropped: 0, Duplicate: true})
		return
	}

	// 5) Fan-out THROUGH THE BROKER. In the default memory mode the
	//    publish IS the hub fan-out (synchronous, same counters as
	//    before); in redis mode this call additionally propagates the
	//    envelope to the other replicas, and the Stats below remain the
	//    local node's — response semantics are transport-independent.
	//    Tenancy is structural below this line either way: the hub only
	//    ever touches (tenantID, room) — no code path in the package can
	//    deliver into another tenant.
	msg := publishMessage{TenantID: tenantID, Room: req.Room, Kind: req.Kind, EventID: req.EventID, Payload: req.Payload}
	frame, err := json.Marshal(msg)
	if err != nil {
		// msg was fully validated above; a marshal failure here is a
		// can't-happen guard (byte-tainted RawMessage would already have
		// failed json.Valid). Refuse as a shape error, never panic.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "frame_marshal")
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "payload could not be framed"})
		return
	}
	stats, err := s.bus.Publish(ingestTopic, frame)
	if err != nil {
		// Only broker.ErrClosed exists today: shutdown in flight. The API
		// retries 5xx with backoff, so a retry lands on the replacement
		// replica — converge, don't dead-letter.
		s.registry.IngestRejected()
		s.emitIngestRejected(r, "bus_closed")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"detail": "gateway is shutting down"})
		return
	}
	delivered, dropped := stats.Delivered, stats.Dropped
	s.registry.IngestAccepted()
	s.registry.Delivered(int64(delivered))
	s.registry.Dropped(int64(dropped))
	s.emitIngest(r, "ingest.accepted", "tenant", tenantID, "room", req.Room, "kind", req.Kind, "event_id", req.EventID, "delivered", strconv.Itoa(delivered), "dropped", strconv.Itoa(dropped))
	writeJSON(w, http.StatusOK, ingestResponse{Delivered: delivered, Dropped: dropped, Duplicate: false})
}

// handleIngestEnvelope is the broker's local delivery point for EVERY
// ingest-homage envelope: locally-published (the serving Publish call
// itself — accounting returned to the HTTP handler) and bus-received from
// other replicas (accounting counted straight into metrics here, since no
// HTTP response ever summarizes a remote hop). Decode failures are logged
// and skipped: one malformed frame must not poison the topic.
func (s *Server) handleIngestEnvelope(env broker.Envelope) broker.Stats {
	var msg publishMessage
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		s.logf("[gateway] dropping undecodable ingest envelope from %s: %v", env.Origin, err)
		return broker.Stats{}
	}
	delivered, dropped := s.hub.Publish(msg.TenantID, msg.Room, msg.Kind, msg.EventID, msg.Payload)
	if !env.Local {
		s.registry.Delivered(int64(delivered))
		s.registry.Dropped(int64(dropped))
	}
	return broker.Stats{Delivered: delivered, Dropped: dropped}
}

// ingestAuthorized verifies the Bearer credential against the configured
// ingest secret in constant time (hashed first, as in metrics.go). The
// Bearer extraction + constant-time compare are shared with auth package's
// middleware helpers since the package split — one comparison
// implementation for every secret on this edge.
func ingestAuthorized(header, secret string) bool {
	token, ok := auth.ExtractBearer(header)
	if !ok {
		return false
	}
	return auth.ConstantTimeTokenEqual(token, secret)
}

// emitIngestRejected publishes the structured reject fact with its reason;
// the HTTP response stays the API-facing contract, the event is the
// operator-facing audit trail (same fields accepted publishes emit, minus
// the fan-out counters that do not exist on a reject).
func (s *Server) emitIngestRejected(r *http.Request, reason string) {
	s.emitIngest(r, "ingest.rejected", "reason", reason)
}

// emitIngest publishes one ingest event with the request's correlation id
// attached — the moment API → gateway → browser becomes one traceable line
// is a /ingest POST arriving with the API's X-Request-ID.
func (s *Server) emitIngest(r *http.Request, name string, kv ...string) {
	fields := make(map[string]string, len(kv)/2+1)
	for i := 0; i+1 < len(kv); i += 2 {
		fields[kv[i]] = kv[i+1]
	}
	if id := observability.RequestIDFrom(r.Context()); id != "" {
		fields["request_id"] = id
	}
	s.events.Emit(observability.Event{Name: name, At: time.Now(), Fields: fields})
}
