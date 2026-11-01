"""Which shell actions need an explicit yes (his order 2026-11-01: only
delete, format, force push, uninstall, sending messages/emails, payments)."""
from __future__ import annotations

import base64
import unittest

import action_policy


def _enc(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class ShellPolicyTests(unittest.TestCase):
    def test_destructive_actions_are_gated(self):
        for command in (
            # delete
            "Remove-Item -Recurse .\\build",
            "cmd.exe /c del /f old.log",
            "rm -rf C:\\Users\\giorg\\test",
            "rmdir /s /q test",
            "python -c \"import os; os.remove('file')\"",
            "py -c \"import shutil; shutil.rmtree('x')\"",
            "node -e \"require('fs').unlinkSync('a')\"",
            "git clean --force -d",
            "git reset --hard HEAD~1",
            "git restore -- source.py",
            "git checkout -f main",
            "sqlite3 db.sqlite \"DROP TABLE users\"",
            "reg delete HKCU\\Software\\Example /f",
            # format
            "Format-Volume -DriveLetter E",
            "format E: /q",
            "diskpart /s wipe.txt",
            # force push
            "git push --force origin main",
            "git push -f",
            "git push origin +main",
            "git push --force-with-lease",
            # uninstall
            "winget uninstall Spotify",
            "pip uninstall numpy -y",
            "py -3.13 -m pip uninstall numpy",
            "npm uninstall -g typescript",
            "Get-AppxPackage *xbox* | Remove-AppxPackage",
            "msiexec /x {GUID}",
            # send
            "Send-MailMessage -To a@b.c -Subject hi",
            "python -c \"import smtplib\"",
            "gh issue create --title bug",
            # power
            "shutdown /s /t 0",
            "Restart-Computer -Force",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(action_policy.shell_reason(command))

    def test_encoded_payloads_are_judged_by_content(self):
        self.assertIn("encoded", action_policy.shell_reason(
            f"powershell.exe -NoProfile -EncodedCommand {_enc('Remove-Item -Recurse C:/temp/old')}"))
        self.assertIn("unreadable", action_policy.shell_reason(
            "pwsh -EncodedCommand not-a-valid-payload"))
        self.assertIsNone(action_policy.shell_reason(
            f"powershell -EncodedCommand {_enc('Get-Process | Select -First 3')}"))
        self.assertIn("decodes", action_policy.shell_reason(
            "echo cHJpbnQoMSk= | base64 -d | bash"))
        # a nested shell is judged by what it runs, not gated for being nested
        self.assertIsNotNone(action_policy.shell_reason("cmd /c \"git push --force\""))
        self.assertIsNone(action_policy.shell_reason("cmd /c \"git push origin main\""))

    def test_routine_work_runs_without_asking(self):
        for command in (
            # read-only / build (always free)
            "git status --short",
            "rg -n TODO .",
            "Get-Process | Select-Object -First 5",
            "python -m compileall .",
            "py -3.13 test_voice_hands.py 2>&1 | tail -5",
            "npm run build 2>&1",
            "py -c \"print(3 > 2)\"",
            # gated before 2026-11-01, free now
            "Move-Item a.txt b\\a.txt",
            "Copy-Item -Recurse src dst",
            "Rename-Item old.txt new.txt",
            "mv a b",
            "Set-Content .\\config.json '{}'",
            "echo hi > out.txt",
            "Get-ChildItem | Out-File list.txt",
            "py -c \"open('x.txt', 'w').write('hi')\"",
            "python -c \"import subprocess; subprocess.run(['git','status'])\"",
            "git push origin main",
            "git commit -m \"fix\"",
            "taskkill /IM notepad.exe",
            "Stop-Process -Name notepad",
            "reg add HKCU\\Software\\Example /v x /d 1",
            "curl.exe --data payload http://localhost:8797/api",
            "Invoke-RestMethod http://localhost:3781/x -Method Post",
            "powershell -NoProfile -Command \"Get-ChildItem\"",
            "winget install Git.Git",
            "pip install requests",
            "docker run --rm hello-world",
            "Get-ChildItem C:\\ -Recurse | Sort-Object Length -Descending",
        ):
            with self.subTest(command=command):
                self.assertIsNone(action_policy.shell_reason(command))


if __name__ == "__main__":
    unittest.main(verbosity=2)
