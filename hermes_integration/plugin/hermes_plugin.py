"""
hermes_plugin.py — Hermes Agent plugin for Medication Safety Companion.

Registers 5 tools that Hermes calls during the med-safety-companion skill:
  - med_safety_check              main safety pipeline (one call per user turn)
  - med_safety_resolve            resolve ambiguity after user clarifies
  - med_safety_logs               show recent audit log
  - med_safety_caregiver_correct  initiate caregiver correction
  - med_safety_caregiver_confirm  confirm caregiver correction

Install: copy this file to ~/.hermes/plugins/med_safety_plugin.py
         OR drop it in .hermes/plugins/ inside your project directory.

The plugin will auto-discover on next `hermes` start.
"""

from __future__ import annotations

import json
import os
import sys

# ── Path setup ────────────────────────────────────────────────────────────────
# Resolve the location of our safety modules.
# They live in the same repo as this plugin under hermes_integration/../
# i.e. the project root.

_HERE      = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.abspath(os.path.join(_HERE, ".."))  # project root

if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

# DB path — from env or default
_DB_PATH = os.getenv("MED_SAFETY_DB",
           os.path.expanduser("~/.hermes/med_safety/med_safety.db"))


# ── Lazy import helper ────────────────────────────────────────────────────────
# Import safety modules only when a tool is called — keeps Hermes startup fast.

def _import_router():
    from safety_router import route, RouterOutcome  # noqa: F401
    return route, RouterOutcome

def _import_ambiguity():
    from ambiguity_handler import resolve_clarification, ClarificationResult  # noqa: F401
    return resolve_clarification, ClarificationResult

def _import_caregiver():
    from caregiver_override import request_override, confirm_override, OverrideResult  # noqa: F401
    return request_override, confirm_override, OverrideResult

def _import_logging():
    from logging_events import get_recent_logs  # noqa: F401
    return get_recent_logs

def _ensure_db():
    """Ensure DB exists and is seeded. Called once on first tool use."""
    if not os.path.exists(_DB_PATH):
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        from db import ensure_schema
        ensure_schema(_DB_PATH)
        from seed import seed
        seed(_DB_PATH)


# ── Tool 1: med_safety_check ─────────────────────────────────────────────────

def _med_safety_check(transcript: str) -> str:
    """Run the full deterministic safety pipeline for one user turn."""
    try:
        _ensure_db()
        route, RouterOutcome = _import_router()
        result = route(raw_transcript=transcript, db_path=_DB_PATH)

        payload = {
            "outcome":     result.outcome,
            "message":     result.message,
            "session_key": result.session_key,
            "log_id":      result.log_id,
        }
        if result.medication:
            payload["medication"] = {
                "id":           result.medication.get("id"),
                "nickname":     result.medication.get("nickname"),
                "clinical_name": result.medication.get("clinical_name"),
            }
        if result.existing_log:
            payload["existing_log_time"] = (
                result.existing_log.get("logged_at", "")[:16]
            )
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"outcome": "ERROR", "message": f"Safety check failed: {e}"})


MED_SAFETY_CHECK_SCHEMA = {
    "name": "med_safety_check",
    "description": (
        "Run the medication safety pipeline for a user's input. "
        "Returns outcome (CONFIRMED / DUPLICATE_BLOCKED / AMBIGUOUS / UNCERTAIN / "
        "ESCALATION / SUPPLEMENT / UNKNOWN_MED / LOW_CONFIDENCE), "
        "a user-facing message, and a session_key for ambiguity resolution. "
        "Call once per user turn with the user's exact words."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "transcript": {
                "type": "string",
                "description": "The user's exact input, verbatim."
            }
        },
        "required": ["transcript"]
    }
}


# ── Tool 2: med_safety_resolve ───────────────────────────────────────────────

