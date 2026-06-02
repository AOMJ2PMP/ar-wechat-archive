#!/usr/bin/env python3
"""Caption WeChat-article images and embed the caption back into the article
markdown, so a text-grounding skill can "read" image content.

For every image ref ![...](images/xxx) that is NOT already captioned (no
"【图注】" right after it), generate a detailed Chinese caption (product names,
numbers, chart/data values, customer names, architecture components, region maps)
and insert a "【图注】<caption>" paragraph after the image. Idempotent.

Providers:
  --provider anthropic   Claude vision (default). Message Batches API for big
                         backfills (~50% cheaper) + prompt caching.
  --provider kimi        Moonshot/Kimi (OpenAI-compatible). Cheaper; concurrent
                         only (Moonshot has no batch API). base_url + key are
                         configurable for any OpenAI-compatible vision endpoint.

Env: provider key (ANTHROPIC_API_KEY, or MOONSHOT_API_KEY/KIMI_API_KEY for kimi).
Deps: pip install pillow  +  anthropic (anthropic provider) / openai (kimi provider)

Examples:
  python scripts/caption_images.py --provider kimi --root output_by_account --limit 5
  python scripts/caption_images.py --provider kimi --root output_by_account/腾讯云原生
  python scripts/caption_images.py --provider anthropic --batch              # big backfill
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
    from PIL import Image
except ImportError as e:  # pragma: no cover
    sys.exit(f"Missing dependency: {e}. Run: pip install pillow")

# --- Captioning instruction (stable → prompt-cached on providers that support it)
SYSTEM_PROMPT = """你是中文图注撰写助手。给定一张来自腾讯云微信公众号文章的配图，用中文写一段客观、详细的图注，聚焦可检索、可引用的事实信息：产品名称、数字与指标、图表/表格中的数值、客户/伙伴名称、架构组件、区域或地图标注、流程阶段。

