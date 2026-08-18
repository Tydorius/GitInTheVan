"""Wait until a GitInTheVan instance is genuinely serving, and report its scheme.

Shared by the POSIX and Windows provisioners so TLS handling exists in one
place. The deploy scripts generate a self-signed certificate and write
GITV_SSL_CERTFILE into .env, so a freshly deployed instance answers on HTTPS,
not HTTP -- polling only http:// waits forever.

Certificate verification is deliberately disabled: the certificate under test
is self-signed by design, and this is a loopback check on a throwaway install.

Prints 'SCHEME=https' or 'SCHEME=http' on success. Exit 0 ready, 1 timed out.

Usage:
    python wait_health.py --port 8100 [--timeout 900] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

# Self-signed by design; see module docstring.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


def probe(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_UNVERIFIED) as resp:
            if resp.status != 200:
                return False
            return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    deadline = time.time() + args.timeout
    # https first: that is what a default deploy produces.
    schemes = ("https", "http")
    consecutive = {s: 0 for s in schemes}

    while time.time() < deadline:
        for scheme in schemes:
            if probe(f"{scheme}://{args.host}:{args.port}/health"):
                consecutive[scheme] += 1
                # Two in a row. A single response proves nothing: the updater's
                # maintenance page binds the same port and answers every path.
                if consecutive[scheme] >= 2:
                    print(f"SCHEME={scheme}", flush=True)
                    return 0
            else:
                consecutive[scheme] = 0
        time.sleep(3)

    print("timed out waiting for /health", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
