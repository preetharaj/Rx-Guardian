"""
dispatch.py — Single CLI entry point for Hermes Agent.

Hermes calls this via its terminal tool. All output is JSON.
The safety pipeline is deterministic; Hermes only reads the result and formats it.

Usage:
  python dispatch.py "user transcript"
  python dispatch.py --session "ambig_key" "clarification reply"
  python dispatch.py --caregiver-override LOG_ID "reason"
  python dispatch.py --confirm-override TOKEN "yes, correct the record"
  python dispatch.py --logs [--limit N]
  python dispatch.py --reset          # wipe DB and re-seed (demo reset)
"""

import argparse
import json
import os
import sys

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = os.getenv("DB_PATH", "med_safety.db")


def _out(data: dict) -> None:
    """Print JSON to stdout — Hermes reads this."""
    print(json.dumps(data, indent=2))


def cmd_route(transcript: str, session_key: str | None) -> None:
    from safety_router import route, RouterOutcome
    from ambiguity_handler import resolve_clarification, ClarificationResult

    if session_key:
        # Continuing ambiguity resolution
        resolution = resolve_clarification(session_key, transcript, db_path=DB_PATH)

        if resolution.outcome == ClarificationResult.RESOLVED:
            med = resolution.medication
            # Run duplicate guard + confirm for resolved med
            from duplicate_guard import check_duplicate
            from logging_events import log_confirmed
            from datetime import datetime, timezone

            guard = check_duplicate(med["id"], transcript, db_path=DB_PATH)
            if not guard.is_allowed:
                _out({
                    "outcome": "DUPLICATE_BLOCKED",
                    "message": (
                        f"I already have a record that you took your "
                        f"{med.get('nickname') or med['clinical_name']} today. "
                        "You don't need to take it again. "
                        "If you think there's a mistake, ask your caregiver to check."
                    ),
                    "session_key": None,
                    "log_id": guard.block_log_id,
                })
                return

            log_id = log_confirmed(
                medication_id=med["id"],
                raw_transcript=transcript,
                input_mode="text",
                db_path=DB_PATH,
            )
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            _out({
                "outcome": "CONFIRMED",
                "message": (
                    f"Got it. I've recorded that you took your "
                    f"{med.get('nickname') or med['clinical_name']} "
                    f"({med['clinical_name']}) at {now}. "
                    "You don't need to log it again today."
                ),
                "session_key": None,
                "log_id": log_id,
            })

        elif resolution.outcome == ClarificationResult.UNRESOLVABLE:
            _out({
                "outcome": "UNRESOLVABLE",
                "message": resolution.prompt or (
                    "I wasn't able to identify which medication you mean. "
                    "I've marked this as uncertain. Please ask your caregiver to check."
                ),
                "session_key": None,
                "log_id": resolution.log_id,
            })

        else:
            # Still needs clarification
            _out({
                "outcome": "AMBIGUOUS",
                "message": resolution.prompt or "Could you say which pill you mean?",
                "session_key": session_key,
                "log_id": None,
            })
        return

    # Normal route
    result = route(raw_transcript=transcript, db_path=DB_PATH)

    _out({
        "outcome": result.outcome,
        "message": result.message,
        "session_key": result.session_key,
        "log_id": result.log_id,
        "block_log_id": result.block_log_id,
    })


def cmd_caregiver_override(log_id: int, reason: str) -> None:
    from caregiver_override import request_override, OverrideResult

    result, token = request_override(log_id, reason, db_path=DB_PATH)
    _out({
        "outcome": result.outcome,
        "message": result.prompt or "Override requested.",
        "token": token,
        "original_log_id": log_id,
    })


def cmd_confirm_override(token: str, phrase: str) -> None:
    from caregiver_override import confirm_override

    result = confirm_override(token, phrase, db_path=DB_PATH)
    _out({
        "outcome": result.outcome,
        "message": result.prompt or "Done.",
        "correction_id": result.correction_id,
    })


def cmd_logs(limit: int = 10) -> None:
    from logging_events import get_recent_logs

    rows = get_recent_logs(limit=limit, db_path=DB_PATH)
    if not rows:
        _out({"outcome": "OK", "message": "No medication events logged yet.", "rows": []})
        return

    lines = []
    for r in rows:
        name = r.get("nickname") or r.get("clinical_name") or "unknown"
        lines.append(f"[{r['logged_at'][:16]}] {r['state_status']} — {name}")

    _out({
        "outcome": "OK",
        "message": "\n".join(lines),
        "rows": rows,
    })


def cmd_reset() -> None:
    import subprocess
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    result = subprocess.run(
        [sys.executable, "seed.py"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        _out({"outcome": "OK", "message": result.stdout.strip()})
    else:
        _out({"outcome": "ERROR", "message": result.stderr.strip()})


def cmd_remind() -> None:
    """Check for due reminders — called by Hermes cron job."""
    from reminder import get_due_reminders, mark_all_reminded, ensure_reminder_table
    from db import ensure_schema
    ensure_schema(DB_PATH)          # create tables if DB is fresh
    ensure_reminder_table(DB_PATH)
    reminders = get_due_reminders(DB_PATH)
    if not reminders:
        _out({"outcome": "OK", "message": "[SILENT]", "reminders": []})
        return
    msgs = [r.message for r in reminders]
    _out({
        "outcome": "REMINDERS_DUE",
        "message": "\n\n".join(msgs),
        "reminders": [{"nickname": r.nickname, "is_critical": r.is_critical}
                      for r in reminders],
    })
    mark_all_reminded(reminders, channel="hermes_cron", db_path=DB_PATH)
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Medication Safety Companion — Hermes dispatch interface"
    )
    parser.add_argument("transcript", nargs="?", help="User's medication-related words")
    parser.add_argument("--session", metavar="KEY",
                        help="Pending ambiguity session key (from previous AMBIGUOUS response)")
    parser.add_argument("--caregiver-override", metavar="LOG_ID", type=int,
                        help="Request caregiver correction for this log row ID")
    parser.add_argument("--confirm-override", metavar="TOKEN",
                        help="Confirm pending override with this token")
    parser.add_argument("--logs", action="store_true",
                        help="Show recent medication events")
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of log rows to return (default 10)")
    parser.add_argument("--remind", action="store_true",
                        help="Check for unconfirmed doses and return reminders")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe database and re-seed (demo reset)")

    args = parser.parse_args()

    if args.remind:
        cmd_remind()
    elif args.reset:
        cmd_reset()
    elif args.logs:
        cmd_logs(args.limit)
    elif args.caregiver_override is not None:
        reason = args.transcript or "caregiver correction"
        cmd_caregiver_override(args.caregiver_override, reason)
    elif args.confirm_override:
        phrase = args.transcript or ""
        cmd_confirm_override(args.confirm_override, phrase)
    elif args.transcript:
        cmd_route(args.transcript, args.session)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
