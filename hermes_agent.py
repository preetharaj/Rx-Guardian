"""
hermes_agent.py — LLM via OpenRouter free tier (no credit card, 25+ free models).
Get key at: https://openrouter.ai — sign up with GitHub or email.
Free models end in :free  e.g. nvidia/nemotron-3-super-120b-a12b:free
Falls back to deterministic messages if key not set.
ESCALATION outcome -> verbatim message, no LLM call ever.
"""
from __future__ import annotations
import json, os, re, time
from typing import Optional
import requests

OR_KEY   = os.getenv("OPENROUTER_API_KEY", "")
OR_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
OR_URL   = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOKENS = 512
BACKEND    = "openrouter" if OR_KEY else "none"


def _call_openrouter(system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
    resp = requests.post(
        OR_URL,
        headers={
            "Authorization": f"Bearer {OR_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/med-safety-companion",
            "X-Title": "Medication Safety Companion",
        },
        json={
            "model": OR_MODEL,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _call_llm(system: str, user: str, max_tokens: int = MAX_TOKENS) -> Optional[str]:
    if BACKEND == "none":
        return None
    for attempt in range(3):
        try:
            return _call_openrouter(system, user, max_tokens)
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"[hermes_agent] Rate limited — waiting {wait}s (retry {attempt+2}/3)")
                time.sleep(wait)
                continue
            print(f"[hermes_agent] OpenRouter call failed: {e}")
            return None
    return None


INTENT_SYSTEM = (
    "You are a medication safety assistant parser. "
    "Extract medication information from user messages. "
    "Return ONLY valid JSON matching the schema. No other text."
)

INTENT_USER_TMPL = """\
Extract medication information from this message. Return JSON only.
Message: "{transcript}"
Return exactly:
{{
  "intent": "CONFIRM" | "UNCERTAIN" | "SKIP" | "ASKING" | "EMERGENCY" | "UNKNOWN",
  "medication_reference": "<exact words used, or null>",
  "quantity_mentioned": "<number if any, or null>",
  "time_reference": "<when if any, or null>",
  "other_medication_mentioned": "<secondary med if any, or null>",
  "confidence_note": "<doubt/certainty phrase, or null>"
}}
Rules: use EXACT words; EMERGENCY=overdose/too many/someone else's med;
UNCERTAIN=not sure/I think/maybe; CONFIRM=clear taking statement; null if absent."""

_FALLBACK = {"intent": "UNKNOWN", "medication_reference": None,
             "quantity_mentioned": None, "time_reference": None,
             "other_medication_mentioned": None, "confidence_note": None}


def extract_intent(raw_transcript: str) -> dict:
    result = _call_llm(INTENT_SYSTEM, INTENT_USER_TMPL.format(
        transcript=raw_transcript.replace('"', '\\"')))
    if not result:
        return {**_FALLBACK, "medication_reference": raw_transcript}
    try:
        return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", result.strip()))
    except Exception:
        return {**_FALLBACK, "medication_reference": raw_transcript}


RESPONSE_SYSTEM = (
    "You are a medication safety assistant for older adults. "
    "Max 3 short sentences. Everyday words. One next step at the end. "
    "No medical advice. No 'probably'. If ESCALATION: reproduce message exactly."
)

RESPONSE_USER_TMPL = """\
Convert this safety result into a message for an older adult.
Result: {outcome}
Message: {system_message}
Nickname: {nickname}
Rules:
- Use nickname not clinical name
- Max 3 sentences total
- Last sentence must be one clear next step
- If ESCALATION: reproduce message field exactly, word for word
- Never use: probably / I think / don't worry
- NO reasoning, NO explanation, NO alternatives — output the final message ONLY
Example output for CONFIRMED:
Got it. I've recorded that you took your heart pill at 08:14 UTC. Let me know if you miss a dose.
Output the message only. Do not show your thinking."""


def format_response(outcome: str, system_message: str,
                    patient_name: str = "there", nickname: str = "") -> str:
    if outcome == "ESCALATION":
        return system_message
    result = _call_llm(RESPONSE_SYSTEM, RESPONSE_USER_TMPL.format(
        outcome=outcome, system_message=system_message,
        nickname=nickname or "medication"))
    return result if result else system_message


CLARIFY_SYSTEM = (
    "Medication safety assistant. Ask ONE question about bottle colour "
    "or morning/evening schedule to identify which pill. Simple language only."
)

CLARIFY_TMPL = "Candidates:\n{candidate_summary}\nOne question about bottle colour or schedule. No list. No dose confirmation."


def format_clarification(candidate_summary: str) -> str:
    result = _call_llm(CLARIFY_SYSTEM,
                       CLARIFY_TMPL.format(candidate_summary=candidate_summary),
                       max_tokens=128)
    return result if result else (
        "I found more than one pill that could match. "
        "Could you describe the bottle colour or say whether it's a morning or evening pill?"
    )
