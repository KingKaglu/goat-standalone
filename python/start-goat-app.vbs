' GOAT desktop app launcher — double-click to bring GOAT up.
' Native Qt window, no browser. Silent: no console; errors land in
' python\goat-app.log. If GOAT is already running, just focus it.

Const APP_DIR = "C:\Users\user\goat-standalone\python"

Set sh = CreateObject("WScript.Shell")
Set wmi = GetObject("winmgmts:\\.\root\cimv2")

Set running = wmi.ExecQuery( _
  "SELECT * FROM Win32_Process WHERE (Name='python.exe' OR Name='pythonw.exe') " & _
  "AND CommandLine LIKE '%ui_qt.py%'")
If running.Count > 0 Then
  ' Only trust the running instance if it actually has a focusable window.
  ' A hung/headless leftover (engine died, window never came up) used to
  ' make this launcher quit silently — double-click did nothing.
  If sh.AppActivate("GOAT") Then WScript.Quit
  For Each p In running
    On Error Resume Next
    p.Terminate
    On Error Goto 0
  Next
  WScript.Sleep 500
End If

' Rotate the log on EVERY launch — one .old generation kept. It used to
' rotate only past 5 MB, so goat-app.log held months of runs and
' goat_doctor.py's "last 80 lines" check kept reporting errors from a dead
' session (2026-09-14: a July Gemini 400 failed the doctor on a healthy
' boot). One file per run means the doctor reads THIS run or nothing.
'
' 2026-09-27 crash: "the app is not running" was NOT enough. A self-restart
' killed python.exe but its engine (the SDK's bundled claude.exe) lived on as
' an orphan, still holding goat-app.log through the inherited `>>` handle.
' MoveFile threw "Permission denied", this script died before sh.Run, and
' GOAT never came back. So: first clear any orphaned GOAT engine (no GOAT is
' alive at this point, so any engine carrying GOAT's persona is an orphan),
' and never let rotation be fatal — if the file is still held, this run
' logs to a fresh file. The launch below must ALWAYS be reached.
Set orphans = wmi.ExecQuery( _
  "SELECT * FROM Win32_Process WHERE Name='claude.exe' " & _
  "AND CommandLine LIKE '%You are GOAT%'")
For Each p In orphans
  On Error Resume Next
  p.Terminate
  On Error Goto 0
Next
If orphans.Count > 0 Then WScript.Sleep 500

Set fso = CreateObject("Scripting.FileSystemObject")
logName = "goat-app.log"
logPath = APP_DIR & "\" & logName
On Error Resume Next
If fso.FileExists(logPath) Then
  oldPath = logPath & ".old"
  If fso.FileExists(oldPath) Then fso.DeleteFile oldPath, True
  Err.Clear
  fso.MoveFile logPath, oldPath
  If Err.Number <> 0 Then
    Err.Clear
    ' cmd's `>>` can't open a held file either, so this run gets its own.
    logName = "goat-app." & Year(Now) & Right("0" & Month(Now), 2) & _
      Right("0" & Day(Now), 2) & "-" & Right("0" & Hour(Now), 2) & _
      Right("0" & Minute(Now), 2) & Right("0" & Second(Now), 2) & ".log"
    Set lg = fso.OpenTextFile(APP_DIR & "\restart-goat.log", 8, True)
    lg.WriteLine "[launcher] " & Now & " goat-app.log is held by another " & _
      "process - logging this run to " & logName & ", launching anyway"
    lg.Close
  End If
End If
Err.Clear
On Error Goto 0

' python.exe (hidden console), NOT pythonw: under pythonw the Claude SDK
' can't spawn its CLI subprocess (WinError 50 duplicating std handles),
' which killed the engine thread at boot while the window stayed up.
' Window style 0 hides the console anyway.
sh.CurrentDirectory = APP_DIR
' py -3.13 pinned: Ada-SI's install (2026-07-14) put Python 3.12 first on
' PATH; bare "python" then lost PySide6/numpy and GOAT died at import.
' /startup (the Windows Startup-folder shortcut) boots GOAT asleep as the dot.
extra = ""
If WScript.Arguments.Count > 0 Then
  If LCase(WScript.Arguments(0)) = "/startup" Then extra = " --startup"
End If
sh.Run "cmd /c py -3.13 -u ui_qt.py" & extra & " >> " & logName & " 2>&1", 0, False
