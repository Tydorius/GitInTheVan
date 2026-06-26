import datetime
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "ssl"
CERT_PATH = CERT_DIR / "cert.pem"
KEY_PATH = CERT_DIR / "key.pem"
VALIDITY_DAYS = 3650


def generate_self_signed_cert(
    cert_path: Path | str = CERT_PATH,
    key_path: Path | str = KEY_PATH,
    extra_ips: list[str] | None = None,
    extra_dns: list[str] | None = None,
) -> tuple[Path, Path]:
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(key_size=2048, public_exponent=65537)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "GitInTheVan"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GitInTheVan"),
    ])

    san_list = [x509.DNSName("localhost")]
    for dns_name in (extra_dns or []):
        san_list.append(x509.DNSName(dns_name))
    for ip_str in (extra_ips or []):
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    logger.info("Self-signed certificate generated at %s", cert_path)
    return cert_path, key_path


def get_ssl_status() -> dict:
    cert_exists = CERT_PATH.exists()
    key_exists = KEY_PATH.exists()
    has_cert = bool(cert_exists and key_exists)

    cert_info = None
    if cert_exists:
        try:
            cert_data = CERT_PATH.read_bytes()
            cert = x509.load_pem_x509_certificate(cert_data)
            cert_info = {
                "subject": cert.subject.rfc4514_string(),
                "issuer": cert.issuer.rfc4514_string(),
                "not_before": cert.not_valid_before_utc.isoformat() if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.isoformat(),
                "not_after": cert.not_valid_after_utc.isoformat() if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.isoformat(),
            }
        except Exception as e:
            logger.warning("Could not read cert info: %s", e)

    from app.config import settings
    return {
        "cert_configured": settings.ssl_enabled,
        "cert_exists": has_cert,
        "cert_path": str(CERT_PATH) if has_cert else None,
        "key_path": str(KEY_PATH) if has_cert else None,
        "cert_info": cert_info,
        "is_active": settings.ssl_enabled and has_cert,
    }
