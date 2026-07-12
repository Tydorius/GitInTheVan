from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CRITICAL_PATTERNS = [
    (r"\bfetch\s*\(", "Network access: fetch()"),
    (r"\bXMLHttpRequest\b", "Network access: XMLHttpRequest"),
    (r"\bWebSocket\b", "Network access: WebSocket"),
    (r"\bDeno\.network\b", "Network access: Deno.network"),
    (r"\bDeno\.connect\b", "Network access: Deno.connect"),
    (r"\bDeno\.readFile\b", "Filesystem access: Deno.readFile"),
    (r"\bDeno\.writeFile\b", "Filesystem access: Deno.writeFile"),
    (r"\bDeno\.open\b", "Filesystem access: Deno.open"),
    (r"\bDeno\.remove\b", "Filesystem access: Deno.remove"),
    (r"\bDeno\.mkdir\b", "Filesystem access: Deno.mkdir"),
    (r"\bDeno\.run\b", "Process execution: Deno.run"),
    (r"\bDeno\.Command\b", "Process execution: Deno.Command"),
    (r"\bimport\s*\(", "Dynamic import"),
    (r"\bnew\s+Worker\s*\(", "Sandbox-escape attempt: new Worker()"),
    (r"\bnavigator\.serviceWorker\b", "Sandbox-escape attempt: serviceWorker"),
]

WARNING_PATTERNS = [
    (r"\beval\s*\(", "Dynamic code execution: eval()"),
    (r"\bFunction\s*\(", "Dynamic code execution: Function()"),
    (r"https?://[^\s\"')]+", "External URL reference"),
    (r"\bwhile\s*\(\s*true\s*\)", "Potential infinite loop: while(true)"),
    (r"\bfor\s*\(\s*;\s*;\s*\)", "Potential infinite loop: for(;;)"),
    (r"\batob\s*\(", "Base64 decoding: atob() — verify decoded content isn't obfuscated logic"),
    (r"(?:\\x[0-9a-fA-F]{2}){6,}", "Possible obfuscated payload: chained \\x hex escapes"),
    (r"(?:\\u[0-9a-fA-F]{4}){6,}", "Possible obfuscated payload: chained \\u unicode escapes"),
    (r"(?:String\.fromCharCode\s*\([^)]*\)\s*[+,]\s*){2,}String\.fromCharCode",
     "Possible obfuscated payload: chained String.fromCharCode()"),
    (r"[A-Za-z0-9+/]{80,}={0,2}(?!\w)", "Possible encoded/obfuscated payload: long base64-like blob"),
    (r"__proto__|\.prototype\s*\[|constructor\s*\.\s*prototype", "Possible prototype pollution pattern"),
]

MAX_CONTENT_SIZE = 50000


@dataclass
class ScanFinding:
    severity: str
    pattern: str
    description: str
    line: int = 0


@dataclass
class ScanResult:
    safe: bool
    findings: list[ScanFinding] = field(default_factory=list)

    @property
    def max_severity(self) -> str:
        if not self.findings:
            return "clean"
        if any(f.severity == "critical" for f in self.findings):
            return "critical"
        if any(f.severity == "warning" for f in self.findings):
            return "warning"
        return "info"

    @property
    def summary(self) -> str:
        if not self.findings:
            return "No issues detected"
        critical = [f for f in self.findings if f.severity == "critical"]
        warning = [f for f in self.findings if f.severity == "warning"]
        parts = []
        if critical:
            parts.append(f"{len(critical)} critical issue(s)")
        if warning:
            parts.append(f"{len(warning)} warning(s)")
        return ", ".join(parts)


def _find_line(text: str, pattern: str, start: int = 0) -> int:
    match = re.search(pattern, text[start:], re.IGNORECASE)
    if match:
        return text[:start + match.start()].count("\n") + 1
    return 0


def scan_cantrip(code: str) -> ScanResult:
    result = ScanResult(safe=True)

    if len(code) > MAX_CONTENT_SIZE:
        result.findings.append(ScanFinding(
            severity="warning",
            pattern="size",
            description=f"Large content: {len(code)} chars (max {MAX_CONTENT_SIZE})",
        ))

    for pattern, description in CRITICAL_PATTERNS:
        matches = re.finditer(pattern, code, re.IGNORECASE)
        for match in matches:
            line = code[:match.start()].count("\n") + 1
            result.findings.append(ScanFinding(
                severity="critical",
                pattern=pattern,
                description=description,
                line=line,
            ))

    for pattern, description in WARNING_PATTERNS:
        matches = re.finditer(pattern, code, re.IGNORECASE)
        for match in matches:
            line = code[:match.start()].count("\n") + 1
            result.findings.append(ScanFinding(
                severity="warning",
                pattern=pattern,
                description=description,
                line=line,
            ))

    result.safe = not any(f.severity == "critical" for f in result.findings)
    return result


def scan_lorebook(entries: list[dict]) -> ScanResult:
    result = ScanResult(safe=True)

    for i, entry in enumerate(entries):
        content = entry.get("content", "")
        if len(content) > MAX_CONTENT_SIZE:
            result.findings.append(ScanFinding(
                severity="warning",
                pattern="size",
                description=f"Entry '{entry.get('name', f'entry-{i}')}' is very large: {len(content)} chars",
            ))

        script_patterns = re.findall(r"<script", content, re.IGNORECASE)
        if script_patterns:
            result.findings.append(ScanFinding(
                severity="critical",
                pattern="script_tag",
                description=f"Entry '{entry.get('name', f'entry-{i}')}' contains <script> tags",
            ))

    result.safe = not any(f.severity == "critical" for f in result.findings)
    return result


def scan_json_content(raw_json: str, resource_type: str) -> ScanResult:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return ScanResult(
            safe=False,
            findings=[ScanFinding(
                severity="critical",
                pattern="json",
                description=f"Invalid JSON: {e}",
            )],
        )

    if resource_type == "cantrip":
        code = data.get("code", "")
        base = scan_cantrip(code)
        return base

    if resource_type == "lorebook":
        entries = data.get("entries", [])
        if isinstance(entries, dict):
            entries = list(entries.values())
        return scan_lorebook(entries)

    if resource_type == "rule":
        prompt = data.get("prompt", "")
        if len(prompt) > MAX_CONTENT_SIZE:
            return ScanResult(
                safe=True,
                findings=[ScanFinding(
                    severity="warning",
                    pattern="size",
                    description=f"Large prompt: {len(prompt)} chars",
                )],
            )
        return ScanResult(safe=True)

    return ScanResult(safe=True)


def scan_file(raw_content: str, resource_type: str) -> ScanResult:
    """Scan a raw file content for safety issues.

    Returns ScanResult with findings. Files with critical findings
    should not be installed without explicit user override.
    """
    return scan_json_content(raw_content, resource_type)
