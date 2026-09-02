"""app/routers/diagnostics.py:307 referenced `status_code` in the direct
(non-provider) connectivity branch, where only `resp` is bound -- `status_code`
belongs to the sibling LiteLLM branch above it. Any unexpected upstream status
(anything outside 401/403/200/429/504) raised a `NameError` that the outer
`except Exception` swallowed into a generic "Connection failed" message,
masking the real upstream status from the user.
"""

import pytest

MOCK_ENDPOINT_URL = "http://mock-diag-backend:9999"


async def _default_endpoint(client):
    resp = await client.post(
        "/api/endpoints",
        json={"name": "Diag EP", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-diag"},
    )
    assert resp.status_code == 201, resp.text
    ep = resp.json()
    settings_resp = await client.put("/api/settings", json={"default_endpoint_id": ep["id"]})
    assert settings_resp.status_code == 200, settings_resp.text
    return ep


@pytest.mark.asyncio
async def test_unexpected_status_is_reported_not_swallowed(admin_client, httpx_mock):
    """Mutation check: before the fix this asserted the NameError message and
    passed; after the fix it asserts the real upstream status is surfaced."""
    client, _, _ = admin_client
    await _default_endpoint(client)

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        status_code=418,
        json={"error": "teapot"},
    )

    resp = await client.get("/api/diagnostics/audit")
    assert resp.status_code == 200
    data = resp.json()

    connectivity = next(
        (r for r in data["results"] if r["check"].startswith("Endpoint Connectivity")),
        None,
    )
    assert connectivity is not None, f"no connectivity result in {data['results']}"
    assert connectivity["check"] == "Endpoint Connectivity (Direct)", connectivity
    assert "418" in connectivity["message"], connectivity
    assert "name 'status_code' is not defined" not in connectivity["detail"]
    assert "name 'status_code' is not defined" not in connectivity["message"]
