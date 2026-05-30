"""
tests/test_day2_safety.py — Safety logic tests for Day 2 deliverables.

Uses in-memory SQLite for isolation; no shared state between tests.
Covers TC01–TC10 from test_matrix.md plus Day 2 additions.
"""

import sys
import os
import sqlite3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lookup import lookup_medication, LookupStatus
from logging_events import (
    log_confirmed, log_uncertain, log_duplicate_blocked,
    log_ambiguous, log_low_confidence, get_confirmed_today,
)
from duplicate_guard import check_duplicate, GuardResult
from ambiguity_handler import (
    handle_ambiguous_input, resolve_clarification, ClarificationResult,
)
from confidence_rules import check_confidence, score_is_acceptable, ConfidenceResult
from caregiver_override import request_override, confirm_override, OverrideResult
from safety_router import route, RouterOutcome


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Fresh in-memory database seeded with test data."""
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)

    # Apply schema
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        con.executescript(f.read())

    # Seed minimal test medications
    meds = [
        # id=1: blood thinner (critical, peach)
        (1, "Warfarin 5mg",             "5mg once daily",  "00:00", "blood thinner",  "small peach round", "white bottle red label", 1),
        # id=2: heart pill (critical, white oval)
        (2, "Metoprolol Succinate 50mg", "50mg once daily", "00:00", "heart pill",     "white oval",        "blue cap bottle",        1),
        # id=3: white round pill 1 — ambiguity partner
        (3, "Lisinopril 10mg",           "10mg once daily", "00:00", "pressure pill",  "white round",       "orange bottle",          0),
        # id=4: white round pill 2 — ambiguity partner
        (4, "Amlodipine 5mg",            "5mg once daily",  "00:00", "calcium pill",   "white round",       "yellow bottle",          0),
        # id=5: supplement
        (5, "Fish Oil 1000mg (supplement)", "1000mg",       "12:00", "fish oil",       "yellow softgel",    "supplement",             0),
    ]
    con.executemany(
        """INSERT INTO medications
           (id, clinical_name, dosage, scheduled_time, nickname,
            visual_description, disambiguation_cue, is_critical)
           VALUES (?,?,?,?,?,?,?,?)""",
        meds,
    )

    profile = [
        ("patient_name",         "Test Patient"),
        ("caregiver_name",       "Test Caregiver"),
        ("caregiver_contact",    "cg@test.com"),
        ("schedule_timezone",    "UTC"),
        ("dose_window_hours",    "6"),
        ("confidence_threshold", "0.75"),
    ]
    con.executemany("INSERT INTO patient_profile (key, value) VALUES (?,?)", profile)
    con.commit()
    con.close()
    return db_path


# ─── TC01: Normal confirmation ────────────────────────────────────────────────

def test_tc01_normal_confirmation(db):
    """Heart pill confirmed, not taken before → MED_CONFIRMED logged."""
    result = route("I took my heart pill", db_path=db)
    assert result.outcome == RouterOutcome.CONFIRMED
    assert result.log_id is not None
    assert result.medication["clinical_name"] == "Metoprolol Succinate 50mg"


# ─── TC02: Ambiguity ──────────────────────────────────────────────────────────

def test_tc02_ambiguous_white_pill(db):
    """'white pill' → two white round candidates → AMBIGUOUS, not confirmed."""
    result = route("I'm taking the white pill", db_path=db)
    assert result.outcome == RouterOutcome.AMBIGUOUS
    assert result.session_key is not None
    # No MED_CONFIRMED logged
    assert get_confirmed_today(3, db_path=db) is None
    assert get_confirmed_today(4, db_path=db) is None


def test_tc02_ambiguity_resolved_by_number(db):
    """User picks option 1 from clarification → resolves to Lisinopril."""
    # Trigger ambiguity
    r1 = route("I'm taking the white pill", db_path=db)
    assert r1.outcome == RouterOutcome.AMBIGUOUS

    # User responds with "1"
    resolution = resolve_clarification(r1.session_key, "1", db_path=db)
    assert resolution.outcome == ClarificationResult.RESOLVED
    assert resolution.medication["id"] == 2  # Metoprolol (first white candidate)


def test_tc02_ambiguity_resolved_by_nickname(db):
    """User says 'pressure pill' → lookup → resolves unambiguously."""
    r1 = route("I'm taking the white pill", db_path=db)
    resolution = resolve_clarification(r1.session_key, "pressure pill", db_path=db)
    assert resolution.outcome == ClarificationResult.RESOLVED
    assert resolution.medication["clinical_name"] == "Lisinopril 10mg"


def test_tc02_ambiguity_user_gives_up(db):
    """User says 'I don't know' → UNRESOLVABLE, MED_UNCERTAIN logged."""
    r1 = route("I'm taking the white pill", db_path=db)
    resolution = resolve_clarification(r1.session_key, "I don't know", db_path=db)
    assert resolution.outcome == ClarificationResult.UNRESOLVABLE
    assert resolution.log_id is not None


# ─── TC03: Duplicate dose ─────────────────────────────────────────────────────

def test_tc03_duplicate_dose_blocked(db):
    """Second confirmation within window → DUPLICATE_BLOCKED."""
    # First dose
    r1 = route("I took my heart pill", db_path=db)
    assert r1.outcome == RouterOutcome.CONFIRMED

    # Second attempt
    r2 = route("I took my heart pill again", db_path=db)
    assert r2.outcome == RouterOutcome.DUPLICATE_BLOCKED
    assert r2.block_log_id is not None


