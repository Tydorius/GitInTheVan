import tempfile
from pathlib import Path

from cryptography import x509

from app.services.ssl_manager import generate_self_signed_cert, get_ssl_status


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
