"""
tests/test_reminder.py — Tests for proactive reminders and voice transcription flow.
"""
import os
import sys
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reminder import (
    get_due_reminders, mark_all_reminded, ensure_reminder_table,
    _already_confirmed, _already_reminded, _mark_reminded,
)
from logging_events import log_confirmed, log_uncertain
from confidence_rules import check_confidence, ConfidenceResult


# ─── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "reminder_test.db")
    con = sqlite3.connect(db_path)
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        con.executescript(f.read())

    # Seed medications with scheduled times in the PAST (so they are all "due")
    meds = [
        (1, "Warfarin 5mg",             "5mg once daily", "00:00",
         "blood thinner", "small peach round", "white bottle", 1),
        (2, "Metoprolol Succinate 50mg", "50mg once daily", "00:00",
         "heart pill",    "white oval",       "blue cap",      1),
        (3, "Amlodipine 5mg",            "5mg once daily",  "00:00",
         "calcium pill",  "white round",      "yellow bottle", 0),
        # Supplement — should never appear in reminders
        (7, "Fish Oil 1000mg (supplement)", "1000mg", "00:00",
         "fish oil", "yellow softgel", "supplement", 0),
    ]
    con.executemany(
        "INSERT INTO medications "
        "(id,clinical_name,dosage,scheduled_time,nickname,visual_description,disambiguation_cue,is_critical)"
        " VALUES (?,?,?,?,?,?,?,?)", meds
    )
    profile = [
        ("patient_name",      "Test Patient"),
        ("schedule_timezone", "UTC"),
        ("dose_window_hours", "6"),
    ]
    con.executemany("INSERT INTO patient_profile (key,value) VALUES (?,?)", profile)
    con.commit()
    con.close()
    return db_path


# ─── Reminder engine tests ────────────────────────────────────────────────────

class TestReminderEngine:

    def test_all_due_when_nothing_confirmed(self, db):
        """No confirmations → all non-supplement meds should be due."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        nicknames = {r.nickname for r in reminders}
        assert "heart pill"    in nicknames
        assert "blood thinner" in nicknames
        assert "calcium pill"  in nicknames

    def test_supplement_never_in_reminders(self, db):
        """Supplements must never appear in reminder list."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        nicknames = {r.nickname for r in reminders}
        assert "fish oil" not in nicknames

    def test_confirmed_med_not_reminded(self, db):
        """MED_CONFIRMED today → should NOT appear in reminders."""
        ensure_reminder_table(db)
        log_confirmed(2, "I took my heart pill", db_path=db)
        reminders = get_due_reminders(db)
        nicknames = {r.nickname for r in reminders}
        assert "heart pill" not in nicknames

    def test_uncertain_med_not_reminded(self, db):
        """MED_UNCERTAIN today → also should NOT appear (caregiver must resolve)."""
        ensure_reminder_table(db)
        log_uncertain("not sure if I took it", medication_id=2, db_path=db)
        reminders = get_due_reminders(db)
        nicknames = {r.nickname for r in reminders}
        assert "heart pill" not in nicknames

    def test_critical_med_has_warning_prefix(self, db):
        """Critical medications should have ⚠️ in the reminder message."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        critical = [r for r in reminders if r.is_critical]
        assert len(critical) >= 1
        for r in critical:
            assert "⚠️" in r.message or "important" in r.message.lower()

    def test_non_critical_has_gentle_message(self, db):
        """Non-critical medications should have 💊 in the reminder message."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        non_critical = [r for r in reminders if not r.is_critical]
        assert len(non_critical) >= 1
        for r in non_critical:
            assert "💊" in r.message

    def test_mark_reminded_prevents_duplicate(self, db):
        """Once reminder is marked sent, same med should not appear again today."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        assert len(reminders) > 0
        mark_all_reminded(reminders, channel="telegram", db_path=db)

        reminders_again = get_due_reminders(db)
        assert len(reminders_again) == 0

    def test_mark_reminded_idempotent(self, db):
        """Marking reminded twice should not raise an error."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        mark_all_reminded(reminders, channel="telegram", db_path=db)
        # Second call should not raise
        mark_all_reminded(reminders, channel="telegram", db_path=db)

    def test_already_reminded_flag(self, db):
        """_already_reminded returns correct state."""
        ensure_reminder_table(db)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert not _already_reminded(2, today, db)
        _mark_reminded(2, today, channel="test", db_path=db)
        assert _already_reminded(2, today, db)

    def test_reminder_message_contains_nickname(self, db):
        """Each reminder message must contain the medication nickname."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        for r in reminders:
            assert r.nickname.lower() in r.message.lower(), \
                f"Nickname '{r.nickname}' not in message: {r.message}"

    def test_reminder_message_contains_reply_instruction(self, db):
        """Each reminder must tell the patient how to confirm."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        for r in reminders:
            assert "I took" in r.message or "reply" in r.message.lower(), \
                f"No reply instruction in: {r.message}"


