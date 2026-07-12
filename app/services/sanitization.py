from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Zero-width and other invisible/formatting characters that have no legitimate
# reason to appear in roleplay text but are a known technique for smuggling
# hidden instructions or covert markers into content that looks clean on screen.
_ZERO_WIDTH_CHARS = (
    "​"  # zero-width space
    "‌"  # zero-width non-joiner
    "‍"  # zero-width joiner
    "⁠"  # word joiner
    "﻿"  # zero-width no-break space / BOM
    "᠎"  # mongolian vowel separator
)
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH_CHARS}]")

# Common prompt-injection role/instruction markers. Flag-only: legitimate roleplay
# content can resemble these (a character quoting a "system" in-fiction, etc.), so
# this is a signal for the admin/audit log, not an automatic block.
_INJECTION_PATTERNS = [
    re.compile(r"\[\s*/?\s*SYSTEM\s*\]", re.IGNORECASE),
    re.compile(r"<\|im_(start|end)\|>"),
    re.compile(r"<\|(system|user|assistant)\|>"),
    re.compile(r"###\s*Instruction\s*:", re.IGNORECASE),
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE),
]

_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


@dataclass
class SanitizeFinding:
    kind: str  # "zero_width" | "url_blocked" | "injection_pattern"
    detail: str = ""


@dataclass
class SanitizeResult:
    text: str
    findings: list[SanitizeFinding] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.findings)


def strip_control_chars(text: str) -> str:
    """Remove non-printable control characters, keeping newline/tab/CR."""
    if not text:
        return text
    return "".join(
        ch for ch in text
        if ch in ("\n", "\t", "\r") or unicodedata.category(ch) != "Cc"
    )


def detect_zero_width(text: str) -> bool:
    """Flag (don't strip) zero-width/invisible characters — see module docstring."""
    return bool(text) and bool(_ZERO_WIDTH_RE.search(text))


def scan_urls(text: str, blocklist: list[str] | None = None) -> list[str]:
    """Return URLs in text that match a domain on the blocklist."""
    if not text or not blocklist:
        return []
    blocked = []
    for url in _URL_RE.findall(text):
        for domain in blocklist:
            domain = domain.strip().lower()
            if domain and domain in url.lower():
                blocked.append(url)
                break
    return blocked


def flag_injection_patterns(text: str) -> list[str]:
    """Return human-readable descriptions of injection-marker patterns found."""
    if not text:
        return []
    found = []
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append(match.group(0))
    return found


def sanitize_input(text: str, blocklist: list[str] | None = None) -> SanitizeResult:
    """Sanitize text before database storage.

    Strips control characters (behavioral change, safe — these have no legitimate
    use in stored text). Zero-width chars, blocklisted URLs, and injection-pattern
    markers are flagged in the result but left in the text: none of these can be
    reliably distinguished from legitimate roleplay content by pattern matching
    alone, so the caller (a router write endpoint) logs the finding via
    app.services.audit.log_action rather than silently altering user content.
    """
    cleaned = strip_control_chars(text)
    findings: list[SanitizeFinding] = []

    if detect_zero_width(cleaned):
        findings.append(SanitizeFinding("zero_width", "Zero-width/invisible characters detected"))

    blocked_urls = scan_urls(cleaned, blocklist)
    for url in blocked_urls:
        findings.append(SanitizeFinding("url_blocked", url))

    injections = flag_injection_patterns(cleaned)
    for pattern in injections:
        findings.append(SanitizeFinding("injection_pattern", pattern))

    return SanitizeResult(text=cleaned, findings=findings)


def sanitize_for_injection(text: str, blocklist: list[str] | None = None) -> SanitizeResult:
    """Check DB-sourced content immediately before it's injected into an outbound
    LLM request. Never strips — by this point the content has already been through
    sanitize_input() at write time and stripping here would silently corrupt
    lorebook/memory/skill text a user is relying on. This is a second read-time
    check (content can be edited directly in the DB, imported in bulk bypassing
    per-field validation, or written by save_memories_for_chat from LLM output
    rather than a router) that only flags for the audit log.
    """
    findings: list[SanitizeFinding] = []

    if detect_zero_width(text):
        findings.append(SanitizeFinding("zero_width", "Zero-width/invisible characters detected"))

    blocked_urls = scan_urls(text, blocklist)
    for url in blocked_urls:
        findings.append(SanitizeFinding("url_blocked", url))

    injections = flag_injection_patterns(text)
    for pattern in injections:
        findings.append(SanitizeFinding("injection_pattern", pattern))

    return SanitizeResult(text=text, findings=findings)
