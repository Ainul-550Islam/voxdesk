// Package brokertest provides a fake RESP-speaking Redis for tests: a
// real TCP server implementing exactly the commands and pub/sub semantics
// the gateway's hand-rolled broker client uses (AUTH, SELECT, SUBSCRIBE,
// PUBLISH), including re-broadcasting a publication to EVERY subscriber
// connection of the topic — own-node echoes included, which is precisely
// the behaviour the broker's origin suppression must handle.
//
// It lives in a package of its own (not a _test.go file) so tests OUTSIDE
// the broker package — notably internal/server's multi-replica e2e — can
// stand up the same fake without copying it.
package brokertest

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"
)

// FakeRedis is one running fake server. Zero value is not usable; build
// with Start. Safe under -race (all shared state behind mu).
type FakeRedis struct {
	ln   net.Listener
	Addr string

	mu    sync.Mutex
	subs  map[string][]net.Conn // topic → subscribed connections
	conns map[net.Conn]struct{}
	die   chan struct{}
}

// Start binds an ephemeral port and begins serving. The caller owns Stop.
func Start() (*FakeRedis, error) { return StartAt("127.0.0.1:0") }

// StartAt binds a specific address — used to bring the bus BACK on the
// same endpoint after a Stop, which is how reconnect behaviour is tested
// (clients retry the address they were configured with).
func StartAt(addr string) (*FakeRedis, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	f := &FakeRedis{
		ln: ln, Addr: ln.Addr().String(),
		subs: make(map[string][]net.Conn), conns: make(map[net.Conn]struct{}),
		die: make(chan struct{}),
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			f.mu.Lock()
			f.conns[conn] = struct{}{}
			f.mu.Unlock()
			go f.serve(conn)
		}
	}()
	return f, nil
}

// URL renders the address as a redis:// URL for config/client use.
func (f *FakeRedis) URL() string { return "redis://" + f.Addr }

// Stop kills the listener AND every client connection — violent enough to
// force a broker's read loop into its reconnect cycle (the outage test
// relies on that). Rebind with StartAt to model "the bus is back".
func (f *FakeRedis) Stop() {
	f.mu.Lock()
	defer f.mu.Unlock()
	select {
	case <-f.die:
	default:
		close(f.die)
	}
	_ = f.ln.Close()
	for c := range f.conns {
		_ = c.Close()
	}
	f.conns = map[net.Conn]struct{}{}
	f.subs = map[string][]net.Conn{}
}

// SubscriberCount reports live SUBSCRIBE registrations on topic (asserts
// that reconnect re-subscribes happened).
func (f *FakeRedis) SubscriberCount(topic string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.subs[topic])
}

// WaitForSubscribers polls until topic has n subscriber connections or the
// timeout lapses (reconnect-driven subscriptions are asynchronous).
func (f *FakeRedis) WaitForSubscribers(topic string, n int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if f.SubscriberCount(topic) >= n {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return false
}

func (f *FakeRedis) serve(conn net.Conn) {
	defer conn.Close()
	br := bufio.NewReader(conn)
	w := bufio.NewWriter(conn)
	for {
		args, err := readCommand(br)
		if err != nil {
			f.drop(conn)
			return
		}
		if len(args) == 0 {
			continue
		}
		switch strings.ToUpper(args[0]) {
		case "AUTH", "SELECT":
			fmt.Fprint(w, "+OK\r\n")
		case "SUBSCRIBE":
			if len(args) != 2 {
				fmt.Fprint(w, "-ERR wrong args\r\n")
				break
			}
			topic := args[1]
			f.mu.Lock()
			f.subs[topic] = append(f.subs[topic], conn)
			count := 0
			for _, conns := range f.subs {
				for _, c := range conns {
					if c == conn {
						count++
					}
				}
			}
			f.mu.Unlock()
			writeArray(w, "subscribe", topic, strconv.Itoa(count))
		case "PUBLISH":
			if len(args) != 3 {
				fmt.Fprint(w, "-ERR wrong args\r\n")
				break
			}
			topic, payload := args[1], args[2]
			f.mu.Lock()
			recipients := append([]net.Conn(nil), f.subs[topic]...)
			f.mu.Unlock()
			for _, rc := range recipients {
				// Best-effort fan-out per connection: one stalled client
				// never head-of-line-blocks the fake's publish loop.
				rc.SetWriteDeadline(time.Now().Add(2 * time.Second))
				rw := bufio.NewWriter(rc)
				writeArray(rw, "message", topic, payload)
				rw.Flush()
				rc.SetWriteDeadline(time.Time{})
			}
			fmt.Fprintf(w, ":%d\r\n", len(recipients))
		default:
			fmt.Fprint(w, "-ERR unknown command\r\n")
		}
		w.Flush()
	}
}

func (f *FakeRedis) drop(conn net.Conn) {
	f.mu.Lock()
	delete(f.conns, conn)
	for topic, conns := range f.subs {
		kept := conns[:0]
		for _, c := range conns {
			if c != conn {
				kept = append(kept, c)
			}
		}
		f.subs[topic] = kept
	}
	f.mu.Unlock()
}

// readCommand parses one client command (a RESP array of bulk strings).
func readCommand(br *bufio.Reader) ([]string, error) {
	line, err := br.ReadString('\n')
	if err != nil {
		return nil, err
	}
	line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	if !strings.HasPrefix(line, "*") {
		return nil, fmt.Errorf("expected array, got %q", line)
	}
	n, err := strconv.Atoi(line[1:])
	if err != nil {
		return nil, err
	}
	args := make([]string, 0, n)
	for i := 0; i < n; i++ {
		header, err := br.ReadString('\n')
		if err != nil {
			return nil, err
		}
		header = strings.TrimSuffix(strings.TrimSuffix(header, "\n"), "\r")
		if !strings.HasPrefix(header, "$") {
			return nil, fmt.Errorf("expected bulk header, got %q", header)
		}
		size, err := strconv.Atoi(header[1:])
		if err != nil {
			return nil, err
		}
		buf := make([]byte, size+2)
		if _, err := io.ReadFull(br, buf); err != nil {
			return nil, err
		}
		args = append(args, string(buf[:size]))
	}
	return args, nil
}

// writeArray emits a RESP array of bulk strings.
func writeArray(w *bufio.Writer, parts ...string) {
	fmt.Fprintf(w, "*%d\r\n", len(parts))
	for _, p := range parts {
		fmt.Fprintf(w, "$%d\r\n%s\r\n", len(p), p)
	}
}
