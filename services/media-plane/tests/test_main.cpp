// Self-contained unit tests for the media-plane foundation. No external test
// framework: a tiny CHECK macro plus a non-zero exit is enough for a module
// this size, and it keeps the build dependency-free (g++ + make only).

#include "voxdesk/media/consistent_hash_router.hpp"
#include "voxdesk/media/jitter_buffer.hpp"

#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace {

int g_checks = 0;
int g_failures = 0;

void Report(bool ok, const char* file, int line, const char* expr) {
  ++g_checks;
  if (!ok) {
    ++g_failures;
    std::printf("FAIL %s:%d: %s\n", file, line, expr);
  }
}

}  // namespace

#define CHECK(expr) Report((expr), __FILE__, __LINE__, #expr)

using voxdesk::media::AudioFrame;
using voxdesk::media::ConsistentHashRouter;
using voxdesk::media::InsertResult;
using voxdesk::media::JitterBuffer;
using voxdesk::media::PopResult;

namespace {

AudioFrame Frame(std::uint32_t seq) {
  AudioFrame frame;
  frame.seq = seq;
  frame.timestamp = seq * 160u;  // 20 ms @ 8 kHz
  frame.payload = {1, 2, 3, 4};
  return frame;
}

void TestJitterBufferInOrder() {
  JitterBuffer buffer(8, 4);
  for (std::uint32_t seq = 0; seq < 5; ++seq) {
    CHECK(buffer.Insert(Frame(seq)) == InsertResult::Buffered);
  }
  CHECK(buffer.Size() == 5u);
  for (std::uint32_t seq = 0; seq < 5; ++seq) {
    AudioFrame out;
    CHECK(buffer.Pop(&out) == PopResult::Ok);
    CHECK(out.seq == seq);
  }
  CHECK(buffer.Size() == 0u);
  CHECK(buffer.Pop(nullptr) == PopResult::Underflow);
}

void TestJitterBufferReorder() {
  JitterBuffer buffer(8, 4);
  CHECK(buffer.Insert(Frame(1)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(2)) == InsertResult::Buffered);
  AudioFrame out;
  // Frame 0 has not arrived: wait, do not declare it lost yet.
  CHECK(buffer.Pop(&out) == PopResult::Underflow);
  CHECK(buffer.Insert(Frame(0)) == InsertResult::Buffered);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 0u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 1u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 2u);
  CHECK(buffer.Pop(&out) == PopResult::Underflow);
}

void TestJitterBufferDuplicate() {
  // Seed the playout base at the stream's negotiated sequence (see the class
  // docstring: the buffer never guesses the starting sequence).
  JitterBuffer buffer(8, 4, 7);
  CHECK(buffer.Insert(Frame(7)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(7)) == InsertResult::Duplicate);
  CHECK(buffer.duplicate_dropped() == 1u);
  AudioFrame out;
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 7u);
  CHECK(buffer.Pop(&out) == PopResult::Underflow);
}

void TestJitterBufferLate() {
  JitterBuffer buffer(8, 4, 10);
  CHECK(buffer.Insert(Frame(10)) == InsertResult::Buffered);
  AudioFrame out;
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 10u);
  // Expected is now 11; a frame with seq 9 arrived after it was needed.
  CHECK(buffer.Insert(Frame(9)) == InsertResult::Late);
  CHECK(buffer.late_dropped() == 1u);
}

void TestJitterBufferOverflow() {
  JitterBuffer buffer(8, 2);  // reorder window of 2 frames
  CHECK(buffer.Insert(Frame(0)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(1)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(100)) == InsertResult::Buffered);
  AudioFrame out;
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 0u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 1u);
  // Expected is 2, oldest buffered is 100, gap 98 > window 2: frames 2..99 are
  // declared lost rather than stalling the stream forever.
  CHECK(buffer.Pop(&out) == PopResult::Overflow);
  CHECK(buffer.overflow_declared() == 1u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 100u);
}

void TestJitterBufferFull() {
  JitterBuffer buffer(3, 4);
  CHECK(buffer.Insert(Frame(0)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(1)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(2)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(3)) == InsertResult::DroppedFull);
  CHECK(buffer.full_dropped() == 1u);
}

