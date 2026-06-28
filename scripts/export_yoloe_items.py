"""Export an open-vocabulary item detector (YOLO-World / YOLOE) to OpenVINO IR
with a baked-in RETAIL vocabulary, then smoke-test it on a frame — CPU-only.

Why: the stock yolo11n item model only knows ~10 retail-relevant COCO classes,
so most merchandise a shopper can pick up (snacks, cartons, jars, cosmetics) is
invisible — the root cause behind "concealment never fires". An open-vocab model
exported with a fixed retail vocabulary detects "хүний барьж болох зүйл"
generically. Setting the vocabulary at export time bakes the text embeddings into
the head, so the exported IR runs like a plain N-class YOLO: no ultralytics, no
torch, no GPU at inference — the lean OpenVINO path loads it unchanged.

This is the ONE step that needs ultralytics (export only). Run it once on a dev
machine (GPU optional — export is CPU-fine), commit the IR under bin/, and every
store PC just gets the new detector via self-update.

  # 1. install the export-only deps (kept off the shipped agent):
  uv pip install ultralytics openvino

  # 2. export + smoke-test on a still frame from a store camera:
  python scripts/export_yoloe_items.py --image sample_frame.jpg --draw out.jpg

  # 3. flip EdgeConfig.open_vocab_items = True (per-store from superadmin) and
  #    (re)start the camera — the lean detector picks up bin/yoloe_items_*.

The IR lands at  src/sentry_agent_pc/bin/yoloe_items_openvino_model/  alongside a
vocab.json (the ordered class names) that the lean decoder reads at runtime.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Default retail vocabulary — "things a shopper can pick up". Open-vocab models
# generalise best from CONCRETE noun phrases, so this leans specific. Tune freely
# (or pass --vocab a JSON list): it's data, baked into the head at export and
# written to vocab.json for the decoder. Keep personal items (phone) OUT — the
# behaviour engine ignores those for the wrist→item hold signal anyway.
DEFAULT_VOCAB: list[str] = [
    "bottle",
    "plastic bottle",
    "can",
    "canned food",
    "box",
    "carton",
    "milk carton",
    "jar",
    "packet",
    "snack bag",
    "bag of chips",
    "chocolate bar",
    "candy",
    "instant noodle",
    "cup",
    "container",
    "tube",
    "cosmetic bottle",
    "shampoo bottle",
    "boxed product",
    "handheld product",
    "bag",
    "backpack",
    "handbag",
]

OUT_NAME = "yoloe_items_openvino_model"


def _bin_dir() -> Path:
    """src/sentry_agent_pc/bin — the dev base bundled_model_xml() reads from."""
    import sentry_agent_pc.edge.ov_lean as ovl

    return Path(ovl.__file__).parent.parent / "bin"


def _load_vocab(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_VOCAB
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(s, str) for s in data):
        raise SystemExit(f"--vocab {path} must be a JSON list of strings")
    return data


def _build_model(arch: str, weights: str, vocab: list[str]):  # noqa: ANN201 — ultralytics types
    """Load the open-vocab model and bake the vocabulary into its head."""
    if arch == "world":
        from ultralytics import YOLOWorld

        model = YOLOWorld(weights)
        model.set_classes(vocab)
        return model
    if arch == "yoloe":
        from ultralytics import YOLOE

        model = YOLOE(weights)
        # YOLOE text-prompt: embed the vocabulary as the detection vocabulary.
        model.set_classes(vocab, model.get_text_pe(vocab))
        return model
    raise SystemExit(f"unknown --arch {arch!r} (expected: world | yoloe)")


def _export(model, imgsz: int) -> Path:  # noqa: ANN001
    """Export to OpenVINO IR; return the produced .xml path."""
    out = model.export(format="openvino", imgsz=imgsz)
    # ultralytics returns the IR directory (str/Path); find the .xml inside.
    out_path = Path(out)
    return out_path if out_path.suffix == ".xml" else next(out_path.glob("*.xml"))


def _install_ir(src_xml: Path) -> Path:
    """Copy the exported .xml + .bin into bin/<OUT_NAME>/, renaming both to the
    bundled name. OpenVINO auto-pairs the .bin with the .xml by stem in the same
    dir, so renaming both keeps read_model() working with no .xml edits."""
    dst_dir = _bin_dir() / OUT_NAME
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_bin = src_xml.with_suffix(".bin")
    if not src_bin.exists():
        raise SystemExit(f"exported .bin not found next to {src_xml}")
    shutil.copyfile(src_xml, dst_dir / f"{OUT_NAME}.xml")
    shutil.copyfile(src_bin, dst_dir / f"{OUT_NAME}.bin")
    return dst_dir


def _smoke_test(dst_dir: Path, vocab: list[str], image: str, draw: str | None, item_conf: float) -> None:
    """Load the freshly-exported IR with raw OpenVINO (the shipped path) and run
    it on one frame — proves CPU-only open-vocab detection works end-to-end."""
    import cv2
    import numpy as np
    import openvino as ov

    from sentry_agent_pc.edge.ov_lean import decode_openvocab_output, letterbox

    frame = cv2.imread(image)
    if frame is None:
        raise SystemExit(f"could not read --image {image}")
    core = ov.Core()
    compiled = core.compile_model(core.read_model(dst_dir / f"{OUT_NAME}.xml"), "CPU")
    blob, scale, pad_x, pad_y = letterbox(frame)
    raw = np.asarray(compiled(blob)[compiled.output(0)], dtype=np.float32)
    print(f"[smoke] output tensor shape = {raw.shape}  (expect [1, 4+{len(vocab)}, N])")
    items = decode_openvocab_output(raw, scale, pad_x, pad_y, vocab, conf=item_conf)
    print(f"[smoke] {len(items)} item(s) >= conf {item_conf} on CPU:")
    for it in sorted(items, key=lambda d: -d.score):
        x1, y1, x2, y2 = (int(v) for v in it.box)
        print(f"    {it.label:<20} {it.score:.2f}  [{x1},{y1},{x2},{y2}]")
    if draw:
        for it in items:
            x1, y1, x2, y2 = (int(v) for v in it.box)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(
                frame, f"{it.label} {it.score:.2f}", (x1, max(0, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA,
            )
        cv2.imwrite(draw, frame)
        print(f"[smoke] annotated frame written -> {draw}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arch", choices=["world", "yoloe"], default="world",
                    help="open-vocab backbone (default: world — most reliable OpenVINO detect export)")
    ap.add_argument("--weights", default="yolov8s-worldv2.pt",
                    help="model weights (auto-downloaded by ultralytics if absent)")
    ap.add_argument("--vocab", default=None, help="JSON list of class names (default: built-in retail vocab)")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--image", default=None, help="still frame to smoke-test the exported IR on (CPU)")
    ap.add_argument("--draw", default=None, help="write the annotated smoke-test frame here")
    ap.add_argument("--item-conf", type=float, default=0.25,
                    help="confidence for the smoke test (lower than prod default to see weak hits)")
    ap.add_argument("--skip-export", action="store_true",
                    help="only smoke-test an already-installed IR (no ultralytics needed)")
    args = ap.parse_args(argv)

    vocab = _load_vocab(args.vocab)
    dst_dir = _bin_dir() / OUT_NAME

    if not args.skip_export:
        print(f"[export] {args.arch}:{args.weights}  vocab={len(vocab)} classes  imgsz={args.imgsz}")
        model = _build_model(args.arch, args.weights, vocab)
        xml = _export(model, args.imgsz)
        dst_dir = _install_ir(xml)
        (dst_dir / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[export] IR + vocab.json installed -> {dst_dir}")

    if args.image:
        _smoke_test(dst_dir, vocab, args.image, args.draw, args.item_conf)
    elif args.skip_export:
        print("[smoke] nothing to do (pass --image to test)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
