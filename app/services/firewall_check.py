"""Firewall detection for startup warnings.

Checks whether port 8000 might be blocked by the OS firewall.
Logs a WARNING if the firewall appears active and the port is not open.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)


def check_firewall(port: int = 8000) -> None:
    """Check firewall status and log a warning if port may be blocked."""
    system = platform.system()

    if system == "Windows":
        _check_windows(port)
    elif system == "Darwin":
        _check_macos(port)
    elif system == "Linux":
        _check_linux(port)
    else:
        logger.debug("Firewall check: unsupported OS '%s', skipping", system)


def _check_windows(port: int) -> None:
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=GitInTheVan"],
            capture_output=True, text=True, timeout=5,
        )
        if "No rules match" in result.stdout or result.returncode != 0:
            logger.warning(
                "Windows Firewall rule 'GitInTheVan' not found for port %d. "
                "If you cannot connect from other devices, run as administrator: "
                "netsh advfirewall firewall add rule name=\"GitInTheVan\" "
                "dir=in action=allow protocol=TCP localport=%d",
                port, port,
            )
        else:
            logger.debug("Windows Firewall: rule 'GitInTheVan' exists")
    except Exception:
        logger.debug("Windows Firewall: check failed (non-admin or netsh unavailable)")


def _check_macos(port: int) -> None:
    fw_path = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    if not shutil.which("socketfilterfw") and not __import__("os").path.exists(fw_path):
        return

    try:
        result = subprocess.run(
            [fw_path, "--getglobalstate"],
            capture_output=True, text=True, timeout=5,
        )
        if "enabled" in result.stdout.lower():
            logger.warning(
                "macOS Application Firewall is enabled. "
                "Incoming connections on port %d may be blocked. "
                "To allow, run: "
                "sudo %s --add '$(which python3)' && "
                "sudo %s --unblockapp '$(which python3)'",
                port, fw_path, fw_path,
            )
        else:
            logger.debug("macOS Firewall: not enabled")
    except Exception:
        logger.debug("macOS Firewall: check failed")


def _check_linux(port: int) -> None:
    if shutil.which("ufw"):
        try:
            result = subprocess.run(
                ["ufw", "status"], capture_output=True, text=True, timeout=5,
            )
            output = result.stdout.lower()
            if "status: active" in output and str(port) not in output:
                logger.warning(
                    "ufw is active but port %d does not appear to be open. "
                    "To open it, run: sudo ufw allow %d/tcp",
                    port, port,
                )
            elif "status: active" in output:
                logger.debug("ufw: port %d appears open", port)
            else:
                logger.debug("ufw: not active")
        except Exception:
            logger.debug("ufw: check failed")
        return

    if shutil.which("firewall-cmd"):
        try:
            state = subprocess.run(
                ["firewall-cmd", "--state"],
                capture_output=True, text=True, timeout=5,
            )
            if state.stdout.strip() == "running":
                ports = subprocess.run(
                    ["firewall-cmd", "--list-ports"],
                    capture_output=True, text=True, timeout=5,
                )
                if f"{port}/tcp" not in ports.stdout:
                    logger.warning(
                        "firewalld is running but port %d is not open. "
                        "To open it, run: "
                        "sudo firewall-cmd --permanent --add-port=%d/tcp && "
                        "sudo firewall-cmd --reload",
                        port, port,
                    )
                else:
                    logger.debug("firewalld: port %d is open", port)
            else:
                logger.debug("firewalld: not running")
        except Exception:
            logger.debug("firewalld: check failed")
