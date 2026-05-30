"""
tests/test_scenarios.py — End-to-end scenario tests aligned to scenarios.md.

Each test maps to a named scenario and covers the complete deterministic path
through safety_router + supporting modules. No Hermes API calls made here —
safety logic is fully testable standalone.
"""

import sys
import os
import sqlite3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from safety_router import route, RouterOutcome
from ambiguity_handler import resolve_clarification, ClarificationResult
from caregiver_override import request_override, confirm_override, OverrideResult
from duplicate_guard import check_duplicate, GuardResult
from emergency_escalation import check_escalation, EscalationCategory, is_escalation_transcript
from confidence_rules import check_confidence, ConfidenceResult
from logging_events import log_confirmed, log_uncertain, get_confirmed_today, get_recent_logs


# ─── Shared fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "scenario_test.db")
    con = sqlite3.connect(db_path)
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        con.executescript(f.read())

    meds = [
        (1, "Warfarin 5mg",                  "5mg once daily",            "00:00", "blood thinner",  "small peach round",  "white bottle red label",  1),
        (2, "Metoprolol Succinate 50mg",      "50mg once daily",           "00:00", "heart pill",     "white oval",         "blue cap bottle",          1),
        (3, "Lisinopril 10mg",                "10mg once daily",           "00:00", "pressure pill",  "white round",        "orange bottle",            0),
        (4, "Amlodipine 5mg",                 "5mg once daily",            "00:00", "calcium pill",   "white round",        "yellow bottle",            0),
        (5, "Insulin Glargine 10 units",      "10 units at bedtime",       "00:00", "insulin",        "injection pen",      "long pen in fridge",       1),
        (6, "Omeprazole 20mg",                "20mg before breakfast",     "00:00", "stomach pill",   "purple capsule",     "purple-grey capsule",      0),
        (7, "Fish Oil 1000mg (supplement)",   "1000mg with meals",         "12:00", "fish oil",       "large yellow softgel","supplement",               0),
    ]
    con.executemany(
        "INSERT INTO medications (id,clinical_name,dosage,scheduled_time,nickname,"
        "visual_description,disambiguation_cue,is_critical) VALUES (?,?,?,?,?,?,?,?)",
        meds,
    )
    profile = [
        ("patient_name",      "Margaret Chen"),
        ("caregiver_name",    "David Chen"),
        ("caregiver_contact", "david@example.com"),
        ("schedule_timezone", "Asia/Singapore"),
        ("dose_window_hours", "6"),
        ("confidence_threshold", "0.75"),
        ("emergency_number",  "995"),
    ]
    con.executemany("INSERT INTO patient_profile (key,value) VALUES (?,?)", profile)
    con.commit()
    con.close()
    return db_path


# ─── Scenario 1: Normal dose confirmation ─────────────────────────────────────

class TestScenario1NormalConfirmation:
    def test_heart_pill_confirmed(self, db):
        r = route("I took my heart pill after breakfast", db_path=db)
        assert r.outcome == RouterOutcome.CONFIRMED
        assert r.log_id is not None

    def test_confirmed_stored_in_log(self, db):
        route("I took my heart pill after breakfast", db_path=db)
        confirmed = get_confirmed_today(2, db_path=db)
        assert confirmed is not None
        assert confirmed["state_status"] == "MED_CONFIRMED"

    def test_response_contains_nickname(self, db):
        r = route("I took my heart pill", db_path=db)
        assert "heart pill" in r.message.lower()

    def test_stomach_pill_confirmed(self, db):
        r = route("I took my stomach pill", db_path=db)
        assert r.outcome == RouterOutcome.CONFIRMED


# ─── Scenario 2: Similar-looking pills (ambiguity) ───────────────────────────

