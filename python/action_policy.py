"""Conservative checks for high-impact shell commands used by GOAT.

This is a second enforcement layer in front of the SDK's bypass-permissions
mode. It catches common destructive, publishing, and send operations; the
approval token is bound separately to the exact command before it can run.
"""
from __future__ import annotations

import base64
import binascii
import re


_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)\b(?:remove-item|ri|rm|del|erase|rmdir|rd)\b"),
     "the command removes files or folders"),
    (re.compile(r"(?i)\b(?:move-item|mv|rename-item|ren|copy-item|cp)\b"),
     "the command moves, renames, or copies files"),
    (re.compile(r"(?i)\b(?:clear-content|set-content|out-file|"
                r"format-volume|format-disk|clear-disk|clear-volume|"
                r"diskpart|stop-computer|restart-computer|shutdown(?:\.exe)?)\b"),
     "the command changes or shuts down the system"),
    (re.compile(r"(?i)\breg(?:\.exe)?\s+(?:add|delete)\b"),
     "the command changes registry data"),
    (re.compile(r"(?i)\b(?:git\s+reset\s+--hard|git\s+clean\s+(?:-[a-z]*f|--force)|"
                r"git\s+restore\b|git\s+checkout\s+(?:--|\.|-f\b))"),
     "the command discards local changes"),
    (re.compile(r"(?i)\bgit\s+push\b"),
     "the command publishes code to a remote repository"),
    (re.compile(r"(?i)\b(?:npm\s+publish|twine\s+upload|docker\s+push|"
                r"gh\s+(?:pr|release|gist)\s+create|gh\s+pr\s+merge|"
                r"gh\s+issue\s+create|gh\s+release\s+upload|"
                r"gh\s+(?:repo|release)\s+delete|npm\s+unpublish|"
                r"vercel\s+(?:deploy|--prod)|wrangler\s+deploy|"
                r"firebase\s+deploy|fly\s+deploy|terraform\s+apply|"
                r"kubectl\s+apply|helm\s+upgrade)\b"),
     "the command publishes something externally"),
    (re.compile(r"(?i)\b(?:send-mailmessage|send-mguser-mail|sendmail|smtp\.sendmail)\b"),
     "the command sends a message"),
    (re.compile(r"(?i)\b(?:invoke-webrequest|invoke-restmethod|iwr|irm|wget)\b"
                r"(?:(?![;\n]).){0,240}\s+(?:-method\s+(?:post|put|patch|delete)|"
                r"-custommethod\s+(?:post|put|patch|delete))\b"),
     "the command sends a state-changing web request"),
    (re.compile(r"(?i)\b(?:curl|curl\.exe)\b(?:(?![;\n]).){0,240}"
                r"(?:-x|--request)\s*(?:post|put|patch|delete)\b"),
     "the command sends a state-changing web request"),
    (re.compile(r"(?i)\b(?:requests|httpx)\.(?:post|put|patch|delete)\s*\("),
     "the command sends a state-changing web request"),
    (re.compile(r"(?i)\b(?:curl|curl\.exe)\b(?:(?![;\n]).{0,240})"
                r"(?:--data(?:-binary|-raw|-urlencode)?\b|--form\b|\s-[dF]\b)"),
     "the command sends a state-changing web request"),
    (re.compile(r"(?i)\b(?:wget|wget\.exe)\b(?:(?![;\n]).{0,240})"
                r"(?:--post-data\b|--body-data\b|--method\s*=\s*(?:post|put|patch|delete)\b)"),
     "the command sends a state-changing web request"),
    (re.compile(r"(?i)\b(?:os\.(?:remove|unlink)|shutil\.rmtree|"
                r"Path\([^\n)]*\)\.(?:unlink|rmdir))\s*\("),
     "the command removes files or folders"),
    (re.compile(r"(?i)\b(?:drop\s+(?:database|table)|delete\s+from)\b"),
     "the command deletes database data"),
    (re.compile(r"(?i)\b(?:docker\s+rm|kubectl\s+delete)\b"),
     "the command deletes containers or cluster resources"),
    (re.compile(r"(?i)\b(?:sc(?:\.exe)?\s+(?:delete|stop|config)|"
                r"bcdedit\s+/delete|net\s+user\s+\S+\s+/delete|"
                r"remove-appxpackage|stop-process|taskkill(?:\.exe)?|"
                r"(?:pkill|kill)\s+-[a-z]*\b)"),
     "the command stops a process or changes system/account configuration"),
)

_POWERSHELL_ENC_SWITCH = re.compile(
    r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b"
    r"(?:(?![;&|\n]).)*?\s-(?:e|en|enc|encodedcommand)\b")
