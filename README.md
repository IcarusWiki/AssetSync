# AssetSync

Keeps the game images on [icarus.wiki.gg](https://icarus.wiki.gg) in step with Icarus. It covers every
texture under `Assets/2DArt/UI`, which the wiki files under
[Category:Assets/2DArt/UI](https://icarus.wiki.gg/wiki/Category:Assets/2DArt/UI).

## How it works

```
IcarusWiki/Data  (weekly Steam poll)
  ├─ pakchunk0_s20 processor ── assetsync processor
  │     per-file SHA1s from the pak headers → only changed textures are decoded (Ue4Export)
  ├─ assetsync finalizer ── assetsync finalize → commits ledger/ + queue/<build>/ here
  └─ "icarus-updated" dispatch ── redirects.yml → queue/redirects-<date>/
AssetSync
  └─ publish.yml: dry run in the job summary → reviewer approves "wiki-live" → bot edits the wiki
```

- **The ledger** (`ledger/textures.json`) holds, for each texture, its pak fingerprint and a hash of its
  visible pixels, plus the pixel hash of what the wiki currently has. Colour under fully transparent pixels is
  ignored, because exporters disagree about it.
- **Re-cooked but identical** textures (new pak bytes, same pixels) are recorded and not uploaded.
- **Changed** textures are uploaded over `File:<TextureName>.png`. MediaWiki keeps the old version in the file history.
- **New** textures are never uploaded automatically. They're listed in a GitHub issue for review.
- **Removed** textures get `{{Historical content|Removed in [[Changelog:V…]]}}`. Files are never deleted.
- **Display-name redirects** `File:ITEM_<DisplayName>.png` → `File:<TextureName>.png` keep `{{Item icon}}` working.
  Renamed items get a new redirect, and the old one is kept.

New file pages use `== Licensing ==`, `{{License|game}}` and `[[Category:Assets/2DArt/UI/<folder>]]`.

## Setup (repo admins)

- Org secrets: `WIKIGG_BOT_USERNAME`, `WIKIGG_BOT_APP_PASSWORD` (a `Special:BotPasswords` login for the bot account,
  with grants for high-volume editing, editing, creating pages, and uploading/replacing files).
- Environment `wiki-live` with required reviewers.
- In IcarusWiki/Data: a processor for `pakchunk0_s20`, an `assetsync` consumer finalizer, and `AssetSync` in the
  consumer dispatch list.

## Local use

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m assetsync scan "D:\...\Icarus\Content\Paks\pakchunk0_s20-WindowsNoEditor.pak" --out s20.json
.\.venv\Scripts\python -m assetsync publish            # dry run of whatever is queued
```
