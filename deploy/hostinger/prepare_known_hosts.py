"""Pin the SSH host key for the deployment host using the configured trusted keys."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _key_pairs(path: Path) -> set[tuple[str, str]]:
    pairs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        offset = 1 if fields and fields[0].startswith("@") else 0
        if len(fields) == 2 and fields[0].startswith(("ssh-", "ecdsa-")):
            pairs.add((fields[0], fields[1]))
        elif len(fields) >= offset + 3:
            pairs.add((fields[offset + 1], fields[offset + 2]))
    return pairs


def main() -> int:
    host = os.environ["HOSTINGER_HOST"].strip()
    port = int(os.getenv("HOSTINGER_PORT") or "22")
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    pinned_keys = _key_pairs(known_hosts)
    if not pinned_keys:
        print(
            "HOSTINGER_KNOWN_HOSTS must contain a server host key, either as a "
            "known_hosts line (HOST KEY_TYPE BASE64) or a public-key line "
            "(KEY_TYPE BASE64).",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        ["ssh-keyscan", "-T", "8", "-p", str(port), host],
        capture_output=True,
        check=False,
        text=True,
        timeout=12,
    )
    scanned_keys = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and (fields[1], fields[2]) in pinned_keys:
            scanned_keys.append(f"{fields[0]} {fields[1]} {fields[2]}")

    if not scanned_keys:
        print(
            "The SSH key presented by HOSTINGER_HOST does not match any key in "
            "HOSTINGER_KNOWN_HOSTS. Verify that the host points to the expected VPS "
            "and update HOSTINGER_KNOWN_HOSTS with that VPS's verified host key.",
            file=sys.stderr,
        )
        return 1

    target = host if port == 22 else f"[{host}]:{port}"
    with known_hosts.open("a", encoding="utf-8") as known_hosts_file:
        for line in scanned_keys:
            _, key_type, key = line.split()
            known_hosts_file.write(f"{target} {key_type} {key}\n")
    print("Verified the deployment host key against HOSTINGER_KNOWN_HOSTS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