class TestScenario2AmbiguousPills:
    def test_white_pill_triggers_ambiguity(self, db):
        r = route("I am taking the white pill", db_path=db)
        assert r.outcome == RouterOutcome.AMBIGUOUS
        assert r.session_key is not None

    def test_ambiguity_not_logged_as_confirmed(self, db):
        route("I am taking the white pill", db_path=db)
        # Neither white-round med should be confirmed
        assert get_confirmed_today(3, db_path=db) is None
        assert get_confirmed_today(4, db_path=db) is None

    def test_clarification_by_bottle_colour(self, db):
        r = route("I am taking the white pill", db_path=db)
        # User describes bottle
        res = resolve_clarification(r.session_key, "pressure pill", db_path=db)
        assert res.outcome == ClarificationResult.RESOLVED
        assert res.medication["clinical_name"] == "Lisinopril 10mg"  # matched by nickname

    def test_clarification_by_number(self, db):
        r = route("I am taking the white pill", db_path=db)
        res = resolve_clarification(r.session_key, "1", db_path=db)
        assert res.outcome == ClarificationResult.RESOLVED

    def test_clarification_give_up_logs_uncertain(self, db):
        r = route("I am taking the white pill", db_path=db)
        res = resolve_clarification(r.session_key, "not sure", db_path=db)
        assert res.outcome == ClarificationResult.UNRESOLVABLE
        assert res.log_id is not None

    def test_prompt_contains_disambiguation_cues(self, db):
        r = route("I am taking the white pill", db_path=db)
        # Prompt should mention bottle colours to help distinguish
        assert "bottle" in r.message.lower()


# ─── Scenario 3: Forgotten dose uncertainty ──────────────────────────────────

class TestScenario3ForgottenDose:
    def test_uncertainty_not_confirmed(self, db):
        r = route("I cannot remember whether I took my heart pill", db_path=db)
        assert r.outcome == RouterOutcome.UNCERTAIN

    def test_uncertain_log_written(self, db):
        route("I cannot remember whether I took my heart pill", db_path=db)
        # No MED_CONFIRMED should exist
        assert get_confirmed_today(2, db_path=db) is None

    def test_dont_remember_phrase(self, db):
        r = route("I don't remember if I took it", db_path=db)
        assert r.outcome == RouterOutcome.UNCERTAIN

    def test_maybe_phrase_triggers_uncertain(self, db):
        r = route("Maybe I took my blood thinner this morning", db_path=db)
        assert r.outcome == RouterOutcome.UNCERTAIN

    def test_uncertain_message_says_do_not_redose(self, db):
        r = route("I'm not sure if I took my heart pill", db_path=db)
        assert r.outcome == RouterOutcome.UNCERTAIN
        msg = r.message.lower()
        # Message must tell patient not to take another dose
        assert "do not" in msg or "don't" in msg or "caregiver" in msg


# ─── Scenario 4: Duplicate dose prevention ───────────────────────────────────

class TestScenario4DuplicateDose:
    def test_second_attempt_blocked(self, db):
        route("I took my heart pill", db_path=db)
        r2 = route("I took my heart pill", db_path=db)
        assert r2.outcome == RouterOutcome.DUPLICATE_BLOCKED

    def test_block_log_written(self, db):
        route("I took my heart pill", db_path=db)
        r2 = route("I took my heart pill", db_path=db)
        assert r2.block_log_id is not None

    def test_original_log_untouched(self, db):
        r1 = route("I took my heart pill", db_path=db)
        route("I took my heart pill", db_path=db)
        # Original confirmed row still exists
        confirmed = get_confirmed_today(2, db_path=db)
        assert confirmed is not None
        assert confirmed["id"] == r1.log_id

    def test_uncertain_then_confirm_blocked(self, db):
        """MED_UNCERTAIN in window must also block a second attempt."""
        log_uncertain("not sure if I took heart pill", medication_id=2, db_path=db)
        guard = check_duplicate(2, "I took my heart pill", db_path=db)
        assert guard.outcome == GuardResult.BLOCKED
        assert "uncertain" in guard.reason.lower()


# ─── Scenario 5: Unsafe medication combination ───────────────────────────────

