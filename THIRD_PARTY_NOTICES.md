# Third-party notices

Saturday is MIT-licensed. This file lists code adapted from other projects
under their own license, as required by that license, rather than Saturday's.

## graphify (github.com/Graphify-Labs/graphify)

**File:** `src/saturday/memcluster.py`
**Source:** `graphify/cluster.py` at github.com/Graphify-Labs/graphify
**License:** Apache License, Version 2.0. Full text:
`docs/third_party/graphify-APACHE-2.0.txt`.
**Copyright:** 2026 Safi Shamsi and the Graphify contributors.

**Changes made:** none to the algorithm. Only a header comment was added to
`memcluster.py` recording this notice and the provenance of the file; every
docstring, comment, and line of code below the header is verbatim from the
source. Saturday calls `cluster()`, `cohesion_score()`, `score_all()`,
`label_communities_by_hub()`, `community_member_sigs()`, and
`remap_communities_to_previous()` from `memgraph.py`'s clustering pass, gated
behind the optional `graph` extra (`pip install saturday[graph]`, adds
`networkx`). The file's own `graspologic`/`graspologic_native` paths are kept
exactly as authored and degrade to plain networkx Louvain when neither is
installed, which is the only tier Saturday's `graph` extra actually provides
today - real Leiden quality via `graspologic` is available to anyone who
separately installs it, unchanged from upstream behavior, but Saturday does
not require or bundle it (see `docs/third_party/graphify-APACHE-2.0.txt` for
why: `graspologic`'s own import chain pulls in umap/pynndescent/numba, real
weight this pass chose not to make Saturday depend on).
