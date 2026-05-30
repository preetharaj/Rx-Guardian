"""
cli.py — Interactive CLI and Telegram bot.

Modes:
  python cli.py               interactive text CLI
  python cli.py --bot         Telegram bot (text + voice notes + scheduled reminders)

Telegram features:
  - Text messages → safety pipeline
  - Voice notes   → auto-transcribed via faster-whisper (free, local) → safety pipeline
  - Proactive dose reminders via JobQueue (no Hermes cron needed)
  - /logs /caregiver /patient /remindnow commands
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from db import ensure_schema
from safety_router import route, RouterOutcome
from ambiguity_handler import resolve_clarification, ClarificationResult
from caregiver_override import request_override, confirm_override, OverrideResult
from hermes_agent import format_response
from logging_events import get_recent_logs
from reminder import get_due_reminders, mark_all_reminded, ensure_reminder_table

DB_PATH   = os.getenv("DB_PATH", "med_safety.db")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Reminder check interval in seconds (default every 15 minutes)
REMINDER_INTERVAL_SECONDS = int(os.getenv("REMINDER_INTERVAL_SECONDS", "900"))


# ─── Session state ────────────────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.pending_ambiguity_key:  Optional[str] = None
        self.pending_override_token: Optional[str] = None
        self.is_caregiver:           bool          = False
        self.retry_count:            int           = 0


# ─── Voice transcription (Layer 2) ───────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes) -> tuple[str, Optional[float]]:
    """
    Transcribe audio bytes using faster-whisper (free, local, no API key).
    Returns (transcript, confidence_score).
    confidence_score is average segment probability (0.0–1.0), or None on failure.

    Falls back gracefully if faster-whisper is not installed.
    Install with:  pip install faster-whisper
    """
    try:
        import io
        from faster_whisper import WhisperModel

        # Load base model (tiny=~75MB, base=~150MB — base is good enough for clear speech)
        model = WhisperModel("base", device="cpu", compute_type="int8")

        # Write to temp file — faster-whisper needs a file path
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            segments, info = model.transcribe(tmp_path, language="en")
            seg_list = list(segments)
            if not seg_list:
                return "", None
            transcript   = " ".join(s.text.strip() for s in seg_list).strip()
            # Whisper avg_logprob is typically -0.1 (perfect) to -1.0+ (poor).
            # Map to 0–1: logprob of -0.0 → 1.0, -0.5 → ~0.82, -1.0 → 0.63
            # Use no_speech_prob as a secondary gate: if > 0.6, speech is absent.
            avg_logprob    = sum(s.avg_logprob for s in seg_list) / len(seg_list)
            avg_no_speech  = sum(s.no_speech_prob for s in seg_list) / len(seg_list)
            if avg_no_speech > 0.6:
                return "", None   # no speech detected
            # Remap: logprob -0.0→1.0, -0.5→0.78, -1.0→0.61, -2.0→0.38
            import math
            conf = 1.0 / (1.0 + math.exp(-avg_logprob * 2))
            return transcript, round(conf, 3)
        finally:
            os.unlink(tmp_path)

    except ImportError:
        return "", None
    except Exception as e:
        print(f"[voice] Transcription failed: {e}")
        return "", None


# ─── Core turn handler ────────────────────────────────────────────────────────

def handle_turn(
    user_text:        str,
    session:          Session,
    confidence_score: Optional[float] = None,
) -> str:
    text = user_text.strip()
    if not text:
        return "I didn't catch that. Could you try again?"

    # ── Mode toggles ──────────────────────────────────────────────────────────
    if text.lower() in {"caregiver mode", "switch to caregiver", "caregiver"}:
        session.is_caregiver = True
        return "Switched to caregiver mode. What would you like to correct?"
    if text.lower() in {"patient mode", "exit caregiver", "done"}:
        session.is_caregiver = False
        session.pending_override_token = None
        return "Back to patient mode."

    # ── Pending caregiver override ────────────────────────────────────────────
    if session.pending_override_token:
        result = confirm_override(session.pending_override_token, text, db_path=DB_PATH)
        if result.is_done:
            session.pending_override_token = None
        return result.prompt or "Override processed."

    # ── Pending ambiguity clarification ──────────────────────────────────────
    if session.pending_ambiguity_key:
        resolution = resolve_clarification(
            session.pending_ambiguity_key, text, db_path=DB_PATH
        )
        if resolution.outcome == ClarificationResult.RESOLVED:
            session.pending_ambiguity_key = None
            session.retry_count = 0
            med = resolution.medication
            from duplicate_guard import check_duplicate
            from logging_events import log_confirmed
            from datetime import datetime, timezone

            guard = check_duplicate(med["id"], text, db_path=DB_PATH)
            if not guard.is_allowed:
                return (
                    f"I already have a record that you took your "
                    f"{med.get('nickname') or med['clinical_name']} today. "
                    "You don't need to take it again."
                )
            log_id = log_confirmed(
                medication_id=med["id"],
                raw_transcript=text,
                input_mode="text",
                db_path=DB_PATH,
            )
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            msg = (
                f"Got it. I've recorded that you took your "
                f"{med.get('nickname') or med['clinical_name']} at {now}."
            )
            return format_response("CONFIRMED", msg, nickname=med.get("nickname", ""))

        if resolution.outcome == ClarificationResult.UNRESOLVABLE:
            session.pending_ambiguity_key = None
            session.retry_count = 0
            return resolution.prompt or \
                "I've marked this as uncertain. Please ask your caregiver to check."
        return resolution.prompt or "Could you say which pill you mean?"

    # ── Caregiver override request ────────────────────────────────────────────
    if session.is_caregiver and text.lower().startswith("correct log"):
        parts = text.split(maxsplit=3)
        if len(parts) >= 3:
            try:
                log_id = int(parts[2])
                reason = parts[3] if len(parts) > 3 else "caregiver correction"
                result, token = request_override(log_id, reason, db_path=DB_PATH)
                if result.outcome == OverrideResult.AWAITING_CONFIRMATION:
                    session.pending_override_token = token
                return result.prompt or "Override requested."
            except (ValueError, IndexError):
                return "Usage: correct log <id> <reason>"
        return "Usage: correct log <id> <reason>"

    # ── Show logs ─────────────────────────────────────────────────────────────
    if text.lower() in {"show logs", "recent logs", "logs"}:
        rows = get_recent_logs(limit=10, db_path=DB_PATH)
        if not rows:
            return "No medication events logged yet."
        lines = ["Recent events:"]
        for r in rows:
            name = r.get("nickname") or r.get("clinical_name") or "unknown"
            lines.append(f"  [{r['logged_at'][:16]}] {r['state_status']} — {name}")
        return "\n".join(lines)

    # ── Main safety pipeline ──────────────────────────────────────────────────
    result = route(
        raw_transcript=text,
        confidence_score=confidence_score,
        retry_count=session.retry_count,
        input_mode="voice" if confidence_score is not None else "text",
        db_path=DB_PATH,
    )

    if result.outcome == RouterOutcome.AMBIGUOUS:
        session.pending_ambiguity_key = result.session_key
        session.retry_count = 0
        return result.message

    if result.outcome == RouterOutcome.LOW_CONFIDENCE:
        session.retry_count += 1
        return result.message

    session.retry_count = 0
    nickname = (result.medication or {}).get("nickname", "")
    return format_response(
        outcome=result.outcome,
        system_message=result.message,
        nickname=nickname,
    )


# ─── CLI mode ─────────────────────────────────────────────────────────────────

def run_cli():
    print("Medication Safety Companion")
    print("Type your message. 'quit' to exit. 'logs' to see recent events.")
    print("Commands: 'caregiver mode' | 'patient mode' | 'correct log <id> <reason>'")
    print("-" * 60)

    ensure_schema(DB_PATH)
    ensure_reminder_table(DB_PATH)
    session = Session()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if user_input.lower() in {"quit", "exit", "q"}:
            print("Goodbye.")
            break
        if not user_input:
            continue
        response = handle_turn(user_input, session)
        print(f"Assistant: {response}\n")


# ─── Telegram bot mode (Layer 2 — voice + reminders) ─────────────────────────

def run_telegram_bot():
    try:
        from telegram import Update
        from telegram.ext import (
            ApplicationBuilder, CommandHandler, MessageHandler,
            filters, ContextTypes, JobQueue,
        )
    except ImportError:
        print("ERROR: python-telegram-bot not installed.")
        print("Run: pip install 'python-telegram-bot[job-queue]'")
        sys.exit(1)

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.WARNING,
    )

    ensure_schema(DB_PATH)
    ensure_reminder_table(DB_PATH)

    chat_sessions: dict[int, Session] = {}
    # Store chat_ids that have started the bot (for broadcasting reminders)
    registered_chats: set[int] = set()

    def get_session(chat_id: int) -> Session:
        if chat_id not in chat_sessions:
            chat_sessions[chat_id] = Session()
        return chat_sessions[chat_id]

    # ── /start ────────────────────────────────────────────────────────────────
    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        registered_chats.add(chat_id)
        await update.message.reply_text(
            "👋 Hello! I'm your Medication Safety Companion.\n\n"
            "You can send me a *text message* or a 🎙️ *voice note*.\n"
            "I'll record your dose safely and remind you if you forget.\n\n"
            "Commands:\n"
            "/logs — show recent medication events\n"
            "/remindnow — check for missed doses right now\n"
            "/caregiver — switch to caregiver mode\n"
            "/help — show this message",
            parse_mode="Markdown",
        )

    # ── /logs ─────────────────────────────────────────────────────────────────
    async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = get_recent_logs(limit=10, db_path=DB_PATH)
        if not rows:
            await update.message.reply_text("No medication events logged yet.")
            return
        lines = ["📋 Recent events:"]
        for r in rows:
            name = r.get("nickname") or r.get("clinical_name") or "unknown"
            icon = {
                "MED_CONFIRMED":         "✅",
                "MED_UNCERTAIN":         "❓",
                "MED_DUPLICATE_BLOCKED": "🔒",
                "MED_AMBIGUOUS":         "🔍",
                "DRUG_INTERACTION_ALERT":"⚠️",
                "EMERGENCY_ESCALATION":  "🚨",
                "CAREGIVER_CORRECTION":  "✏️",
            }.get(r["state_status"], "•")
            lines.append(f"{icon} [{r['logged_at'][:16]}] {r['state_status']} — {name}")
        await update.message.reply_text("\n".join(lines))

    # ── /caregiver ────────────────────────────────────────────────────────────
    async def caregiver_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = get_session(update.effective_chat.id)
        session.is_caregiver = True
        await update.message.reply_text(
            "👩‍⚕️ Caregiver mode active.\n"
            "To correct a log: send `correct log <id> <reason>`\n"
            "To exit: /patient",
            parse_mode="Markdown",
        )

    # ── /patient ──────────────────────────────────────────────────────────────
    async def patient_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = get_session(update.effective_chat.id)
        session.is_caregiver = False
        session.pending_override_token = None
        await update.message.reply_text("Back to patient mode. 💊")

    # ── /remindnow — manual reminder trigger (for testing) ───────────────────
    async def remindnow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        registered_chats.add(chat_id)
        reminders = get_due_reminders(DB_PATH)
        if not reminders:
            await update.message.reply_text(
                "✅ All doses are confirmed or no reminders are due right now."
            )
            return
        for reminder in reminders:
            await context.bot.send_message(chat_id=chat_id, text=reminder.message)
        mark_all_reminded(reminders, channel="telegram", db_path=DB_PATH)

    # ── Text message handler ──────────────────────────────────────────────────
    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id   = update.effective_chat.id
        user_text = update.message.text or ""
        session   = get_session(chat_id)
        registered_chats.add(chat_id)

        response = handle_turn(user_text, session)
        await update.message.reply_text(response)

    # ── Voice note handler (Layer 2) ──────────────────────────────────────────
    async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Receives Telegram voice notes, transcribes locally with faster-whisper,
        passes transcript + confidence to the safety pipeline.
        """
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        registered_chats.add(chat_id)

        # Download the voice file
        try:
            voice_file = await update.message.voice.get_file()
            audio_bytes = await voice_file.download_as_bytearray()
        except Exception as e:
            await update.message.reply_text(
                "Sorry, I couldn't download your voice message. Please try again or type your message."
            )
            return

        # Show "typing..." while transcribing
        await context.bot.send_chat_action(
            chat_id=chat_id, action="typing"
        )

        transcript, confidence = transcribe_audio(bytes(audio_bytes))

        if not transcript:
            # faster-whisper not installed or failed
            await update.message.reply_text(
                "🎙️ I received your voice note but couldn't transcribe it.\n"
                "Please type your message instead, or install voice support:\n"
                "`pip install faster-whisper`",
                parse_mode="Markdown",
            )
            return

        # Echo transcript so patient can verify what was heard
        await update.message.reply_text(
            f"🎙️ I heard: _{transcript}_",
            parse_mode="Markdown",
        )

        # Run through safety pipeline with confidence score
        response = handle_turn(transcript, session, confidence_score=None)
        await update.message.reply_text(response)

    # ── Proactive reminder job (runs every REMINDER_INTERVAL_SECONDS) ─────────
    async def reminder_job(context) -> None:
        """
        Runs on schedule. Checks for unconfirmed doses and sends reminders
        to all registered Telegram chats.
        """
        if not registered_chats:
            return

        reminders = get_due_reminders(DB_PATH)
        if not reminders:
            return

        for chat_id in list(registered_chats):
            for reminder in reminders:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=reminder.message,
                    )
                except Exception as e:
                    print(f"[reminder] Failed to send to {chat_id}: {e}")

        mark_all_reminded(reminders, channel="telegram", db_path=DB_PATH)

    # ── Build app ─────────────────────────────────────────────────────────────
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start",       start_handler))
    app.add_handler(CommandHandler("help",        start_handler))
    app.add_handler(CommandHandler("logs",        logs_handler))
    app.add_handler(CommandHandler("caregiver",   caregiver_handler))
    app.add_handler(CommandHandler("patient",     patient_handler))
    app.add_handler(CommandHandler("remindnow",   remindnow_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(
        filters.VOICE, voice_handler))

    # Schedule proactive reminders
    app.job_queue.run_repeating(
        reminder_job,
        interval=REMINDER_INTERVAL_SECONDS,
        first=60,   # first check 60s after start
        name="dose_reminders",
    )

    print(f"✅ Telegram bot running.")
    print(f"   Reminder check every {REMINDER_INTERVAL_SECONDS}s")
    print(f"   Voice transcription: {'enabled' if _whisper_available() else 'disabled (pip install faster-whisper)'}")
    print(f"   Send /start to your bot in Telegram.")
    app.run_polling()


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa
        return True
    except ImportError:
        return False


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Medication Safety Companion")
    parser.add_argument("--bot", action="store_true", help="Run Telegram bot")
    args = parser.parse_args()
    if args.bot:
        run_telegram_bot()
    else:
        run_cli()
