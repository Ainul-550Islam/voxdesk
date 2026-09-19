// Package observability is the gateway's single home for everything it
// says about itself: the leveled logger (this file), the request-id
// middleware (tracing.go), the structured event emitter (events.go), and
// the Prometheus metrics core (metrics/).
//
// The logger is deliberately a thin, leveled skin over the standard
// library's log package: the gateway has always logged unlabelled text
// lines (matching log.Printf everywhere), and what the package split adds
// is levels and a structured key/value suffix — enough to silence chatter
// in one knob and to grep a request id out of a mixed log stream — without
// trading in the operational simplicity of plain text logs. The Python
// side's structured-logging module (app/core/logging.py) remains the rich
// sibling; this edge stays greppable.
package observability

import (
	"fmt"
	"io"
	"log"
	"strings"
	"sync"
	"sync/atomic"
)

// Level filters log output. Ordered by severity: a Logger set to Info
// prints Info/Warn/Error and drops Debug.
type Level int32

const (
	Debug Level = iota
	Info
	Warn
	Error
	// Off silences the logger entirely (tests).
	Off
)

// String renders the level for log lines ("info", "warn", ...).
func (l Level) String() string {
	switch l {
	case Debug:
		return "debug"
	case Info:
		return "info"
	case Warn:
		return "warn"
	case Error:
		return "error"
	case Off:
		return "off"
	default:
		return fmt.Sprintf("level(%d)", int32(l))
	}
}

// ParseLevel accepts case-insensitive names; unknown values fall back to
// Info (a misconfigured level must never silently disable logging).
func ParseLevel(s string) Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return Debug
	case "warn", "warning":
		return Warn
	case "error":
		return Error
	case "off":
		return Off
	default:
		return Info
	}
}

// Logger is a level-filtered, optionally key-tagged logger. All methods
// are safe for concurrent use; the level may be raised/lowered at runtime
// (SetLevel) without a restart, which is how an operator cranks a live
// node to debug without a redeploy.
type Logger struct {
	mu   sync.Mutex // guards only the bound kvPairs suffix (set once at With)
	core *log.Logger
	min  atomic.Int32
	kv   string // pre-rendered " k=v k=v" suffix from With()
}

// New returns a Logger writing to out with the given line prefix and
// minimum level. A nil out discards everything (useful in tests that only
// exercise level logic).
func New(out io.Writer, prefix string, min Level) *Logger {
	if out == nil {
		out = io.Discard
	}
	l := &Logger{core: log.New(out, prefix, log.LstdFlags|log.LUTC)}
	l.min.Store(int32(min))
	return l
}

// With returns a child logger whose lines carry the bound key=value pairs
// as a stable suffix ('user=… tenant=…') — the cheap half of structured
// logging: fixed in the line, greppable, zero allocation per call beyond
// the message itself.
func (l *Logger) With(kv ...string) *Logger {
	l.mu.Lock()
	base := l.kv
	l.mu.Unlock()
	child := &Logger{core: l.core, kv: base + renderKV(kv)}
	child.min.Store(l.min.Load())
	return child
}

// SetLevel changes the filter threshold at runtime (only this Logger AND
// any children whose levels were copied BEFORE the change — construct the
// shared Logger first, then With(); children minted later inherit it).
func (l *Logger) SetLevel(min Level) { l.min.Store(int32(min)) }

// Level reports the current threshold.
func (l *Logger) Level() Level { return Level(l.min.Load()) }

// Enabled reports whether a message at level would print.
func (l *Logger) Enabled(level Level) bool { return int32(level) >= l.min.Load() }

// Debugf/Infof/Warnf/Errorf log at the respective level.
func (l *Logger) Debugf(format string, args ...any) { l.logf(Debug, format, args...) }
func (l *Logger) Infof(format string, args ...any)  { l.logf(Info, format, args...) }
func (l *Logger) Warnf(format string, args ...any)  { l.logf(Warn, format, args...) }
func (l *Logger) Errorf(format string, args ...any) { l.logf(Error, format, args...) }

func (l *Logger) logf(level Level, format string, args ...any) {
	if !l.Enabled(level) {
		return
	}
	l.mu.Lock()
	suffix := l.kv
	l.mu.Unlock()
	msg := fmt.Sprintf(format, args...)
	l.core.Printf("%s %s%s", strings.ToUpper(level.String()), msg, suffix)
}

// renderKV formats an alternating key/value slice into " k1=v1 k2=v2".
// Odd trailing values render as " key" (a key with no value); spaces in
// values are quoted so the suffix stays machine-splittable.
func renderKV(kv []string) string {
	var b strings.Builder
	for i := 0; i < len(kv); i += 2 {
		if i+1 >= len(kv) {
			fmt.Fprintf(&b, " %s", kv[i])
			break
		}
		value := kv[i+1]
		if strings.ContainsAny(value, " \t\"") {
			value = fmt.Sprintf("%q", value)
		}
		fmt.Fprintf(&b, " %s=%s", kv[i], value)
	}
	return b.String()
}
