#!/usr/bin/env python3
"""Caption WeChat-article images with Claude vision and embed the caption back
into the article markdown, so the text-grounding skill can "read" image content.

For every image ref ![...](images/xxx) in output_by_account/<account>/<date - title>/<title>.md
that is NOT already captioned, this generates a detailed Chinese caption (focused
on groundable facts — product names, numbers, chart values, customer names,
architecture components, region maps) and inserts a "【图注】<caption>" paragraph
right after the image. Idempotent: a re-run only captions new images.

Modes:
  - Batch (Message Batches API): ~50% cheaper, used by default for large sets
    (the one-time backfill of ~955 images). Chunked to respect the 256MB/100k limits.
  - Sync (concurrent): fast, used by default for small sets (the daily run's few
    new images). Override with --batch / --no-batch.

Env: ANTHROPIC_API_KEY.
Deps: pip install anthropic pillow

Examples:
  python scripts/caption_images.py --root output_by_account            # auto mode
  python scripts/caption_images.py --root output_by_account --limit 5  # test on 5 images
  python scripts/caption_images.py --batch                             # force batch (backfill)
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    from PIL import Image
except ImportError as e:  # pragma: no cover
    sys.exit(f"Missing dependency: {e}. Run: pip install anthropic pillow")

# --- Captioning instruction (stable → prompt-cached across every request) ------
SYSTEM_PROMPT = """你是中文图注撰写助手。给定一张来自腾讯云微信公众号文章的配图，用中文写一段客观、详细的图注，聚焦可检索、可引用的事实信息：产品名称、数字与指标、图表/表格中的数值、客户/伙伴名称、架构组件、区域或地图标注、流程阶段。