class TestScenario5UnsafeCombo:
    def test_pain_pill_escalates(self, db):
        r = route("I want to take an old pain pill with my daily blood thinner", db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION

    def test_ibuprofen_escalates(self, db):
        r = route("Can I take ibuprofen for my headache", db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION

    def test_escalation_logged(self, db):
        r = route("I want to take an old pain pill", db_path=db)
        assert r.log_id is not None
        logs = get_recent_logs(limit=1, db_path=db)
        assert logs[0]["state_status"] in ("DRUG_INTERACTION_ALERT", "EMERGENCY_ESCALATION")

    def test_normal_flow_does_not_continue_after_escalation(self, db):
        """After escalation, no MED_CONFIRMED should be written."""
        route("I want to take aspirin with my heart pill", db_path=db)
        assert get_confirmed_today(2, db_path=db) is None

    def test_escalation_message_not_free_form_advice(self, db):
        r = route("I want to take ibuprofen", db_path=db)
        msg = r.message.lower()
        # Must not make clinical recommendations
        forbidden = ["you should", "it's safe", "it is safe", "you can take"]
        assert not any(f in msg for f in forbidden)


# ─── Scenario 6: Caregiver correction ────────────────────────────────────────

class TestScenario6CaregiverCorrection:
    def test_correction_requires_explicit_confirmation(self, db):
        log_id = log_confirmed(2, "I took heart pill", db_path=db)
        result, token = request_override(log_id, "Wrong med logged", db_path=db)
        assert result.outcome == OverrideResult.AWAITING_CONFIRMATION

    def test_correction_confirmed_writes_audit_row(self, db):
        log_id = log_confirmed(2, "I took heart pill", db_path=db)
        _, token = request_override(log_id, "Should be pressure pill",
                                    new_medication_id=3, db_path=db)
        result = confirm_override(token, "yes, correct the record", db_path=db)
        assert result.outcome == OverrideResult.CONFIRMED
        assert result.correction_id is not None

    def test_original_row_not_deleted(self, db):
        log_id = log_confirmed(2, "I took heart pill", db_path=db)
        _, token = request_override(log_id, "Fix needed", db_path=db)
        confirm_override(token, "yes, correct the record", db_path=db)
        con = sqlite3.connect(db)
        cur = con.cursor()
        cur.execute("SELECT state_status FROM ingestion_logs WHERE id=?", (log_id,))
        row = cur.fetchone()
        con.close()
        assert row[0] == "MED_CONFIRMED"  # original untouched

    def test_ambiguous_confirmation_phrase_rejected(self, db):
        log_id = log_confirmed(2, "I took heart pill", db_path=db)
        _, token = request_override(log_id, "Fix", db_path=db)
        result = confirm_override(token, "yeah sure go ahead", db_path=db)
        assert result.outcome == OverrideResult.AWAITING_CONFIRMATION


# ─── Scenario 7: Poor speech recognition ─────────────────────────────────────

class TestScenario7PoorSTT:
    def test_low_confidence_triggers_retry(self, db):
        r = route("hair pill", confidence_score=0.35, db_path=db)
        assert r.outcome == RouterOutcome.LOW_CONFIDENCE

    def test_very_low_confidence_same_result(self, db):
        r = route("unclear mumbling", confidence_score=0.10, db_path=db)
        assert r.outcome == RouterOutcome.LOW_CONFIDENCE

    def test_high_confidence_passes(self, db):
        r = route("I took my heart pill", confidence_score=0.92, db_path=db)
        assert r.outcome == RouterOutcome.CONFIRMED

    def test_none_confidence_typed_input_passes(self, db):
        """Typed input has no confidence score → always PASS."""
        conf = check_confidence(None, "heart pill", db_path=db)
        assert conf.is_pass

    def test_threshold_boundary_below(self, db):
        """Score exactly at threshold - 0.01 → RETRY."""
        conf = check_confidence(0.74, "heart pill", db_path=db)
        assert conf.outcome == ConfidenceResult.RETRY

    def test_threshold_boundary_at(self, db):
        """Score exactly at threshold → PASS."""
        conf = check_confidence(0.75, "heart pill", db_path=db)
        assert conf.outcome == ConfidenceResult.PASS


# ─── Scenario 9: Emergency overdose ──────────────────────────────────────────

class TestScenario9Overdose:
    def test_four_pills_escalates(self, db):
        r = route("I accidentally took four pills", db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION

    def test_overdose_keyword_escalates(self, db):
        esc = check_escalation("I think I overdosed", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.OVERDOSE
        assert esc.is_emergency is True

    def test_emergency_log_written(self, db):
        route("I took too many pills by mistake", db_path=db)
        logs = get_recent_logs(limit=1, db_path=db)
        assert logs[0]["state_status"] == "EMERGENCY_ESCALATION"

    def test_emergency_message_contains_emergency_number(self, db):
        r = route("I accidentally took too much of my medication", db_path=db)
        assert "995" in r.message  # Singapore emergency number from profile

    def test_by_mistake_triggers_emergency(self, db):
        esc = check_escalation("I took my blood thinner by mistake twice", db_path=db)
        assert esc is not None
        assert esc.is_emergency is True


# ─── Scenario 10: Supplement confusion ───────────────────────────────────────

class TestScenario10Supplements:
    def test_fish_oil_not_logged_as_rx(self, db):
        r = route("I took my fish oil", db_path=db)
        assert r.outcome == RouterOutcome.SUPPLEMENT

    def test_supplement_outcome_no_confirmed_log(self, db):
        route("I took my fish oil", db_path=db)
        assert get_confirmed_today(7, db_path=db) is None

    def test_supplement_response_explains_separation(self, db):
        r = route("I took fish oil", db_path=db)
        msg = r.message.lower()
        assert "supplement" in msg or "prescription" in msg


# ─── Scenario: Someone else's medication (R02) ───────────────────────────────

class TestSomeoneElsesMedication:
    def test_husbands_pill_escalates(self, db):
        r = route("I took my husband's pill by mistake", db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION

    def test_category_is_someone_elses(self, db):
        esc = check_escalation("I found a pill and took it, it's not mine", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.SOMEONE_ELSES

    def test_not_mine_escalates(self, db):
        esc = check_escalation("This is not my medication but I took it", db_path=db)
        assert esc is not None


# ─── Scenario: Wrong route of administration ─────────────────────────────────

class TestWrongRoute:
    def test_crushing_pill_escalates(self, db):
        esc = check_escalation("Can I crush my blood thinner to swallow it easier", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.WRONG_ROUTE
        assert esc.is_emergency is False

    def test_opening_capsule_escalates(self, db):
        esc = check_escalation("I opened the capsule and mixed it with water", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.WRONG_ROUTE

    def test_wrong_route_message_does_not_panic(self, db):
        r = route("I want to crush my heart pill", db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION
        # Message should be calm, not alarming
        assert "emergency" not in r.message.lower()


# ─── Scenario: Missed critical medication ────────────────────────────────────

class TestCriticalDose:
    def test_skipped_insulin_escalates(self, db):
        esc = check_escalation("I skipped my insulin for two days", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.CRITICAL_DOUBT

    def test_ran_out_escalates(self, db):
        esc = check_escalation("I ran out of my blood thinner pills", db_path=db)
        assert esc is not None
        assert esc.category == EscalationCategory.CRITICAL_DOUBT

    def test_stopped_taking_escalates(self, db):
        esc = check_escalation("I stopped taking my warfarin last week", db_path=db)
        assert esc is not None


# ─── Fast pre-check (no DB) ──────────────────────────────────────────────────

class TestIsEscalationFastCheck:
    def test_overdose_detected(self):
        assert is_escalation_transcript("I took too many pills") is True

    def test_safe_transcript_not_escalated(self):
        assert is_escalation_transcript("I took my heart pill this morning") is False

    def test_pain_pill_detected(self):
        assert is_escalation_transcript("I want to take an old pain pill") is True

    def test_supplement_not_escalated(self):
        assert is_escalation_transcript("I took my fish oil supplement") is False
