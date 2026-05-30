#!/usr/bin/env bash
# setup_hermes_integration.sh
# Install the med-safety-companion skill and plugin into Hermes Agent.
#
# Run from the project root AFTER installing Hermes Agent:
#   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
#   Then: bash hermes_integration/setup_hermes_integration.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Medication Safety Companion — Hermes Integration Setup ==="
echo "Project root: $PROJECT_ROOT"

# ── 1. Verify Hermes is installed ─────────────────────────────────────────────
if ! command -v hermes &>/dev/null; then
  echo ""
  echo "ERROR: hermes command not found."
  echo "Install Hermes Agent first:"
  echo "  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
  exit 1
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
echo "Hermes home: $HERMES_HOME"

# ── 2. Create DB directory and seed ───────────────────────────────────────────
DB_DIR="$HERMES_HOME/med_safety"
DB_PATH="$DB_DIR/med_safety.db"

mkdir -p "$DB_DIR"
echo ""
echo "Setting up medication database at $DB_PATH ..."
export MED_SAFETY_DB="$DB_PATH"
cd "$PROJECT_ROOT"
python3 seed.py
echo "Database seeded."

# ── 3. Install the skill ──────────────────────────────────────────────────────
SKILL_SRC="$SCRIPT_DIR/skills/med-safety-companion"
SKILL_DST="$HERMES_HOME/skills/med-safety-companion"

echo ""
echo "Installing skill to $SKILL_DST ..."
mkdir -p "$(dirname "$SKILL_DST")"
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "Skill installed."

# ── 4. Install the plugin ─────────────────────────────────────────────────────
PLUGIN_SRC="$SCRIPT_DIR/plugin/hermes_plugin.py"
PLUGIN_DST="$HERMES_HOME/plugins/med_safety_plugin.py"

mkdir -p "$HERMES_HOME/plugins"
cp "$PLUGIN_SRC" "$PLUGIN_DST"
echo "Plugin installed to $PLUGIN_DST"

# ── 5. Write MED_SAFETY_DB to Hermes .env ─────────────────────────────────────
HERMES_ENV="$HERMES_HOME/.env"
touch "$HERMES_ENV"

if grep -q "^MED_SAFETY_DB=" "$HERMES_ENV" 2>/dev/null; then
  # Update existing
  sed -i "s|^MED_SAFETY_DB=.*|MED_SAFETY_DB=$DB_PATH|" "$HERMES_ENV"
else
  echo "MED_SAFETY_DB=$DB_PATH" >> "$HERMES_ENV"
fi

# Also add project root to PYTHONPATH so plugin can import safety modules
if grep -q "^MED_SAFETY_PYTHONPATH=" "$HERMES_ENV" 2>/dev/null; then
  sed -i "s|^MED_SAFETY_PYTHONPATH=.*|MED_SAFETY_PYTHONPATH=$PROJECT_ROOT|" "$HERMES_ENV"
else
  echo "MED_SAFETY_PYTHONPATH=$PROJECT_ROOT" >> "$HERMES_ENV"
fi

echo "Environment variables written to $HERMES_ENV"

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Start Hermes:"
echo "     hermes"
echo ""
echo "  2. Load the medication safety skill:"
echo "     /med-safety-companion"
echo ""
echo "  3. Say: 'I took my heart pill'"
echo ""
echo "  4. For Telegram bot (built into Hermes):"
echo "     hermes setup  (choose Telegram)"
echo "     hermes gateway start"
echo "     Then message your bot: /med-safety-companion"
echo ""
echo "  5. Run tests (no Hermes needed — safety logic is standalone):"
echo "     cd $PROJECT_ROOT && python -m pytest tests/ -v"
