import tempfile
import time
from pathlib import Path

import pytest
from cryptography import x509

from app.services import ssl_manager
from app.services.ssl_manager import (
    acknowledge_cert_ip_mismatch,
    check_cert_ip_mismatch,
    generate_self_signed_cert,
    get_cert_ip_sans,
    get_ssl_status,
    reset_cert_ip_check_state,
)


def test_generate_self_signed_cert():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        result_cert, result_key = generate_self_signed_cert(
            cert_path=cert_path,
            key_path=key_path,
            extra_ips=["10.0.0.1", "192.168.1.50"],
            extra_dns=["myhost.local"],
        )

        assert result_cert == cert_path
        assert result_key == key_path
        assert cert_path.exists()
        assert key_path.exists()

        cert_text = cert_path.read_text()
        assert "BEGIN CERTIFICATE" in cert_text
        assert "END CERTIFICATE" in cert_text

        key_text = key_path.read_text()
        assert "BEGIN PRIVATE KEY" in key_text or "BEGIN RSA PRIVATE KEY" in key_text
        assert "END PRIVATE KEY" in key_text or "END RSA PRIVATE KEY" in key_text


def test_generates_ca_and_leaf():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(
            cert_path=cert_path,
            key_path=key_path,
            extra_ips=["172.16.0.1"],
        )

        pem_data = cert_path.read_bytes()
        certs = list(x509.load_pem_x509_certificates(pem_data))
        assert len(certs) == 2, f"Expected leaf + CA in chain, got {len(certs)}"

        leaf = certs[0]
        ca = certs[1]

        leaf_bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
        assert leaf_bc.value.ca is False, "First cert should be leaf (not CA)"

        ca_bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        assert ca_bc.value.ca is True, "Second cert should be CA"

        assert leaf.issuer == ca.subject, "Leaf should be issued by the CA"

        ip_values = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
        import ipaddress
        assert ipaddress.ip_address("172.16.0.1") in ip_values


def test_ca_key_usage_set_correctly():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(cert_path=cert_path, key_path=key_path)

        pem_data = cert_path.read_bytes()
        certs = list(x509.load_pem_x509_certificates(pem_data))
        ca = certs[1]

        key_usage = ca.extensions.get_extension_for_class(x509.KeyUsage).value
        assert key_usage.key_cert_sign is True
        assert key_usage.digital_signature is True


def test_leaf_key_usage_set_correctly():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(cert_path=cert_path, key_path=key_path)

        pem_data = cert_path.read_bytes()
        certs = list(x509.load_pem_x509_certificates(pem_data))
        leaf = certs[0]

        key_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        assert key_usage.key_cert_sign is False
        assert key_usage.digital_signature is True
        assert key_usage.key_encipherment is True

        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_generate_cert_no_extras():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(cert_path=cert_path, key_path=key_path)

        assert cert_path.exists()
        assert key_path.exists()


def test_generate_cert_invalid_ip_ignored():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(
            cert_path=cert_path,
            key_path=key_path,
            extra_ips=["not-an-ip"],
        )

        assert cert_path.exists()
        assert key_path.exists()


def test_get_ssl_status_no_cert():
    status = get_ssl_status()
    assert "cert_configured" in status
    assert "cert_exists" in status
    assert "is_active" in status
    assert "cert_info" in status


def test_generate_writes_ca_beside_the_leaf_not_into_the_real_cert_dir():
    """A caller passing an explicit path must not touch the live CA.

    Overwriting data/ssl/ca-key.pem invalidates every client device that has
    already trusted this install's CA, so this must stay scoped.
    """
    from app.services.ssl_manager import CA_CERT_PATH, CA_KEY_PATH

    real_ca_before = CA_CERT_PATH.read_bytes() if CA_CERT_PATH.exists() else None
    real_ca_key_before = CA_KEY_PATH.read_bytes() if CA_KEY_PATH.exists() else None

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(cert_path=cert_path, key_path=key_path)

        assert (Path(tmpdir) / "ca.pem").exists(), "CA should land beside the leaf"
        assert (Path(tmpdir) / "ca-key.pem").exists()

    real_ca_after = CA_CERT_PATH.read_bytes() if CA_CERT_PATH.exists() else None
    real_ca_key_after = CA_KEY_PATH.read_bytes() if CA_KEY_PATH.exists() else None
    assert real_ca_after == real_ca_before, "Live CA certificate was overwritten"
    assert real_ca_key_after == real_ca_key_before, "Live CA private key was overwritten"


