"""Community detection for the memory graph: Louvain, written directly
against the plain node/edge dicts _Builder already produces in memgraph.py.

No dependency - not networkx, not a vendored library. The Louvain algorithm
(Blondel et al., 2008) doesn't inherently need a graph library; networkx and
graspologic are just convenient containers and, for graspologic, a faster
native implementation for graphs orders of magnitude larger than anything
Saturday's memory graph ever holds (MAX_NODES caps it at 4000). A first pass
at this reused graphify's cluster.py (github.com/Graphify-Labs/graphify)
under its optional-extra pattern, but that meant every user installing
networkx just to see clusters - real bundle weight for a feature that runs
fine on a few thousand nodes in plain Python. This replaces it: nothing to
install, works the same for everyone, and it's actually Saturday's own code
rather than someone else's module with a dependency trailing behind it.

Two phases, standard Louvain:

  1. Local moving - each node starts in its own community; repeatedly move
     each node into whichever neighbouring community gives the best
     modularity gain, until nothing moves.
  2. Aggregation - collapse each community into one node (self-loop weight
     = its internal edges, inter-node weight = the sum of edges between the
     two communities) and repeat phase 1 on that coarser graph.

Repeat until a pass produces no further improvement. Community IDs from the
deepest level are mapped back down through every aggregation step onto the
original node indices.
"""
from __future__ import annotations

import random


def _adjacency(n: int, edges: list[tuple[int, int, float]]
              ) -> tuple[list[dict[int, float]], list[float], list[float]]:
    """Returns (adj, degree, self_loop). A self-loop (a==b) never becomes a
    neighbor entry - a node was never "adjacent to itself" for movement
    purposes - but its weight still counts twice toward that node's own
    degree, same as a regular edge counts once toward each endpoint, and
    the raw self-loop total is returned too so _aggregate can carry it
    forward. This matters past level 0: _aggregate turns each community's
    internal edges into a self-loop on its collapsed node, and that
    self-loop is the ONLY place a coarser level still carries "how
    cohesive was this community already" - drop it here (or fail to fold
    it back in at the next _aggregate) and every level past the first
    silently under-counts degree, which is exactly the bug that let
    unrelated communities merge into one
    on a real, hub-heavy graph even though a reference Louvain split it
    cleanly on the same input."""
    adj: list[dict[int, float]] = [dict() for _ in range(n)]
    self_loop = [0.0] * n
    for a, b, w in edges:
        if w <= 0:
            continue
        if a == b:
            self_loop[a] += w
            continue
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w
    degree = [sum(adj[i].values()) + 2 * self_loop[i] for i in range(n)]
    return adj, degree, self_loop


def _local_moving(n: int, adj: list[dict[int, float]], degree: list[float], m2: float,
                  seed: int) -> tuple[list[int], bool]:
    """One Louvain pass. Returns {node: community} (dense-renumbered) and
    whether anything moved - a caller uses that to know whether another
    round of aggregation is worth trying."""
    community = list(range(n))
    community_degree = list(degree)
    if m2 <= 0:
        return community, False

    order = list(range(n))
    random.Random(seed).shuffle(order)  # a fixed seed keeps results reproducible run to run
    moved_any = False
    changed = True
    while changed:
        changed = False
        for i in order:
            ci = community[i]
            community_degree[ci] -= degree[i]
            neighbor_gain: dict[int, float] = {}
            for j, w in adj[i].items():
                cj = community[j]
                neighbor_gain[cj] = neighbor_gain.get(cj, 0.0) + w
            best_c, best_gain = ci, neighbor_gain.get(ci, 0.0) - community_degree[ci] * degree[i] / m2
            for c, k_i_in in neighbor_gain.items():
                gain = k_i_in - community_degree[c] * degree[i] / m2
                if gain > best_gain + 1e-12:
                    best_gain, best_c = gain, c
            community[i] = best_c
            community_degree[best_c] += degree[i]
            if best_c != ci:
                changed = True
                moved_any = True

    # renumber to a dense 0..k-1 range so the next aggregation's node ids
    # are compact, not sparse original community labels
    remap: dict[int, int] = {}
    for c in community:
        if c not in remap:
            remap[c] = len(remap)
    return [remap[c] for c in community], moved_any