要求：
- 只描述图中可见内容，绝不臆测或编造任何数字、名称。
- 架构图/产品矩阵/流程图：列出关键模块或阶段及其名称与层级关系。
- 数据图表：说明坐标轴/维度含义并给出关键数值。
- 地图/区域图：列出标注的地区、城市、节点。
- 纯装饰图/海报/无信息量配图：简短说明即可（例如“文章配图，装饰性，无可提取的产品或数据信息”）。
- 输出纯文本图注本身：不要加“图注：”前缀、不要 markdown、不要 emoji、不要引号。长度随信息量，通常 1–4 句。"""

USER_TEXT = "为这张图写一段中文图注。"
IMAGE_REF = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")
CAPTION_MARK = "【图注】"
MAX_TOKENS = 512

DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5", "kimi": "moonshot-v1-8k-vision-preview"}
KIMI_BASE_URL = "https://api.moonshot.cn/v1"


# --- Image → base64 (downscaled to keep tokens + payload small) ----------------
def encode_image(path: Path, max_side: int = 1024) -> tuple[str, str]:
    img = Image.open(path).convert("RGB")  # first frame for animated gif/webp
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode("ascii")


def clean_caption(text: str) -> str:
    text = (text or "").strip().strip("「」\"'`").strip()
    text = re.sub(r"^图注[:：]\s*", "", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()  # keep a single paragraph line


# --- Per-image captioners -------------------------------------------------------
def caption_one_anthropic(client, model: str, img: Path) -> str:
    media_type, data = encode_image(img)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": USER_TEXT},
        ]}],
    )
    return clean_caption("".join(b.text for b in resp.content if b.type == "text"))


def caption_one_openai(client, model: str, img: Path) -> str:
    media_type, data = encode_image(img)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}},
                {"type": "text", "text": USER_TEXT},
            ]},
        ],
    )
    return clean_caption(resp.choices[0].message.content)


def caption_sync(fn, images: list[Path], concurrency: int) -> dict:
    captions: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fn, img): img for img in images}
        for i, fut in enumerate(as_completed(futures), 1):
            img = futures[fut]
            try:
                captions[img] = fut.result()
                print(f"  [{i}/{len(images)}] {img.name}: {captions[img][:48]}")
            except Exception as e:  # noqa: BLE001 — one bad image shouldn't kill the run
                print(f"  [{i}/{len(images)}] {img.name}: FAILED ({e})", file=sys.stderr)
    return captions


def caption_batch_anthropic(client, model: str, images: list[Path], batch_size: int) -> dict:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    sys_blocks = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    captions: dict[Path, str] = {}
    chunks = [images[i : i + batch_size] for i in range(0, len(images), batch_size)]
    for ci, chunk in enumerate(chunks, 1):
        idx_to_img, requests = {}, []
        for idx, img in enumerate(chunk):
            try:
                media_type, data = encode_image(img)
            except Exception as e:  # noqa: BLE001
                print(f"  skip (encode failed) {img.name}: {e}", file=sys.stderr)
                continue
            cid = f"i{idx}"
            idx_to_img[cid] = img
            requests.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
                model=model, max_tokens=MAX_TOKENS, system=sys_blocks,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": USER_TEXT},
                ]}],
            )))
        if not requests:
            continue
        batch = client.messages.batches.create(requests=requests)
        print(f"  batch {ci}/{len(chunks)} ({len(requests)} imgs) → {batch.id}, polling…")
        while client.messages.batches.retrieve(batch.id).processing_status != "ended":
            time.sleep(20)
        for result in client.messages.batches.results(batch.id):
            img = idx_to_img.get(result.custom_id)
            if img and result.result.type == "succeeded":
                m = result.result.message
                captions[img] = clean_caption("".join(b.text for b in m.content if b.type == "text"))
            elif img:
                print(f"  {img.name}: batch {result.result.type}", file=sys.stderr)
        print(f"  batch {ci} done ({len(captions)} captioned so far)")
    return captions


# --- Scan markdown → uncaptioned image occurrences -----------------------------
def scan(root: Path):
    occurrences, md_contents, seen_imgs, seen_set = [], {}, [], set()
    for md in sorted(root.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        md_contents[md] = text
        for m in IMAGE_REF.finditer(text):
            end = m.end()
            if text[end : end + 12].lstrip().startswith(CAPTION_MARK):
                continue
            img = (md.parent / m.group(1)).resolve()
            if not img.is_file():
                continue
            occurrences.append({"md": md, "img": img, "end": end})
            if img not in seen_set:
                seen_set.add(img)
                seen_imgs.append(img)
    return occurrences, md_contents, seen_imgs


def write_captions(occurrences, md_contents, captions) -> int:
    by_md = defaultdict(list)
    for occ in occurrences:
        if captions.get(occ["img"]):
            by_md[occ["md"]].append(occ)
    written = 0
    for md, occs in by_md.items():
        text = md_contents[md]
        for occ in sorted(occs, key=lambda o: o["end"], reverse=True):
            text = text[: occ["end"]] + f"\n\n{CAPTION_MARK}{captions[occ['img']]}\n" + text[occ["end"] :]
            written += 1
        md.write_text(text, encoding="utf-8")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Caption WeChat-article images into the markdown.")
    ap.add_argument("--root", type=Path, default=Path("output_by_account"))
    ap.add_argument("--provider", choices=["anthropic", "kimi"], default="anthropic")
    ap.add_argument("--model", default=None, help="vision model (provider default if unset)")
    ap.add_argument("--base-url", default=None, help="OpenAI-compatible base URL (kimi default: Moonshot)")
    ap.add_argument("--api-key-env", default=None, help="env var holding the API key")
    ap.add_argument("--limit", type=int, default=0, help="cap unique images (0 = no cap; for testing)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=200, help="images per Anthropic batch")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--batch", action="store_true", help="Anthropic Message Batches API (cheaper)")
    mode.add_argument("--no-batch", action="store_true")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"ERROR: --root {args.root} is not a directory.")
    model = args.model or DEFAULT_MODEL[args.provider]

    occurrences, md_contents, images = scan(args.root)
    if args.limit:
        images = images[: args.limit]
        imgset = set(images)
        occurrences = [o for o in occurrences if o["img"] in imgset]
    if not images:
        print("Nothing to caption — every image already has a 【图注】.")
        return

    # --- build the captioning callable for the chosen provider ---
    if args.provider == "anthropic":
        import anthropic
        key_env = args.api_key_env or "ANTHROPIC_API_KEY"
        if not os.environ.get(key_env):
            sys.exit(f"ERROR: set {key_env} in the environment.")
        client = anthropic.Anthropic(api_key=os.environ[key_env])
        use_batch = args.batch or (not args.no_batch and len(images) >= 40)
        print(f"{len(images)} image(s) · provider=anthropic · model={model} · mode={'batch' if use_batch else 'sync'}")
        captions = (caption_batch_anthropic(client, model, images, args.batch_size) if use_batch
                    else caption_sync(lambda im: caption_one_anthropic(client, model, im), images, args.concurrency))
    else:  # kimi / OpenAI-compatible
        from openai import OpenAI
        key_env = args.api_key_env or ("MOONSHOT_API_KEY" if os.environ.get("MOONSHOT_API_KEY") else "KIMI_API_KEY")
        if not os.environ.get(key_env):
            sys.exit(f"ERROR: set {key_env} (or MOONSHOT_API_KEY/KIMI_API_KEY) in the environment.")
        client = OpenAI(api_key=os.environ[key_env], base_url=args.base_url or KIMI_BASE_URL)
        print(f"{len(images)} image(s) · provider=kimi · model={model} · base_url={client.base_url} · mode=sync")
        captions = caption_sync(lambda im: caption_one_openai(client, model, im), images, args.concurrency)

    written = write_captions(occurrences, md_contents, captions)
    print(f"Done: captioned {len(captions)} image(s), inserted {written} 【图注】 line(s).")


if __name__ == "__main__":
    main()