# --- Certificate / LAN-address drift -----------------------------------------


@pytest.fixture
def ssl_env(tmp_path, monkeypatch):
    """Serve a certificate issued for `cert_ips` on a host holding `local_ips`."""
    from types import SimpleNamespace

    from app.config import settings

    state = {"local_ips": []}
    monkeypatch.setattr(ssl_manager, "get_local_ips", lambda: list(state["local_ips"]))

    def configure(cert_ips, local_ips):
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        generate_self_signed_cert(cert_path=cert_path, key_path=key_path, extra_ips=cert_ips)
        monkeypatch.setattr(settings, "ssl_certfile", str(cert_path))
        monkeypatch.setattr(settings, "ssl_keyfile", str(key_path))
        state["local_ips"] = list(local_ips)
        reset_cert_ip_check_state()

    def move_host_to(ips):
        """The machine picks up a different address while the server runs."""
        state["local_ips"] = list(ips)

    def restart_server():
        """A restart drops the acknowledgement and re-reads the served cert."""
        reset_cert_ip_check_state()

    return SimpleNamespace(configure=configure, move_host_to=move_host_to, restart_server=restart_server)


def test_get_cert_ip_sans_reads_ip_addresses(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    generate_self_signed_cert(
        cert_path=cert_path, key_path=key_path,
        extra_ips=["192.168.1.50", "10.0.0.5"], extra_dns=["myhost.local"],
    )

    assert get_cert_ip_sans(cert_path) == ["10.0.0.5", "192.168.1.50"]


def test_no_warning_when_cert_covers_current_address(ssl_env):
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.50"])

    status = check_cert_ip_mismatch(force=True)

    assert status["mismatch"] is False
    assert status["reason"] == "ok"


def test_no_warning_when_any_cert_ip_is_still_held(ssl_env):
    """Extra SANs for addresses the host no longer has are harmless."""
    ssl_env.configure(cert_ips=["192.168.1.50", "10.0.0.5"], local_ips=["10.0.0.5"])

    assert check_cert_ip_mismatch(force=True)["mismatch"] is False


def test_warns_when_address_changed(ssl_env):
    """The reported failure: a router reboot hands out a different lease."""
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])

    status = check_cert_ip_mismatch(force=True)

    assert status["mismatch"] is True
    assert status["reason"] == "ip_changed"
    assert status["cert_ips"] == ["192.168.1.50"]
    assert status["local_ips"] == ["192.168.1.72"]
    assert status["fingerprint"]
    assert status["acknowledged"] is False


def test_hostname_only_cert_is_not_flagged(ssl_env):
    """No IP SANs means LAN IP coverage was never requested."""
    ssl_env.configure(cert_ips=[], local_ips=["192.168.1.72"])

    status = check_cert_ip_mismatch(force=True)

    assert status["mismatch"] is False
    assert status["reason"] == "no_ip_sans"


def test_no_check_when_https_disabled(ssl_env, monkeypatch):
    from app.config import settings

    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    monkeypatch.setattr(settings, "ssl_certfile", "")
    monkeypatch.setattr(settings, "ssl_keyfile", "")
    reset_cert_ip_check_state()

    status = check_cert_ip_mismatch(force=True)

    assert status["mismatch"] is False
    assert status["reason"] == "https_disabled"


def test_no_warning_when_host_has_no_lan_address(ssl_env):
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=[])

    status = check_cert_ip_mismatch(force=True)

    assert status["mismatch"] is False
    assert status["reason"] == "no_local_ips"


def test_acknowledgement_hides_the_warning(ssl_env):
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    fingerprint = check_cert_ip_mismatch(force=True)["fingerprint"]

    acknowledged = acknowledge_cert_ip_mismatch(fingerprint)

    assert acknowledged["acknowledged"] is True
    assert check_cert_ip_mismatch(force=True)["acknowledged"] is True


def test_acknowledgement_with_wrong_fingerprint_is_ignored(ssl_env):
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])

    result = acknowledge_cert_ip_mismatch("not-the-current-mismatch")

    assert result["acknowledged"] is False
    assert check_cert_ip_mismatch(force=True)["acknowledged"] is False


