"""Retrieval scoring for memory: recency, relevance, salience.

Ranking recall by text match alone answers "what mentions this" when the
question is "what should I remember about this". The three-factor form comes
from Generative Agents (Park et al., 2304.03442), whose retrieval combines
recency, importance and relevance rather than any single signal.

One deliberate departure from that paper. Its *importance* is an LLM's rating
of how significant a memory is, which costs a model call per write; memory
writes happen constantly and a system that bills a call per write is one
people switch off. **Salience** replaces it and is cheaper and better defined:
how much a memory adds to what is already stored, computed as
``1 - max_similarity`` against everything indexed before it. A note restating
something known scores near zero; a genuinely new fact scores near one. It is
measured once at index time, never at query time.

    score = 0.3*recency + 0.5*relevance + 0.2*salience

Similarity is MinHash over character shingles - a small, well understood
algorithm, reimplemented rather than imported to hold the zero-dependency
line. Banded LSH keeps indexing near-linear instead of comparing every new
row against every old one.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

W_RECENCY, W_RELEVANCE, W_SALIENCE = 0.3, 0.5, 0.2
DEFAULT_HALF_LIFE_DAYS = 14.0
SHINGLE = 5          # characters per shingle
NUM_PERM = 64        # MinHash signature length
LSH_BANDS = 16       # bands x rows must equal NUM_PERM
_MASK = (1 << 32) - 1
_WS_RE = re.compile(r"\s+")


def recency(ts: float, now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """exp(-dt / half_life), clamped to [0, 1]. A missing timestamp scores 0."""
    if not ts or ts <= 0 or now <= 0:
        return 0.0
    half_life = max(1e-6, half_life_days * 86400.0)
    dt = max(0.0, now - ts)
    return math.exp(-dt / half_life)


def normalize_relevance(ranks: Sequence[float]) -> list[float]:
    """Map raw match scores onto [0, 1], best first.

    SQLite's bm25() returns a NEGATIVE number where more negative is a better
    match, so this normalizes by position rather than by value: comparing
    magnitudes across queries is meaningless, but order is not."""
    n = len(ranks)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    return [1.0 - (i / (n - 1)) for i in range(n)]


def _shingles(text: str) -> set[str]:
    s = _WS_RE.sub(" ", (text or "").strip().lower())
    if len(s) < SHINGLE:
        return {s} if s else set()
    return {s[i:i + SHINGLE] for i in range(len(s) - SHINGLE + 1)}


def _hash(value: str, seed: int) -> int:
    # deterministic across processes, unlike hash() with randomized seeding
    h = 2166136261 ^ (seed * 16777619)
    for ch in value:
        h = ((h ^ ord(ch)) * 16777619) & _MASK
    return h


def minhash(text: str, num_perm: int = NUM_PERM) -> tuple[int, ...]:
    sh = _shingles(text)
    if not sh:
        return tuple([_MASK] * num_perm)
    return tuple(min(_hash(s, i) for s in sh) for i in range(num_perm))


def jaccard(a: Sequence[int], b: Sequence[int]) -> float:
    """Estimated Jaccard similarity of two MinHash signatures."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def bands(sig: Sequence[int], num_bands: int = LSH_BANDS) -> list[tuple[int, int]]:
    """(band index, band hash) pairs used as LSH buckets."""
    if not sig:
        return []
    rows = max(1, len(sig) // num_bands)
    out = []
    for b in range(num_bands):
        chunk = sig[b * rows:(b + 1) * rows]
        if chunk:
            out.append((b, hash(tuple(chunk)) & _MASK))
    return out


class SalienceIndex:
    """Streaming novelty scorer.

    Feed documents in index order; each is scored against everything already
    seen, then added. Candidates come from shared LSH buckets, so a corpus is
    not compared pairwise against itself."""

    def __init__(self, num_perm: int = NUM_PERM, num_bands: int = LSH_BANDS) -> None:
        self.num_perm = num_perm
        self.num_bands = num_bands
        self._sigs: list[tuple[int, ...]] = []
        self._buckets: dict[tuple[int, int], list[int]] = {}

    def add(self, text: str) -> float:
        """Return salience in [0, 1] for ``text``, then remember it."""
        sig = minhash(text, self.num_perm)
        keys = bands(sig, self.num_bands)
        seen: set[int] = set()
        for key in keys:
            seen.update(self._buckets.get(key, ()))
        best = 0.0
        for idx in seen:
            best = max(best, jaccard(sig, self._sigs[idx]))
            if best >= 0.999:
                break
        me = len(self._sigs)
        self._sigs.append(sig)
        for key in keys:
            self._buckets.setdefault(key, []).append(me)
        return max(0.0, min(1.0, 1.0 - best))


def diffuse(scores: dict[str, float], edges: Iterable[tuple[str, str]],
            damping: float = 0.5) -> dict[str, float]:
    """One hop of graph diffusion: a neighbour of a strong match inherits a
    damped share of it.

    HippoRAG's Personalized PageRank insight at the cheapest depth that still
    helps. One hop is a deliberate stop: it recovers the note that answers the
    question without matching its words, while staying a single pass over the
    edge list."""
    out = dict(scores)
    for a, b in edges:
        sa, sb = scores.get(a, 0.0), scores.get(b, 0.0)
        if sa:
            out[b] = max(out.get(b, 0.0), sa * damping)
        if sb:
            out[a] = max(out.get(a, 0.0), sb * damping)
    return out


def combine(recency_score: float, relevance_score: float, salience_score: float,
            weights: tuple[float, float, float] = (W_RECENCY, W_RELEVANCE, W_SALIENCE)) -> float:
    wr, wl, ws = weights
    return wr * recency_score + wl * relevance_score + ws * salience_score
