# GOAT self-restart helper — spawned DETACHED (via WMI, so it survives the
# app's death) by GOAT's own brain when Giorgi orders a restart.
#
# Self-edit safety gate (2026-07-10): a code change must prove itself BEFORE
# the live app dies, and a boot crash after the swap rolls back automatically.
#   1. preflight  — self_check.py compiles + imports the code on disk. Fails?
#                   The running app is left alone; nothing is killed.
#   2. restart    — goodbye line gets 8s to be spoken, then kill + relaunch.
#   3. watchdog   — if the fresh instance dies within 45s, restore the
#                   last-good snapshot (pure PowerShell — the broken thing
#                   might be self_check itself) and relaunch that.
$py = "C:\Users\user\goat-standalone\python"
# Own file: goat-app.log is held open by the app's `>>` redirect, so every
# Add-Content to it failed and the watchdog's verdicts were never recorded.
$log = Join-Path $py "restart-goat.log"
function Log($m) { Add-Content -Path $log -Value "[restart] $(Get-Date -Format s) $m" }

function GoatAlive {
    (Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'ui_qt\.py' } | Measure-Object).Count -ge 1
}

function KillGoat {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'ui_qt\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # Stop-Process -Force only requests termination - it doesn't block until the
    # process is actually reaped. If the launcher's "already running?" WMI check
    # (start-goat-app.vbs) runs while the old process is still dying, it thinks
    # GOAT is up and quits without launching a new one, so wait for real death.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (GoatAlive)) {
        Start-Sleep -Milliseconds 300
    }
}

function LaunchGoat {
    Start-Process wscript.exe -ArgumentList '"C:\Users\user\goat-standalone\python\start-goat-app.vbs"'
}

# 1. PREFLIGHT — while the old instance is still alive and talking.
$pf = Start-Process py -ArgumentList '-3.13', 'self_check.py', 'preflight' `
        -WorkingDirectory $py -WindowStyle Hidden -Wait -PassThru
if ($pf.ExitCode -ne 0) {
    Log "PREFLIGHT FAILED - restart ABORTED, current app left running. Fix the code or run: python self_check.py rollback"
    exit 1
}
Log "preflight passed - restarting"

# 2. The brain's goodbye line needs time to be synthesized and spoken.
Start-Sleep -Seconds 8
KillGoat
# Let the mic/audio handles fully release before the fresh instance grabs them.
Start-Sleep -Seconds 2
# Daily self-update: a pending engine update installs HERE, the only moment
# the bundled claude.exe isn't locked by a running GOAT.
if (Test-Path (Join-Path $py ".update-pending.json")) {
    $up = Start-Process py -ArgumentList '-3.13', 'self_update.py', 'install' `
            -WorkingDirectory $py -WindowStyle Hidden -Wait -PassThru
    Log "self-update install exit $($up.ExitCode)"
}
$engineUp = Join-Path $py ".engine-up"
Remove-Item $engineUp -Force -ErrorAction SilentlyContinue
$launchedAt = Get-Date
LaunchGoat

# 3. WATCHDOG — preflight can't catch everything (a bug that only fires on
# real boot: audio devices, SDK connect, Qt event loop). The fresh instance
# must prove its ENGINE connected (goat_app writes .engine-up), not just
# that a window exists: on 2026-09-27 an engine update died on connect, the
# window stayed up, this check said "restart OK", and GOAT was deaf.
function EngineUp {
    (Test-Path $engineUp) -and ((Get-Item $engineUp).LastWriteTime -gt $launchedAt)
}
$deadline = $launchedAt.AddSeconds(120)
$alive = $true
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $alive = GoatAlive
    if (-not $alive -or (EngineUp)) { break }
}
$ok = $alive -and (EngineUp)
if (-not $ok) {
    if ($alive) { Log "ENGINE NEVER CAME UP after restart (window alive, brain dead) - rolling back" }
    else { Log "BOOT CRASH after restart - rolling back to last-good snapshot" }
    $backup = Join-Path $py ".self-backup\last-good"
    if (Test-Path $backup) {
        KillGoat
        Copy-Item (Join-Path $backup '*.py') $py -Force
        # A just-installed engine may be the thing that broke boot.
        Start-Process py -ArgumentList '-3.13', 'self_update.py', 'revert' `
            -WorkingDirectory $py -WindowStyle Hidden -Wait | Out-Null
        Start-Sleep -Seconds 2
        LaunchGoat
        Log "rolled back and relaunched"
    } else {
        Log "no last-good snapshot exists - manual fix needed"
    }
} else {
    Log "restart OK - new instance is up"
}
