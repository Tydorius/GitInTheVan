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
CA_CERT_PATH = CERT_DIR / "ca.pem"
CA_KEY_PATH = CERT_DIR / "ca-key.pem"
LEAF_CERT_PATH = CERT_DIR / "cert.pem"
LEAF_KEY_PATH = CERT_DIR / "key.pem"
VALIDITY_DAYS = 3650


def _generate_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    ca_key = rsa.generate_private_key(key_size=2048, public_exponent=65537)

    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "GitInTheVan Local CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GitInTheVan"),
    ])

    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    return ca_key, ca_cert


def _generate_leaf(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    extra_ips: list[str] | None = None,
    extra_dns: list[str] | None = None,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    leaf_key = rsa.generate_private_key(key_size=2048, public_exponent=65537)

    leaf_name = x509.Name([
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
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    return leaf_key, leaf_cert


def generate_self_signed_cert(
    cert_path: Path | str = LEAF_CERT_PATH,
    key_path: Path | str = LEAF_KEY_PATH,
    extra_ips: list[str] | None = None,
    extra_dns: list[str] | None = None,
) -> tuple[Path, Path]:
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    ca_key, ca_cert = _generate_ca()
    leaf_key, leaf_cert = _generate_leaf(ca_key, ca_cert, extra_ips, extra_dns)

    CA_CERT_PATH.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    CA_CERT_PATH.with_suffix(".crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    CA_CERT_PATH.with_suffix(".der").write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    CA_KEY_PATH.write_bytes(
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(leaf_pem + ca_pem)
    cert_path.with_suffix(".crt").write_bytes(leaf_pem + ca_pem)
    key_path.write_bytes(
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    logger.info("CA + leaf certificate chain generated at %s (CA: %s)", cert_path, CA_CERT_PATH)
    return cert_path, key_path


def get_ca_cert_path() -> Path | None:
    return CA_CERT_PATH if CA_CERT_PATH.exists() else None


def get_ssl_status() -> dict:
    cert_exists = LEAF_CERT_PATH.exists()
    key_exists = LEAF_KEY_PATH.exists()
    has_cert = bool(cert_exists and key_exists)

    cert_info = None
    if cert_exists:
        try:
            cert_data = LEAF_CERT_PATH.read_bytes()
            cert = x509.load_pem_x509_certificate(cert_data)
            cert_info = {
                "subject": cert.subject.rfc4514_string(),
                "issuer": cert.issuer.rfc4514_string(),
                "is_ca": _is_ca_cert(cert),
                "has_chain": _has_ca_in_file(LEAF_CERT_PATH),
                "not_before": cert.not_valid_before_utc.isoformat() if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.isoformat(),
                "not_after": cert.not_valid_after_utc.isoformat() if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.isoformat(),
            }
        except Exception as e:
            logger.warning("Could not read cert info: %s", e)

    from app.config import settings
    return {
        "cert_configured": settings.ssl_enabled,
        "cert_exists": has_cert,
        "cert_path": str(LEAF_CERT_PATH) if has_cert else None,
        "key_path": str(LEAF_KEY_PATH) if has_cert else None,
        "ca_cert_path": str(CA_CERT_PATH) if CA_CERT_PATH.exists() else None,
        "cert_info": cert_info,
        "is_active": settings.ssl_enabled and has_cert,
    }


def _is_ca_cert(cert: x509.Certificate) -> bool:
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        return bc.value.ca
    except x509.ExtensionNotFound:
        return False


def _has_ca_in_file(path: Path) -> bool:
    try:
        pem_data = path.read_bytes()
        certs = x509.load_pem_x509_certificates(pem_data)
        return len(certs) > 1
    except Exception:
        return False
