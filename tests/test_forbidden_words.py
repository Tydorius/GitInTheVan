import pytest

from app.services.admin import update_admin_settings


@pytest.fixture
async def auth_client(client):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert setup_resp.status_code == 201
    token = setup_resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.headers.pop("Authorization", None)


@pytest.mark.asyncio
class TestForbiddenWordCRUD:
    async def test_create_word(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "badword"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["phrase"] == "badword"
        assert data["is_regex"] is False

    async def test_create_regex_word(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": r"\bbad\w*\b", "is_regex": True})
        assert resp.status_code == 201
        assert resp.json()["is_regex"] is True

    async def test_create_empty_phrase_rejected(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "   "})
        assert resp.status_code == 400

    async def test_list_words(self, auth_client):
        await auth_client.post("/api/forbidden-words", json={"phrase": "one"})
        await auth_client.post("/api/forbidden-words", json={"phrase": "two"})
        resp = await auth_client.get("/api/forbidden-words")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 2

    async def test_delete_word(self, auth_client):
        create_resp = await auth_client.post("/api/forbidden-words", json={"phrase": "deleteme"})
        word_id = create_resp.json()["id"]
        resp = await auth_client.delete(f"/api/forbidden-words/{word_id}")
        assert resp.status_code == 204

    async def test_create_strips_control_chars(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "bad\x00word"})
        assert resp.status_code == 201
        assert resp.json()["phrase"] == "badword"

    async def test_create_exceeds_size_limit(self, auth_client):
        try:
            await update_admin_settings({"max_rule_size_kb": 1})
            oversized = "x" * 2000
            resp = await auth_client.post("/api/forbidden-words", json={"phrase": oversized})
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_rule_size_kb": 25})


class TestForbiddenWordScanning:
    """Cover _scan itself.

    Everything above tests CRUD -- creating, listing and deleting words. None of
    it ever asserts that scanning *matches* anything, so the entire detection
    path (case folding, regex handling, position/count reporting) was uncovered.
    Mutation testing confirmed it: disabling case-insensitive matching and
    ignoring regex words both left the suite green.
    """

    @staticmethod
    def _word(phrase: str, *, is_regex: bool = False):
        from app.models.forbidden_word import ForbiddenWord

        return ForbiddenWord(user_id="u", phrase=phrase, is_regex=is_regex)

    def test_plain_phrase_matches(self):
        from app.services.forbidden_words import _scan

        result = _scan("this contains badword here", [self._word("badword")], False)
        assert result.has_matches
        assert result.matches[0].phrase == "badword"
        assert result.matches[0].count == 1
        assert result.matches[0].positions == [14]

    def test_no_match_reports_nothing(self):
        from app.services.forbidden_words import _scan

        result = _scan("perfectly clean text", [self._word("badword")], False)
        assert result.has_matches is False
        assert result.summary == ""

    def test_case_insensitive_by_default(self):
        from app.services.forbidden_words import _scan

        result = _scan("This Contains BadWord here", [self._word("badword")], False)
        assert result.has_matches, "case-insensitive scan missed a differently-cased match"

    def test_case_sensitive_mode_respects_case(self):
        from app.services.forbidden_words import _scan

        words = [self._word("badword")]
        assert _scan("contains BadWord", words, True).has_matches is False
        assert _scan("contains badword", words, True).has_matches is True

    def test_counts_every_occurrence(self):
        from app.services.forbidden_words import _scan

        result = _scan("bad bad bad", [self._word("bad")], False)
        assert result.matches[0].count == 3
        assert result.matches[0].positions == [0, 4, 8]

    def test_overlapping_scan_advances_past_each_hit(self):
        """'aa' in 'aaaa' is 2 non-overlapping hits, not 3 overlapping ones."""
        from app.services.forbidden_words import _scan

        result = _scan("aaaa", [self._word("aa")], False)
        assert result.matches[0].positions == [0, 2]

    def test_regex_word_matches(self):
        from app.services.forbidden_words import _scan

        result = _scan("he said damn loudly", [self._word(r"\bdam\w*\b", is_regex=True)], False)
        assert result.has_matches, "regex forbidden word was not applied as a regex"
        assert result.matches[0].count == 1

    def test_regex_word_is_not_treated_as_a_literal(self):
        from app.services.forbidden_words import _scan

        # If the pattern were matched literally this text would not hit.
        result = _scan("value is 4321", [self._word(r"\d{4}", is_regex=True)], False)
        assert result.has_matches

    def test_regex_honours_case_sensitivity(self):
        from app.services.forbidden_words import _scan

        words = [self._word(r"secret", is_regex=True)]
        assert _scan("SECRET", words, False).has_matches is True
        assert _scan("SECRET", words, True).has_matches is False

    def test_invalid_regex_is_skipped_not_raised(self):
        """A user can save a broken pattern; scanning must not 500 the response."""
        from app.services.forbidden_words import _scan

        result = _scan("anything", [self._word("(unclosed", is_regex=True)], False)
        assert result.has_matches is False

    def test_blank_phrase_is_ignored(self):
        from app.services.forbidden_words import _scan

        assert _scan("some text", [self._word("   ")], False).has_matches is False

    def test_summary_lists_each_match(self):
        from app.services.forbidden_words import _scan

        summary = _scan("bad and worse", [self._word("bad"), self._word("worse")], False).summary
        assert "[FORBIDDEN CONTENT DETECTED]" in summary
        assert '"bad"' in summary
        assert '"worse"' in summary
