# setup_hermes_integration.ps1
# Install the med-safety-companion skill and plugin into Hermes Agent on Windows.
#
# Run from the project root AFTER installing Hermes Agent:
#   iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
#   Then: .\hermes_integration\setup_hermes_integration.ps1

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "=== Medication Safety Companion — Hermes Integration Setup ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"

# ── 1. Verify Hermes is installed ────────────────────────────────────────────
if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    Write-Error @"
hermes command not found.
Install Hermes Agent first (in PowerShell):
  iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
"@
    exit 1
}

$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:USERPROFILE\.hermes" }
Write-Host "Hermes home: $HermesHome"

# ── 2. Create DB directory and seed ──────────────────────────────────────────
$DbDir  = "$HermesHome\med_safety"
$DbPath = "$DbDir\med_safety.db"

New-Item -ItemType Directory -Force -Path $DbDir | Out-Null

Write-Host "`nSetting up medication database at $DbPath ..."
$env:MED_SAFETY_DB = $DbPath
Set-Location $ProjectRoot
python seed.py
Write-Host "Database seeded."

# ── 3. Install the skill ─────────────────────────────────────────────────────
$SkillSrc = "$ScriptDir\skills\med-safety-companion"
$SkillDst = "$HermesHome\skills\med-safety-companion"

Write-Host "`nInstalling skill to $SkillDst ..."
if (Test-Path $SkillDst) { Remove-Item $SkillDst -Recurse -Force }
Copy-Item $SkillSrc $SkillDst -Recurse
Write-Host "Skill installed."

# ── 4. Install the plugin ────────────────────────────────────────────────────
$PluginSrc = "$ScriptDir\plugin\hermes_plugin.py"
$PluginDst = "$HermesHome\plugins\med_safety_plugin.py"

New-Item -ItemType Directory -Force -Path "$HermesHome\plugins" | Out-Null
Copy-Item $PluginSrc $PluginDst -Force
Write-Host "Plugin installed to $PluginDst"

# ── 5. Write env vars to Hermes .env ─────────────────────────────────────────
$HermesEnv = "$HermesHome\.env"
if (-not (Test-Path $HermesEnv)) { New-Item $HermesEnv -ItemType File | Out-Null }

$content = Get-Content $HermesEnv -Raw -ErrorAction SilentlyContinue
if ($content -match "^MED_SAFETY_DB=") {
    $content = $content -replace "(?m)^MED_SAFETY_DB=.*", "MED_SAFETY_DB=$DbPath"
} else {
    $content += "`nMED_SAFETY_DB=$DbPath"
}
if ($content -match "^MED_SAFETY_PYTHONPATH=") {
    $content = $content -replace "(?m)^MED_SAFETY_PYTHONPATH=.*", "MED_SAFETY_PYTHONPATH=$ProjectRoot"
} else {
    $content += "`nMED_SAFETY_PYTHONPATH=$ProjectRoot"
}
Set-Content $HermesEnv $content
Write-Host "Environment variables written to $HermesEnv"

# ── 6. Done ──────────────────────────────────────────────────────────────────
Write-Host "`n=== Setup complete ===" -ForegroundColor Green
Write-Host @"

Next steps:

  1. Start Hermes:
     hermes

  2. Load the medication safety skill:
     /med-safety-companion

  3. Say: 'I took my heart pill'

  4. For Telegram bot (built into Hermes):
     hermes setup   (choose Telegram)
     hermes gateway start
     Then message your bot: /med-safety-companion

  5. Run tests (no Hermes needed):
     cd $ProjectRoot
     python -m pytest tests\ -v
"@
