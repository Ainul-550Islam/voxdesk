#include "voxdesk/media/consistent_hash_router.hpp"

#include <algorithm>

namespace voxdesk::media {

std::uint64_t ConsistentHashRouter::Hash64(std::string_view data) {
  // FNV-1a over the bytes, then a SplitMix64 finalizer for avalanche. FNV-1a
  // alone clusters short, similar keys ("node-a#0", "node-a#1", "tenant-0"):
  // its low bits vary but the high bits barely move, so an entire key family
  // can land in one arc of the ring and starve the other nodes. The finalizer
  // spreads consecutive inputs uniformly across the full 64-bit space, which
  // is exactly the property consistent hashing needs.
  constexpr std::uint64_t kOffsetBasis = 1469598103934665603ULL;
  constexpr std::uint64_t kFnvPrime = 1099511628211ULL;
  std::uint64_t hash = kOffsetBasis;
  for (const unsigned char c : data) {
    hash ^= c;
    hash *= kFnvPrime;
  }
  hash += 0x9e3779b97f4a7c15ULL;
  hash = (hash ^ (hash >> 30)) * 0xbf58476d1ce4e5b9ULL;
  hash = (hash ^ (hash >> 27)) * 0x94d049bb133111ebULL;
  return hash ^ (hash >> 31);
}

bool ConsistentHashRouter::AddNode(std::string node_id,
                                   std::uint32_t virtual_nodes) {
  if (node_id.empty()) {
    return false;
  }
  if (std::find(nodes_.begin(), nodes_.end(), node_id) != nodes_.end()) {
    return false;
  }
  nodes_.push_back(node_id);
  for (std::uint32_t i = 0; i < virtual_nodes; ++i) {
    // Copy node_id into every virtual node: it is re-read on every iteration,
    // so moving it would empty it after the first replica.
    ring_.push_back(VirtualNode{Hash64(node_id + '#' + std::to_string(i)),
                                node_id});
  }
  std::sort(ring_.begin(), ring_.end(),
            [](const VirtualNode& a, const VirtualNode& b) {
              return a.hash < b.hash;
            });
  return true;
}

bool ConsistentHashRouter::RemoveNode(const std::string& node_id) {
  const std::size_t before = ring_.size();
  ring_.erase(std::remove_if(ring_.begin(), ring_.end(),
                             [&node_id](const VirtualNode& vn) {
                               return vn.node_id == node_id;
                             }),
              ring_.end());
  const auto node_it = std::find(nodes_.begin(), nodes_.end(), node_id);
  if (node_it != nodes_.end()) {
    nodes_.erase(node_it);
  }
  return ring_.size() != before;
}

std::string ConsistentHashRouter::Route(const std::string& tenant_id) const {
  if (ring_.empty()) {
    return {};
  }
  const std::uint64_t hash = Hash64(tenant_id);
  const auto it = std::lower_bound(
      ring_.begin(), ring_.end(), hash,
      [](const VirtualNode& vn, std::uint64_t key) { return vn.hash < key; });
  return (it == ring_.end() ? ring_.begin() : it)->node_id;
}

}  // namespace voxdesk::media
