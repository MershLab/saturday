"""memcluster.py is vendored from graphify (Apache-2.0, see
THIRD_PARTY_NOTICES.md) - these tests exercise it through Saturday's own
integration point (memgraph.build_graph's community pass), not as a
reimplementation check of graphify's own algorithm."""
from __future__ import annotations

import pytest

try:
    import networkx as nx

    HAS_NX = True
except Exception:
    HAS_NX = False

pytestmark = pytest.mark.skipif(not HAS_NX, reason="graph extra (networkx) not installed")


def test_cluster_separates_two_disjoint_dense_groups():
    from saturday import memcluster

    g = nx.Graph()
    # two fully-connected triangles, no edges between them - the textbook
    # case any real community-detection pass must get right
    g.add_edges_from([(0, 1), (1, 2), (0, 2)])
    g.add_edges_from([(10, 11), (11, 12), (10, 12)])

    communities = memcluster.cluster(g)
    by_node = {n: cid for cid, members in communities.items() for n in members}
    assert by_node[0] == by_node[1] == by_node[2]
    assert by_node[10] == by_node[11] == by_node[12]
    assert by_node[0] != by_node[10]


def test_cluster_empty_graph_returns_empty():
    from saturday import memcluster

    assert memcluster.cluster(nx.Graph()) == {}


def test_label_communities_by_hub_names_after_highest_degree_member():
    from saturday import memcluster

    g = nx.Graph()
    g.add_node("hub", label="hub.py")
    g.add_node("leaf", label="leaf.py")
    g.add_edge("hub", "leaf")
    g.add_node("other", label="other.py")
    g.add_edge("hub", "other")

    labels = memcluster.label_communities_by_hub(g, {0: ["hub", "leaf", "other"]})
    assert labels[0] == "hub.py"


def test_cohesion_score_full_triangle_is_one():
    from saturday import memcluster

    g = nx.Graph()
    g.add_edges_from([(0, 1), (1, 2), (0, 2)])
    assert memcluster.cohesion_score(g, [0, 1, 2]) == 1.0
