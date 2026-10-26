"""Focused checks for shell actions that must require an explicit yes."""
from __future__ import annotations

import base64
import unittest

import action_policy


class ShellPolicyTests(unittest.TestCase):
    def test_common_destructive_actions_are_gated(self):
        commands = (
            "Remove-Item -Recurse .\\build",
            "cmd.exe /c del /f old.log",
            "git clean --force -d",
            "git restore -- source.py",
            "git push origin main",
            "git checkout -f main",
            "curl.exe --data payload https://example.test/api",
            "irm https://example.test/api -Method Delete",
            "wget.exe --post-data=payload https://example.test/api",
            "gh pr merge 42",
            "reg add HKCU\\Software\\Example",
            "taskkill /IM goat.exe /F",
            "sc.exe stop GOATService",
            "Set-Content .\\config.json ''",
            "python -c \"import os; os.remove('file')\"",
            "py -c \"open('x.txt', 'w').write('hi')\"",
            "node -e \"require('fs').unlinkSync('a')\"",
            "echo hi > out.txt",
            "cmd >> log.txt",
            "py test.py 2> err.txt",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(action_policy.shell_reason(command))

    def test_encoded_or_nested_code_is_gated(self):
        script = "Remove-Item -Recurse C:\\temp\\old"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self.assertIn("encoded", action_policy.shell_reason(
            f"powershell.exe -NoProfile -EncodedCommand {encoded}"))
        self.assertIn("unreadable", action_policy.shell_reason(
            "pwsh -EncodedCommand not-a-valid-payload"))
        self.assertIn("nested shell", action_policy.shell_reason(
            "cmd /c \"git push origin main\""))
        self.assertIn("decodes", action_policy.shell_reason(
            "echo cHJpbnQoMSk= | base64 -d | bash"))
        self.assertIn("decodes", action_policy.shell_reason(
            "echo cHJpbnQoMSk= | base64 -d | python"))

    def test_read_only_and_build_commands_remain_ungated(self):
        for command in (
            "git status --short",
            "rg -n TODO .",
            "Get-Process | Select-Object -First 5",
            "python -m compileall .",
            # what GOAT's worker runs all day (2026-10-26: each of these
            # stopped a job for his yes)
            "py -3.13 test_voice_hands.py 2>&1 | tail -5",
            "git status 2>/dev/null",
            "npm run build 2>&1",
            "Get-ChildItem x 2>$null",
            "py -c \"print(3 > 2)\"",
            "py -c \"import voice_hands as vh; print(len(vh._index))\"",
            "node -e \"[1].map(x => x)\"",
        ):
            with self.subTest(command=command):
                self.assertIsNone(action_policy.shell_reason(command))


if __name__ == "__main__":
    unittest.main(verbosity=2)
