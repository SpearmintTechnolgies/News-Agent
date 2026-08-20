# Set-StrictMode -Version Latest # Use if needed for stricter error checking

# 1. Resolve project slug (arg > env > default)
$PROJECT_SLUG_INPUT = $env:PROJECT_SLUG
if (-not $PROJECT_SLUG_INPUT) {
    $PROJECT_SLUG_INPUT = "coinnetwork"
}

# Validate the project exists
$PROJECT_CONFIG_PATH = "$env:USERPROFILE\.openclaw\projects\$PROJECT_SLUG_INPUT.json"
if (-not (Test-Path $PROJECT_CONFIG_PATH)) {
    Write-Error "[INIT] ERROR: project config not found: $PROJECT_CONFIG_PATH"
    python3 "$env:USERPROFILE\.openclaw\workspace-orchestrator\skills\pipeline\project_config.py" --list 2>&1
    exit 1
}

$PROJECT_SLUG = (python3 "$env:USERPROFILE\.openclaw\workspace-orchestrator\skills\pipeline\project_config.py" --slug "$PROJECT_SLUG_INPUT" --field slug).Trim()

# RUN_ID must be globally unique
$RUN_ID = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + ([System.Guid]::NewGuid().ToString().Split('-')[0])
$RUN_DIR = "$env:TEMP\${PROJECT_SLUG}-run-${RUN_ID}"

# 2. Create nested directory tree + empty placeholder files
New-Item -ItemType Directory -Path "$RUN_DIR\research", "$RUN_DIR\article", "$RUN_DIR\media", "$RUN_DIR\publish", "$RUN_DIR\picker" -Force | Out-Null

$null | New-Item -ItemType File -Path "$RUN_DIR\research\raw.json"
$null | New-Item -ItemType File -Path "$RUN_DIR\research\validated.json"
$null | New-Item -ItemType File -Path "$RUN_DIR\research\headlines.json"
$null | New-Item -ItemType File -Path "$RUN_DIR\picker\picker_input.json"
$null | New-Item -ItemType File -Path "$RUN_DIR\picker\picks.json"
$null | New-Item -ItemType File -Path "$RUN_DIR\article\raw.md"
$null | New-Item -ItemType File -Path "$RUN_DIR\article\final.md"
$null | New-Item -ItemType File -Path "$RUN_DIR\article\with-image.md"
$null | New-Item -ItemType File -Path "$RUN_DIR\media\feature.jpg"
$null | New-Item -ItemType File -Path "$RUN_DIR\media\chart.png"
$null | New-Item -ItemType File -Path "$RUN_DIR\publish\news-card.json"

(Get-Date -UFormat %s) | Out-File -FilePath "$RUN_DIR\.run_started" -Encoding utf8

# 3. Write manifest.json
$scriptBlock = @"
import datetime, json, os, sys

run_id, run_dir, project, project_cfg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

manifest = {
    "run_id":              run_id,
    "run_dir":             run_dir,
    "project":             project,
    "project_config_path": project_cfg,
    "created_at":          datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "current_step":        "init",
    "story":               {},
    "batch": {
        "target_count":   1,
        "pick_run_id":    "",
        "current_pick":   0,
        "completed_picks": [],
    },
    "artifacts": {
        "research_raw":       f"{run_dir}/research/raw.json",
        "research_validated": f"{run_dir}/research/validated.json",
        "headlines":          f"{run_dir}/research/headlines.json",
        "picker_input":       f"{run_dir}/picker/picker_input.json",
        "picks":              f"{run_dir}/picker/picks.json",
        "article_raw":        f"{run_dir}/article/raw.md",
        "article_final":      f"{run_dir}/article/final.md",
        "article_with_image": f"{run_dir}/article/with-image.md",
        "docx":               f"{run_dir}/article/article.docx",
        "feature_image":      f"{run_dir}/media/feature.jpg",
        "chart":              f"{run_dir}/media/chart.png",
        "google_drive":       f"{run_dir}/publish/google-drive.json",
        "wordpress":          f"{run_dir}/publish/wordpress.json",
        "news_card":          f"{run_dir}/publish/news-card.json",
    },
    "steps":   {},
    "checks":  {},
    "results": {},
}

