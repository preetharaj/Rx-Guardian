-- Medication Safety Companion
-- schema.sql

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ─── Medications ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS medications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    clinical_name       TEXT    NOT NULL,          -- e.g. "Warfarin 5mg"
    rxcui               TEXT,                      -- RxNorm concept ID (informational)
    dosage              TEXT    NOT NULL,           -- e.g. "5mg once daily"
    scheduled_time      TEXT    NOT NULL,           -- "08:00" 24h local
    nickname            TEXT,                      -- e.g. "heart pill", "blood thinner"
    visual_description  TEXT,                      -- e.g. "small white oval"
    disambiguation_cue  TEXT,                      -- e.g. "comes in orange bottle"
    is_critical         INTEGER NOT NULL DEFAULT 0, -- 1 = escalate on any doubt
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_med_nickname ON medications(nickname);
CREATE INDEX IF NOT EXISTS idx_med_visual   ON medications(visual_description);

-- ─── Ingestion Logs ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_logs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id        INTEGER REFERENCES medications(id),  -- NULL = unresolved
    logged_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    dose_window_date     TEXT    NOT NULL,   -- "YYYY-MM-DD" for dedup key
    input_mode           TEXT    NOT NULL DEFAULT 'text',  -- 'text' | 'voice'
    state_status         TEXT    NOT NULL,   -- see EVENT TYPES below
    raw_transcript       TEXT,              -- verbatim user input, always kept
    confidence_score     REAL,              -- STT or NLU confidence 0.0–1.0
    resolved_by          TEXT,              -- 'user' | 'caregiver' | 'system'
    notes                TEXT
);
-- EVENT TYPES:
--   MED_CONFIRMED            confirmed dose, no issues
--   MED_UNCERTAIN            user unsure; do not treat as confirmed
--   MED_SKIPPED              explicit skip
--   MED_DUPLICATE_BLOCKED    duplicate attempt within window
--   MED_AMBIGUOUS            multiple med matches; pending clarification
--   MED_LOW_CONFIDENCE       STT score too low; pending repeat
--   DRUG_INTERACTION_ALERT   unsafe combo flagged
--   CAREGIVER_ESCALATION     escalated to caregiver
--   EMERGENCY_ESCALATION     overdose / unsafe situation
--   CAREGIVER_CORRECTION     caregiver corrected an existing log entry

CREATE INDEX IF NOT EXISTS idx_log_med_date ON ingestion_logs(medication_id, dose_window_date);
CREATE INDEX IF NOT EXISTS idx_log_status   ON ingestion_logs(state_status);

-- ─── Patient Profile ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patient_profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Example keys: 'patient_name', 'caregiver_name', 'caregiver_contact',
--               'schedule_timezone', 'dose_window_hours', 'confidence_threshold'

-- ─── Disambiguation Sessions ─────────────────────────────────────────────────
-- Tracks pending clarification (multi-turn ambiguity resolution)
CREATE TABLE IF NOT EXISTS disambiguation_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key      TEXT    NOT NULL UNIQUE,   -- e.g. "ambig_20260528_143200"
    candidate_ids    TEXT    NOT NULL,           -- JSON array of medication IDs
    raw_transcript   TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at      TEXT,
    resolved_med_id  INTEGER REFERENCES medications(id),
    status           TEXT    NOT NULL DEFAULT 'pending'  -- 'pending' | 'resolved' | 'expired'
);
