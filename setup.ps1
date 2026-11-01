# GOAT setup after a fresh Windows install (2026-11-01).
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1            # check + fix what it can
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -CheckOnly # report only, change nothing
#
# Safe to re-run. Steps that need admin (firewall rule, winget installs) say so
# and are skipped when not elevated. Nothing here deletes anything.
param([switch]$CheckOnly)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$Py   = Join-Path $Root 'python'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$todo = New-Object System.Collections.Generic.List[string]

function Row($name, $state, $detail = '') {
    $color = @{ OK = 'Green'; FIXED = 'Cyan'; MISSING = 'Yellow'; BROKEN = 'Red'; OPTIONAL = 'DarkGray' }[$state]
    Write-Host ("[{0,-8}] {1}" -f $state, $name) -ForegroundColor $color -NoNewline
    if ($detail) { Write-Host "  - $detail" } else { Write-Host '' }
}

Write-Host "GOAT setup  root=$Root  admin=$admin  checkOnly=$CheckOnly`n"

# ---- Python 3.13 -------------------------------------------------------------
$pyOk = $false
try { $v = & py -3.13 -c "import sys;print(sys.version.split()[0])" 2>$null; $pyOk = $LASTEXITCODE -eq 0 } catch {}
if ($pyOk) { Row 'Python 3.13' OK $v }
else {
    Row 'Python 3.13' MISSING 'winget install Python.Python.3.13 (then re-run this script)'
    $todo.Add('Install Python 3.13: winget install Python.Python.3.13')
}

# ---- Python packages -----------------------------------------------------------
if ($pyOk) {
    $missing = & py -3.13 -c @"
import re, importlib.metadata as md
miss = []
for line in open(r'$Py\requirements.txt', encoding='utf-8'):
    s = line.split('#')[0].strip()
    if not s: continue
    name = re.split(r'[<>=!~\[; ]', s)[0]
    try: md.version(name)
    except md.PackageNotFoundError: miss.append(s)
print('\n'.join(miss))
"@
    $missing = @($missing | Where-Object { $_ })
    if (-not $missing) { Row 'Python packages (requirements.txt)' OK }
    elseif ($CheckOnly) { Row 'Python packages' MISSING ($missing -join ', ') }
    else {
        & py -3.13 -m pip install --user -r "$Py\requirements.txt"
        if ($LASTEXITCODE -eq 0) { Row 'Python packages' FIXED ($missing -join ', ') }
        else { Row 'Python packages' BROKEN 'pip install failed - see output above' }
    }
}

# ---- models and bundled binaries (not in git - restore from backup) ---------
$need = @{
    'stt\bin\Release\whisper-server.exe' = 'whisper.cpp (CPU)'
    'stt\ggml-large-v3-turbo-q8_0.bin'   = 'Whisper model'
    'tts\piper\piper.exe'                = 'Piper TTS'
    'tts\en_GB-alan-medium.onnx'         = 'Piper voice (Alan)'
}
foreach ($k in $need.Keys) {
    if (Test-Path (Join-Path $Root $k)) { Row $need[$k] OK $k }
    else { Row $need[$k] MISSING "$k - restore stt\ and tts\ from the Google backup"; $todo.Add("Restore $k from backup") }
}
if (Test-Path "$Root\stt\bin-cuda\whisper-server.exe") { Row 'whisper.cpp (CUDA)' OK } else { Row 'whisper.cpp (CUDA)' OPTIONAL 'GPU ear; CPU build is used without it' }

# ---- secrets (names only, never values) -------------------------------------
$sec = Join-Path $Root '.goat-secrets.json'
if (Test-Path $sec) {
    $j = Get-Content $sec -Raw | ConvertFrom-Json
    foreach ($key in 'gemini_api_key', 'elevenlabs_api_key', 'gladia_api_key', 'my_email', 'gmail_account') {
        $val = [string]$j.$key
        if ($val.Trim()) { Row "secret: $key" OK } else { Row "secret: $key" MISSING 'add it to .goat-secrets.json'; $todo.Add("Add $key to .goat-secrets.json") }
    }
} else {
    Row '.goat-secrets.json' MISSING 'restore from backup, or create: {"gemini_api_key":"...","elevenlabs_api_key":"...","gladia_api_key":"...","my_email":"...","gmail_account":"..."}'
    $todo.Add('Restore .goat-secrets.json')
}