def _aggregate(n_communities: int, community: list[int], adj: list[dict[int, float]],
               self_loop: list[float]) -> list[tuple[int, int, float]]:
    """Collapse each community into one node. A self-loop's weight is that
    community's own internal edges (each counted once, not twice, matching
    _adjacency's undirected-edge convention), PLUS whatever self-loop
    weight its members already carried in from the level below - a
    member that is itself a previously-collapsed community brings its own
    internal cohesion with it, and dropping that here is what silently
    corrupted every level past the first (see _adjacency's docstring)."""
    agg: dict[tuple[int, int], float] = {}
    for i, neighbors in enumerate(adj):
        ci = community[i]
        for j, w in neighbors.items():
            if j < i:
                continue  # each undirected edge appears twice in adj; take it once
            cj = community[j]
            key = (ci, cj) if ci <= cj else (cj, ci)
            agg[key] = agg.get(key, 0.0) + w
    for i, loop_w in enumerate(self_loop):
        if loop_w > 0:
            ci = community[i]
            agg[(ci, ci)] = agg.get((ci, ci), 0.0) + loop_w
    return [(a, b, w) for (a, b), w in agg.items()]


_HUB_EXCLUDE_PERCENTILE = 95   # nodes above this degree percentile bridge everything and
                               # tell you nothing about which subsystem they're "really" in
_MAX_COMMUNITY_FRACTION = 0.25   # communities bigger than this share of the graph get split
_MIN_SPLIT_SIZE = 10              # below this, a community is small enough to just keep


def _louvain_core(n: int, edges: list[tuple[int, int, float]], seed: int) -> dict[int, int]:
    """The Louvain algorithm itself, no hub handling or size splitting -
    plain multi-level local-moving + aggregation, returning {node: cid}
    with no particular numbering. cluster() below is what a caller wants;
    this is the part every layer of it (initial pass, hub reattachment
    excluded, oversized-community re-split) runs."""
    if n == 0:
        return {}
    adj, degree, self_loop = _adjacency(n, edges)
    m2 = sum(degree)
    if m2 <= 0:
        return {i: i for i in range(n)}

    # level 0: original nodes. Each later level's "node" is a community from
    # the level below - membership is tracked back through all of them so
    # the final assignment lands on original node indices, not level-k ones.
    level_adj, level_degree, level_self_loop, level_n = adj, degree, self_loop, n
    membership = list(range(n))

    while True:
        community, moved = _local_moving(level_n, level_adj, level_degree, m2, seed)
        n_communities = len(set(community))
        membership = [community[c] for c in membership]
        if not moved or n_communities == level_n:
            break
        agg_edges = _aggregate(n_communities, community, level_adj, level_self_loop)
        level_adj, level_degree, level_self_loop = _adjacency(n_communities, agg_edges)
        level_n = n_communities
    return {i: cid for i, cid in enumerate(membership)}


def _split_oversized(members: list[int], edges: list[tuple[int, int, float]], seed: int) -> list[list[int]]:
    """Re-run Louvain on one community's own induced subgraph. Used when a
    community swallowed a large share of the graph - almost always a real
    subsystem plus a hub-adjacent blob rather than one true community."""
    local_id = {node: i for i, node in enumerate(members)}
    sub_edges = [(local_id[a], local_id[b], w) for a, b, w in edges
                if a in local_id and b in local_id]
    if not sub_edges:
        return [[m] for m in members]
    sub_result = _louvain_core(len(members), sub_edges, seed)
    groups: dict[int, list[int]] = {}
    for local_i, cid in sub_result.items():
        groups.setdefault(cid, []).append(members[local_i])
    if len(groups) <= 1:
        return [members]
    return list(groups.values())


