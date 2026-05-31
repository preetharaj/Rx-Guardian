# risk_register.md
# Medication Safety Companion — Risk Register
# Hermes Agent Challenge 2026

---

## Technical Risks

### R01 — In-memory caregiver override sessions
**Risk:** `_pending_overrides` dict in `caregiver_override.py` is lost on process restart.
**Severity:** Medium. Demo is single-session; no real impact on contest submission.
**Mitigation:** Note in README. Production fix: persist pending_overrides to
`disambiguation_sessions` table or a dedicated `override_sessions` table.
**Status:** Accepted for demo scope.

---

### R02 — Fuzzy match candidate sort order not guaranteed stable
**Risk:** When two medications score identically in `lookup.py`, order of `candidates[]`
is non-deterministic. Option "1" in clarification prompt may map to different meds
across runs if scores tie.
**Severity:** Low for demo (seeded data produces stable scores). High for production.
**Mitigation:** Sort `candidates` by `id ASC` before building session and prompt.
Current code does not do this — flagged for post-contest fix.
**Status:** Known gap.

---

### R03 — `_extract_med_query` strips too aggressively
**Risk:** "I took aspirin and my heart pill" → stripped to "aspirin and my heart pill"
or possibly just "aspirin" — misses the heart pill mention.
**Severity:** Medium. Safety impact: aspirin triggers escalation correctly regardless.
General impact: compound sentences parsed incorrectly.
**Mitigation:** Hermes intent extraction (structured JSON) should separate
`medication_reference` from `other_medication_mentioned` before calling router.
Router currently receives raw transcript; this works for simple sentences only.
**Status:** Acceptable for demo. Production needs Hermes pre-extraction.

---

### R04 — Emergency keyword matching is substring-based
**Risk:** "I have not taken an overdose" matches "overdose" and triggers escalation.
**Severity:** Medium. False positive is safer than false negative for emergency detection.
**Mitigation:** Accept the over-trigger. Response is "call emergency services if needed"
— a false trigger causes mild inconvenience; a missed trigger is dangerous.
Log all escalations so caregivers can review false positives.
**Status:** Accepted by design.

---

### R05 — No real STT integration
**Risk:** `confidence_score` in demo is always `None` (typed input). Low-confidence
path (TC05) only exercised in unit tests, not in live demo.
**Severity:** Low for contest. High for real-world use.
**Mitigation:** Demo uses typed input with a note that STT integration would supply
real confidence scores. `confidence_rules.py` is fully implemented and tested.
**Status:** Out of scope for contest.

---

### R06 — SQLite single-writer constraint
**Risk:** If caregiver and patient act simultaneously, SQLite WAL mode handles reads
but concurrent writes can queue. No connection pooling.
**Severity:** Low. Single-user demo. Home-care use case is inherently single-user.
**Mitigation:** WAL pragma already set in schema. Acceptable for scope.
**Status:** Accepted.

---

### R07 — Hermes API not stubbed for offline testing
**Risk:** Tests run against the safety layer only; no test covers the full
Hermes intent-extraction → router → Hermes response-generation round trip.
**Severity:** Medium for contest judging if live API is unavailable.
**Mitigation:** `safety_router.py` is fully testable standalone (all 19 tests pass).
Hermes integration in `hermes_agent.py` is a thin wrapper — failure modes are
API key missing or quota exceeded, not logic errors.
**Status:** Document clearly in README.

---

### R08 — Supplement detection relies on `(supplement)` suffix in clinical_name
**Risk:** If a new supplement is seeded without the suffix, it's treated as an Rx med.
**Severity:** Low. False-positive prescription logging for a supplement is not dangerous.
**Mitigation:** Add `is_supplement` boolean column in a future schema version.
For demo: seed.py enforces the convention; noted in README.
**Status:** Accepted for demo scope.

---

## Safety Risks

### SR01 — LLM may hallucinate medication names in intent extraction
**Risk:** Hermes returns `medication_reference: "Warfin"` (misspelling) and lookup
returns UNKNOWN, causing a missed confirmation.
**Severity:** Low from safety perspective — missed confirmation is safer than
false confirmation. User is prompted to check bottle name.
**Mitigation:** Intent extraction uses the exact words the user said; prompt
explicitly forbids normalisation. Lookup is a separate deterministic step.
**Status:** Acceptable.

---

### SR02 — Escalation keyword list is not exhaustive
**Risk:** User says "I took way too many" — "way too many" not in keyword set.
"too many" IS in the set and would match — but phrase variants may be missed.
**Severity:** Medium. Each miss = potential safety incident.
**Mitigation:** Keyword list reviewed against ISMP (Institute for Safe Medication
Practices) common error phrases. Hermes intent extraction also flags `intent: EMERGENCY`
as a second independent signal. Two-layer detection reduces miss rate.
**Status:** Known gap. Recommend ongoing keyword review post-contest.

---

### SR03 — `MED_UNCERTAIN` does not automatically notify caregiver
**Risk:** An uncertain event is logged but no push notification is sent.
Caregiver may not check logs promptly.
**Severity:** Medium for real use. Low for demo (caregiver manually reviews logs).
**Mitigation:** Add caregiver notification hook (email/SMS) in `log_uncertain()`.
Out of scope for contest. Flagged in README.
**Status:** Out of scope.

---

### SR04 — Clock skew between scheduled_time and system time
**Risk:** Patient's device clock is wrong → duplicate_guard uses wrong UTC time →
dose window check incorrect.
**Severity:** Low. `schedule_timezone` stored in patient_profile allows correction.
**Mitigation:** Use `datetime.now(zoneinfo.ZoneInfo(...))` in duplicate_guard (implemented).
**Status:** Mitigated.

---

## Intentionally Out of Scope

| Feature | Reason excluded |
|---|---|
| Computer vision for pill identification | Unreliable without controlled lighting; adds model dependency |
| Real STT / voice interface | Adds device/OS dependency; confidence_rules handles it when present |
| Push notifications to caregiver | Requires messaging infrastructure (Twilio, FCM) — not a Hermes feature |
| Multi-patient profiles | Out of scope per plan.md |
| Drug interaction database lookup (DrugBank/RxNorm) | Requires API key and adds latency to safety-critical path |
| Prescription refill reminders | Useful but unrelated to core safety demo |
| FHIR / EHR integration | Enterprise feature, not home-care demo |
| Autonomous dose adjustment | Prohibited by design; no medical advice |
| iOS/Android app | CLI harness sufficient for demo |
| User authentication / login | Single-user home device; out of scope |

---

## What Is Production-Ready (as submitted)

- Deterministic safety pipeline (confidence → escalation → lookup → duplicate → confirm)
- Immutable audit log with 10 event types
- Ambiguity resolution with session expiry
- Caregiver correction with two-step explicit confirmation
- 5-category escalation engine with 80+ trigger phrases
- 19 passing unit + integration tests
- Locale-aware emergency number
- Timezone-aware dose window
- Clean schema with foreign keys, indexes, WAL mode
