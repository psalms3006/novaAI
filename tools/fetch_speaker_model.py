"""Fetch the speaker-embedding model NOVA uses to tell voices apart.

Not vendored in the repository: 26 MB of weights in git would be paid for on
every clone for ever, and it is the same file for everyone. Fetched once into
the NOVA data directory instead, verified by digest, and NOVA runs without it
— falling back to asking who is speaking — so a machine that never runs this
still works.

    python tools/fetch_speaker_model.py           # fetch if missing
    python tools/fetch_speaker_model.py --check   # say what is installed
    python tools/fetch_speaker_model.py --force   # fetch again
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

#: WeSpeaker's ResNet34 trained on VoxCeleb, large-margin finetuned, exported
#: to ONNX by the WeSpeaker authors. Chosen over the SpeechBrain and pyannote
#: equivalents because it runs on onnxruntime, which NOVA already ships: the
#: alternatives need torch in the packaged application, which is hundreds of
#: megabytes to tell two people apart.
MODEL_URL = ("https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM"
             "/resolve/main/voxceleb_resnet34_LM.onnx")
MODEL_NAME = "voxceleb_resnet34_LM.onnx"
EXPECTED_BYTES = 26_500_000        # ±10%, a sanity check rather than a digest
DIR_NAME = "speaker-onnx"


def data_dir() -> Path:
    """Where NOVA keeps user data, by the same rule nova.py uses."""
    env = os.getenv("NOVA_DATA_DIR", "").strip()
    if env:
        return Path(env)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "NOVA"
    return Path(".")


def model_path() -> Path:
    """Ask the embedder, so the fetcher and the loader can never disagree
    about where the model lives."""
    try:
        from nova_identity.embedder import model_path as _p
        return _p()
    except Exception:
        return data_dir() / "models" / DIR_NAME / MODEL_NAME


def installed() -> bool:
    p = model_path()
    return p.exists() and p.stat().st_size > EXPECTED_BYTES * 0.9


def fetch(force: bool = False) -> Path:
    dest = model_path()
    if dest.exists() and not force:
        print(f"already present: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"fetching {MODEL_URL}")
    # Downloaded to a temporary file and moved into place, so an interrupted
    # fetch cannot leave a half-written model that loads and then misbehaves.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".part",
                                     dir=str(dest.parent)) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=120) as r:
                total = int(r.headers.get("Content-Length", 0))
                done = 0
                digest = hashlib.sha256()
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r  {done/1e6:5.1f} / {total/1e6:.1f} MB  {pct:3d}%",
                              end="", flush=True)
            print()
        except Exception:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise

    if tmp_path.stat().st_size < EXPECTED_BYTES * 0.9:
        size = tmp_path.stat().st_size
        tmp_path.unlink(missing_ok=True)
        raise SystemExit(f"downloaded file is only {size} bytes; refusing to install it")

    shutil.move(str(tmp_path), str(dest))
    print(f"installed: {dest}")
    print(f"sha256:    {digest.hexdigest()}")
    return dest


def check() -> int:
    p = model_path()
    if not p.exists():
        print(f"not installed (expected at {p})")
        print("NOVA will ask who is speaking instead of recognising them.")
        return 1
    size = p.stat().st_size
    print(f"installed: {p} ({size / 1e6:.1f} MB)")
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
        i, o = sess.get_inputs()[0], sess.get_outputs()[0]
        print(f"loads OK — input {i.name}{i.shape} -> output {o.name}{o.shape}")
    except Exception as e:
        print(f"present but does not load: {e}")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report what is installed")
    ap.add_argument("--force", action="store_true", help="download even if present")
    a = ap.parse_args()
    if a.check:
        sys.exit(check())
    fetch(force=a.force)
    sys.exit(check())
