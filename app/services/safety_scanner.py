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

    if resource_type == "map":
        return scan_map(data)

    return ScanResult(safe=True)


@dataclass
class EmbeddedResource:
    """One object carried by a map export.

    A map is a collection, not a blob: each of these is scanned, deduplicated
    and installed in its own right. ``content`` is None for a *linked* resource,
    which names a source but ships no payload -- it is resolved from the repo at
    install time so the map always gets the current version.
    """
    resource_type: str
    name: str
    stage_name: str = ""
    stage_order: int = 0
    stage_index: int = 0
    index: int = 0
    position: str = "pre_driver"
    sticky: bool = False
    source_url: str = ""
    source_path: str = ""
    declared_hash: str = ""
    declared_version: str = ""
    content: dict | None = None

    @property
    def is_linked(self) -> bool:
        return self.content is None

    @property
    def label(self) -> str:
        stage = f"stage '{self.stage_name}' > " if self.stage_name else ""
        return f"{stage}{self.resource_type} '{self.name}'"


@dataclass
class ResourceScan:
    resource: EmbeddedResource
    result: ScanResult
    reused: bool = False


@dataclass
class MapScanReport:
    """Per-object scan results for a map, plus a rollup for the map itself."""
    resources: list[ResourceScan] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return all(r.result.safe for r in self.resources)

    @property
    def aggregate(self) -> ScanResult:
        """One ScanResult for the map, with each finding attributed to its object."""
        rolled = ScanResult(safe=True)
        for entry in self.resources:
            for finding in entry.result.findings:
                rolled.findings.append(ScanFinding(
                    severity=finding.severity,
                    pattern=finding.pattern,
                    description=f"[{entry.resource.label}] {finding.description}",
                    line=finding.line,
                ))
            if not entry.result.safe:
                rolled.safe = False
        return rolled

    def to_dict(self) -> dict:
        return {
            "resources": [
                {
                    "name": e.resource.name,
                    "type": e.resource.resource_type,
                    "stage": e.resource.stage_name,
                    "path": e.resource.source_path,
                    "linked": e.resource.is_linked,
                    "reused": e.reused,
                    "safe": e.result.safe,
                    "max_severity": e.result.max_severity,
                    "findings": [
                        {"severity": f.severity, "description": f.description, "line": f.line}
                        for f in e.result.findings
                    ],
                }
                for e in self.resources
            ],
            "safe": self.safe,
            "max_severity": self.aggregate.max_severity,
        }


def decompose_map(data: dict) -> list[EmbeddedResource]:
    """Break a map export into the individual objects it carries.

    Shared by the scanner and the installer so both see exactly the same set of
    objects -- if they disagreed, something could install unscanned.
    """
    out: list[EmbeddedResource] = []

    for stage_index, stage in enumerate(data.get("stages", []) or []):
        if not isinstance(stage, dict):
            continue
        stage_name = stage.get("name", "") or ""
        stage_order = stage.get("stage_order", 0) or 0

        for idx, res in enumerate(stage.get("resources", []) or []):
            if not isinstance(res, dict):
                continue

            res_type = res.get("resource_type", "") or ""
            if res_type not in ("cantrip", "lorebook", "skill", "sample"):
                continue

            content = res.get("resource_content")
            if content is not None and not isinstance(content, dict):
                content = None

            source = res.get("source") or {}
            if not isinstance(source, dict):
                source = {}

            name = ""
            if isinstance(content, dict):
                name = content.get("name") or ""
            name = name or res.get("resource_name") or "unnamed"

            out.append(EmbeddedResource(
                resource_type=res_type,
                name=name,
                stage_name=stage_name,
                stage_order=stage_order,
                stage_index=stage_index,
                index=idx,
                position=res.get("position", "pre_driver") or "pre_driver",
                sticky=bool(res.get("sticky")),
                source_url=(source.get("url") or "").strip(),
                source_path=(source.get("path") or "").strip(),
                declared_hash=(source.get("content_hash") or "").strip(),
                declared_version=(source.get("version") or "").strip(),
                content=content,
            ))

    return out


def scan_embedded_resource(resource: EmbeddedResource) -> ScanResult:
    """Scan one object from a map through the same path a direct install uses.

    A linked resource ships no payload, so there is nothing to scan here -- it
    is scanned after it is fetched, exactly like a file installed directly.
    """
    if resource.content is None:
        return ScanResult(safe=True)
    raw = json.dumps(resource.content)
    return scan_json_content(raw, resource.resource_type)


def scan_map_report(data: dict) -> MapScanReport:
    """Per-object scan of a map export."""
    return MapScanReport(resources=[
        ResourceScan(resource=r, result=scan_embedded_resource(r))
        for r in decompose_map(data)
    ])


def scan_map(data: dict) -> ScanResult:
    """Rollup of scan_map_report(), for callers that want one verdict.

    A map carries the *full source* of the cantrips it attaches, so installing
    one installs that JS. Without this, code that would be flagged on a direct
    cantrip install passed unexamined when it arrived inside a map.
    """
    return scan_map_report(data).aggregate


def scan_file(raw_content: str, resource_type: str) -> ScanResult:
    """Scan a raw file content for safety issues.

    Returns ScanResult with findings. Files with critical findings
    should not be installed without explicit user override.
    """
    return scan_json_content(raw_content, resource_type)
