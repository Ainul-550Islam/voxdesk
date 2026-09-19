package observability

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestLoggerFiltersByLevelAndCarriesFields(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "[gw] ", Info).With("tenant", "t-1")

	l.Debugf("hidden %d", 1)
	l.Infof("shown %s", "yes")

	out := buf.String()
	if strings.Contains(out, "hidden") {
		t.Fatalf("debug must be filtered at Info: %q", out)
	}
	if !strings.Contains(out, "INFO shown yes") || !strings.Contains(out, "tenant=t-1") {
		t.Fatalf("info line must carry message and bound fields: %q", out)
	}
	if !strings.HasPrefix(out, "[gw] ") {
		t.Fatalf("prefix lost: %q", out)
	}
}

func TestLoggerRuntimeLevelChangeAndValueQuoting(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "", Warn)
	l.Infof("quiet")
	l.SetLevel(Debug)
	l.Debugf("loud %s", "here")
	l.With("path", "a b", "n", "3").Infof("kv")

	out := buf.String()
	if strings.Contains(out, "quiet") {
		t.Fatal("info below threshold must not print")
	}
	if !strings.Contains(out, "DEBUG loud here") {
		t.Fatalf("raised threshold must admit debug: %q", out)
	}
	if !strings.Contains(out, `path="a b" n=3`) {
		t.Fatalf("values with spaces must be quoted: %q", out)
	}
}

func TestParseLevelNeverDisablesAccidentally(t *testing.T) {
	t.Parallel()
	if ParseLevel("DEBUG") != Debug || ParseLevel("warn") != Warn || ParseLevel("error") != Error {
		t.Fatal("named levels must parse")
	}
	if ParseLevel("garbage") != Info {
		t.Fatal("unknown level must fall back to Info, never Off")
	}
}

func TestRequestIDMiddlewareHonoursSanitizesAndStamps(t *testing.T) {
	t.Parallel()
	var seen string
	handler := RequestIDMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = RequestIDFrom(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	for _, tc := range []struct{ in, want string }{
		{"api-trace-123", "api-trace-123"}, // kept: the trace continues
		{"bad\r\ninjected-x", ""},          // control bytes rejected → replaced
		{strings.Repeat("x", 200), ""},     // oversized rejected → replaced
	} {
		seen = ""
		req := httptest.NewRequest(http.MethodGet, "/x", nil)
		req.Header.Set(RequestIDHeader, tc.in)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)

		respID := rec.Header().Get(RequestIDHeader)
		if tc.want != "" {
			if seen != tc.want || respID != tc.want {
				t.Fatalf("presented %q must survive: ctx=%q resp=%q", tc.in, seen, respID)
			}
			continue
		}
		if len(respID) != 32 || seen != respID {
			t.Fatalf("id %q must be replaced with a fresh 32-hex id (ctx=%q)", tc.in, seen)
		}
	}
}

func TestRequestIDFromAbsent(t *testing.T) {
	t.Parallel()
	if got := RequestIDFrom(context.Background()); got != "" {
		t.Fatalf("no middleware, no id — got %q", got)
	}
}

func TestMultiEmitterFansOutAndSkipsNilAndNop(t *testing.T) {
	t.Parallel()
	var a, b []string
	em := MultiEmitter(
		nil,
		NopEmitter,
		EmitterFunc(func(e Event) { a = append(a, e.Name) }),
		EmitterFunc(func(e Event) { b = append(b, e.Name) }),
	)
	em.Emit(Event{Name: "ingest.accepted", At: time.Now()})
	if len(a) != 1 || len(b) != 1 {
		t.Fatalf("both real emitters must see the event: a=%v b=%v", a, b)
	}
	if MultiEmitter() != NopEmitter {
		t.Fatal("an all-empty multi must collapse to the nop emitter")
	}
}

func TestLogEmitterRendersSortedJSONOnlyWhenEnabled(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	em := NewLogEmitter(New(&buf, "", Info))

	em.Emit(Event{Name: "ingest.accepted", At: time.Unix(1_700_000_000, 0).UTC(),
		Fields: map[string]string{"room": "calls", "delivered": "3", "request_id": "r-1"}})

	line := strings.TrimSpace(buf.String())
	payload := line[strings.Index(line, "EVENT ")+6:]
	var decoded struct {
		Event  string            `json:"event"`
		At     time.Time         `json:"at"`
		Fields map[string]string `json:"fields"`
	}
	if err := json.Unmarshal([]byte(payload), &decoded); err != nil {
		t.Fatalf("emitter must produce one JSON line, got %q: %v", line, err)
	}
	if decoded.Event != "ingest.accepted" || decoded.Fields["delivered"] != "3" || !decoded.At.Equal(time.Unix(1_700_000_000, 0).UTC()) {
		t.Fatalf("decoded event wrong: %+v", decoded)
	}

	// Level filter really filters.
	buf.Reset()
	quietLogger := New(&buf, "", Warn)
	NewLogEmitter(quietLogger).Emit(Event{Name: "x", At: time.Now()})
	if buf.Len() != 0 {
		t.Fatal("events must respect the underlying logger's level")
	}
	// A nil logger is a safe discard.
	NewLogEmitter(nil).Emit(Event{Name: "x", At: time.Now()})
}

func TestEmittersAndLoggerAreConcurrencySafe(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	l := New(&buf, "", Info)
	em := NewLogEmitter(l)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 25; i++ {
				l.With("g", "x").Infof("spin")
				em.Emit(Event{Name: "spin", At: time.Now(), Fields: map[string]string{"g": "x"}})
			}
		}(g)
	}
	wg.Wait()
}
