"""Export NOVA's bundled sentence-transformers embedder to ONNX.

Why this exists: `nova_embedder/` already holds all-MiniLM-L6-v2 (90 MB), but
the packaged application cannot load it. sentence-transformers needs torch,
torch would add roughly two gigabytes to the installer, so the PyInstaller
spec excludes both -- and semantic memory silently degrades to keyword search
in the shipped product while working perfectly in development.

Exporting the same weights to ONNX removes the gap: onnxruntime is about
15 MB, runs on CPU, and produces the same vectors. Run this once; the result
goes to the user's model directory, not the install directory, so an update
never deletes it.

    python tools/export_embedder_onnx.py [--source nova_embedder] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def detect_pooling(source: Path) -> str:
    cfg = source / "1_Pooling" / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if data.get("pooling_mode_cls_token"):
                return "cls"
            if data.get("pooling_mode_mean_tokens"):
                return "mean"
        except Exception:
            pass
    return "mean"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(ROOT / "nova_embedder"))
    ap.add_argument("--out", default="")
    ap.add_argument("--opset", type=int, default=14)
    args = ap.parse_args()

    source = Path(args.source)
    if not (source / "config.json").exists():
        print(f"No model at {source}", file=sys.stderr)
        return 2

    from nova_core.rag.embeddings import onnx_model_dir
    out = Path(args.out) if args.out else onnx_model_dir()
    out.mkdir(parents=True, exist_ok=True)

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as e:
        print(f"Exporting needs torch and transformers in this environment "
              f"only ({e}). The exported model then runs without them.",
              file=sys.stderr)
        return 2

    print(f"loading  {source}")
    model = AutoModel.from_pretrained(str(source))
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(str(source))

    sample = tokenizer(["nova exports its embedder"], return_tensors="pt",
                       padding=True, truncation=True, max_length=512)
    inputs = ["input_ids", "attention_mask"]
    if "token_type_ids" in sample:
        inputs.append("token_type_ids")

    target = out / "model.onnx"
    print(f"exporting -> {target}")
    torch.onnx.export(
        model,
        tuple(sample[k] for k in inputs),
        str(target),
        input_names=inputs,
        output_names=["last_hidden_state"],
        dynamic_axes={k: {0: "batch", 1: "sequence"} for k in inputs}
                     | {"last_hidden_state": {0: "batch", 1: "sequence"}},
        opset_version=args.opset,
        do_constant_folding=True,
    )

    for name in ("tokenizer.json", "config.json", "special_tokens_map.json",
                 "tokenizer_config.json", "vocab.txt"):
        src = source / name
        if src.exists():
            shutil.copy2(src, out / name)

    pooling = detect_pooling(source)
    hidden = json.loads((source / "config.json").read_text(
        encoding="utf-8")).get("hidden_size", 384)
    (out / "nova_model.json").write_text(json.dumps({
        "source": str(source.name),
        "pooling": pooling,
        "dim": int(hidden),
        "normalise": True,
        "exported_by": "tools/export_embedder_onnx.py",
    }, indent=2), encoding="utf-8")

    size = target.stat().st_size / 1048576
    print(f"done: {size:.1f} MB, {pooling} pooling, {hidden} dimensions")
    print(f"model directory: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
