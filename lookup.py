"""
lookup.py — Deterministic medication matching.

Rules (from plan.md + design constraints):
  - 0 matches → UNKNOWN  (do not guess)
  - 1 match   → MATCH    (safe to proceed)
  - 2+ matches→ AMBIGUOUS (must clarify, never auto-pick)

Matching priority:
  1. Exact nickname match (case-insensitive)
  2. Exact clinical_name substring match
  3. Exact visual_description match
  4. Fuzzy nickname/visual (token overlap ≥ threshold)

NEVER returns a single result when evidence supports multiple.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

DB_PATH_DEFAULT = "med_safety.db"

# Minimum token-overlap ratio to count as fuzzy match
FUZZY_MATCH_THRESHOLD = 0.5

# Pronouns and short words that must never be treated as medication queries.
# 'it' matches 'insulin', 'he' could match things, etc.
_LOOKUP_STOPWORDS = frozenset({
    'it', 'it again', 'that', 'this', 'them', 'they',
    'one', 'same', 'other', 'another', 'more',
    'again', 'too', 'also', 'ok', 'okay', 'yes', 'no',
})

# Supplement marker — meds with this suffix handled differently (TC11)
SUPPLEMENT_MARKER = "(supplement)"


class LookupStatus(str, Enum):
    MATCH     = "MATCH"       # exactly one candidate
    AMBIGUOUS = "AMBIGUOUS"   # two or more candidates — must clarify
    UNKNOWN   = "UNKNOWN"     # zero candidates
    SUPPLEMENT= "SUPPLEMENT"  # matched a supplement, not a prescription


@dataclass
class LookupResult:
    status:     LookupStatus
    candidates: list[dict]        = field(default_factory=list)
    match:      Optional[dict]    = None   # populated only when status == MATCH
    query:      str               = ""

    @property
    def is_safe_to_proceed(self) -> bool:
        return self.status == LookupStatus.MATCH

    @property
    def candidate_ids(self) -> list[int]:
        return [c["id"] for c in self.candidates]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text)


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _fuzzy_score(query: str, target: str) -> float:
    """Token overlap ratio: |intersection| / |query_tokens|."""
    qt = _tokens(query)
    tt = _tokens(target)
    if not qt:
        return 0.0
    return len(qt & tt) / len(qt)


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ─── Core Lookup ─────────────────────────────────────────────────────────────

def lookup_medication(query: str, db_path: str = DB_PATH_DEFAULT) -> LookupResult:
    """
    Main entry point. Returns LookupResult with status + candidates.

    Always call this; never access DB directly from safety layer.
    """
    if not query or not query.strip():
        return LookupResult(status=LookupStatus.UNKNOWN, query=query)

    # Reject pure pronouns/stopwords — never match medication
    if query.strip().lower() in _LOOKUP_STOPWORDS:
        return LookupResult(status=LookupStatus.UNKNOWN, query=query)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Fetch all active medications
    cur.execute(
        "SELECT * FROM medications WHERE is_active = 1"
    )
    all_meds = [dict(row) for row in cur.fetchall()]
    con.close()

    norm_query = _normalize(query)

    # ── Pass 1: Exact nickname match ──────────────────────────────────────────
    exact_nick = [
        m for m in all_meds
        if m["nickname"] and _normalize(m["nickname"]) == norm_query
    ]
    if exact_nick:
        return _build_result(exact_nick, query)

    # ── Pass 2: Clinical name substring ──────────────────────────────────────
    exact_clinical = [
        m for m in all_meds
        if norm_query in _normalize(m["clinical_name"])
    ]
    if exact_clinical:
        return _build_result(exact_clinical, query)

    # ── Pass 3: Exact visual description ─────────────────────────────────────
    exact_visual = [
        m for m in all_meds
        if m["visual_description"] and
           _normalize(m["visual_description"]) == norm_query
    ]
    if exact_visual:
        return _build_result(exact_visual, query)

    # ── Pass 4: Fuzzy match (nickname + visual, score ≥ threshold) ───────────
    # Color-mismatch guard: if query has a color and med has a DIFFERENT color, skip.
    # Prevents "white pill" matching "purple capsule".
    COLORS = {"white", "red", "blue", "green", "yellow", "orange",
              "pink", "purple", "brown", "peach", "gray", "grey", "black"}
    query_colors = _tokens(norm_query) & COLORS

    fuzzy_hits = []
    for m in all_meds:
        if query_colors and m["visual_description"]:
            visual_colors = _tokens(m["visual_description"]) & COLORS
            nick_colors   = _tokens(m["nickname"] or "") & COLORS
            med_colors    = visual_colors | nick_colors
            if med_colors and not (query_colors & med_colors):
                continue  # color mismatch — skip

        scores = []
        if m["nickname"]:
            scores.append(_fuzzy_score(norm_query, m["nickname"]))
        if m["visual_description"]:
            scores.append(_fuzzy_score(norm_query, m["visual_description"]))
        best = max(scores) if scores else 0.0
        if best >= FUZZY_MATCH_THRESHOLD:
            fuzzy_hits.append((best, m))

    if fuzzy_hits:
        # Sort descending; keep all that tie at the top score
        fuzzy_hits.sort(key=lambda x: x[0], reverse=True)
        top_score = fuzzy_hits[0][0]
        top_meds = [m for score, m in fuzzy_hits if score >= top_score - 0.05]
        return _build_result(top_meds, query)

    return LookupResult(status=LookupStatus.UNKNOWN, query=query)


def _build_result(candidates: list[dict], query: str) -> LookupResult:
    """Convert candidate list to typed LookupResult."""
    # Separate supplements from prescriptions
    rx = [m for m in candidates if SUPPLEMENT_MARKER not in m["clinical_name"]]
    supps = [m for m in candidates if SUPPLEMENT_MARKER in m["clinical_name"]]

    # Supplement-only match
    if supps and not rx:
        return LookupResult(
            status=LookupStatus.SUPPLEMENT,
            candidates=supps,
            match=supps[0] if len(supps) == 1 else None,
            query=query,
        )

    # Mixed supplement + Rx → only evaluate Rx candidates
    active = rx if rx else candidates

    if len(active) == 1:
        return LookupResult(
            status=LookupStatus.MATCH,
            candidates=active,
            match=active[0],
            query=query,
        )

    return LookupResult(
        status=LookupStatus.AMBIGUOUS,
        candidates=active,
        query=query,
    )


# ─── Disambiguation helpers (called after user clarifies) ────────────────────

def resolve_by_id(medication_id: int, db_path: str = DB_PATH_DEFAULT) -> Optional[dict]:
    """Fetch a single medication by primary key. Returns None if not found."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM medications WHERE id = ? AND is_active = 1", (medication_id,))
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None


def candidates_summary(candidates: list[dict]) -> str:
    """
    Human-readable summary of ambiguous candidates for clarification prompt.
    Example: "1) Lisinopril 10mg (orange bottle)  2) Amlodipine 5mg (yellow bottle)"
    """
    parts = []
    for i, m in enumerate(candidates, start=1):
        cue = m.get("disambiguation_cue") or m.get("dosage") or ""
        parts.append(f"{i}) {m['clinical_name']} — {cue}")
    return "\n".join(parts)


# ─── Quick smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "heart pill"
    result = lookup_medication(query)
    print(f"Query   : {result.query!r}")
    print(f"Status  : {result.status.value}")
    if result.match:
        print(f"Match   : {result.match['clinical_name']}")
    if result.candidates:
        print("Candidates:")
        for c in result.candidates:
            print(f"  id={c['id']} {c['clinical_name']} | nick={c['nickname']} | visual={c['visual_description']}")
