package observability

import (
	"encoding/json"
	"sort"
	"time"
)

// Event is one structured operational fact: a named moment plus the small
// set of key/value fields that explain it. Events are the audit-shaped
// half of observability — /metrics answers "how much/many", an event
// answers "what exactly happened to THIS publish/session". Field values
// must stay operator-safe (ids, counts, durations), never payloads or
// secrets: emitters may write events to plain text logs.
type Event struct {
	Name   string            // dotted, stable: "ingest.accepted", "session.opened"
	At     time.Time         // when it happened (callers set time.Now())
	Fields map[string]string // ids and counters; see package note above
}

// Field returns the value of one field ("" when unset).
func (e Event) Field(key string) string { return e.Fields[key] }

// Emitter consumes events. Implementations must be safe for concurrent
// use and must never block on slow sinks for longer than a producer is
// prepared to wait — events are telemetry, not a control channel.
type Emitter interface {
	Emit(Event)
}

// EmitterFunc adapts a function to Emitter.
type EmitterFunc func(Event)

// Emit implements Emitter.
func (f EmitterFunc) Emit(e Event) { f(e) }

// nopEmitter is the discard implementation behind NopEmitter. It is a
// struct (not an EmitterFunc) so interface equality checks against
// NopEmitter never hit Go's "comparing uncomparable func values" panic.
type nopEmitter struct{}

// Emit implements Emitter.
func (nopEmitter) Emit(Event) {}

// NopEmitter discards everything — the zero-dependency default.
var NopEmitter Emitter = nopEmitter{}

// MultiEmitter fans one event out to every emitter it holds.
func MultiEmitter(emitters ...Emitter) Emitter {
	flat := make([]Emitter, 0, len(emitters))
	for _, em := range emitters {
		if em != nil && em != NopEmitter {
			flat = append(flat, em)
		}
	}
	if len(flat) == 0 {
		return NopEmitter
	}
	return EmitterFunc(func(e Event) {
		for _, em := range flat {
			em.Emit(e)
		}
	})
}

// LogEmitter renders events as single JSON lines through a Logger at Info
// level — the gateway's production emitter. JSON (not printf) because the
// shipper side of this log stream already parses the Python app's JSON
// logs; one shape for the whole platform. Field keys are SORTED so two
// emits of the same event diff cleanly.
type LogEmitter struct {
	logger *Logger
}

// NewLogEmitter emits through logger at Info level. A nil logger discards.
func NewLogEmitter(logger *Logger) *LogEmitter {
	return &LogEmitter{logger: logger}
}

// Emit implements Emitter.
func (e *LogEmitter) Emit(ev Event) {
	if e.logger == nil || !e.logger.Enabled(Info) {
		return
	}
	keys := make([]string, 0, len(ev.Fields))
	for k := range ev.Fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	fields := make(map[string]string, len(ev.Fields))
	for _, k := range keys {
		fields[k] = ev.Fields[k]
	}
	line, err := json.Marshal(struct {
		Name   string            `json:"event"`
		At     time.Time         `json:"at"`
		Fields map[string]string `json:"fields,omitempty"`
	}{Name: ev.Name, At: ev.At, Fields: fields})
	if err != nil {
		return // a map[string]string cannot fail to marshal; can't-happen guard
	}
	e.logger.Infof("EVENT %s", line)
}
