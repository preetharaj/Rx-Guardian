"""
reminder.py — Proactive dose reminder engine.

Checks which medications are due and not yet confirmed today.
Called by:
  1. Hermes cron job (via dispatch.py --remind)
  2. Telegram bot JobQueue (scheduled via APScheduler built into python-telegram-bot)
  3. Manually: python reminder.py

Logic:
  - For each active medication, check if scheduled_time has passed today.
  - If yes and no MED_CONFIRMED exists in the dose window → send reminder.
  - Uses patient timezone from patient_profile.
  - Never reminds for a medication that already has MED_CONFIRMED today.
  - Never reminds for a medication that has MED_UNCERTAIN (caregiver must resolve).
  - Reminder window: scheduled_time + REMINDER_GRACE_MINUTES before alerting.
  - Max 1 reminder per medication per day (tracked in reminder_log table).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = os.getenv("DB_PATH", "med_safety.db")

# How many minutes after scheduled_time before we remind (grace period)
REMINDER_GRACE_MINUTES = int(os.getenv("REMINDER_GRACE_MINUTES", "30"))


# ─── Schema extension ─────────────────────────────────────────────────────────

_REMINDER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reminder_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id INTEGER NOT NULL REFERENCES medications(id),
    reminder_date TEXT NOT NULL,       -- YYYY-MM-DD
    sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
    channel       TEXT NOT NULL DEFAULT 'telegram'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_once
    ON reminder_log(medication_id, reminder_date);
"""


def ensure_reminder_table(db_path: str = DB_PATH) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(_REMINDER_TABLE_SQL)
    con.commit()
    con.close()


# ─── Data helpers ─────────────────────────────────────────────────────────────

def _get_profile(key: str, db_path: str) -> Optional[str]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT value FROM patient_profile WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def _local_now(db_path: str) -> datetime:
    tz_name = _get_profile("schedule_timezone", db_path) or "UTC"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz)


def _dose_date(db_path: str) -> str:
    return _local_now(db_path).strftime("%Y-%m-%d")


def _already_confirmed(med_id: int, dose_date: str, db_path: str) -> bool:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """SELECT 1 FROM ingestion_logs
           WHERE medication_id=? AND dose_window_date=?
           AND state_status IN ('MED_CONFIRMED','MED_UNCERTAIN')
           LIMIT 1""",
        (med_id, dose_date),
    )
    row = cur.fetchone()
    con.close()
    return row is not None


def _already_reminded(med_id: int, dose_date: str, db_path: str) -> bool:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM reminder_log WHERE medication_id=? AND reminder_date=? LIMIT 1",
        (med_id, dose_date),
    )
    row = cur.fetchone()
    con.close()
    return row is not None


def _mark_reminded(med_id: int, dose_date: str,
                   channel: str = "telegram", db_path: str = DB_PATH) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    try:
        cur.execute(
            "INSERT INTO reminder_log (medication_id, reminder_date, channel) VALUES (?,?,?)",
            (med_id, dose_date, channel),
        )
        con.commit()
    except sqlite3.IntegrityError:
        pass   # unique constraint — already marked, safe to ignore
    finally:
        con.close()


# ─── Core check ───────────────────────────────────────────────────────────────

@dataclass
class DueReminder:
    medication_id:   int
    nickname:        str
    clinical_name:   str
    scheduled_time:  str   # "HH:MM"
    is_critical:     bool
    message:         str


def get_due_reminders(db_path: str = DB_PATH) -> list[DueReminder]:
    # Guard: if DB doesn't exist or has no medications, return empty
    import os as _os
    if not _os.path.exists(db_path):
        return []
    """
    Return list of medications that are due for a reminder right now.
    Empty list = nothing to send.
    """
    ensure_reminder_table(db_path)

    now       = _local_now(db_path)
    dose_date = now.strftime("%Y-%m-%d")
    patient   = _get_profile("patient_name", db_path) or "there"

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM medications WHERE is_active=1 AND clinical_name NOT LIKE '%(supplement)%'"
    )
    meds = [dict(r) for r in cur.fetchall()]
    con.close()

    due = []
    for med in meds:
        sched = med.get("scheduled_time", "08:00")
        try:
            h, m   = int(sched[:2]), int(sched[3:5])
            sched_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except (ValueError, IndexError):
            continue

        # Not yet due — scheduled time hasn't passed + grace period
        if now < sched_dt + timedelta(minutes=REMINDER_GRACE_MINUTES):
            continue

        # Already confirmed or uncertain — skip
        if _already_confirmed(med["id"], dose_date, db_path):
            continue

        # Already sent a reminder today — skip
        if _already_reminded(med["id"], dose_date, db_path):
            continue

        nick    = med.get("nickname") or med["clinical_name"]
        critical = bool(med.get("is_critical", 0))

        if critical:
            msg = (
                f"⚠️ Reminder: You haven't confirmed your {nick} yet today.\n"
                f"This is an important medication. Please take it and reply:\n"
                f"'I took my {nick}'"
            )
        else:
            msg = (
                f"💊 Reminder: Have you taken your {nick} today?\n"
                f"If yes, reply: 'I took my {nick}'\n"
                f"If you already took it, just say so and I'll record it."
            )

        due.append(DueReminder(
            medication_id=med["id"],
            nickname=nick,
            clinical_name=med["clinical_name"],
            scheduled_time=sched,
            is_critical=critical,
            message=msg,
        ))

    return due


def mark_all_reminded(reminders: list[DueReminder],
                      channel: str = "telegram",
                      db_path: str = DB_PATH) -> None:
    dose_date = _dose_date(db_path)
    for r in reminders:
        _mark_reminded(r.medication_id, dose_date, channel=channel, db_path=db_path)


# ─── CLI entry point (used by dispatch.py --remind) ──────────────────────────

def check_and_print(db_path: str = DB_PATH) -> list[dict]:
    """
    Returns list of reminder dicts for dispatch.py --remind.
    Prints JSON-friendly summary.
    """
    reminders = get_due_reminders(db_path)
    return [
        {
            "medication_id": r.medication_id,
            "nickname":      r.nickname,
            "is_critical":   r.is_critical,
            "message":       r.message,
        }
        for r in reminders
    ]


if __name__ == "__main__":
    import json
    reminders = check_and_print()
    if reminders:
        print(f"Due reminders ({len(reminders)}):")
        for r in reminders:
            print(f"  [{r['nickname']}] {'CRITICAL' if r['is_critical'] else 'normal'}")
            print(f"  {r['message']}\n")
    else:
        print("No reminders due right now.")
