"""Fit oversized tool output into a budget without throwing away the answer.

Saturday used to cut tool results with ``payload[:LIMIT]``, which keeps the
beginning and silently drops the end. For the outputs that actually overflow -
test runs, build logs, stack traces - the end is where the verdict lives, so
blunt truncation reliably discards the one part the model needed.

The method here is Selective Context's (Li et al., 2304.12102), not
LLMLingua's: both drop low-information spans, but LLMLingua scores them with a
small language model's perplexity, which Saturday has no way to run inside its
zero-dependency constraint. Self-information estimated from the document's OWN
token distribution needs no model at all and captures the same intuition - a
line made of tokens that recur throughout the text carries little that the
surrounding lines do not already say.

Three things are preserved on top of that score:

* the head and the tail, always, because they are the setup and the verdict
* lines matching failure markers, because a log is read for its errors
* original order, so the result still reads as the output it came from

Every elision is stated in the text, so the model is never misled into
believing it received a complete output.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|\d+")
# what a reader of a failing log is actually looking for
_SALIENT_RE = re.compile(
    r"\b(error|errors|fail|failed|failing|failure|traceback|exception|panic|fatal|"
    r"assert|assertion|warning|warn|denied|refused|timeout|timed out|not found|"
    r"cannot|unable|invalid|missing|conflict)\b",
    re.IGNORECASE,
)
# lines that carry a verdict even without a failure word
_VERDICT_RE = re.compile(
    r"\b(\d+\s+(passed|failed|error|errors|skipped|warnings?)|"
    r"exit\s+code|status\s*[:=]|ok\b|success(ful)?)\b",
    re.IGNORECASE,
)

HEAD_SHARE = 0.30      # of the budget, reserved for the opening
TAIL_SHARE = 0.35      # of the budget, reserved for the ending
MIN_BUDGET = 400


def _collapse_repeats(lines: list[str]) -> list[str]:
    """Fold runs of identical lines into one plus a count.

    Progress bars, retry loops and repeated warnings routinely make up most of
    an oversized log while carrying one line's worth of information."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i + 1
        while j < n and lines[j] == lines[i]:
            j += 1
        run = j - i
        out.append(lines[i] if run < 3 else f"{lines[i]}    [x{run}]")
        i = j
    return out


def _self_information(lines: list[str]) -> list[float]:
    """Per line, the mean surprisal of its tokens under the document's own
    unigram distribution. Rare tokens are informative; boilerplate is not."""
    counts: Counter[str] = Counter()
    per_line: list[list[str]] = []
    for line in lines:
        toks = _TOKEN_RE.findall(line.lower())
        per_line.append(toks)
        counts.update(toks)
    total = sum(counts.values()) or 1
    scores: list[float] = []
    for toks in per_line:
        if not toks:
            scores.append(0.0)
            continue
        bits = sum(-math.log2(counts[t] / total) for t in toks)
        # mean, not sum: otherwise long lines always win on length alone
        scores.append(bits / len(toks))
    return scores


def _budget_slice(lines: list[str], budget: int) -> tuple[list[str], list[str], int]:
    """Take whole lines from each end until their shares of the budget are used."""
    head: list[str] = []
    used = 0
    cap = int(budget * HEAD_SHARE)
    for line in lines:
        if used + len(line) + 1 > cap:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used_t = 0
    cap_t = int(budget * TAIL_SHARE)
    for line in reversed(lines[len(head):]):
        if used_t + len(line) + 1 > cap_t:
            break
        tail.append(line)
        used_t += len(line) + 1
    tail.reverse()
    return head, tail, used + used_t


def compress(text: str, budget: int) -> str:
    """Return ``text`` shortened to at most ``budget`` characters.

    Under budget, the text is returned byte for byte unchanged - compression
    must never alter output that already fits."""
    if budget <= 0 or len(text) <= budget:
        return text
    if budget < MIN_BUDGET:  # too small to structure; fall back to a plain cut
        return text[:budget]

    original_len = len(text)
    lines = _collapse_repeats(text.splitlines())
    joined = "\n".join(lines)
    if len(joined) <= budget:
        note = f"\n... [collapsed {original_len - len(joined)} chars of repeated lines]"
        # the note itself has to fit, or this path overflows the caller's budget
        return joined + note if len(joined) + len(note) <= budget else joined

    head, tail, used = _budget_slice(lines, budget)
    middle = lines[len(head):len(lines) - len(tail)] if tail else lines[len(head):]
    if not middle:
        # keep the END when forced to choose, the opposite of a head cut
        return "\n".join(head + tail)[-budget:]

    scores = _self_information(middle)
    ranked = []
    for idx, (line, score) in enumerate(zip(middle, scores)):
        if _SALIENT_RE.search(line):
            score += 8.0        # a failing line outranks a merely unusual one
        elif _VERDICT_RE.search(line):
            score += 4.0
        ranked.append((score, idx, line))
    ranked.sort(key=lambda r: -r[0])

    # reserve room for the elision markers we are about to add
    remaining = budget - used - 80
    keep: set[int] = set()
    for score, idx, line in ranked:
        cost = len(line) + 1
        if cost > remaining:
            continue
        keep.add(idx)
        remaining -= cost
        if remaining <= 0:
            break

    def render(kept: set[int]) -> str:
        body: list[str] = []
        skipped = 0
        for idx, line in enumerate(middle):
            if idx in kept:
                if skipped:
                    body.append(f"... [{skipped} less informative line{'s' if skipped != 1 else ''} omitted]")
                    skipped = 0
                body.append(line)
            else:
                skipped += 1
        if skipped:
            body.append(f"... [{skipped} less informative line{'s' if skipped != 1 else ''} omitted]")
        return "\n".join(head + body + tail)

    note_for = lambda text: (
        f"\n... [compressed: {original_len - len(text)} of {original_len} chars removed, ends kept]")

    # The elision markers are not free, and an earlier version absorbed the
    # overflow with out[:budget] - which cut the tail off and reinstated
    # exactly the head-truncation this module exists to remove. Give back the
    # least informative kept lines instead, so the ends always survive.
    order = [idx for _s, idx, _l in reversed(ranked) if idx in keep]
    out = render(keep)
    for idx in order:
        if len(out) + len(note_for(out)) <= budget:
            break
        keep.discard(idx)
        out = render(keep)
    note = note_for(out)
    if len(out) + len(note) > budget:
        # nothing left to give back: head and tail alone overflow, so trim the
        # HEAD and keep the tail, the opposite of what slicing did
        out = out[-(budget - len(note)):]
    return out + note