def test_warning_returns_after_a_restart(ssl_env):
    """The acknowledgement is process-scoped, so a reboot re-raises it."""
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    acknowledge_cert_ip_mismatch(check_cert_ip_mismatch(force=True)["fingerprint"])
    assert check_cert_ip_mismatch(force=True)["acknowledged"] is True

    ssl_env.restart_server()

    status = check_cert_ip_mismatch(force=True)
    assert status["mismatch"] is True
    assert status["acknowledged"] is False


def test_acknowledgement_does_not_cover_a_later_address_change(ssl_env):
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    acknowledge_cert_ip_mismatch(check_cert_ip_mismatch(force=True)["fingerprint"])

    ssl_env.move_host_to(["192.168.1.99"])

    status = check_cert_ip_mismatch(force=True)
    assert status["mismatch"] is True
    assert status["acknowledged"] is False, "A new mismatch must not inherit the old ack"


def test_regenerating_does_not_clear_the_warning_before_restart(ssl_env, tmp_path):
    """uvicorn holds the certificate it started with until the process restarts."""
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    assert check_cert_ip_mismatch(force=True)["mismatch"] is True

    generate_self_signed_cert(
        cert_path=tmp_path / "cert.pem", key_path=tmp_path / "key.pem",
        extra_ips=["192.168.1.72"],
    )

    assert check_cert_ip_mismatch(force=True)["mismatch"] is True, (
        "Banner must persist until the server is restarted onto the new cert"
    )

    ssl_env.restart_server()
    assert check_cert_ip_mismatch(force=True)["mismatch"] is False


async def test_ip_check_endpoint_reports_mismatch(admin_client, ssl_env):
    client, _, _ = admin_client
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])

    resp = await client.get("/api/admin/ssl/ip-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mismatch"] is True
    assert body["cert_ips"] == ["192.168.1.50"]
    assert body["acknowledged"] is False


async def test_acknowledge_endpoint_silences_the_banner(admin_client, ssl_env):
    client, _, _ = admin_client
    ssl_env.configure(cert_ips=["192.168.1.50"], local_ips=["192.168.1.72"])
    fingerprint = (await client.get("/api/admin/ssl/ip-check")).json()["fingerprint"]

    ack = await client.post(
        "/api/admin/ssl/ip-check/acknowledge", json={"fingerprint": fingerprint},
    )

    assert ack.status_code == 200
    assert ack.json()["acknowledged"] is True
    assert (await client.get("/api/admin/ssl/ip-check")).json()["acknowledged"] is True


class TestHostnameLookupIsBounded:
    """A hostname lookup that never returns must not stall the status check.

    On macOS the default hostname is an mDNS `.local` name, and under a
    Homebrew Python `getaddrinfo` spent 35 real seconds on it before failing --
    longer than the status cache's own 30s TTL, so every poll from the admin UI
    re-entered the block. Apple's python3 answered instantly, so it depended
    entirely on which interpreter the deploy script found.
    """

    @staticmethod
    def _hang(seconds: float):
        def fake_getaddrinfo(*_args, **_kwargs):
            time.sleep(seconds)
            raise OSError("nodename nor servname provided, or not known")
        return fake_getaddrinfo

    def test_slow_lookup_is_abandoned(self, monkeypatch):
        monkeypatch.setattr(ssl_manager.socket, "getaddrinfo", self._hang(10))

        started = time.monotonic()
        result = ssl_manager._hostname_ips(timeout=0.2)
        elapsed = time.monotonic() - started

        assert result == set()
        assert elapsed < 2.0, f"lookup was not abandoned; took {elapsed:.2f}s"

    def test_fast_lookup_is_still_used(self, monkeypatch):
        monkeypatch.setattr(
            ssl_manager.socket, "getaddrinfo",
            lambda *a, **k: [(None, None, None, "", ("192.168.4.7", 0))],
        )

        assert ssl_manager._hostname_ips(timeout=2.0) == {"192.168.4.7"}

    def test_local_ips_survives_a_hung_lookup(self, monkeypatch):
        """The UDP probe still supplies the default-route address."""
        monkeypatch.setattr(ssl_manager.socket, "getaddrinfo", self._hang(10))
        monkeypatch.setattr(ssl_manager, "_hostname_ips", lambda timeout=0.2: set())

        # No assertion on the value: it depends on the host running the suite.
        # What matters is that it returns rather than blocking on the resolver.
        started = time.monotonic()
        ssl_manager.get_local_ips()
        assert time.monotonic() - started < 2.0
