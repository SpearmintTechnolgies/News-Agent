# Prepare openclaw-data + build-context for Docker on Windows (no bash required)
$ErrorActionPreference = "Stop"
$DockerDir = $PSScriptRoot
$Src = Split-Path $DockerDir -Parent
$DestData = Join-Path $DockerDir "openclaw-data"
$DestCtx = Join-Path $DockerDir "build-context"
$GogBin = Join-Path $DockerDir "gog-bin"
$ContainerRoot = "/home/openclaw/.openclaw"
$BifrostUrl = "http://bifrost:8080/v1"

Write-Host "SRC  = $Src"
Write-Host "DATA = $DestData"
Write-Host "CTX  = $DestCtx"

function Copy-Tree($from, $to) {
    if (-not (Test-Path $from)) { return }
    New-Item -ItemType Directory -Force -Path $to | Out-Null
    robocopy $from $to /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed $from -> $to (code $LASTEXITCODE)" }
}

# --- openclaw-data (persistent volumes) ---
foreach ($d in @(
    "data", "credentials/wp", "projects", "telegram", "identity", "devices",
    "logs", "workspace-orchestrator/state", "gogcli-config", "assets",
    "media/inbound", "backups"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DestData $d) | Out-Null
}

if (Test-Path (Join-Path $Src "data\editorial.db")) {
    Copy-Item -Force (Join-Path $Src "data\editorial.db") (Join-Path $DestData "data\editorial.db")
} else {
    throw "Missing data/editorial.db - need pipeline DB seed"
}
if (Test-Path (Join-Path $Src "article_history.db")) {
    Copy-Item -Force (Join-Path $Src "article_history.db") (Join-Path $DestData "article_history.db")
} else {
    # empty file mount still required by compose
    New-Item -ItemType File -Force -Path (Join-Path $DestData "article_history.db") | Out-Null
}

Copy-Tree (Join-Path $Src "credentials") (Join-Path $DestData "credentials")
Copy-Tree (Join-Path $Src "projects") (Join-Path $DestData "projects")
Copy-Tree (Join-Path $Src "telegram") (Join-Path $DestData "telegram")
Copy-Tree (Join-Path $Src "identity") (Join-Path $DestData "identity")
Copy-Tree (Join-Path $Src "devices") (Join-Path $DestData "devices")
Copy-Tree (Join-Path $Src "workspace-orchestrator\state") (Join-Path $DestData "workspace-orchestrator\state")
Copy-Tree (Join-Path $Src "assets") (Join-Path $DestData "assets")
if (Test-Path (Join-Path $Src "media\inbound")) {
    Copy-Tree (Join-Path $Src "media\inbound") (Join-Path $DestData "media\inbound")
}
if (Test-Path (Join-Path $Src ".backups")) {
    Copy-Tree (Join-Path $Src ".backups") (Join-Path $DestData "backups")
}
# clear logs on host for fresh container logs
Get-ChildItem (Join-Path $DestData "logs") -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "openclaw-data ready"

# --- build-context (baked into image) ---
if (Test-Path $DestCtx) { Remove-Item -Recurse -Force $DestCtx }
New-Item -ItemType Directory -Force -Path $DestCtx | Out-Null

$excludeDirs = @(
    "data", "logs", "browser", "node_modules", "agents", "sawan", "docker",
    ".pytest_cache", "extra-non-related-web-resources-for-reference", "identity",
    "devices", "credentials", "projects", "telegram", ".git", "npm",
    "bin-shim", ".cursor"
)
$robolog = Join-Path $env:TEMP "openclaw-robocopy.log"
$xd = ($excludeDirs | ForEach-Object { "/XD"; $_ })
$args = @($Src, $DestCtx, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np") + $xd + @("/XF", "article_history.db")
& robocopy @args | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy build-context failed code=$LASTEXITCODE" }

New-Item -ItemType Directory -Force -Path (Join-Path $DestCtx "workspace-orchestrator\state") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DestCtx "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DestCtx "data") | Out-Null

# Path patch openclaw.json (and rewrite host-specific workspace paths)
$cfgPath = Join-Path $DestCtx "openclaw.json"
if (-not (Test-Path $cfgPath)) { throw "openclaw.json missing in build-context" }

$py = @(
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "python.exe"
) | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $py) { $py = "python" }

