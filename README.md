# ar-wechat-archive

A lightweight, **text-only** Markdown archive of Tencent Cloud WeChat official-account
articles, kept fresh automatically and shared so anyone (or any tool) can consume it
without depending on one person's laptop.

It is the grounding source for the `analyst-grounding` skill (analyst-questionnaire suite):
the skill walks `output_by_account/`, greps article bodies, and cites matches with their
公众号 / 发布时间 / 原文链接.

## Layout

```
output_by_account/                       ← point WECHAT_ARCHIVE_PATHS here
└── <公众号>/                            e.g. 腾讯云, 腾讯云出海服务, 腾讯CodeBuddy
    └── YYYY-MM-DD - <标题>/
        └── <标题>.md                    # frontmatter (> 公众号 / > 发布时间 / > 原文链接) + body
```

Images are tracked (full mirror), and each image is turned into a text **【图注】** — a Claude
vision caption embedded right after the image in its article's `.md`. This lets the grounding
skill "read" image content (architecture diagrams, product matrices, data charts, region maps)
as citable text. See `scripts/caption_images.py`.

## Use it with the skill

```bash
git clone https://github.com/AOMJ2PMP/ar-wechat-archive.git
# then, persistently, in ~/.claude/settings.json -> "env":
#   "WECHAT_ARCHIVE_PATHS": "/abs/path/to/ar-wechat-archive/output_by_account"
```

`WECHAT_ARCHIVE_PATHS` is a comma-separated list of absolute paths; add more roots if you
keep several archives.

## How it stays fresh (GitHub Action)

`.github/workflows/sync.yml` runs daily (and on manual dispatch). It pulls new articles from
the wewe-rss instance via the [wechat-feed-to-markdown](https://github.com/AOMJ2PMP/wechat-feed-to-markdown)
scraper and commits any new Markdown.

**One-time setup (repo Settings → Secrets and variables → Actions):**

| Kind | Name | Value |
|---|---|---|
| Secret | `WEWERSS_AUTH_CODE` | your wewe-rss `AUTH_CODE` |
| Secret (optional) | `ANTHROPIC_API_KEY` | enables AI image captions (【图注】) during each sync |
| Variable | `WEWERSS_BASE_URL` | your wewe-rss URL (defaults to `https://ar-wechat.zeabur.app`) |
| File | `feeds.txt` | one `MP_WXS_…` id per line (get them from the dashboard URL `…/dash/feeds/MP_WXS_…`) |

Quick CLI version:

```bash
gh secret set   WEWERSS_AUTH_CODE   --repo AOMJ2PMP/ar-wechat-archive          # paste the code (never commit it)
gh variable set WEWERSS_BASE_URL    --repo AOMJ2PMP/ar-wechat-archive --body https://ar-wechat.zeabur.app
# edit feeds.txt, add the 3 MP_IDs, commit, then trigger a first run:
gh workflow run sync-wechat-archive --repo AOMJ2PMP/ar-wechat-archive
```

## Run a sync locally (optional)

```bash
git clone https://github.com/AOMJ2PMP/wechat-feed-to-markdown.git && cd wechat-feed-to-markdown
uv sync
printf '%s' 'YOUR-AUTH-CODE' > .auth && chmod 600 .auth
export WEWERSS_BASE_URL=https://ar-wechat.zeabur.app
uv run python scripts/sync.py --mp-id MP_WXS_xxxx --output /path/to/ar-wechat-archive/output_by_account
```

## Caption images (`scripts/caption_images.py`)

Turns every image into a text 【图注】 embedded in the article markdown. Idempotent — a
re-run only captions new images. Daily sync uses fast concurrent mode for the few new
images; the one-time backfill uses the Message Batches API (~50% cheaper).

```bash
pip install anthropic pillow
export ANTHROPIC_API_KEY=...           # never commit it

python scripts/caption_images.py --root output_by_account --limit 5   # test on 5 images
python scripts/caption_images.py --root output_by_account --batch      # backfill all (~955), cheap
```

Default model is the cost-effective `claude-haiku-4-5` (override with `--model`). Images are
downscaled to ~1024px before sending; the shared instruction is prompt-cached.

