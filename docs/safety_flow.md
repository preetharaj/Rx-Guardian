# Safety Pipeline Flow — Day 2

## End-to-End Flow

```
User input (text or voice)
        │
        ▼
┌─────────────────────────────────────────────┐
│  safety_router.py  — deterministic pipeline │
│  LLM cannot override any step below         │
└─────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────┐
│  1. confidence_rules │  confidence_score < threshold?
│                      │──── RETRY ──→ "Say that again"
│                      │              (log MED_LOW_CONFIDENCE)
│                      │
│                      │──── retries exhausted?
│                      │──── UNCERTAIN ──→ notify caregiver
└──────────┬───────────┘               (log MED_UNCERTAIN)
           │ PASS
           ▼
┌──────────────────────┐
│  2. Emergency check  │  overdose / "too many" / unsafe combo?
│  (keyword list)      │──── ESCALATION ──→ "Call 995 now"
│                      │                   (log EMERGENCY_ESCALATION
└──────────┬───────────┘                    or DRUG_INTERACTION_ALERT)
           │ clean
           ▼
┌──────────────────────┐
│  3. Uncertainty      │  "not sure" / "maybe" / "forgot"?
│  intent check        │──── UNCERTAIN ──→ "Do not re-dose"
│                      │                  (log MED_UNCERTAIN)
└──────────┬───────────┘
           │ clear intent
           ▼
┌──────────────────────┐
│  4. lookup.py        │  extract med query from transcript
│                      │  match: exact nick → clinical → visual → fuzzy
│                      │
│                      │──── 0 matches ──→ UNKNOWN_MED
│                      │──── supplement ─→ SUPPLEMENT (not Rx)
│                      │──── 2+ matches ─→ step 5 (ambiguity)
└──────────┬───────────┘
           │ 1 match
           ▼
┌──────────────────────┐
│  5. ambiguity_       │  (only reached on 2+ candidates)
│  handler.py          │
│                      │  open disambiguation_session
│                      │──── NEEDS_CLARIFICATION ──→ prompt user
│                      │     (log MED_AMBIGUOUS)
│                      │
│                      │  user replies → resolve_clarification()
│                      │──── RESOLVED      → continue to step 6
│                      │──── still unclear → re-prompt (max 1 retry)
│                      │──── gave up       → UNRESOLVABLE
│                      │                   (log MED_UNCERTAIN)
└──────────┬───────────┘
           │ single med resolved
           ▼
┌──────────────────────┐
│  6. duplicate_guard  │  check ingestion_logs for same med + window
│  .py                 │
│                      │──── MED_CONFIRMED in window ──→ BLOCKED
│                      │──── MED_UNCERTAIN in window ──→ BLOCKED
│                      │     (log MED_DUPLICATE_BLOCKED)
│                      │     message: "Already recorded at HH:MM"
└──────────┬───────────┘
           │ no prior dose
           ▼
┌──────────────────────┐
│  7. log_confirmed()  │  write MED_CONFIRMED to ingestion_logs
│  logging_events.py   │  stores: med_id, timestamp, transcript,
│                      │          confidence, input_mode
└──────────┬───────────┘
           │
           ▼
   CONFIRMED response
   "Got it. Recorded {nickname} at {time}."


════════════════════════════════════════════════════════
CAREGIVER CORRECTION FLOW (separate from normal flow)
════════════════════════════════════════════════════════

Caregiver identifies wrong log
        │
        ▼
caregiver_override.request_override(log_id, note)
        │
        ▼
Returns prompt: "To correct X logged at HH:MM — say 'yes, correct the record'"
        │
        ▼ caregiver responds
confirm_override(token, response)
        │
        ├─ response NOT in CONFIRM_PHRASES ──→ AWAITING_CONFIRMATION (re-prompt)
        ├─ "cancel" ──────────────────────── → CANCELLED (no change)
        ├─ token expired ─────────────────── → EXPIRED (start again)
        └─ confirmed ─────────────────────── → write CAREGIVER_CORRECTION row
                                               original MED_CONFIRMED NOT touched
                                               audit: corrected_by, timestamp, note
```

## Safety Invariants

| Rule | Enforced by | Overridable? |
|---|---|---|
| Low confidence → no confirm | `confidence_rules.py` | No |
| Uncertain transcript → no confirm | `safety_router.py` | No |
| 0 or 2+ matches → no confirm | `lookup.py` | No |
| Prior dose in window → block | `duplicate_guard.py` | No |
| Uncertain dose also blocks | `duplicate_guard.py` | No |
| Caregiver correction needs explicit phrase | `caregiver_override.py` | No |
| Original log never deleted | `logging_events.py` schema | No |
| Emergency keywords → hard stop | `safety_router.py` | No |

## Event Types Written Per Path

| Path taken | Event written |
|---|---|
| Normal confirm | `MED_CONFIRMED` |
| User unsure | `MED_UNCERTAIN` |
| 2+ matches pending | `MED_AMBIGUOUS` |
| Ambiguity gave up | `MED_UNCERTAIN` |
| Low confidence retry | `MED_LOW_CONFIDENCE` |
| Low confidence exhausted | `MED_UNCERTAIN` |
| Duplicate attempt | `MED_DUPLICATE_BLOCKED` |
| Unsafe combo | `DRUG_INTERACTION_ALERT` |
| Overdose/emergency | `EMERGENCY_ESCALATION` |
| Caregiver correction | `CAREGIVER_CORRECTION` |