# ─── Voice / confidence integration tests ────────────────────────────────────

class TestVoiceConfidenceIntegration:

    def test_high_confidence_voice_routes_normally(self, db):
        """High-confidence voice transcript goes through normal pipeline."""
        from safety_router import route, RouterOutcome
        r = route("heart pill", confidence_score=0.92, db_path=db)
        assert r.outcome == RouterOutcome.CONFIRMED

    def test_low_confidence_voice_triggers_retry(self, db):
        """Low-confidence voice → LOW_CONFIDENCE, ask to repeat."""
        from safety_router import route, RouterOutcome
        r = route("hair pill", confidence_score=0.35, db_path=db)
        assert r.outcome == RouterOutcome.LOW_CONFIDENCE
        assert "again" in r.message.lower() or "repeat" in r.message.lower() \
               or "catch" in r.message.lower()

    def test_none_confidence_typed_always_passes(self, db):
        """Typed input (None confidence) always passes confidence check."""
        conf = check_confidence(None, "heart pill", db_path=db)
        assert conf.is_pass

    def test_confidence_threshold_boundary(self, db):
        """Exactly at threshold passes; just below fails."""
        conf_pass = check_confidence(0.75, "heart pill", db_path=db)
        assert conf_pass.outcome == ConfidenceResult.PASS

        conf_fail = check_confidence(0.74, "heart pill", db_path=db)
        assert conf_fail.outcome == ConfidenceResult.RETRY

    def test_retries_exhausted_logs_uncertain(self, db):
        """After MAX_RETRIES low-confidence attempts → MED_UNCERTAIN."""
        from confidence_rules import MAX_RETRIES
        conf = check_confidence(0.3, "mumbled input",
                                retry_count=MAX_RETRIES, db_path=db)
        assert conf.outcome == ConfidenceResult.UNCERTAIN
        assert conf.log_id is not None

    def test_escalation_not_affected_by_confidence(self, db):
        """Even with high confidence, escalation triggers take priority."""
        from safety_router import route, RouterOutcome
        r = route("I took four pills by mistake",
                  confidence_score=0.99, db_path=db)
        assert r.outcome == RouterOutcome.ESCALATION

    def test_voice_uncertain_phrase_logs_uncertain(self, db):
        """Low-confidence voice saying uncertain phrase → MED_UNCERTAIN."""
        from safety_router import route, RouterOutcome
        # High enough confidence to pass threshold but uncertain content
        r = route("I'm not sure if I took my heart pill",
                  confidence_score=0.85, db_path=db)
        assert r.outcome == RouterOutcome.UNCERTAIN


# ─── Dispatch --remind integration test ──────────────────────────────────────

class TestDispatchRemind:

    def test_remind_returns_silent_when_all_confirmed(self, db):
        """If all meds confirmed, --remind should return SILENT."""
        ensure_reminder_table(db)
        # Confirm all meds
        for med_id in [1, 2, 3]:
            log_confirmed(med_id, "confirmed", db_path=db)
        reminders = get_due_reminders(db)
        assert reminders == []

    def test_remind_returns_reminders_when_missed(self, db):
        """If meds not confirmed, --remind returns REMINDERS_DUE."""
        ensure_reminder_table(db)
        reminders = get_due_reminders(db)
        assert len(reminders) > 0
        # Mark as reminded
        mark_all_reminded(reminders, channel="hermes_cron", db_path=db)
        # Second call should be empty
        assert get_due_reminders(db) == []
