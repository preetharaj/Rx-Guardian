---
name: med-safety
description: >
  Medication safety assistant for home care. Confirms doses, resolves ambiguous
  pill descriptions, prevents duplicate dosing, and escalates unsafe situations.
  Uses a deterministic Python safety pipeline — the agent handles conversation,
  the pipeline handles all safety decisions.
version: 1.0.0
author: Hermes Agent Challenge 2026
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [Healthcare, Safety, Home-Care, Medication, Elderly-Care]
    category: healthcare
    config:
      - key: med_safety.project_dir
        description: Absolute path to the med-safety-companion project directory
        default: "~/med-safety-companion"
        prompt: "Path to your med-safety-companion folder (where seed.py lives)"
      - key: med_safety.db_path
        description: Path to the SQLite database file
        default: "~/med-safety-companion/med_safety.db"
        prompt: "Path to med_safety.db"
required_environment_variables:
  - name: GEMINI_API_KEY
    prompt: "Google Gemini API key (free at aistudio.google.com)"
    help: "Get a free key at https://aistudio.google.com — no credit card required"
    required_for: "LLM-polished response formatting (system works without it)"
---

# Medication Safety Assistant

A home-care medication safety companion. The agent handles conversation and
multi-turn clarification. A deterministic Python pipeline handles all safety
decisions — the agent cannot override them.

## When to Use

Load this skill when the user:
- Says they took a medication and wants to log it
- Asks about whether they already took a dose
- Mentions a pill by description ("the white pill", "the small round one")
- Expresses uncertainty about a dose
- Mentions taking someone else's medication
- Mentions combining medications
- Reports taking too many pills

## Architecture

```
User message
    │
    ▼
[You — Hermes] ← interprets intent, manages conversation turn
    │
    ▼
[terminal → python dispatch.py "transcript"] ← deterministic safety engine
    │
    ▼ Returns JSON: {outcome, message, session_key, log_id}
    │
    ▼
[You — Hermes] ← reads outcome, formats final response for user
```

**Safety rule: NEVER override or reinterpret the pipeline's outcome.**
If outcome is ESCALATION — deliver the message verbatim, no softening.
If outcome is DUPLICATE_BLOCKED — do not confirm the dose.
If outcome is UNCERTAIN — do not say "you probably took it".

## Setup (run once)

```bash
cd {med_safety.project_dir}
pip install -r requirements.txt
python seed.py
```

Expected output: `Seeded 7 medications and 6 profile keys into med_safety.db`

Verify the pipeline works:
```bash
python dispatch.py "I took my heart pill"
```
Expected JSON with `"outcome": "CONFIRMED"`.

## Procedure

### Step 1: Receive user input

Read the user's message. Extract the medication-related intent.
Do NOT guess the medication identity yourself — pass the raw words to the pipeline.

### Step 2: Call the safety pipeline

```bash
cd {med_safety.project_dir}
python dispatch.py "{exact user words about medication}"
```

The pipeline returns JSON:
```json
{
  "outcome": "CONFIRMED|AMBIGUOUS|DUPLICATE_BLOCKED|UNCERTAIN|ESCALATION|UNKNOWN_MED|SUPPLEMENT",
  "message": "...",
  "session_key": "ambig_...|null",
  "log_id": 42
}
```

### Step 3: Act on outcome

| Outcome | Your action |
|---|---|
| `CONFIRMED` | Deliver `message` to user. Done. |
| `AMBIGUOUS` | Deliver `message` (clarification question). Wait for user reply. Go to Step 4. |
| `DUPLICATE_BLOCKED` | Deliver `message`. Do NOT confirm the dose. |
| `UNCERTAIN` | Deliver `message`. Do NOT say they probably took it. |
| `ESCALATION` | Deliver `message` **verbatim**. No additions. No softening. |
| `UNKNOWN_MED` | Deliver `message`. Ask user to check bottle name. |
| `SUPPLEMENT` | Deliver `message`. Explain it's not a prescription dose. |
| `LOW_CONFIDENCE` | Deliver `message` (asks user to repeat). |

### Step 4: Resolve ambiguity (multi-turn)

When outcome is `AMBIGUOUS`, save the `session_key` from the response.
When user replies with their clarification:

```bash
python dispatch.py --session "{saved_session_key}" "{user clarification}"
```

Returns same JSON structure. If outcome is `CONFIRMED` — deliver confirmation.
If `UNRESOLVABLE` — explain dose is marked uncertain, caregiver will be notified.

### Step 5: Caregiver mode

When a caregiver wants to correct a logged entry:

```bash
# List recent events to find the log ID
python dispatch.py --logs

# Request override
python dispatch.py --caregiver-override {log_id} "{reason}"
```

Returns a confirmation prompt. Caregiver must reply with exact phrase:
`yes, correct the record`

```bash
python dispatch.py --confirm-override {token} "yes, correct the record"
```

### Step 6: Check for missed doses (proactive reminder)

Hermes can also proactively check if the patient has missed a dose:

```bash
python dispatch.py --remind
```

Returns `[SILENT]` if all doses are confirmed, or reminder messages if any are overdue.
Use this in a Hermes cron job for automatic daily reminders:

```
Create a cron job: every day at 08:30, run:
  cd C:\Projects\emcr && python dispatch.py --remind
If the result is not [SILENT], send the reminder message to Telegram.
```

### Step 7: Show audit log

When user asks to see their medication history:

```bash
python dispatch.py --logs
```

Returns last 10 events as formatted text. Share with user.

## Seeded Medications (demo registry)

| Nickname | Clinical Name | Schedule | Critical |
|---|---|---|---|
| heart pill | Metoprolol Succinate 50mg | 08:00 | yes |
| blood thinner | Warfarin 5mg | 08:00 | yes |
| pressure pill | Lisinopril 10mg | 08:00 | no |
| calcium pill | Amlodipine 5mg | 20:00 | no |
| insulin | Insulin Glargine 10 units | 22:00 | yes |
| stomach pill | Omeprazole 20mg | 07:30 | no |
| fish oil | Fish Oil 1000mg (supplement) | 12:00 | — |

## Safety Guardrails (enforced by pipeline, not by you)

These rules are deterministic — the Python code enforces them regardless of LLM output:

- **Ambiguity** → no confirmation until resolved. "White pill" matches 3 meds.
- **Duplicate** → confirmed dose within 6h window → blocked, not re-logged.
- **Uncertain** → "not sure", "I think", "maybe" → MED_UNCERTAIN, no confirm.
- **Escalation** → NSAIDs, painkillers, overdose, someone else's med → hard stop.
- **Immutable log** → no row ever deleted; caregiver corrections add new rows.

## Pitfalls

**Do not call dispatch.py with the whole conversation** — only the medication-specific part.
Wrong: `python dispatch.py "Hello! I wanted to let you know I took my heart pill today after breakfast"`
Right: `python dispatch.py "heart pill"`

**Pronouns don't work** — "I took it again" resolves to UNKNOWN_MED. Ask for the name.

**Duplicate session keys** — if ambiguity session expires (>10 min), the pipeline returns UNRESOLVABLE. Start fresh.

**Escalation messages** — do not rephrase them. `outcome == ESCALATION` → output `message` exactly.

## Verification

After each successful confirmation:
```bash
python dispatch.py --logs
```
Most recent row should show `MED_CONFIRMED` with the correct medication nickname.

After escalation:
```bash
python dispatch.py --logs
```
Most recent row should show `DRUG_INTERACTION_ALERT` or `EMERGENCY_ESCALATION`.
No `MED_CONFIRMED` should appear after an escalation for the same session.
