"""memcluster.py: a native Louvain implementation, no dependency. Written
directly against plain (node_index, edges) tuples - the same shape
memgraph.py's own _Builder already produces - rather than a graph library
object, so there's nothing to install and nothing to gate these tests on."""
from __future__ import annotations

from saturday import memcluster


def test_cluster_separates_two_disjoint_dense_groups():
    edges = [(0, 1, 1.0), (1, 2, 1.0), (0, 2, 1.0), (10, 11, 1.0), (11, 12, 1.0), (10, 12, 1.0)]
    result = memcluster.cluster(13, edges)
    assert result[0] == result[1] == result[2]
    assert result[10] == result[11] == result[12]
    assert result[0] != result[10]


def test_cluster_empty_graph_returns_empty():
    assert memcluster.cluster(0, []) == {}


def test_cluster_isolated_nodes_each_get_their_own_community():
    edges = [(0, 1, 1.0), (1, 2, 1.0), (0, 2, 1.0)]
    result = memcluster.cluster(5, edges)  # nodes 3, 4 have no edges at all
    assert result[3] != result[4]
    assert result[3] not in (result[0],)


def test_cluster_finds_four_clean_clusters_with_no_ambiguity():
    """The real regression case: this exact shape (4 dense groups, no cross
    edges) is what first exposed the aggregation bug - a self-loop
    representing a community's own internal cohesion was silently dropped
    every time the graph got coarsened past level 0, corrupting degree
    accounting and merging unrelated communities into one by level 2."""
    edges = []
    for k in range(4):
        base = k * 8
        nodes = list(range(base, base + 8))
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                edges.append((nodes[i], nodes[j], 1.0))
    result = memcluster.cluster(32, edges)
    groups: dict[int, set[int]] = {}
    for node, cid in result.items():
        groups.setdefault(cid, set()).add(node)
    assert len(groups) == 4
    for members in groups.values():
        assert {m // 8 for m in members} == {next(iter(members)) // 8}


def test_cluster_real_graph_has_no_dominant_mega_community():
    """Direct regression test for the aggregation bug on data big enough to
    need multiple levels: without the fix, Saturday's own real codebase
    graph collapsed into one ~180-node blob plus singletons (modularity
    0.0 - the mathematical signature of a trivial, structureless
    partition). A healthy result has no single community anywhere near
    the size of the graph."""
    from saturday.memgraph import build_graph

    out = build_graph(workspace_root=".")
    communities = out.get("communities", [])
    assert len(communities) > 5
    n = len(out["nodes"])
    assert max(c["size"] for c in communities) < n * 0.5


def test_degrees_counts_self_loops_twice():
    # a self-loop of weight 3 on node 0, plus a real edge to node 1
    edges = [(0, 0, 3.0), (0, 1, 1.0)]
    assert memcluster.degrees(2, edges) == [1.0 + 2 * 3.0, 1.0]


def test_label_by_hub_names_after_highest_degree_member():
    edges = [(0, 1, 1.0), (0, 2, 1.0)]
    degree = memcluster.degrees(3, edges)
    labels = ["hub.py", "leaf.py", "other.py"]
    assert memcluster.label_by_hub([0, 1, 2], degree, labels) == "hub.py"


def test_cohesion_score_full_triangle_is_one():
    edges = [(0, 1, 1.0), (1, 2, 1.0), (0, 2, 1.0)]
    assert memcluster.cohesion_score(3, edges, [0, 1, 2]) == 1.0


def test_cohesion_score_no_internal_edges_is_zero():
    edges = [(0, 1, 1.0)]  # nodes 2, 3 have no edges between them
    assert memcluster.cohesion_score(4, edges, [2, 3]) == 0.0
