"""
seed.py — Load synthetic medications and patient profile into med_safety.db.

Run once: python seed.py
Re-run safe: wipes medications + patient_profile, re-inserts.
"""

import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "med_safety.db")

# ─── Synthetic Medications ────────────────────────────────────────────────────
# 6 meds designed to exercise key test scenarios:
#   - Two white pills  → ambiguity test (TC02)
#   - Blood thinner    → unsafe-combo escalation (TC06)
#   - Critical med     → emergency escalation (TC12)
#   - Insulin          → timing-sensitive
#   - Supplement       → must not mix with Rx (TC11)
MEDICATIONS = [
    {
        "clinical_name":       "Warfarin 5mg",
        "rxcui":               "855332",
        "dosage":              "5mg once daily",
        "scheduled_time":      "08:00",
        "nickname":            "blood thinner",
        "visual_description":  "small peach round",
        "disambiguation_cue":  "comes in white bottle with red label",
        "is_critical":         1,
    },
    {
        "clinical_name":       "Metoprolol Succinate 50mg",
        "rxcui":               "866514",
        "dosage":              "50mg once daily",
        "scheduled_time":      "08:00",
        "nickname":            "heart pill",
        "visual_description":  "white oval",
        "disambiguation_cue":  "scored down the middle, blue cap bottle",
        "is_critical":         1,
    },
    {
        "clinical_name":       "Lisinopril 10mg",
        "rxcui":               "314076",
        "dosage":              "10mg once daily",
        "scheduled_time":      "08:00",
        "nickname":            "pressure pill",
        "visual_description":  "white round",
        # INTENTIONALLY shares 'white round' with Amlodipine below → ambiguity test
        "disambiguation_cue":  "white round, orange bottle, morning dose",
        "is_critical":         0,
    },
    {
        "clinical_name":       "Amlodipine 5mg",
        "rxcui":               "197361",
        "dosage":              "5mg once daily",
        "scheduled_time":      "20:00",
        "nickname":            "calcium pill",
        "visual_description":  "white round",
        # Same visual as Lisinopril → triggers TC02 ambiguity
        "disambiguation_cue":  "white round, yellow bottle, evening dose at 8pm",
        "is_critical":         0,
    },
    {
        "clinical_name":       "Insulin Glargine 10 units",
        "rxcui":               "274783",
        "dosage":              "10 units subcutaneous at bedtime",
        "scheduled_time":      "22:00",
        "nickname":            "insulin",
        "visual_description":  "injection pen",
        "disambiguation_cue":  "long thin pen in refrigerator",
        "is_critical":         1,
    },
    {
        "clinical_name":       "Omeprazole 20mg",
        "rxcui":               "40790",
        "dosage":              "20mg once daily before breakfast",
        "scheduled_time":      "07:30",
        "nickname":            "stomach pill",
        "visual_description":  "purple capsule",
        "disambiguation_cue":  "purple-and-grey capsule, brown bottle",
        "is_critical":         0,
    },
]

# Supplements — stored separately, never treated as prescriptions.
# Present in DB so supplement-mention logic can identify them (TC11).
SUPPLEMENTS = [
    {
        "clinical_name":       "Fish Oil 1000mg (supplement)",
        "rxcui":               None,
        "dosage":              "1000mg with meals",
        "scheduled_time":      "12:00",
        "nickname":            "fish oil",
        "visual_description":  "large yellow softgel",
        "disambiguation_cue":  "supplement, not prescription",
        "is_critical":         0,
    },
]

PATIENT_PROFILE = {
    "patient_name":         "Demo Patient",
    "caregiver_name":       "Demo Caregiver",
    "caregiver_contact":    "caregiver@example.com",
    "schedule_timezone":    "Asia/Singapore",
    "dose_window_hours":    "6",     # hours after scheduled_time → duplicate window
    "confidence_threshold": "0.75",  # below = ask repeat
}


def seed(db_path: str = DB_PATH) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Apply schema if not yet applied
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_path) as f:
        con.executescript(f.read())

    # Wipe existing seed data (idempotent re-run)
    # Delete child rows first to satisfy FK constraints
    cur.execute("DELETE FROM disambiguation_sessions")
    cur.execute("DELETE FROM ingestion_logs")
    cur.execute("DELETE FROM medications")
    cur.execute("DELETE FROM patient_profile")
    con.commit()

    # Insert medications
    for med in MEDICATIONS + SUPPLEMENTS:
        cur.execute(
            """
            INSERT INTO medications
                (clinical_name, rxcui, dosage, scheduled_time,
                 nickname, visual_description, disambiguation_cue, is_critical)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                med["clinical_name"],
                med.get("rxcui"),
                med["dosage"],
                med["scheduled_time"],
                med.get("nickname"),
                med.get("visual_description"),
                med.get("disambiguation_cue"),
                med["is_critical"],
            ),
        )

    # Insert patient profile
    for key, value in PATIENT_PROFILE.items():
        cur.execute(
            "INSERT INTO patient_profile (key, value) VALUES (?, ?)",
            (key, value),
        )

    con.commit()
    con.close()

    total = len(MEDICATIONS) + len(SUPPLEMENTS)
    print(f"Seeded {total} medications and {len(PATIENT_PROFILE)} profile keys into {db_path}")


if __name__ == "__main__":
    seed()
