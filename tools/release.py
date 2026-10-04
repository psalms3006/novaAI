"""Make and sign a NOVA release.

    python tools/release.py keygen --out C:\\safe\\place
        Creates the release-signing key pair. The PRIVATE key file must stay
        with the owner (never in the repository, never on the server). The
        public key is written to packaging/update_public_key.txt, which is
        compiled into every NOVA build, and printed for NOVA_UPDATE_PUBLIC_KEY
        on the server.

    python tools/release.py sign --installer packaging\\out\\NOVA-Setup.exe
           --version 1.1.0 --url https://github.com/.../NOVA-Setup-1.1.0.exe
           --key C:\\safe\\place\\nova_release_private.pem [--min-supported 1.0.0]
        Hashes the installer, writes <installer>.manifest.json and .sig, and
        verifies the signature before finishing. Publish with:
        python -m nova_cloud.manage publish-release --manifest ... --signature ...
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PUBLIC_KEY_FILE = REPO / "packaging" / "update_public_key.txt"


def _inside_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(REPO)
        return True
    except ValueError:
        return False


def manifest_for(installer: Path, version: str, url: str, min_supported: str,
                 notes: str) -> str:
    h = hashlib.sha256()
    with installer.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    data = {
        "version": version, "url": url, "sha256": h.hexdigest(),
        "size": installer.stat().st_size, "min_supported": min_supported,
        "notes": notes, "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installer_args": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
    }
    # Canonical form: the signature covers these exact bytes, and they are
    # stored and served verbatim.
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def keygen(args) -> int:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    out = Path(args.out)
    if _inside_repo(out):
        print("Refusing to write the private key inside the repository. "
              "Choose a folder outside it (and back it up).")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    priv_path = out / "nova_release_private.pem"
    if priv_path.exists():
        print(f"{priv_path} already exists; not overwriting a signing key.")
        return 1
    pw = "" if args.no_passphrase else getpass.getpass(
        "Passphrase for the private key (remember it): ")
    enc = ser.BestAvailableEncryption(pw.encode()) if pw else ser.NoEncryption()
    key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, enc))
    pub = base64.b64encode(key.public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()
    if not args.no_repo_write:
        PUBLIC_KEY_FILE.write_text(pub + "\n", encoding="utf-8")
        print(f"Public key written to {PUBLIC_KEY_FILE} (commit this file).")
    print(f"Private key: {priv_path}  <-- keep secret, back up, never commit")
    print(f"NOVA_UPDATE_PUBLIC_KEY={pub}")
    return 0


def _load_private(path: Path, passphrase: str | None):
    from cryptography.hazmat.primitives import serialization as ser
    data = path.read_bytes()
    try:
        return ser.load_pem_private_key(data, password=None)
    except TypeError:
        pw = passphrase if passphrase is not None else getpass.getpass("Key passphrase: ")
        return ser.load_pem_private_key(data, password=pw.encode())


def sign(args) -> int:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    installer = Path(args.installer)
    if not installer.is_file():
        print(f"No installer at {installer}")
        return 1
    key = _load_private(Path(args.key), args.passphrase)
    manifest = manifest_for(installer, args.version, args.url, args.min_supported, args.notes)
    sig = base64.b64encode(key.sign(manifest.encode("utf-8"))).decode()
    pub = base64.b64encode(key.public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()

    # Prove it before anyone downloads it.
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(
        base64.b64decode(sig), manifest.encode("utf-8"))
    shipped = PUBLIC_KEY_FILE.read_text(encoding="utf-8").strip() if PUBLIC_KEY_FILE.exists() else ""
    if shipped and shipped != pub:
        print("WARNING: this key does not match packaging/update_public_key.txt; "
              "installed copies will reject this release.")

    m_path = installer.with_name(installer.name + ".manifest.json")
    s_path = installer.with_name(installer.name + ".manifest.sig")
    m_path.write_text(manifest, encoding="utf-8")
    s_path.write_text(sig + "\n", encoding="utf-8")
    print(f"Manifest:  {m_path}\nSignature: {s_path}")
    print(json.dumps(json.loads(manifest), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tools/release.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", required=True, help="folder OUTSIDE the repository")
    k.add_argument("--no-passphrase", action="store_true")
    k.add_argument("--no-repo-write", action="store_true",
                   help="do not write packaging/update_public_key.txt")
    k.set_defaults(fn=keygen)
    s = sub.add_parser("sign")
    s.add_argument("--installer", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--min-supported", default="0.0.0")
    s.add_argument("--notes", default="")
    s.add_argument("--passphrase", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=sign)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
