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
$log = Join-Path $py "goat-app.log"
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
LaunchGoat

# 3. WATCHDOG — preflight can't catch everything (a bug that only fires on
# real boot: audio devices, SDK connect, Qt event loop). Give the fresh
# instance 45s; if it's gone, put the last provably-booting code back.
Start-Sleep -Seconds 10
$alive = GoatAlive
if ($alive) {
    Start-Sleep -Seconds 35
    $alive = GoatAlive
}
if (-not $alive) {
    Log "BOOT CRASH after restart - rolling back to last-good snapshot"
    $backup = Join-Path $py ".self-backup\last-good"
    if (Test-Path $backup) {
        Copy-Item (Join-Path $backup '*.py') $py -Force
        Start-Sleep -Seconds 2
        LaunchGoat
        Log "rolled back and relaunched"
    } else {
        Log "no last-good snapshot exists - manual fix needed"
    }
} else {
    Log "restart OK - new instance is up"
}