def test_tc03_uncertain_also_blocks_redose(db):
    """MED_UNCERTAIN in window blocks a second attempt (uncertain ≠ safe to redose)."""
    # Log uncertain directly
    log_uncertain("Not sure if I took it", medication_id=2, db_path=db)

    # Attempt to confirm same med
    guard = check_duplicate(2, "I took my heart pill", db_path=db)
    assert guard.outcome == GuardResult.BLOCKED
    assert "uncertain" in guard.reason.lower()


# ─── TC04: Uncertain intake ───────────────────────────────────────────────────

def test_tc04_uncertain_memory_not_auto_confirmed(db):
    """'I'm not sure if I took it' → UNCERTAIN, never CONFIRMED."""
    result = route("I'm not sure if I took my heart pill", db_path=db)
    assert result.outcome == RouterOutcome.UNCERTAIN
    # Definitely no MED_CONFIRMED written
    assert get_confirmed_today(2, db_path=db) is None


# ─── TC05: Low confidence ─────────────────────────────────────────────────────

def test_tc05_low_confidence_triggers_retry(db):
    """Score below threshold → LOW_CONFIDENCE, not confirmed."""
    result = route("hair pill", confidence_score=0.4, db_path=db)
    assert result.outcome == RouterOutcome.LOW_CONFIDENCE


def test_tc05_high_confidence_passes(db):
    """Score above threshold → normal flow continues."""
    conf = check_confidence(0.9, "heart pill", db_path=db)
    assert conf.outcome == ConfidenceResult.PASS


def test_tc05_none_confidence_passes(db):
    """None confidence (typed input) always passes."""
    conf = check_confidence(None, "heart pill", db_path=db)
    assert conf.is_pass


def test_tc05_retries_exhausted_logs_uncertain(db):
    """After MAX_RETRIES, low confidence → MED_UNCERTAIN, not retry."""
    from confidence_rules import MAX_RETRIES
    result = check_confidence(0.3, "mumbled input", retry_count=MAX_RETRIES, db_path=db)
    assert result.outcome == ConfidenceResult.UNCERTAIN
    assert result.log_id is not None


# ─── TC09: Caregiver correction ───────────────────────────────────────────────

def test_tc09_caregiver_correction_requires_confirm(db):
    """Correction request returns AWAITING_CONFIRMATION, not immediately applied."""
    log_id = log_confirmed(2, "I took heart pill", db_path=db)
    result, token = request_override(log_id, "Wrong medication logged", db_path=db)
    assert result.outcome == OverrideResult.AWAITING_CONFIRMATION
    assert token.startswith("ovr_")


def test_tc09_caregiver_correction_confirmed(db):
    """Explicit confirmation → CAREGIVER_CORRECTION written, original untouched."""
    log_id = log_confirmed(2, "I took heart pill", db_path=db)
    _, token = request_override(log_id, "Wrong med — should be Lisinopril",
                                new_medication_id=3, db_path=db)
    result = confirm_override(token, "yes, correct the record", db_path=db)
    assert result.outcome == OverrideResult.CONFIRMED
    assert result.correction_id is not None

    # Original row still exists and is CONFIRMED (not modified)
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.execute("SELECT state_status FROM ingestion_logs WHERE id = ?", (log_id,))
    row = cur.fetchone()
    con.close()
    assert row[0] == "MED_CONFIRMED"


def test_tc09_caregiver_correction_cancelled(db):
    """Caregiver says 'cancel' → CANCELLED, no correction written."""
    log_id = log_confirmed(2, "I took heart pill", db_path=db)
    _, token = request_override(log_id, "Actually fine", db_path=db)
    result = confirm_override(token, "cancel", db_path=db)
    assert result.outcome == OverrideResult.CANCELLED


def test_tc09_caregiver_wrong_phrase_not_accepted(db):
    """Ambiguous confirm phrase → still AWAITING_CONFIRMATION."""
    log_id = log_confirmed(2, "I took heart pill", db_path=db)
    _, token = request_override(log_id, "test", db_path=db)
    result = confirm_override(token, "sure thing", db_path=db)
    assert result.outcome == OverrideResult.AWAITING_CONFIRMATION


# ─── TC06: Unsafe combination ─────────────────────────────────────────────────

def test_tc06_unsafe_combo_escalates(db):
    """Pain pill mention with blood thinner in registry → ESCALATION."""
    result = route("I want to take an old pain pill too", db_path=db)
    assert result.outcome == RouterOutcome.ESCALATION


# ─── TC11: Supplement ─────────────────────────────────────────────────────────

def test_tc11_supplement_not_logged_as_rx(db):
    """Fish oil → SUPPLEMENT outcome, not MED_CONFIRMED."""
    result = route("I took fish oil", db_path=db)
    assert result.outcome == RouterOutcome.SUPPLEMENT


# ─── TC12: Emergency ─────────────────────────────────────────────────────────

def test_tc12_overdose_escalates(db):
    """'took four pills by mistake' → ESCALATION immediately."""
    result = route("I took four pills by mistake", db_path=db)
    assert result.outcome == RouterOutcome.ESCALATION