def cluster(n: int, edges: list[tuple[int, int, float]], seed: int = 42) -> dict[int, int]:
    """Run Louvain with the two refinements a raw pass needs on a real,
    hub-heavy graph (a file mentioned across half the codebase otherwise
    drags every subsystem that touches it into one blob):

      1. Nodes above the 95th degree percentile are excluded from the
         initial partitioning and reattached afterward to whichever
         neighbouring community they connect to most - so a hub still ends
         up SOMEWHERE, it just doesn't get to decide everyone else's
         community on the way in.
      2. Communities bigger than 25% of the graph are re-clustered on their
         own induced subgraph, since a community that large is usually a
         real subsystem plus everything a hub happened to touch, not one
         true community.

    Returns {node_index: community_id}, communities numbered 0..k-1,
    largest first. Isolated nodes each land in their own singleton
    community rather than being dropped."""
    if n == 0:
        return {}
    degree = degrees(n, edges)
    total_degree = sum(degree)
    if total_degree <= 0:
        return {i: i for i in range(n)}

    hub_nodes: set[int] = set()
    if n >= 20:  # percentile exclusion is noise on a small graph
        sorted_deg = sorted(degree)
        idx = max(0, int(n * _HUB_EXCLUDE_PERCENTILE / 100) - 1)
        threshold = sorted_deg[idx]
        hub_nodes = {i for i in range(n) if degree[i] > threshold}

    core_edges = [(a, b, w) for a, b, w in edges if a not in hub_nodes and b not in hub_nodes]
    core_result = _louvain_core(n, core_edges, seed)
    # core_result assigns every node an id (isolated hubs included, alone),
    # but only non-hub membership is real - hubs get reattached next
    members: dict[int, list[int]] = {}
    for node in range(n):
        if node in hub_nodes:
            continue
        members.setdefault(core_result[node], []).append(node)

    if hub_nodes:
        node_community: dict[int, int] = {node: cid for cid, ms in members.items() for node in ms}
        adj, _deg, _self_loop = _adjacency(n, edges)
        next_cid = (max(members.keys(), default=-1)) + 1
        for hub in sorted(hub_nodes):
            votes: dict[int, float] = {}
            for nb, w in adj[hub].items():
                cid = node_community.get(nb)
                if cid is not None:
                    votes[cid] = votes.get(cid, 0.0) + w
            if votes:
                best = max(votes, key=lambda c: (votes[c], -c))
                members.setdefault(best, []).append(hub)
                node_community[hub] = best
            else:
                members[next_cid] = [hub]
                node_community[hub] = next_cid
                next_cid += 1

    max_size = max(_MIN_SPLIT_SIZE, int(n * _MAX_COMMUNITY_FRACTION))
    final: list[list[int]] = []
    for ms in members.values():
        if len(ms) > max_size:
            final.extend(_split_oversized(ms, edges, seed))
        else:
            final.append(ms)

    ordered = sorted(final, key=lambda ms: (-len(ms), min(ms)))
    return {node: cid for cid, ms in enumerate(ordered) for node in ms}


def degrees(n: int, edges: list[tuple[int, int, float]]) -> list[float]:
    """Weighted degree of every node - the same figure the god-node ranking
    and label_by_hub use, exposed separately so a caller doesn't need to
    rebuild the adjacency structure itself just to get it."""
    _adj, degree, _self_loop = _adjacency(n, edges)
    return degree


def cohesion_score(n: int, edges: list[tuple[int, int, float]], members: list[int]) -> float:
    """Ratio of actual intra-community edges to the maximum possible."""
    k = len(members)
    if k <= 1:
        return 1.0
    in_set = set(members)
    actual = sum(1 for a, b, _w in edges if a in in_set and b in in_set)
    possible = k * (k - 1) / 2
    return actual / possible if possible > 0 else 0.0


def label_by_hub(members: list[int], degree: list[float], labels: list[str]) -> str:
    """Name a community after its highest-degree member - the structural
    hub - so a UI reads 'webui.py' instead of 'Community 4'. Ties break by
    node index for determinism."""
    if not members:
        return "Community"
    hub = min(members, key=lambda i: (-degree[i], i))
    name = (labels[hub] or "").strip()
    return name or f"Community {hub}"