def _med_safety_resolve(session_key: str, user_response: str) -> str:
    """Resolve an ambiguous medication session with the user's clarification."""
    try:
        _ensure_db()
        resolve_clarification, ClarificationResult = _import_ambiguity()
        resolution = resolve_clarification(session_key, user_response, db_path=_DB_PATH)

        if resolution.outcome == ClarificationResult.RESOLVED:
            # Run duplicate guard + confirm
            med = resolution.medication
            from duplicate_guard import check_duplicate
            from logging_events import log_confirmed
            from datetime import datetime, timezone

            guard = check_duplicate(med["id"], user_response, db_path=_DB_PATH)
            if not guard.is_allowed:
                return json.dumps({
                    "outcome": "DUPLICATE_BLOCKED",
                    "message": (
                        f"I already have a record that you took your "
                        f"{med.get('nickname') or med['clinical_name']} today. "
                        "You don't need to take it again."
                    ),
                })
            log_id = log_confirmed(
                medication_id=med["id"],
                raw_transcript=user_response,
                db_path=_DB_PATH,
            )
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            return json.dumps({
                "outcome": "CONFIRMED",
                "message": (
                    f"Got it. I've recorded that you took your "
                    f"{med.get('nickname') or med['clinical_name']} at {now}. "
                    "You don't need to log it again today."
                ),
                "log_id": log_id,
                "medication": {
                    "id": med["id"],
                    "nickname": med.get("nickname"),
                    "clinical_name": med.get("clinical_name"),
                },
            })

        if resolution.outcome == ClarificationResult.UNRESOLVABLE:
            return json.dumps({
                "outcome": "UNCERTAIN",
                "message": resolution.prompt or (
                    "I've marked this dose as uncertain. "
                    "Please ask your caregiver to check before taking anything."
                ),
                "log_id": resolution.log_id,
            })

        # Still NEEDS_CLARIFICATION
        return json.dumps({
            "outcome": "NEEDS_CLARIFICATION",
            "message": resolution.prompt or "Could you say which pill you mean?",
            "session_key": session_key,
        })
    except Exception as e:
        return json.dumps({"outcome": "ERROR", "message": f"Resolution failed: {e}"})


MED_SAFETY_RESOLVE_SCHEMA = {
    "name": "med_safety_resolve",
    "description": (
        "Resolve an ambiguous medication session after the user has clarified "
        "which pill they mean. Use the session_key from med_safety_check. "
        "Returns CONFIRMED, DUPLICATE_BLOCKED, UNCERTAIN, or NEEDS_CLARIFICATION."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_key": {
                "type": "string",
                "description": "The session_key returned by med_safety_check."
            },
            "user_response": {
                "type": "string",
                "description": "The user's clarification reply (e.g. '1', 'orange bottle', 'pressure pill')."
            }
        },
        "required": ["session_key", "user_response"]
    }
}


# ── Tool 3: med_safety_logs ──────────────────────────────────────────────────

def _med_safety_logs(limit: int = 10) -> str:
    """Return recent audit log entries."""
    try:
        _ensure_db()
        get_recent_logs = _import_logging()
        rows = get_recent_logs(limit=limit, db_path=_DB_PATH)
        entries = []
        for r in rows:
            entries.append({
                "id":           r["id"],
                "logged_at":    r["logged_at"][:16],
                "status":       r["state_status"],
                "medication":   r.get("nickname") or r.get("clinical_name") or "unknown",
            })
        return json.dumps({"entries": entries, "count": len(entries)})
    except Exception as e:
        return json.dumps({"error": f"Log retrieval failed: {e}"})


MED_SAFETY_LOGS_SCHEMA = {
    "name": "med_safety_logs",
    "description": "Show recent medication audit log entries.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of entries to return (default 10, max 50).",
                "default": 10
            }
        },
        "required": []
    }
}


# ── Tool 4: med_safety_caregiver_correct ─────────────────────────────────────

_pending_tokens: dict[str, str] = {}   # session storage for override tokens


def _med_safety_caregiver_correct(log_id: int, reason: str,
                                   new_medication_id: int | None = None) -> str:
    """Initiate a caregiver correction. Returns a confirmation prompt + token."""
    try:
        _ensure_db()
        request_override, _, _ = _import_caregiver()
        result, token = request_override(
            original_log_id=log_id,
            correction_note=reason,
            new_medication_id=new_medication_id,
            db_path=_DB_PATH,
        )
        _pending_tokens[token] = token  # store for confirm step
        return json.dumps({
            "status":  "AWAITING_CONFIRMATION",
            "prompt":  result.prompt,
            "token":   token,
        })
    except Exception as e:
        return json.dumps({"status": "ERROR", "message": f"Override request failed: {e}"})


