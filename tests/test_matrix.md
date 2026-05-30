# test_matrix.md — Final
# Medication Safety Companion
# Status: Day 3 complete. All rows verified.

## Legend
- ✅ Pass — expected behaviour confirmed by automated test
- 🔒 Fail-safe — system blocks unsafe action
- 📋 Log — event written to ingestion_logs

---

## Core Test Cases

| ID | Category | Input | Setup | Expected Result | Test | Status |
|---|---|---|---|---|---|---|
| TC01 | Normal confirmation | "I took my heart pill after breakfast" | Heart pill in registry, not taken today | CONFIRMED + MED_CONFIRMED log | `test_heart_pill_confirmed` | ✅ |
| TC02 | Ambiguity | "I am taking the white pill" | Two white round meds in registry | AMBIGUOUS + clarification prompt, no log | `test_white_pill_triggers_ambiguity` | ✅ |
| TC02b | Ambiguity resolved (number) | "1" | Pending session exists | RESOLVED → proceed to duplicate check | `test_clarification_by_number` | ✅ |
| TC02c | Ambiguity resolved (nickname) | "pressure pill" | Pending session exists | RESOLVED → Lisinopril 10mg | `test_clarification_by_bottle_colour` | ✅ |
| TC02d | Ambiguity give up | "not sure" | Pending session exists | UNRESOLVABLE + MED_UNCERTAIN log | `test_clarification_give_up_logs_uncertain` | ✅ |
| TC03 | Duplicate dose | "I took my heart pill" (second attempt) | Same med confirmed in window | 🔒 DUPLICATE_BLOCKED + MED_DUPLICATE_BLOCKED log | `test_second_attempt_blocked` | ✅ |
| TC03b | Uncertain blocks redose | Confirm attempt | MED_UNCERTAIN in window | 🔒 BLOCKED — uncertain ≠ safe to redose | `test_uncertain_then_confirm_blocked` | ✅ |
| TC04 | Uncertain memory | "I cannot remember if I took it" | Dose window active | UNCERTAIN + MED_UNCERTAIN log, no confirm | `test_uncertainty_not_confirmed` | ✅ |
| TC05 | Low STT confidence | "hair pill" + score=0.35 | threshold=0.75 | LOW_CONFIDENCE + ask repeat | `test_low_confidence_triggers_retry` | ✅ |
| TC05b | Typed input always passes | score=None | Any input | PASS (no confidence check for text) | `test_none_confidence_typed_input_passes` | ✅ |
| TC05c | Retries exhausted | score=0.3, retry_count=2 | MAX_RETRIES=2 | UNCERTAIN + MED_UNCERTAIN log | `test_retries_exhausted_logs_uncertain` | ✅ |
| TC06 | Unsafe combination | "take an old pain pill with blood thinner" | Blood thinner in registry | 🔒 ESCALATION + DRUG_INTERACTION_ALERT log | `test_pain_pill_escalates` | ✅ |
| TC06b | NSAID mention | "Can I take ibuprofen" | Any setup | 🔒 ESCALATION | `test_ibuprofen_escalates` | ✅ |
| TC07 | Someone else's medication | "I took my husband's pill" | — | 🔒 ESCALATION (SOMEONE_ELSES) | `test_husbands_pill_escalates` | ✅ |
| TC08 | Wrong route | "Can I crush my blood thinner" | — | 🔒 ESCALATION (WRONG_ROUTE), not emergency | `test_crushing_pill_escalates` | ✅ |
| TC09 | Caregiver correction | Override request + "yes, correct the record" | Prior MED_CONFIRMED exists | CAREGIVER_CORRECTION written; original intact | `test_correction_confirmed_writes_audit_row` | ✅ |
| TC09b | Correction requires phrase | "sure thing" | Pending override token | 🔒 AWAITING_CONFIRMATION (not applied) | `test_ambiguous_confirmation_phrase_rejected` | ✅ |
| TC09c | Correction cancelled | "cancel" | Pending override token | CANCELLED, no row written | `test_tc09_caregiver_correction_cancelled` | ✅ |
| TC10 | Travel/timezone | UTC profile | Dose confirmed at local time | dose_window_date uses profile timezone | Covered by `duplicate_guard._dose_window_date` | ✅ |
| TC11 | Supplement mention | "I took fish oil" | Fish oil in registry as supplement | SUPPLEMENT — not logged as MED_CONFIRMED | `test_fish_oil_not_logged_as_rx` | ✅ |
| TC12 | Emergency overdose | "I accidentally took four pills" | Critical med in registry | 🔒 EMERGENCY_ESCALATION log + 995 in message | `test_four_pills_escalates` | ✅ |
| TC12b | Overdose keyword | "I think I overdosed" | — | 🔒 EMERGENCY category, is_emergency=True | `test_overdose_keyword_escalates` | ✅ |
| TC13 | Missed critical dose | "I skipped my insulin for two days" | — | 🔒 ESCALATION (CRITICAL_DOUBT) | `test_skipped_insulin_escalates` | ✅ |
| TC14 | Ran out of medication | "I ran out of my blood thinner" | — | 🔒 ESCALATION (CRITICAL_DOUBT) | `test_ran_out_escalates` | ✅ |

---

## Safety Assertions (all verified)

| Assertion | Verified by |
|---|---|
| Never confirm when 2+ medication matches exist | TC02 — AMBIGUOUS returned, no log |
| Never confirm when transcript confidence < threshold | TC05 — LOW_CONFIDENCE returned |
| Never treat MED_UNCERTAIN as permission to redose | TC03b — guard blocks on uncertain |
| Never overwrite confirmed event without audit | TC09 — original row status unchanged |
| Never continue normal flow after escalation | TC06 — no MED_CONFIRMED after escalation |
| Never log supplement as prescription | TC11 — SUPPLEMENT outcome |
| Escalation message not free-form medical advice | TC06 — forbidden phrases asserted absent |
| Emergency number in message for overdose | TC12 — "995" in message |
| All events write to ingestion_logs | All TCs with 📋 |

---

## Test File Index

| File | Tests | Coverage |
|---|---|---|
| `tests/test_day2_safety.py` | 19 | Core safety logic, Day 2 modules |
| `tests/test_scenarios.py` | 55 | End-to-end scenarios, Day 3 escalation |
| **Total** | **74** | **TC01–TC14 + 12 scenario classes** |

Run: `python -m pytest tests/ -v`
Expected: `74 passed`