要求：
- 只描述图中可见内容，绝不臆测或编造任何数字、名称。
- 架构图/产品矩阵/流程图：列出关键模块或阶段及其名称与层级关系。
- 数据图表：说明坐标轴/维度含义并给出关键数值。
- 地图/区域图：列出标注的地区、城市、节点。
- 纯装饰图/海报/无信息量配图：简短说明即可（例如“文章配图，装饰性，无可提取的产品或数据信息”）。
- 输出纯文本图注本身：不要加“图注：”前缀、不要 markdown、不要 emoji、不要引号。长度随信息量，通常 1–4 句。"""

SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
USER_TEXT = "为这张图写一段中文图注。"

IMAGE_REF = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")
CAPTION_MARK = "【图注】"
MAX_TOKENS = 512


# --- Image → base64 (downscaled to keep tokens + payload small) ----------------
def encode_image(path: Path, max_side: int = 1024) -> tuple[str, str]:
    img = Image.open(path)
    img = img.convert("RGB")  # first frame for animated gif/webp; drops alpha
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode("ascii")


def build_message(media_type: str, data: str) -> list:
    return [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
        {"type": "text", "text": USER_TEXT},
    ]


def clean_caption(text: str) -> str:
    text = (text or "").strip().strip("「」\"'`").strip()
    text = re.sub(r"^图注[:：]\s*", "", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()  # keep it a single paragraph line


# --- Scan markdown → list of uncaptioned image occurrences ---------------------
def scan(root: Path):
    """Returns (occurrences, md_contents, unique_images).

    occurrences: list of dicts {md, img, end} — one per uncaptioned image ref.
    md_contents: {md_path: original_text} (offsets in `end` index into this).
    unique_images: ordered list of distinct existing image paths to caption.
    """
    occurrences = []
    md_contents = {}
    seen_imgs = []
    seen_set = set()
    for md in sorted(root.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        md_contents[md] = text
        for m in IMAGE_REF.finditer(text):
            end = m.end()
            # idempotency: skip if a 【图注】 already follows this image ref
            if md_contents[md][end : end + 12].lstrip().startswith(CAPTION_MARK):
                continue
            img = (md.parent / m.group(1)).resolve()
            if not img.is_file():
                continue
            occurrences.append({"md": md, "img": img, "end": end})
            if img not in seen_set:
                seen_set.add(img)
                seen_imgs.append(img)
    return occurrences, md_contents, seen_imgs


# --- Captioning: sync (concurrent) ---------------------------------------------
def caption_sync(client, model: str, images: list[Path], concurrency: int) -> dict:
    captions: dict[Path, str] = {}

    def one(img: Path):
        media_type, data = encode_image(img)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_BLOCKS,
            messages=[{"role": "user", "content": build_message(media_type, data)}],
        )
        return img, clean_caption("".join(b.text for b in resp.content if b.type == "text"))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(one, img): img for img in images}
        for i, fut in enumerate(as_completed(futures), 1):
            img = futures[fut]
            try:
                _, cap = fut.result()
                captions[img] = cap
                print(f"  [{i}/{len(images)}] {img.name}: {cap[:48]}")
            except Exception as e:  # noqa: BLE001 — one bad image shouldn't kill the run
                print(f"  [{i}/{len(images)}] {img.name}: FAILED ({e})", file=sys.stderr)
    return captions


# --- Captioning: Message Batches API (chunked, ~50% cheaper) -------------------
def caption_batch(client, model: str, images: list[Path], batch_size: int) -> dict:
    captions: dict[Path, str] = {}
    chunks = [images[i : i + batch_size] for i in range(0, len(images), batch_size)]
    for ci, chunk in enumerate(chunks, 1):
        idx_to_img = {}
        requests = []
        for idx, img in enumerate(chunk):
            try:
                media_type, data = encode_image(img)
            except Exception as e:  # noqa: BLE001
                print(f"  skip (encode failed) {img.name}: {e}", file=sys.stderr)
                continue
            cid = f"i{idx}"
            idx_to_img[cid] = img
            requests.append(
                Request(
                    custom_id=cid,
                    params=MessageCreateParamsNonStreaming(
                        model=model,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_BLOCKS,
                        messages=[{"role": "user", "content": build_message(media_type, data)}],
                    ),
                )
            )
        if not requests:
            continue
        batch = client.messages.batches.create(requests=requests)
        print(f"  batch {ci}/{len(chunks)} ({len(requests)} imgs) → {batch.id}, polling…")
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            time.sleep(20)
        for result in client.messages.batches.results(batch.id):
            img = idx_to_img.get(result.custom_id)
            if img is None:
                continue
            if result.result.type == "succeeded":
                msg = result.result.message
                captions[img] = clean_caption(
                    "".join(b.text for b in msg.content if b.type == "text")
                )
            else:
                print(f"  {img.name}: batch result {result.result.type}", file=sys.stderr)
        print(f"  batch {ci} done ({len(captions)} captioned so far)")
    return captions


# --- Insert captions back into markdown (right-to-left to keep offsets valid) --
def write_captions(occurrences, md_contents, captions) -> int:
    by_md = defaultdict(list)
    for occ in occurrences:
        if occ["img"] in captions and captions[occ["img"]]:
            by_md[occ["md"]].append(occ)
    written = 0
    for md, occs in by_md.items():
        text = md_contents[md]
        for occ in sorted(occs, key=lambda o: o["end"], reverse=True):
            cap = captions[occ["img"]]
            text = text[: occ["end"]] + f"\n\n{CAPTION_MARK}{cap}\n" + text[occ["end"] :]
            written += 1
        md.write_text(text, encoding="utf-8")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Caption WeChat-article images into the markdown.")
    ap.add_argument("--root", type=Path, default=Path("output_by_account"))
    ap.add_argument("--model", default="claude-haiku-4-5", help="vision model (default: cost-effective Haiku)")
    ap.add_argument("--limit", type=int, default=0, help="cap unique images (0 = no cap; for testing)")
    ap.add_argument("--concurrency", type=int, default=8, help="parallel requests in sync mode")
    ap.add_argument("--batch-size", type=int, default=200, help="images per Batches API submission")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--batch", action="store_true", help="force Message Batches API (cheaper)")
    mode.add_argument("--no-batch", action="store_true", help="force synchronous concurrent calls")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: set ANTHROPIC_API_KEY in the environment.")
    if not args.root.is_dir():
        sys.exit(f"ERROR: --root {args.root} is not a directory.")

    occurrences, md_contents, images = scan(args.root)
    if args.limit:
        images = images[: args.limit]
        imgset = set(images)
        occurrences = [o for o in occurrences if o["img"] in imgset]
    if not images:
        print("Nothing to caption — every image already has a 【图注】.")
        return

    use_batch = args.batch or (not args.no_batch and len(images) >= 40)
    print(
        f"{len(images)} image(s) to caption across {len({o['md'] for o in occurrences})} file(s) "
        f"· model={args.model} · mode={'batch' if use_batch else 'sync'}"
    )

    client = anthropic.Anthropic()
    captions = (
        caption_batch(client, args.model, images, args.batch_size)
        if use_batch
        else caption_sync(client, args.model, images, args.concurrency)
    )
    written = write_captions(occurrences, md_contents, captions)
    print(f"Done: captioned {len(captions)} image(s), inserted {written} 【图注】 line(s).")


if __name__ == "__main__":
    main()
