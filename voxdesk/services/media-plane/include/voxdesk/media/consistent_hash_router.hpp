#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace voxdesk::media {

// Deterministic tenant -> media-node assignment with minimal reshuffling.
//
// The media plane routes a tenant's audio streams to exactly one node, and the
// tenant isolation contract requires that every tenant is owned by a single,
// stable node. A naive `hash(tenant) % node_count` would re-route nearly every
// tenant whenever a node is added or removed; this router instead places
// virtual nodes on a hash ring so that adding a node claims only a fraction of
// the key space and removing a node moves only the tenants it owned. The
// invariants are unit-tested in tests/test_main.cpp.
class ConsistentHashRouter {
 public:
  // Adds a node with `virtual_nodes` replicas on the ring. Returns false for an
  // empty id or a node that is already present (both are no-ops).
  bool AddNode(std::string node_id, std::uint32_t virtual_nodes = 128);

  // Removes a node and every one of its virtual replicas. Returns false if the
  // node was not present. Tenants that were not owned by the removed node keep
  // their assignment.
  bool RemoveNode(const std::string& node_id);

  // The node that owns `tenant_id`, or an empty string when no node exists.
  std::string Route(const std::string& tenant_id) const;

  std::size_t NodeCount() const { return nodes_.size(); }
  std::size_t VirtualNodeCount() const { return ring_.size(); }

  // 64-bit hash with strong avalanche (FNV-1a over the bytes, then a
  // SplitMix64 finalizer). The finalizer is what makes short, similar keys
  // such as "node-a#0", "node-a#1", "tenant-0" spread uniformly across the
  // ring; FNV-1a alone clusters them, which would concentrate every tenant on
  // one node. Public so callers can pre-hash a tenant id; deterministic across
  // platforms, which is the property the ring depends on.
  static std::uint64_t Hash64(std::string_view data);

 private:
  struct VirtualNode {
    std::uint64_t hash;
    std::string node_id;
  };

  std::vector<VirtualNode> ring_;  // sorted by hash, ascending
  std::vector<std::string> nodes_; // distinct node ids, insertion order
};

}  // namespace voxdesk::media