_POWERSHELL_ENC_VALUE = re.compile(
    r"(?i)\s-(?:e|en|enc|encodedcommand)\b\s*:?[ \t]*['\"]?"
    r"([A-Za-z0-9+/=_-]+)")
_INLINE_CODE = re.compile(
    r"(?i)\b(?:python(?:\d+(?:\.\d+)?)?|py|node|deno|ruby|perl)\b"
    r"(?:(?![;\n]).){0,120}(?:\s-c\s|\s--eval\s|\s-e\s)")
# What inline code would have to contain to change anything: delete, write,
# run a process, or talk to the network. Read-only one-liners ("print the
# index size", "time this search") are most of what the worker runs — gating
# every one of them stopped its jobs for a yes (2026-10-26: GOAT's Claude
# "looked stuck" waiting on approvals for test runs).
_INLINE_RISK = re.compile(
    r"(?i)(?:\bos\.(?:remove|unlink|rmdir|removedirs|system|popen|rename|replace|"
    r"chmod|kill)\b|\bshutil\.|\bsubprocess\b|\brmtree\b|\.unlink\w*\s*\(|"
    r"\b(?:rm|rmSync|writeFile\w*|appendFile\w*|rename\w*|copyFile\w*)\s*\(|"
    r"\brequire\s*\(\s*['\"](?:fs|fs/promises|child_process|net|http|https|dgram)['\"]|"
    r"\.rmdir\s*\(|\bopen\s*\([^)]*['\"][^'\"]*[wax+][^'\"]*['\"]|"
    r"\bwrite_(?:text|bytes)\b|\.write\s*\(|\b(?:requests|httpx|urllib|"
    r"socket|smtplib|ftplib|aiohttp|websockets?)\b|\b(?:exec|eval|compile|"
    r"__import__)\s*\(|\bctypes\b|\bwinreg\b|\bsqlite3\b|\bfs\.|"
    r"\bchild_process\b|\bexecSync\b|\bspawn\b|\bfetch\s*\(|\bprocess\.exit\b|"
    r"\bFile\.(?:write|delete)|\bsystem\s*\(|`)")
# A redirect that writes a file. Not "2>&1" (joins two streams), not a
# discard into /dev/null, $null or NUL, not "=>" / "->" in code.
_REDIRECT = re.compile(
    r"(?<![<>=\-])\d?>>?(?!&)\s*"
    r"(?!(?:/dev/null|\$null|nul)(?=$|[\s;|&)]))[^\s;|&)]")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_DYNAMIC_EXEC = re.compile(
    r"(?i)\b(?:invoke-expression|iex|eval|exec)\s*(?:\(|\$|\{|\S)")
_NESTED_SHELL = re.compile(
    r"(?i)\b(?:bash|sh|cmd(?:\.exe)?|powershell|pwsh)(?:\.exe)?\b"
    r"\s+(?:-c\b|-command\b|/c\b|/k\b)")
_DECODE_AND_EXECUTE = re.compile(
    r"(?i)(?:\bfrombase64string\b|\bcertutil(?:\.exe)?\b"
    r"(?:(?![;\n]).){0,100}\s-decode\b|\bbase64\b"
    r"(?:(?![;\n]).){0,100}(?:\s-d\b|--decode\b)"
    r"(?:(?![;\n]).){0,100}\|\s*(?:bash|sh|pwsh|powershell|"
    r"python(?:\d+(?:\.\d+)?)?|node)\b)")


def _decode_powershell(command: str) -> str | None:
    if not _POWERSHELL_ENC_SWITCH.search(command):
        return None
    match = _POWERSHELL_ENC_VALUE.search(command)
    if not match:
        return "the command contains an unreadable encoded PowerShell payload"
    try:
        raw = base64.b64decode(match.group(1), validate=True)
        decoded = raw.decode("utf-16-le").strip("\x00\ufeff \t\r\n")
        if not decoded:
            raise ValueError("empty script")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "the command contains an unreadable encoded PowerShell payload"
    return "the command runs an encoded PowerShell script"


def shell_reason(command: str) -> str | None:
    """Return a confirmation reason for high-impact or opaque shell actions."""
    command = str(command or "")
    encoded = _decode_powershell(command)
    if encoded:
        return encoded
    if _INLINE_CODE.search(command) and _INLINE_RISK.search(command):
        return "the command runs inline code that can delete, write, run or send things"
    if _DYNAMIC_EXEC.search(command):
        return "the command evaluates dynamically constructed code"
    if _NESTED_SHELL.search(command):
        return "the command launches a nested shell script"
    if _DECODE_AND_EXECUTE.search(command):
        return "the command decodes and executes opaque content"
    for pattern, reason in _RULES:
        if pattern.search(command):
            return reason
    if _REDIRECT.search(_QUOTED.sub("''", command)):
        return "the command overwrites a file through shell redirection"
    return None
