' GOAT launcher — double-click to bring GOAT up.
' GOAT is a native Python/Qt desktop app now (no Node server, no browser).
' The real launcher lives in python\start-goat-app.vbs: it checks for a
' running instance (focuses it instead of double-launching), rotates the
' log, and starts ui_qt.py silently. This shim just forwards to it so old
' shortcuts keep working.
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run _
  "wscript.exe """ & appDir & "\python\start-goat-app.vbs""", 0, False
