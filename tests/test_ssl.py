import tempfile
from pathlib import Path

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


def test_generated_cert_is_valid_x509():
    from cryptography import x509

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "cert.pem"
        key_path = Path(tmpdir) / "key.pem"

        generate_self_signed_cert(
            cert_path=cert_path,
            key_path=key_path,
            extra_ips=["172.16.0.1"],
        )

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

        assert cert.subject.rfc4514_string() == cert.issuer.rfc4514_string()

        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san_values = san_ext.value
        ip_values = san_values.get_values_for_type(x509.IPAddress)
        dns_values = san_values.get_values_for_type(x509.DNSName)

        import ipaddress
        assert ipaddress.ip_address("172.16.0.1") in ip_values
        assert "localhost" in dns_values


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
