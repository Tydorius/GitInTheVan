import datetime
import hashlib
import ipaddress
import logging
import socket
import time
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

    # The CA is written beside the leaf it signed. In production cert_path
    # defaults to CERT_DIR/cert.pem, so this resolves to CERT_DIR exactly as
    # before. It matters for any caller passing an explicit path: writing to
    # the module-level CA_CERT_PATH regardless of cert_path meant a call aimed
    # at a temp directory still replaced the real CA and its private key --
    # invalidating the trust every already-provisioned client device is
    # pinned to, and leaving data/ssl holding a CA that did not sign the leaf
    # being served.
    ca_cert_path = cert_path.parent / CA_CERT_PATH.name
    ca_key_path = cert_path.parent / CA_KEY_PATH.name

    ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_cert_path.with_suffix(".crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_cert_path.with_suffix(".der").write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    ca_key_path.write_bytes(
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

    logger.info("CA + leaf certificate chain generated at %s (CA: %s)", cert_path, ca_cert_path)
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


# --- Certificate / LAN-address drift -----------------------------------------
#
# A certificate is pinned to the IP addresses it was issued for. When a router
# reboot hands the host a different DHCP lease, every LAN client keeps failing
# TLS verification against an address the cert never covered, and nothing in
# the app said so -- the server itself still starts fine.
#
# The acknowledgement below is deliberately process-local rather than stored in
# admin_settings: an admin can silence the banner for the life of the server,
# and a restart that still finds the problem raises it again.

_ack_fingerprint: str | None = None
_status_cache: tuple[float, dict] | None = None
_served_cert_ips: list[str] | None = None
_STATUS_TTL_SECONDS = 30.0


def _is_lan_ip(value: str) -> bool:
    """True for addresses another device on the same network could reach."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


def get_local_ips() -> list[str]:
    """Private IPv4 addresses currently held by this host.

    Two sources, because neither alone is reliable: a UDP socket "connected"
    to an off-link address reveals the address the default route would use
    (no packets are sent), and the hostname lookup catches interfaces that
    are not on the default route -- a second NIC, or Wi-Fi alongside Ethernet.
    """
    ips: set[str] = set()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            # TEST-NET-1 (RFC 5737). Never routed, never contacted.
            sock.connect(("192.0.2.1", 9))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass

    return sorted(ip for ip in ips if _is_lan_ip(ip))


def get_cert_ip_sans(cert_path: Path | str) -> list[str]:
    """IP addresses in the certificate's SubjectAlternativeName extension."""
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except (OSError, ValueError, x509.ExtensionNotFound):
        return []
    return sorted(str(ip) for ip in san.value.get_values_for_type(x509.IPAddress))


def _active_cert_path() -> Path | None:
    """The cert uvicorn was actually started with, not the one we last wrote."""
    from app.config import settings

    if settings.ssl_certfile:
        configured = Path(settings.ssl_certfile)
        return configured if configured.exists() else None
    return LEAF_CERT_PATH if LEAF_CERT_PATH.exists() else None


def _fingerprint(cert_ips: list[str], local_ips: list[str]) -> str:
    """Identifies one specific mismatch, so an ack does not cover the next one.

    If the host's address changes again after an admin dismissed the banner,
    the fingerprint changes with it and the warning comes back.
    """
    raw = f"cert:{','.join(sorted(cert_ips))}|local:{','.join(sorted(local_ips))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _compute_cert_ip_status() -> dict:
    global _served_cert_ips
    from app.config import settings

    local_ips = get_local_ips()
    result = {
        "mismatch": False,
        "reason": "",
        "cert_ips": [],
        "local_ips": local_ips,
        "fingerprint": "",
    }

    if not settings.ssl_enabled:
        result["reason"] = "https_disabled"
        return result

    cert_path = _active_cert_path()
    if cert_path is None:
        result["reason"] = "no_certificate"
        return result

    # Snapshot once per process. uvicorn loaded its certificate at startup and
    # holds it until restart, so re-reading the file would let a regenerated
    # cert clear the banner while the old one is still being served.
    if _served_cert_ips is None:
        _served_cert_ips = get_cert_ip_sans(cert_path)
    cert_ips = _served_cert_ips
    result["cert_ips"] = cert_ips

    if not cert_ips:
        # Hostname-only certificate. The admin never asked for LAN IP coverage,
        # so there is no stale address to warn about.
        result["reason"] = "no_ip_sans"
        return result

    if not local_ips:
        # No private address at all -- off the network, or every interface is
        # public. Either way there is nothing to compare against.
        result["reason"] = "no_local_ips"
        return result

    if set(cert_ips) & set(local_ips):
        # At least one address the cert covers is still live. Extra SANs for
        # addresses this host no longer holds are harmless.
        result["reason"] = "ok"
        return result

    result["mismatch"] = True
    result["reason"] = "ip_changed"
    result["fingerprint"] = _fingerprint(cert_ips, local_ips)
    return result


def check_cert_ip_mismatch(force: bool = False) -> dict:
    """Report whether the active certificate covers any current LAN address.

    Cached briefly: the frontend polls this, and `getaddrinfo` blocks.
    Acknowledgement is resolved outside the cache so dismissing the banner
    takes effect immediately.
    """
    global _status_cache

    now = time.monotonic()
    if not force and _status_cache is not None and now - _status_cache[0] < _STATUS_TTL_SECONDS:
        result = dict(_status_cache[1])
    else:
        result = _compute_cert_ip_status()
        _status_cache = (now, dict(result))

    result["acknowledged"] = bool(
        result["mismatch"] and _ack_fingerprint == result["fingerprint"]
    )
    return result


def acknowledge_cert_ip_mismatch(fingerprint: str) -> dict:
    """Silence the banner for this mismatch until the process restarts.

    The fingerprint must match the mismatch currently on the server, so an ack
    raced against a fresh address change cannot swallow the new warning.
    """
    global _ack_fingerprint

    status = check_cert_ip_mismatch(force=True)
    if status["mismatch"] and fingerprint == status["fingerprint"]:
        _ack_fingerprint = fingerprint
        status["acknowledged"] = True
    return status


def reset_cert_ip_check_state() -> None:
    """Clear acknowledgement, cached status, and the served-cert snapshot.

    Only for tests. In production this state is meant to live exactly as long
    as the process does.
    """
    global _ack_fingerprint, _status_cache, _served_cert_ips
    _ack_fingerprint = None
    _status_cache = None
    _served_cert_ips = None