void TestJitterBufferSeqWrap() {
  // Join a stream mid-call at sequence 65534 and cross the 16-bit wrap.
  JitterBuffer buffer(8, 4, 65534);
  CHECK(buffer.Insert(Frame(65535)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(65534)) == InsertResult::Buffered);
  CHECK(buffer.Insert(Frame(0)) == InsertResult::Buffered);
  AudioFrame out;
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 65534u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 65535u);
  CHECK(buffer.Pop(&out) == PopResult::Ok && out.seq == 0u);
}

void TestRouterEmpty() {
  ConsistentHashRouter router;
  CHECK(router.Route("tenant-a") == "");
  CHECK(router.NodeCount() == 0u);
  CHECK(!router.AddNode("", 16));
}

void TestRouterDeterministicAndSpread() {
  ConsistentHashRouter router;
  CHECK(router.AddNode("node-a"));
  CHECK(router.AddNode("node-b"));
  CHECK(router.AddNode("node-c"));
  CHECK(router.NodeCount() == 3u);
  CHECK(!router.AddNode("node-a"));  // duplicate is a no-op

  std::map<std::string, std::string> assignments;
  for (int i = 0; i < 300; ++i) {
    const std::string tenant = "tenant-" + std::to_string(i);
    const std::string node = router.Route(tenant);
    assignments[tenant] = node;
    CHECK(node == "node-a" || node == "node-b" || node == "node-c");
  }
  // Determinism: the same tenant id always lands on the same node.
  for (const auto& kv : assignments) {
    CHECK(router.Route(kv.first) == kv.second);
  }
  // Spread: every node receives traffic.
  std::set<std::string> used;
  for (const auto& kv : assignments) {
    used.insert(kv.second);
  }
  CHECK(used.size() == 3u);
}

void TestRouterMinimalReshuffleOnAdd() {
  ConsistentHashRouter router;
  CHECK(router.AddNode("a"));
  CHECK(router.AddNode("b"));
  CHECK(router.AddNode("c"));
  CHECK(router.AddNode("d"));

  std::vector<std::string> before;
  for (int i = 0; i < 1000; ++i) {
    before.push_back(router.Route("t" + std::to_string(i)));
  }
  CHECK(router.AddNode("e"));

  std::size_t moved = 0;
  for (int i = 0; i < 1000; ++i) {
    if (router.Route("t" + std::to_string(i)) != before[i]) {
      ++moved;
    }
  }
  // Consistent hashing moves only the tenants the new node claims; a naive
  // `hash % node_count` would move ~800 of 1000 here.
  CHECK(moved > 0u);
  CHECK(moved < 350u);
}

void TestRouterRemoval() {
  ConsistentHashRouter router;
  CHECK(router.AddNode("a"));
  CHECK(router.AddNode("b"));
  CHECK(router.AddNode("c"));

  std::vector<std::pair<std::string, std::string>> before;
  for (int i = 0; i < 300; ++i) {
    const std::string tenant = "t" + std::to_string(i);
    before.emplace_back(tenant, router.Route(tenant));
  }
  CHECK(router.RemoveNode("b"));
  CHECK(!router.RemoveNode("b"));
  CHECK(router.NodeCount() == 2u);

  for (const auto& kv : before) {
    const std::string node = router.Route(kv.first);
    CHECK(node == "a" || node == "c");
    if (kv.second != "b") {
      // Removing a node only ever grows its neighbours' ranges, so tenants
      // that were not on the removed node must keep their assignment.
      CHECK(node == kv.second);
    }
  }
}

}  // namespace

int main() {
  TestJitterBufferInOrder();
  TestJitterBufferReorder();
  TestJitterBufferDuplicate();
  TestJitterBufferLate();
  TestJitterBufferOverflow();
  TestJitterBufferFull();
  TestJitterBufferSeqWrap();
  TestRouterEmpty();
  TestRouterDeterministicAndSpread();
  TestRouterMinimalReshuffleOnAdd();
  TestRouterRemoval();

  std::printf("%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
