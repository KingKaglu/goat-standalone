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

' Rotate the log before launch so the append redirect below can't grow it
' unbounded — one .old generation kept. Safe here: the app is not running
' (checked above), so nothing holds the file open.
Const LOG_MAX = 5242880  ' 5 MB
Set fso = CreateObject("Scripting.FileSystemObject")
logPath = APP_DIR & "\goat-app.log"
If fso.FileExists(logPath) Then
  If fso.GetFile(logPath).Size > LOG_MAX Then
    oldPath = logPath & ".old"
    If fso.FileExists(oldPath) Then fso.DeleteFile oldPath, True
    fso.MoveFile logPath, oldPath
  End If
End If

' python.exe (hidden console), NOT pythonw: under pythonw the Claude SDK
' can't spawn its CLI subprocess (WinError 50 duplicating std handles),
' which killed the engine thread at boot while the window stayed up.
' Window style 0 hides the console anyway.
sh.CurrentDirectory = APP_DIR
' py -3.13 pinned: Ada-SI's install (2026-07-14) put Python 3.12 first on
' PATH; bare "python" then lost PySide6/numpy and GOAT died at import.
sh.Run "cmd /c py -3.13 ui_qt.py >> goat-app.log 2>&1", 0, False
