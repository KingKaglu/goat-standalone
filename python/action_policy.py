"""Which shell commands from GOAT's working brain need Giorgi's yes first.

This is a second enforcement layer in front of the SDK's bypass-permissions
mode; the approval token is bound separately to the exact command.

His order 2026-11-01 (after the Windows reinstall): "Restore full laptop
access for the brain. Ask for confirmation ONLY for destructive actions:
delete, format, force push, uninstall, sending messages/emails, payments."
The earlier list also gated moves, copies, renames, file writes, redirects,
process kills, nested shells, plain git push, deploys and every POST request,
which stopped routine jobs for a yes (2026-10-26: "GOAT's Claude looked
stuck" waiting on approvals). Those now run freely.

Kept beyond his list, on purpose:
- discarding uncommitted git work (reset --hard, clean -f, restore) — it is
  a delete of his work;
- shutting down / restarting the PC — it kills GOAT and every open job;
- encoded or decode-and-run payloads — the gate can't see what they do, so
  it can't tell whether they delete anything.
Payments happen on web pages; screen_policy.py gates those clicks.
"""
from __future__ import annotations

import base64
import binascii
import re

DELETE = "the command deletes files, folders or data"
FORMAT = "the command formats or wipes a disk"
FORCE_PUSH = "the command force-pushes and can overwrite remote history"
UNINSTALL = "the command uninstalls software"
SEND = "the command sends a message or email"
POWER = "the command shuts down or restarts the computer"

_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # ---- delete ----
    (re.compile(r"(?i)(?<![-\w])(?:remove-item|ri|rm|del|erase|rmdir|rd)\b(?!-)"), DELETE),
    (re.compile(r"(?i)(?:\bos\.(?:remove|unlink|rmdir|removedirs)|\bshutil\.rmtree|"
                r"\.unlink(?:Sync)?|\.rm(?:Sync|dir(?:Sync)?)?|\.rmtree)\s*\("), DELETE),
    (re.compile(r"(?i)\b(?:git\s+reset\s+--hard|git\s+clean\s+(?:-[a-z]*f|--force)|"
                r"git\s+restore\b|git\s+checkout\s+(?:--|\.(?:\s|$)|-f\b)|"
                r"git\s+branch\s+-D\b|git\s+stash\s+(?:drop|clear)\b)"), DELETE),
    (re.compile(r"(?i)\b(?:drop\s+(?:database|table|schema)|delete\s+from|truncate\s+table)\b"),
     DELETE),
    (re.compile(r"(?i)\b(?:docker\s+(?:rm|rmi|volume\s+rm|system\s+prune)|"
                r"kubectl\s+delete|reg(?:\.exe)?\s+delete|"
                r"remove-itemproperty|net\s+user\s+\S+\s+/delete|"
                r"bcdedit\s+/delete|sc(?:\.exe)?\s+delete|vssadmin\s+delete|"
                r"wevtutil\s+cl|clear-recyclebin|gh\s+(?:repo|release)\s+delete)\b"),
     DELETE),
    # ---- format / wipe ----
    (re.compile(r"(?i)\b(?:format-volume|format-disk|clear-disk|clear-volume|"
                r"initialize-disk|diskpart|cipher(?:\.exe)?\s+/w|"
                r"format(?:\.com)?\s+[a-z]:)"), FORMAT),
    # ---- force push ----
    (re.compile(r"(?i)\bgit\s+push\b[^;\n|&]*(?:\s(?:-f|--force(?:-with-lease)?(?:=\S*)?)\b|"
                r"\s\+\S)"), FORCE_PUSH),
    # ---- uninstall ----
    (re.compile(r"(?i)\b(?:winget\s+(?:uninstall|remove)|choco\s+uninstall|scoop\s+uninstall|"
                r"(?:pip|pip3|pipx|uv\s+(?:pip|tool))\s+uninstall|"
                r"(?:py|python)(?:\d+(?:\.\d+)?)?(?:\s+-\d\.\d+)?\s+-m\s+pip\s+uninstall|"
                r"npm\s+(?:uninstall|remove|rm|un)\b|"
                r"uninstall-(?:package|module|windowsfeature)|remove-appxpackage|"
                r"disable-windowsoptionalfeature|msiexec(?:\.exe)?\s+/(?:x|uninstall)|"
                r"wmic\b[^\n]*\bcall\s+uninstall)"), UNINSTALL),
    # ---- send a message / email ----
    (re.compile(r"(?i)\b(?:send-mailmessage|send-mguser-mail|sendmail|smtplib|"
                r"smtp\.sendmail|\.send_message\s*\(|"
                r"gh\s+(?:issue|pr)\s+(?:create|comment)|gh\s+api\b[^\n]*\bcomments\b)"),
     SEND),
    # ---- power ----
    (re.compile(r"(?i)\b(?:stop-computer|restart-computer|shutdown(?:\.exe)?\s+[/-][srpf])"),
     POWER),
)

_POWERSHELL_ENC_SWITCH = re.compile(
    r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b"
    r"(?:(?![;&|\n]).)*?\s-(?:e|en|enc|encodedcommand)\b")
_POWERSHELL_ENC_VALUE = re.compile(
    r"(?i)\s-(?:e|en|enc|encodedcommand)\b\s*:?[ \t]*['\"]?"
    r"([A-Za-z0-9+/=_-]+)")
_DECODE_AND_EXECUTE = re.compile(
    r"(?i)(?:\bfrombase64string\b[^\n]*\b(?:iex|invoke-expression)\b|"
    r"\bcertutil(?:\.exe)?\b(?:(?![;\n]).){0,100}\s-decode\b|\bbase64\b"
    r"(?:(?![;\n]).){0,100}(?:\s-d\b|--decode\b)"
    r"(?:(?![;\n]).){0,100}\|\s*(?:bash|sh|pwsh|powershell|"
    r"python(?:\d+(?:\.\d+)?)?|node)\b)")


def _rules(command: str) -> str | None:
    for pattern, reason in _RULES:
        if pattern.search(command):
            return reason
    return None


def _decode_powershell(command: str) -> str | None:
    """An encoded PowerShell payload is judged by what it decodes to."""
    if not _POWERSHELL_ENC_SWITCH.search(command):
        return None
    match = _POWERSHELL_ENC_VALUE.search(command)
    if not match:
        return "the command contains an unreadable encoded PowerShell payload"
    try:
        raw = base64.b64decode(match.group(1), validate=True)
        decoded = raw.decode("utf-16-le").strip("\x00﻿ \t\r\n")
        if not decoded:
            raise ValueError("empty script")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "the command contains an unreadable encoded PowerShell payload"
    inner = _rules(decoded)
    return f"{inner} (inside an encoded PowerShell script)" if inner else ""


def shell_reason(command: str) -> str | None:
    """Why this command needs his yes first, or None to run it now."""
    command = str(command or "")
    encoded = _decode_powershell(command)
    if encoded:
        return encoded
    if _DECODE_AND_EXECUTE.search(command):
        return "the command decodes and executes opaque content"
    return _rules(command)
