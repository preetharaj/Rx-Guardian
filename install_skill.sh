#!/usr/bin/env bash
# install_skill.sh — Install the med-safety skill into Hermes Agent
#
# Usage: bash install_skill.sh
# Requires: Hermes Agent already installed (hermes command available)

set -e

SKILL_SRC="$(cd "$(dirname "$0")/hermes-skill/med-safety" && pwd)"
HERMES_SKILLS_DIR="${HERMES_HOME:-$HOME/.hermes}/skills"
DEST="$HERMES_SKILLS_DIR/healthcare/med-safety"

echo "Installing med-safety skill..."
echo "  Source: $SKILL_SRC"
echo "  Destination: $DEST"

mkdir -p "$DEST/references"

cp "$SKILL_SRC/SKILL.md"                    "$DEST/SKILL.md"
cp "$SKILL_SRC/SOUL.md"                     "$DEST/SOUL.md"
cp "$SKILL_SRC/references/outcomes.md"      "$DEST/references/outcomes.md"

# Set project dir in Hermes config
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_PATH="$PROJECT_DIR/med_safety.db"

echo ""
echo "✓ Skill installed to $DEST"
echo ""
echo "Next steps:"
echo "  1. Set the project path in Hermes config:"
echo "     hermes config set skills.config.med_safety.project_dir \"$PROJECT_DIR\""
echo "     hermes config set skills.config.med_safety.db_path \"$DB_PATH\""
echo ""
echo "  2. Add your Gemini API key (optional — system works without it):"
echo "     echo 'GEMINI_API_KEY=your_key' >> ~/.hermes/.env"
echo ""
echo "  3. Seed the medication database:"
echo "     cd \"$PROJECT_DIR\" && python seed.py"
echo ""
echo "  4. Start Hermes and load the skill:"
echo "     hermes"
echo "     /med-safety"
echo ""
echo "  5. Or use it directly from CLI:"
echo "     hermes chat -q \"/med-safety I took my heart pill\""