# ---- personal data -------------------------------------------------------------
foreach ($f in 'workspace\memory.md', 'stt-fixes.json', 'voiceid\prints.npz', 'workspace\.claude\skills', '.phone\token.txt') {
    if (Test-Path (Join-Path $Root $f)) { Row "data: $f" OK } else { Row "data: $f" MISSING 'restore from the Google backup'; $todo.Add("Restore $f") }
}

# ---- Claude engine -------------------------------------------------------------
if (Get-Command claude -ErrorAction SilentlyContinue) {
    if (Test-Path "$env:USERPROFILE\.claude\.credentials.json") { Row 'Claude Code (engine) + login' OK }
    else { Row 'Claude login' MISSING 'run: claude  (and log in once)'; $todo.Add('Log in to Claude Code: run `claude` once') }
} else {
    Row 'Claude Code CLI' MISSING 'the SDK bundles its own engine; install the CLI for logins: irm https://claude.ai/install.ps1 | iex'
    $todo.Add('Install Claude Code and log in')
}

# ---- mic permission ------------------------------------------------------------
$mic = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged' -ErrorAction SilentlyContinue).Value
if ($mic -eq 'Allow') { Row 'Microphone access for desktop apps' OK }
else { Row 'Microphone access' MISSING 'Settings > Privacy > Microphone > "Let desktop apps access your microphone" ON'; $todo.Add('Turn on microphone access for desktop apps') }

# ---- firewall rule for the phone client (admin) ---------------------------------
$rule = Get-NetFirewallRule -DisplayName 'GOAT phone 8797' -ErrorAction SilentlyContinue
if ($rule) { Row 'Firewall: GOAT phone 8797' OK }
elseif ($CheckOnly -or -not $admin) {
    Row 'Firewall: GOAT phone 8797' MISSING 'needs admin: re-run this script as administrator'
    $todo.Add('Run setup.ps1 once as administrator (phone firewall rule)')
} else {
    New-NetFirewallRule -DisplayName 'GOAT phone 8797' -Direction Inbound -Protocol TCP -LocalPort 8797 -Action Allow -Profile Any | Out-Null
    Row 'Firewall: GOAT phone 8797' FIXED
}

# ---- autostart at login (Startup-folder shortcut, comes up asleep) ------------------
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'GOAT.lnk'
if (Test-Path $lnk) { Row 'Autostart at login' OK }
elseif ($CheckOnly) { Row 'Autostart at login' MISSING 're-run without -CheckOnly' }
else {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $s.Arguments = "`"$Py\start-goat-app.vbs`" /startup"
    $s.WorkingDirectory = $Py
    $s.IconLocation = "$Root\goat.ico"
    $s.Save()
    Row 'Autostart at login' FIXED $lnk
}

# ---- tools GOAT's brain uses -------------------------------------------------------
foreach ($t in @(
        @{ c = 'edge-tts'; why = 'main voice'; fix = 'comes with requirements.txt' },
        @{ c = 'git'; why = 'versioning, Claude commits'; fix = 'winget install Git.Git (admin)' },
        @{ c = 'node'; why = 'phone page tests only'; fix = 'winget install OpenJS.NodeJS.LTS' },
        @{ c = 'ollama'; why = 'optional local brain'; fix = 'winget install Ollama.Ollama; ollama pull qwen3:4b' },
        @{ c = 'ffmpeg'; why = 'optional media jobs'; fix = 'winget install Gyan.FFmpeg' })) {
    if (Get-Command $t.c -ErrorAction SilentlyContinue) { Row "tool: $($t.c)" OK $t.why }
    elseif ($t.c -in 'edge-tts', 'git') { Row "tool: $($t.c)" MISSING "$($t.why) - $($t.fix)"; $todo.Add("Install $($t.c): $($t.fix)") }
    else { Row "tool: $($t.c)" OPTIONAL "$($t.why) - $($t.fix)" }
}

# ---- health check --------------------------------------------------------------
if ($pyOk) {
    Write-Host "`n--- goat_doctor (only meaningful while GOAT is running) ---"
    & py -3.13 "$Py\goat_doctor.py"
}

Write-Host ''
if ($todo.Count) {
    Write-Host 'Needs you:' -ForegroundColor Yellow
    $todo | Select-Object -Unique | ForEach-Object { Write-Host "  - $_" }
} else {
    Write-Host 'All set. Start GOAT with GOAT.bat (or log out and back in for autostart).' -ForegroundColor Green
}