$patchScript = @'
import json, sys, re
path, container_root, bifrost = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as f:
    raw = f.read()
# normalize any previous Windows or Linux host paths to container root
raw = raw.replace("C:/Users/Aditya Singh/OneDrive/Desktop/News Agent", container_root)
raw = raw.replace(r"C:\\Users\\Aditya Singh\\OneDrive\\Desktop\\News Agent", container_root)
raw = raw.replace("/home/bhard/.openclaw", container_root)
data = json.loads(raw)

def walk(o):
    if isinstance(o, dict):
        return {k: walk(v) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v) for v in o]
    if isinstance(o, str):
        s = o
        s = s.replace("C:/Users/Aditya Singh/OneDrive/Desktop/News Agent", container_root)
        s = s.replace("/home/bhard/.openclaw", container_root)
        return s
    return o

data = walk(data)
data.setdefault("env", {})["BIFROST_BASE_URL"] = bifrost
if "browser" in data and isinstance(data["browser"], dict):
    data["browser"]["executablePath"] = "/usr/bin/chromium"
# gateway bind must listen on all interfaces inside the container
gw = data.setdefault("gateway", {})
if isinstance(gw, dict):
    gw["bind"] = "lan"
    # allow control UI from host
    cui = gw.setdefault("controlUi", {})
    if isinstance(cui, dict):
        cui["allowedOrigins"] = [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
        ]
# local-bifrost provider URL
providers = data.setdefault("models", {}).setdefault("providers", {})
if "local-bifrost" in providers and isinstance(providers["local-bifrost"], dict):
    providers["local-bifrost"]["baseUrl"] = bifrost
# plugin absolute paths
if "plugins" in data and isinstance(data["plugins"], dict):
    load = data["plugins"].get("load")
    if isinstance(load, dict) and isinstance(load.get("paths"), list):
        load["paths"] = [
            p.replace("/home/bhard/.openclaw", container_root)
             .replace("C:/Users/Aditya Singh/OneDrive/Desktop/News Agent", container_root)
            for p in load["paths"]
        ]
    installs = data["plugins"].get("installs")
    if isinstance(installs, dict):
        for name, meta in installs.items():
            if isinstance(meta, dict):
                for key in ("sourcePath", "installPath"):
                    if key in meta and isinstance(meta[key], str):
                        meta[key] = (
                            meta[key]
                            .replace("/home/bhard/.openclaw", container_root)
                            .replace("C:/Users/Aditya Singh/OneDrive/Desktop/News Agent", container_root)
                        )

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("patched", path)
'@
$patchFile = Join-Path $env:TEMP "patch_openclaw_docker.py"
Set-Content -Path $patchFile -Value $patchScript -Encoding UTF8
& $py $patchFile $cfgPath $ContainerRoot $BifrostUrl

# Lightweight path replace in common text files inside context (subset for speed)
Get-ChildItem $DestCtx -Recurse -Include *.sh,*.py,*.md,*.service -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch 'node_modules' } |
  ForEach-Object {
    $c = Get-Content -Raw -LiteralPath $_.FullName -ErrorAction SilentlyContinue
    if ($null -eq $c) { return }
    if ($c -match '/home/bhard/\.openclaw|Desktop/News Agent') {
      $n = $c.Replace('/home/bhard/.openclaw', $ContainerRoot)
      $n = $n.Replace('C:/Users/Aditya Singh/OneDrive/Desktop/News Agent', $ContainerRoot)
      $n = $n.Replace('http://host.docker.internal:8888/v1', $BifrostUrl)
      $n = $n.Replace('http://192.168.32.1:8888/v1', $BifrostUrl)
      if ($n -ne $c) {
        Set-Content -LiteralPath $_.FullName -Value $n -NoNewline -Encoding UTF8
      }
    }
  }

# --- gog stub (Drive optional; Linux script for container) ---
New-Item -ItemType Directory -Force -Path $GogBin | Out-Null
$gogPath = Join-Path $GogBin "gog"
$gogContent = "#!/bin/sh`n# placeholder gog - Drive upload disabled until real binary provided`necho `"gog stub: not configured`" >&2`nexit 1`n"
# Write with LF only
[IO.File]::WriteAllText($gogPath, $gogContent.Replace("`r`n", "`n"))

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next: docker compose build news-agent && docker compose up -d"