MED_SAFETY_CAREGIVER_CORRECT_SCHEMA = {
    "name": "med_safety_caregiver_correct",
    "description": (
        "Initiate a caregiver correction for a logged medication event. "
        "Returns a confirmation prompt and a token. "
        "Caregiver must explicitly confirm before the correction is applied."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "log_id": {
                "type": "integer",
                "description": "The ID of the ingestion_log row to correct (from med_safety_logs)."
            },
            "reason": {
                "type": "string",
                "description": "Why the correction is needed."
            },
            "new_medication_id": {
                "type": "integer",
                "description": "Optional. New medication ID if the wrong med was logged."
            }
        },
        "required": ["log_id", "reason"]
    }
}


# ── Tool 5: med_safety_caregiver_confirm ─────────────────────────────────────

def _med_safety_caregiver_confirm(token: str, response: str) -> str:
    """Complete or cancel a caregiver correction."""
    try:
        _ensure_db()
        _, confirm_override, OverrideResult = _import_caregiver()
        result = confirm_override(token, response, db_path=_DB_PATH)
        return json.dumps({
            "status":        result.outcome,
            "message":       result.prompt or "",
            "correction_id": result.correction_id,
        })
    except Exception as e:
        return json.dumps({"status": "ERROR", "message": f"Override confirm failed: {e}"})


MED_SAFETY_CAREGIVER_CONFIRM_SCHEMA = {
    "name": "med_safety_caregiver_confirm",
    "description": (
        "Confirm or cancel a pending caregiver correction. "
        "Pass the token from med_safety_caregiver_correct and the caregiver's response. "
        "Accepted confirmation phrase: 'yes, correct the record'. "
        "To cancel: 'cancel'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "token": {
                "type": "string",
                "description": "The token returned by med_safety_caregiver_correct."
            },
            "response": {
                "type": "string",
                "description": "The caregiver's confirmation or cancellation phrase."
            }
        },
        "required": ["token", "response"]
    }
}


# ── Availability check ────────────────────────────────────────────────────────

def _check_med_safety_available() -> bool:
    """Return True if safety modules are importable from PROJ_ROOT."""
    try:
        import safety_router  # noqa: F401
        return True
    except ImportError:
        return False


# ── Plugin registration ───────────────────────────────────────────────────────
# Hermes discovers plugins at ~/.hermes/plugins/*.py or .hermes/plugins/*.py
# and calls register(context) if defined.

def register(context):
    """Called by Hermes PluginManager on plugin load."""
    ctx = context  # hermes plugin context object

    ctx.register_tool(
        name="med_safety_check",
        schema=MED_SAFETY_CHECK_SCHEMA,
        handler=lambda args, **kw: _med_safety_check(args.get("transcript", "")),
        check_fn=_check_med_safety_available,
        toolset="med_safety",
    )
    ctx.register_tool(
        name="med_safety_resolve",
        schema=MED_SAFETY_RESOLVE_SCHEMA,
        handler=lambda args, **kw: _med_safety_resolve(
            args.get("session_key", ""),
            args.get("user_response", ""),
        ),
        check_fn=_check_med_safety_available,
        toolset="med_safety",
    )
    ctx.register_tool(
        name="med_safety_logs",
        schema=MED_SAFETY_LOGS_SCHEMA,
        handler=lambda args, **kw: _med_safety_logs(
            limit=min(int(args.get("limit", 10)), 50)
        ),
        check_fn=_check_med_safety_available,
        toolset="med_safety",
    )
    ctx.register_tool(
        name="med_safety_caregiver_correct",
        schema=MED_SAFETY_CAREGIVER_CORRECT_SCHEMA,
        handler=lambda args, **kw: _med_safety_caregiver_correct(
            log_id=int(args.get("log_id", 0)),
            reason=args.get("reason", ""),
            new_medication_id=args.get("new_medication_id"),
        ),
        check_fn=_check_med_safety_available,
        toolset="med_safety",
    )
    ctx.register_tool(
        name="med_safety_caregiver_confirm",
        schema=MED_SAFETY_CAREGIVER_CONFIRM_SCHEMA,
        handler=lambda args, **kw: _med_safety_caregiver_confirm(
            token=args.get("token", ""),
            response=args.get("response", ""),
        ),
        check_fn=_check_med_safety_available,
        toolset="med_safety",
    )