tmp = f"{run_dir}/manifest.json.tmp"
with open(tmp, "w") as f:
    json.dump(manifest, f, indent=2)
os.replace(tmp, f"{run_dir}/manifest.json")
"@
python3 -c "$scriptBlock" "$RUN_ID" "$RUN_DIR" "$PROJECT_SLUG" "$PROJECT_CONFIG_PATH"

# 4. Symlinks: (Skipping direct symlink creation on Windows, as it requires elevated privileges or specific settings.)
# Instead, scripts will rely on the RUN_DIR variable.

# 5. Active-run pointer + env file
"$RUN_DIR" | Out-File -FilePath "$env:TEMP\${PROJECT_SLUG}-active-run" -Encoding utf8
"$RUN_DIR" | Out-File -FilePath "$env:TEMP\crypto-active-run" -Encoding utf8 # Legacy

# Set environment variables for the current session (and output for OpenClaw)
Write-Host "Set-Item -Path Env:\RUN_ID -Value \"$RUN_ID\""
Write-Host "Set-Item -Path Env:\RUN_DIR -Value \"$RUN_DIR\""
Write-Host "Set-Item -Path Env:\CRYPTO_RUN_DIR -Value \"$RUN_DIR\""
Write-Host "Set-Item -Path Env:\PIPELINE_MANIFEST -Value \"$RUN_DIR\manifest.json\""
Write-Host "Set-Item -Path Env:\PROJECT_SLUG -Value \"$PROJECT_SLUG\""
Write-Host "Set-Item -Path Env:\PROJECT_CONFIG -Value \"$PROJECT_CONFIG_PATH\""
Write-Host "Set-Item -Path Env:\ENABLE_ARTICLE_CHARTS -Value \"$([int]$env:ENABLE_ARTICLE_CHARTS)\"

# Resolve frequently-used project fields ONCE and bake them into the env
$pythonScriptPath = "$env:TEMP\resolve_project_fields.py"
$pythonScriptContent = @"
import os, shlex, sys
script_dir, cfg_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
try:
    import project_config as pc
    cfg = pc.load_project_config(path=cfg_path)
except Exception as e:  # noqa: BLE001 - best effort; Step 0 has fallbacks
    print(f"# project field resolution skipped: {{e}}", flush=True)
    sys.exit(0)

def emit(var, dotted, *, absolute=False):
    val = cfg.get_path(dotted)
    if val is None or not isinstance(val, (str, int, float)):
        return
    val = str(val)
    if absolute and val:
        resolved = pc.resolve_openclaw_path(val)
        if os.path.exists(resolved):
            val = resolved
    print(f"Set-Item -Path Env:\\{var} -Value \"{val}\"")

emit("GROUP_CHAT_ID", "telegram.group_id")
emit("PROJECT_NAME", "name")
emit("TEMPLATE_PATH", "writer.template_path", absolute=True)
emit("DRIVE_PREFIX", "publisher.drive_doc_prefix")
emit("DRIVE_PARENT", "publisher.drive_parent_id")
emit("DRIVE_ACCT", "publisher.drive_account")
"@
$pythonScriptContent | Out-File -FilePath $pythonScriptPath -Encoding utf8

$generatedCommands = (python3 "$pythonScriptPath" "$env:USERPROFILE\.openclaw\workspace-orchestrator\skills\pipeline" "$PROJECT_CONFIG_PATH") | Out-String

# Remove the temporary Python script
Remove-Item $pythonScriptPath -ErrorAction SilentlyContinue

Write-Host $generatedCommands

# 6. Prune old run dirs
Get-ChildItem -Path $env:TEMP -Filter "${PROJECT_SLUG}-run-*" -Directory | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Recurse -Force | Out-Null
Get-ChildItem -Path $env:TEMP -Filter "crypto-run-*" -Directory | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Recurse -Force | Out-Null
Get-ChildItem -Path $env:TEMP -Filter "${PROJECT_SLUG}-run-env-*.ps1" -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Recurse -Force | Out-Null

Write-Host "[INIT] Project:   $PROJECT_SLUG"
Write-Host "[INIT] Config:    $PROJECT_CONFIG_PATH"
Write-Host "[INIT] Run bundle ready: $RUN_DIR"
Write-Host "[INIT] RUN_ID=$RUN_ID"
Write-Host "[INIT] Environment variables set for current session."
