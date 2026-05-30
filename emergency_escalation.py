"""
emergency_escalation.py — Deterministic escalation for unsafe medication situations.

Categories (in priority order):
  1. OVERDOSE       — patient took too many pills, poison, accidental ingestion
  2. SOMEONE_ELSES  — patient mentions taking another person's medication
  3. UNSAFE_COMBO   — mixing with known interaction-risk drugs
  4. WRONG_ROUTE    — crushing, dissolving, snorting extended-release meds
  5. CRITICAL_DOUBT — uncertainty about a critical (is_critical=1) medication

Rules:
  - Any match → immediate EscalationResult; normal flow STOPS.
  - LLM must not handle these cases; router delegates here first.
  - Emergency number is locale-aware via patient_profile['emergency_number'].
  - All triggers write to ingestion_logs before returning response.
  - Response wording is calm and directive — not alarming, not vague.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from logging_events import log_escalation, DB_PATH_DEFAULT


# ─── Escalation categories ────────────────────────────────────────────────────

class EscalationCategory(str, Enum):
    OVERDOSE       = "OVERDOSE"
    SOMEONE_ELSES  = "SOMEONE_ELSES"
    UNSAFE_COMBO   = "UNSAFE_COMBO"
    WRONG_ROUTE    = "WRONG_ROUTE"
    CRITICAL_DOUBT = "CRITICAL_DOUBT"
    NONE           = "NONE"


# ─── Keyword tables ───────────────────────────────────────────────────────────
# Phrases are matched as substrings (lowercased). Order within each set
# is irrelevant; all are checked. Longer/more-specific phrases listed first
# as documentation — the matching logic iterates the full set.

_OVERDOSE_PHRASES: frozenset[str] = frozenset({
    "overdose", "over dose",
    "took too many", "took too much",
    "took four", "took five", "took six", "took seven", "took eight",
    "four pills", "five pills", "six pills", "seven pills",
    "four tablets", "five tablets", "six tablets",
    "double dose", "triple dose",
    "by mistake", "by accident", "accidentally",
    "swallowed too many", "swallowed too much",
    "poison", "poisoned",
    "emergency", "call 995", "call 911", "call 999", "call 112",
    "not breathing", "unconscious", "fainted", "collapsed",
    "not waking up", "won't wake up",
})

_SOMEONE_ELSES_PHRASES: frozenset[str] = frozenset({
    "husband's pill", "husband's medication", "husband's tablet",
    "wife's pill", "wife's medication", "wife's tablet",
    "my husband's", "my wife's", "my son's", "my daughter's",
    "my mother's pill", "my father's pill",
    "someone else's", "belongs to",
    "not mine", "not my pill", "not my medication",
    "found this pill", "found a pill",
    "neighbour's", "neighbor's",
    "friend's pill", "friend's medication",
})

_UNSAFE_COMBO_PHRASES: frozenset[str] = frozenset({
    # NSAIDs — dangerous with warfarin / blood thinners
    "ibuprofen", "advil", "nurofen",
    "naproxen", "aleve", "naprosyn",
    "diclofenac", "voltaren",
    "aspirin",                         # at non-cardio dose
    "mefenamic acid", "ponstan",
    # Opioids / sedatives — dangerous combination risk
    "old pain pill", "pain pill", "painkiller", "pain killer",
    "tramadol", "codeine", "morphine", "oxycodone",
    "sleeping pill", "sleeping tablet",
    "valium", "diazepam", "xanax", "alprazolam",
    # Anticoagulant interactions
    "fish liver oil",                  # high-dose omega-3 potentiates warfarin
    "st john", "st. john",             # herb interaction
    "grapefruit",                      # CYP3A4 inhibitor
    # General flags
    "old prescription", "old medication", "expired pill",
    "someone gave me", "my friend gave me",
    "leftover pill", "leftover medication",
})

_WRONG_ROUTE_PHRASES: frozenset[str] = frozenset({
    "crush", "crushed", "crushing",
    "chew", "chewed", "chewing",
    "dissolve", "dissolved", "dissolving",
    "break in half",                   # not same as score-line splitting
    "cut the pill",
    "snort", "inject",
    "grind", "ground up",
    "open the capsule",
    "opened the capsule",
    "split the capsule",
})

# Phrases that signal doubt about a critical medication specifically
# (non-critical med uncertainty handled by safety_router uncertainty check)
_CRITICAL_DOUBT_PHRASES: frozenset[str] = frozenset({
    "skipped my insulin", "missed my insulin",
    "skipped my blood thinner", "missed my blood thinner",
    "skipped my warfarin", "missed my warfarin",
    "stopped taking", "stopped my",
    "ran out", "run out",
    "no more pills", "no more medication",
    "haven't taken in", "haven't taken it in",
    "days without", "week without",
})

_CATEGORY_MAP: list[tuple[EscalationCategory, frozenset[str]]] = [
    (EscalationCategory.OVERDOSE,      _OVERDOSE_PHRASES),
    (EscalationCategory.SOMEONE_ELSES, _SOMEONE_ELSES_PHRASES),
    (EscalationCategory.UNSAFE_COMBO,  _UNSAFE_COMBO_PHRASES),
    (EscalationCategory.WRONG_ROUTE,   _WRONG_ROUTE_PHRASES),
    (EscalationCategory.CRITICAL_DOUBT,_CRITICAL_DOUBT_PHRASES),
]


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class EscalationResult:
    category:       EscalationCategory
    matched_phrase: str
    message:        str
    log_id:         Optional[int] = None
    is_emergency:   bool          = False


# ─── Locale helper ───────────────────────────────────────────────────────────

def _emergency_number(db_path: str) -> str:
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT value FROM patient_profile WHERE key = 'emergency_number'")
        row = cur.fetchone()
        con.close()
        return row[0] if row else "995"   # Singapore default
    except Exception:
        return "995"


def _caregiver_name(db_path: str) -> str:
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT value FROM patient_profile WHERE key = 'caregiver_name'")
        row = cur.fetchone()
        con.close()
        return row[0] if row else "your caregiver"
    except Exception:
        return "your caregiver"


# ─── Response builders ────────────────────────────────────────────────────────

def _build_message(
    category:      EscalationCategory,
    emergency_num: str,
    caregiver:     str,
) -> tuple[str, bool]:
    """Return (message, is_emergency)."""

    if category == EscalationCategory.OVERDOSE:
        return (
            f"This sounds urgent. Please call {emergency_num} right now "
            f"or ask someone nearby to call for you.\n"
            f"Tell them which medication was taken and roughly how much.\n"
            f"I have notified {caregiver}.\n"
            f"Do not take anything else until help arrives."
        ), True

    if category == EscalationCategory.SOMEONE_ELSES:
        return (
            "Please do not take that medication — it belongs to someone else "
            "and may not be safe for you.\n"
            f"I have flagged this for {caregiver} to check.\n"
            "Only take medications from your own list."
        ), False

    if category == EscalationCategory.UNSAFE_COMBO:
        return (
            "I need to stop here. That medication may not be safe to take "
            "alongside your current prescriptions.\n"
            "Please do not take it right now.\n"
            "Contact your doctor or pharmacist before taking anything new.\n"
            f"I have made a note for {caregiver}."
        ), False

    if category == EscalationCategory.WRONG_ROUTE:
        return (
            "Please do not crush, chew, or split that medication "
            "unless your doctor has told you it is safe to do so.\n"
            "Some pills must be swallowed whole — changing the form can be dangerous.\n"
            f"I have flagged this for {caregiver} to check."
        ), False

    if category == EscalationCategory.CRITICAL_DOUBT:
        return (
            "Missing doses of that medication can be serious.\n"
            "Please do not try to catch up by taking extra doses.\n"
            f"Contact your doctor or ask {caregiver} to call the clinic today.\n"
            "I have recorded this concern."
        ), False

    # Should not reach here; safe fallback
    return (
        "I am not able to process that request safely right now.\n"
        f"Please ask {caregiver} for help."
    ), False


# ─── Core check ──────────────────────────────────────────────────────────────

def check_escalation(
    raw_transcript: str,
    db_path:        str = DB_PATH_DEFAULT,
) -> Optional[EscalationResult]:
    """
    Scan transcript for escalation triggers.

    Returns EscalationResult if any trigger matched, else None.
    Caller must treat a non-None result as a hard stop —
    do NOT continue the normal medication flow.

    Logs to ingestion_logs before returning.
    """
    lower = raw_transcript.lower()

    for category, phrases in _CATEGORY_MAP:
        for phrase in phrases:
            if phrase in lower:
                emergency_num = _emergency_number(db_path)
                caregiver     = _caregiver_name(db_path)
                message, is_emergency = _build_message(category, emergency_num, caregiver)

                log_id = log_escalation(
                    raw_transcript=raw_transcript,
                    reason=f"{category.value}: matched '{phrase}'",
                    emergency=is_emergency,
                    db_path=db_path,
                )

                return EscalationResult(
                    category=category,
                    matched_phrase=phrase,
                    message=message,
                    log_id=log_id,
                    is_emergency=is_emergency,
                )

    return None


def is_escalation_transcript(raw_transcript: str) -> bool:
    """
    Fast boolean pre-check with no DB access and no logging.
    Use in unit tests or pre-flight guards only.
    """
    lower = raw_transcript.lower()
    return any(
        phrase in lower
        for _, phrases in _CATEGORY_MAP
        for phrase in phrases
    )
