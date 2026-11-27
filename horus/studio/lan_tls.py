"""Self-signed local LAN TLS certificate helpers for zero-dependency Phone PWA HTTPS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess


def detect_lan_ip() -> str | None:
    """Return the primary non-loopback IPv4 address for LAN binding, or ``None``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith(("127.", "0.")):
                return str(ip)
    except OSError:
        pass
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith(("127.", "0.")):
            return str(host_ip)
    except OSError:
        pass
    return None


def _write_private_key(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    if os.name == "posix":
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    if os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def ensure_lan_tls_cert(config_dir: Path, lan_ip: str | None = None) -> tuple[Path, Path]:
    """Return ``(cert_path, key_path)`` for a self-signed LAN HTTPS cert, creating or refreshing if needed."""
    tls_dir = Path(config_dir) / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tls_dir / "horus-lan.crt"
    key_path = tls_dir / "horus-lan.key"
    meta_path = tls_dir / "horus-lan.meta.json"

    if cert_path.is_file() and key_path.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("lan_ip") == lan_ip:
                return cert_path, key_path
        except (OSError, json.JSONDecodeError):
            pass

    if _generate_with_cryptography(cert_path, key_path, lan_ip):
        meta_path.write_text(json.dumps({"lan_ip": lan_ip}) + "\n", encoding="utf-8")
        return cert_path, key_path

    if _generate_with_openssl(cert_path, key_path, lan_ip):
        meta_path.write_text(json.dumps({"lan_ip": lan_ip}) + "\n", encoding="utf-8")
        return cert_path, key_path

    raise RuntimeError(
        "Unable to generate local LAN TLS certificate: install `cryptography` or `openssl`."
    )


def _generate_with_cryptography(cert_path: Path, key_path: Path, lan_ip: str | None) -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "horus-studio-lan"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HORUS Studio Local"),
        ]
    )
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ip_address("127.0.0.1")),
    ]
    if lan_ip:
        try:
            san_entries.append(x509.IPAddress(ip_address(lan_ip)))
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    _write_private_key(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def _generate_with_openssl(cert_path: Path, key_path: Path, lan_ip: str | None) -> bool:
    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        return False
    san = "DNS:localhost,IP:127.0.0.1"
    if lan_ip:
        san += f",IP:{lan_ip}"
    res = subprocess.run(
        [
            openssl_bin,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "365",
            "-subj",
            "/CN=horus-studio-lan",
            "-addext",
            f"subjectAltName={san}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if res.returncode != 0:
        return False
    if os.name == "posix" and key_path.is_file():
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return cert_path.is_file() and key_path.is_file()
