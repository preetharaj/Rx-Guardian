# Outcome Reference

Quick reference for `dispatch.py` return codes and how Hermes should respond.

## Outcome → Response Pattern

| Outcome | Deliver message? | Add anything? | Log written? |
|---|---|---|---|
| `CONFIRMED` | Yes | "You don't need to log it again." | MED_CONFIRMED |
| `AMBIGUOUS` | Yes (clarification Q) | Nothing — wait for reply | MED_AMBIGUOUS |
| `DUPLICATE_BLOCKED` | Yes | "Ask caregiver if mistake" | MED_DUPLICATE_BLOCKED |
| `UNCERTAIN` | Yes | "Do not take another dose" | MED_UNCERTAIN |
| `ESCALATION` | Yes — **verbatim only** | Nothing. No softening. | DRUG_INTERACTION_ALERT or EMERGENCY_ESCALATION |
| `UNKNOWN_MED` | Yes | "Check bottle name" | — |
| `SUPPLEMENT` | Yes | "Not a prescription" | — |
| `LOW_CONFIDENCE` | Yes (retry Q) | Nothing | MED_LOW_CONFIDENCE |
| `UNRESOLVABLE` | Yes | "Caregiver will check" | MED_UNCERTAIN |

## Escalation Categories (all are hard stops)

| Category | Triggers |
|---|---|
| `OVERDOSE` | "four pills", "overdose", "too many", "by mistake", "accidentally" |
| `SOMEONE_ELSES` | "husband's pill", "not mine", "found a pill" |
| `UNSAFE_COMBO` | "ibuprofen", "pain pill", "aspirin", "tramadol", "sleeping pill" |
| `WRONG_ROUTE` | "crush", "chew", "open the capsule", "grind" |
| `CRITICAL_DOUBT` | "skipped my insulin", "ran out", "stopped taking" |

## Dispatch Command Reference

```bash
# Standard route
python dispatch.py "medication words"

# Resolve pending ambiguity
python dispatch.py --session "ambig_20260528_..." "user clarification"

# Show logs
python dispatch.py --logs
python dispatch.py --logs --limit 20

# Caregiver correction (two steps)
python dispatch.py --caregiver-override 3 "reason"
# → returns token
python dispatch.py --confirm-override "ovr_token" "yes, correct the record"

# Reset for demo
python dispatch.py --reset
```

## Example Full Session

```
User: "I took my heart pill"
→ python dispatch.py "I took my heart pill"
→ {"outcome": "CONFIRMED", "message": "Got it..."}
→ Deliver message.

User: "the white pill"
→ python dispatch.py "the white pill"
→ {"outcome": "AMBIGUOUS", "session_key": "ambig_...", "message": "I found..."}
→ Deliver clarification question. Save session_key.

User: "2"
→ python dispatch.py --session "ambig_..." "2"
→ {"outcome": "CONFIRMED", "message": "Got it. Lisinopril..."}
→ Deliver confirmation.

User: "I want to take ibuprofen"
→ python dispatch.py "ibuprofen"
→ {"outcome": "ESCALATION", "message": "I need to stop here..."}
→ Deliver message EXACTLY as returned. Stop normal flow.
```
