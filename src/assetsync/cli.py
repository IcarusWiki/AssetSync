"""assetsync command line.

  scan       fingerprint in-scope textures in local paks (diagnostics)
  processor  [CI, inside IcarusWiki/Data's pak pipeline] diff one pak against the ledger, export changed PNGs
  finalize   [CI] apply processor output to the ledger and queue a publish plan
  redirects  queue display-name redirects (ITEM_, ACH_ and CRE_; see redirects.py)
  publish    carry out queued plans on the wiki (dry run unless --live)
  resolve    record a decision about a texture in review (--upload / --adopt / --ignore)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import ledger as L
from .export import export_textures, hash_exports
from .filepage import mark_historical, new_file_page, redirect_page, redirect_target
from .redirects import (
    KEEP,
    WRITE,
    all_redirects,
    new_reviews,
    plan_redirects,
    redirect_action,
)
from .wiki import MediaWikiClient, MediaWikiError

# Finalizers run from the Data workspace; it tells us where this repo is checked out.
REPO_ROOT = Path(os.environ.get("ICARUS_CONSUMER_REPO_DIR") or Path.cwd())
API_URL = "https://icarus.wiki.gg/api.php"
USER_AGENT = "IcarusWiki-AssetSync/0.1 (https://github.com/IcarusWiki/AssetSync)"
BOT_MIN_WRITE_INTERVAL_SECONDS = 2.0
REDIRECT_PURPOSE = {"ITEM": "{{Item icon}}", "ACH": "achievement icons", "CRE": "creature icons"}


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return Path(value)


def _build_label(explicit: str | None) -> str:
    return explicit or os.environ.get("ICARUS_BUILD_ID") or dt.date.today().isoformat()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def latest_changelog_version(client: MediaWikiClient) -> str | None:
    """Highest V<a.b.c.d> among Changelog: pages, e.g. 'V3.0.26.156677'."""
    info = client.request({"action": "query", "meta": "siteinfo", "siprop": "namespaces"}, post=False)
    ns = next(
        (k for k, v in info["query"]["namespaces"].items() if v.get("canonical", v.get("*")) == "Changelog"),
        None,
    )
    if ns is None:
        return None
    versions, cont = [], {}
    while True:
        data = client.request(
            {"action": "query", "list": "allpages", "apnamespace": ns, "aplimit": "500", **cont},
            post=False,
        )
        for page in data["query"]["allpages"]:
            match = re.search(r":V(\d+(?:\.\d+){3})$", page["title"], re.IGNORECASE)
            if match:
                versions.append(tuple(int(x) for x in match.group(1).split(".")))
        if "continue" not in data:
            break
        cont = data["continue"]
    return "V" + ".".join(map(str, max(versions))) if versions else None


# --- commands ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> None:
    from .manifest import scan  # pyuepak is only needed where paks are read

    paks = sorted(args.paks.glob("*.pak")) if args.paks.is_dir() else [args.paks]
    textures = scan(paks)
    _write_json(args.out, textures)
    print(f"{len(textures)} textures -> {args.out}")


def cmd_processor(args: argparse.Namespace) -> None:
    pak_file = _env_path("ICARUS_PAK_FILE")
    artifact = _env_path("ICARUS_PAK_ARTIFACT_ROOT")
    ue4export = _env_path("UE4EXPORT_EXE")
    from .manifest import scan

    ledger = L.Ledger.load(REPO_ROOT / "ledger")

    scanned = scan([pak_file])
    if not scanned:
        print(f"No in-scope textures in {pak_file.name}; nothing to do.")
        return
    changes = L.diff(ledger, scanned)
    print(
        f"{pak_file.name}: {len(scanned)} textures, {changes.unchanged} unchanged, "
        f"{len(changes.changed)} changed, {len(changes.added)} added, {len(changes.removed)} removed, "
        f"{len(changes.ignored)} ignored assets re-cooked"
    )
    # Written first so the finalizer still gets the scan if the export step fails.
    _write_json(artifact / "scan.json", scanned)
    to_export = changes.to_export
    # Queued PNGs are deleted after a publish, so anything still waiting for an upload
    # (new textures, uploads that failed) is exported again.
    pending = [t for t in L.pending_export(ledger) if t in scanned and t not in to_export]
    if pending:
        print(f"{len(pending)} textures still waiting for an upload are exported again")
    to_export = to_export + pending
    exported = export_textures(
        to_export,
        ue4export=ue4export,
        paks_dir=pak_file.parent,
        out_dir=artifact / "export",
    )
    not_textures = sorted(set(to_export) - set(exported))
    if not_textures:
        print(f"{len(not_textures)} assets produced no image: " + ", ".join(not_textures))
    _write_json(artifact / "not_textures.json", not_textures)
    _write_json(artifact / "pixels.json", hash_exports(exported))


@dataclass
class Partials:
    scanned: dict[str, str]
    pixels: dict[str, str]
    not_textures: set[str]
    export_roots: list[Path]  # each holds Icarus/Content/<texture>.png

    def png(self, texture: str) -> Path | None:
        for root in self.export_roots:
            path = root / "Icarus" / "Content" / f"{texture}.png"
            if path.exists():
                return path
        return None


def load_partials(partials: Path) -> Partials | None:
    """Merge processor output: either one directory, or one subdirectory per pak.

    The Data pipeline stages each pak's artifact as ``<partials>/<pak_slug>/``.
    """
    dirs = [partials] if (partials / "scan.json").exists() else sorted(
        d for d in partials.glob("*/") if (d / "scan.json").exists()
    )
    if not dirs:
        return None

    def read(path: Path, default):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    merged = Partials({}, {}, set(), [])
    for d in dirs:
        merged.scanned.update(read(d / "scan.json", {}))
        merged.pixels.update(read(d / "pixels.json", {}))
        merged.not_textures.update(read(d / "not_textures.json", []))
        merged.export_roots.append(d / "export")
    return merged


def cmd_finalize(args: argparse.Namespace) -> None:
    partials = load_partials(args.partials_dir)
    if partials is None:
        print(f"No processor output (scan.json) under {args.partials_dir}; nothing to finalize.")
        return
    scanned, pixels = partials.scanned, partials.pixels
    ledger_dir = REPO_ROOT / "ledger"
    ledger = L.Ledger.load(ledger_dir)
    build = _build_label(args.build)
    version = args.version or latest_changelog_version(MediaWikiClient(API_URL, user_agent=USER_AGENT))

    changes = L.diff(ledger, scanned)
    plan = L.plan_update(
        ledger, scanned, changes, pixels, build=build, version=version, not_textures=partials.not_textures
    )

    queue = REPO_ROOT / "queue" / build
    for item in plan["uploads"] + plan["new"]:
        png = partials.png(item["texture"])
        if png is not None:
            item["png"] = f"png/{item['texture']}.png"
            dest = queue / item["png"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(png, dest)
    _write_json(queue / "plan.json", plan)
    (queue / "REPORT.md").write_text(render_report(plan), encoding="utf-8")
    ledger.save(ledger_dir)
    print(
        f"Queued build {build}: {len(plan['uploads'])} uploads, {len(plan['new'])} new, "
        f"{len(plan['historical'])} removed, {len(plan['review'])} for review, "
        f"{len(plan['hash_only'])} re-cooked without pixel changes"
    )


def cmd_redirects(args: argparse.Namespace) -> None:
    ledger = L.Ledger.load(REPO_ROOT / "ledger")
    found = all_redirects(args.data_root, L.synced_textures(ledger))
    plan = plan_redirects(ledger.redirects, found.wanted)
    plan["conflicts"] = found.conflicts
    plan["skipped"] = found.skipped
    plan["review"] = new_reviews(found.review, ledger.redirect_review)
    queue = REPO_ROOT / "queue" / _build_label(args.build)
    _write_json(queue / "redirects.json", plan)
    per_prefix: dict[str, int] = {}
    for name in found.wanted:
        prefix = name.split("_", 1)[0]
        per_prefix[prefix] = per_prefix.get(prefix, 0) + 1
    counts = ", ".join(f"{n} {p}_" for p, n in sorted(per_prefix.items()))
    print(
        f"{len(found.wanted)} display-name redirects wanted ({counts}): {len(plan['create'])} to create, "
        f"{len(plan['retarget'])} to retarget, {len(found.conflicts)} conflicts, "
        f"{len(plan['review'])} new for review"
    )


def cmd_publish(args: argparse.Namespace) -> None:
    ledger_dir = REPO_ROOT / "ledger"
    ledger = L.Ledger.load(ledger_dir)
    queue_root = REPO_ROOT / "queue"
    batches = sorted(p for p in queue_root.iterdir() if p.is_dir()) if queue_root.exists() else []
    if not batches:
        print("Queue is empty.")
        return

    client = None
    if args.live:
        client = MediaWikiClient(
            API_URL,
            user_agent=USER_AGENT,
            username=os.environ.get("WIKIGG_USERNAME"),
            password=os.environ.get("WIKIGG_APP_PASSWORD"),
            min_edit_interval_seconds=BOT_MIN_WRITE_INTERVAL_SECONDS,
        )
        client.login()

    review_lines: list[str] = []
    for batch in batches:
        failures = publish_batch(batch, ledger, client, review_lines)
        ledger.save(ledger_dir)  # record progress even if a later batch fails
        if args.live and not failures:
            shutil.rmtree(batch)
        elif failures:
            print(f"{batch.name}: {failures} failures; batch kept in the queue for the next run.")

    if review_lines and args.review_out:
        if any(line.startswith("- [ ] `Assets/") for line in review_lines):
            review_lines = [RESOLVE_HELP, ""] + review_lines
        args.review_out.write_text("\n".join(review_lines) + "\n", encoding="utf-8")


def cmd_resolve(args: argparse.Namespace) -> None:
    """Record a human decision about a texture in review."""
    ledger_dir = REPO_ROOT / "ledger"
    ledger = L.Ledger.load(ledger_dir)
    texture = args.texture.removeprefix("Icarus/Content/").removeprefix("/Game/")
    record = ledger.textures.get(texture)
    if record is None:
        sys.exit(f"Not in the ledger: {texture}")
    if args.ignore:
        record.status, record.review_reason = L.IGNORED, None
        print(f"{texture}: ignored, never uploaded")
    else:
        name = args.as_name or record.wiki_file or L.wiki_file_name(texture)
        clash = [
            t for t, r in ledger.textures.items()
            if t != texture and r.status == L.SYNCED and L.title_key(L.target_name(t, r)) == L.title_key(name)
        ]
        if clash:
            sys.exit(f"File:{name} already belongs to {clash[0]}; pick another name with --as")
        record.status, record.review_reason, record.wiki_file = L.SYNCED, None, name
        if args.upload:
            record.wiki_pixel_sha256 = None  # differs from the game, so the next run uploads it
            print(f"{texture}: will be uploaded as File:{name} on the next run, replacing what is there")
        else:
            record.wiki_pixel_sha256 = record.pixel_sha256
            print(f"{texture}: File:{name} on the wiki is taken as is; later game changes are uploaded")
    ledger.save(ledger_dir)


def publish_batch(batch: Path, ledger: L.Ledger, client: MediaWikiClient | None, review: list[str]) -> int:
    live = client is not None
    failures = 0

    def act(label: str, func, *args, **kwargs) -> bool:
        nonlocal failures
        print(("" if live else "[dry run] ") + label)
        if not live:
            return False
        try:
            func(*args, **kwargs)
            return True
        except MediaWikiError as exc:
            failures += 1
            print(f"  FAILED: {exc}")
            return False

    plan_path = batch / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        version = plan.get("version")
        changelog = f"[[Changelog:{version}]]" if version else f"build {plan['build']}"
        summary = f"Automated asset update - {changelog} (build {plan['build']})"
        for item in plan["uploads"]:
            if "png" not in item:
                continue
            if act(
                f"upload File:{item['wiki_file']}",
                client and client.upload_file,
                item["wiki_file"],
                (batch / item["png"]).read_bytes(),
                comment=summary,
                text=new_file_page(item["category"]),
            ):
                record = ledger.textures[item["texture"]]
                record.wiki_file = item["wiki_file"]
                record.wiki_pixel_sha256 = item["pixel_sha256"]
        for item in plan.get("new", []):
            record = ledger.textures.get(item["texture"])
            if "png" not in item or record is None or not L.is_pending_new(record):
                continue  # no image this run, or already handled (by an earlier batch or `resolve`)
            if not live:
                # Read-only preview of the live guard, so the approver sees what will be held back.
                taken = MediaWikiClient(API_URL, user_agent=USER_AGENT).page_exists(f"File:{item['wiki_file']}")
                note = " (already exists on the wiki: will go to review, not overwritten)" if taken else ""
                print(f"[dry run] upload (new) File:{item['wiki_file']}{note}")
                continue
            print(f"upload (new) File:{item['wiki_file']}")
            try:
                reason = upload_new(client, ledger, item, (batch / item["png"]).read_bytes(), summary)
            except MediaWikiError as exc:
                failures += 1
                print(f"  FAILED: {exc}")
                continue
            if reason:
                print(f"  not uploaded: {reason}")
                record.review_reason = reason
                review.append(review_line(item["texture"], item["wiki_file"], plan["build"], reason))
            else:
                record.status = L.SYNCED
                record.wiki_file = item["wiki_file"]
                record.wiki_pixel_sha256 = item["pixel_sha256"]

        def mark(title: str, label: str) -> None:
            page = client.fetch_page(title)
            if page.exists:
                client.edit_page(title, mark_historical(page.text, label), summary, nocreate=True)

        for item in plan["historical"]:
            title = f"File:{item['wiki_file']}"
            act(f"mark historical {title}", mark, title, version or plan["build"])
        for item in plan["review"] + plan.get("name_conflicts", []):
            reason = item.get("reason") or "shares its wiki file name with another synced texture"
            review.append(review_line(item["texture"], item["wiki_file"], plan["build"], reason))

    redirects_path = batch / "redirects.json"
    if redirects_path.exists():
        plan = json.loads(redirects_path.read_text(encoding="utf-8"))
        reader = client or MediaWikiClient(API_URL, user_agent=USER_AGENT)
        for name, candidates in plan.get("review", {}).items():
            options = ", ".join(f"`File:{c}`" for c in candidates)
            review.append(f"- [ ] redirect `File:{name}`: candidates {options} (set it up by hand)")
            if live:
                ledger.redirect_review[name] = candidates
        for item in plan["create"] + plan["retarget"]:
            title = f"File:{item['redirect']}"
            purpose = REDIRECT_PURPOSE.get(item["redirect"].split("_", 1)[0])
            summary = "Automated display-name redirect" + (f" for {purpose}" if purpose else "")
            page = reader.fetch_page(title)
            decision = redirect_action(page.exists, redirect_target(page.text), item)
            if decision == KEEP:
                print(f"keep {title}: already exists and was set up differently by an editor")
            elif decision == WRITE and not act(
                f"redirect {title} -> File:{item['target']}",
                client and client.edit_page,
                title,
                redirect_page(item["target"]),
                summary,
                nocreate=False,
            ):
                continue
            # Remember the game's target even when an editor's choice is kept, so it isn't re-planned.
            ledger.redirects[item["redirect"]] = item["target"]
            if not client:
                time.sleep(0.2)  # anonymous dry-run reads: stay polite
    return failures


RESOLVE_HELP = (
    "Resolve a texture with `python -m assetsync resolve <texture> --upload [--as NAME.png] | --adopt | --ignore` "
    "and commit `ledger/`."
)


def review_line(texture: str, wiki_file: str, build: str, reason: str) -> str:
    return f"- [ ] `{texture}` -> `File:{wiki_file}` (build {build}): {reason}"


# Upload warnings that mean "a human should look first". `duplicate` is handled separately.
BLOCKING_WARNINGS = {
    "exists": "`File:{}` already exists on the wiki",
    "exists-normalized": "a file with a similar name exists: `File:{}`",
    "was-deleted": "a file with this name was deleted before",
    "filewasdeleted": "a file with this name was deleted before",
    "duplicate-archive": "the same image was uploaded and deleted before as `File:{}`",
    "page-exists": "a non-file page already uses this name",
    "badfilename": "MediaWiki changed the name to `{}`",
}


def upload_new(client: MediaWikiClient, ledger: L.Ledger, item: dict, data: bytes, summary: str) -> str | None:
    """Upload a new texture only if the wiki has nothing in its way.

    Returns None when uploaded, else the reason it was left for review.
    """
    kwargs = {"comment": summary, "text": new_file_page(item["category"])}
    result = client.upload_file(item["wiki_file"], data, ignore_warnings=False, **kwargs)
    if result.get("result") == "Success":
        return None
    warnings = dict(result.get("warnings") or {})
    duplicates = warnings.pop("duplicate", None) or []
    if isinstance(duplicates, str):
        duplicates = [duplicates]
    reasons = []
    for key, value in warnings.items():
        template = BLOCKING_WARNINGS.get(key, f"MediaWiki warning `{key}`" + (": {}" if value else ""))
        reasons.append(template.format(value if isinstance(value, str) else ", ".join(map(str, value))))
    # The same pixels are already on the wiki under another name. Fine when that file is
    # another game texture (name = texture name, so both should exist); otherwise someone
    # uploaded this image by hand and should decide.
    ours = {L.title_key(L.target_name(t, r)) for t, r in ledger.textures.items() if r.status == L.SYNCED}
    foreign = [d for d in duplicates if L.title_key(d) not in ours]
    if foreign:
        reasons.append("the same image is already on the wiki as " + ", ".join(f"`File:{d}`" for d in foreign))
    if reasons:
        return "; ".join(reasons)
    client.upload_file(item["wiki_file"], data, ignore_warnings=True, **kwargs)
    return None


def render_report(plan: dict) -> str:
    lines = [
        f"# Asset update for build {plan['build']}",
        "",
        f"Changelog version: **{plan.get('version') or 'unknown'}** (confirm before approving).",
        "",
        "| | Count |",
        "|---|---|",
        f"| Unchanged | {plan['unchanged']} |",
        f"| Re-cooked, pixels identical (no upload) | {len(plan['hash_only'])} |",
        f"| **Uploads** (pixels changed) | {len(plan['uploads'])} |",
        f"| **New textures** (uploaded unless the name is taken on the wiki) | {len(plan.get('new', []))} |",
        f"| Removed from game (mark historical) | {len(plan['historical'])} |",
        f"| For review (wiki name clash) | {len(plan['review']) + len(plan.get('name_conflicts', []))} |",
        f"| New assets with no image (ignored) | {len(plan.get('not_textures', []))} |",
        f"| Ignored assets re-cooked (not exported) | {plan.get('ignored_recooked', 0)} |",
        "",
    ]
    sections = (("Uploads", "uploads"), ("New textures", "new"), ("Removed", "historical"),
                ("For review", "review"), ("Name conflicts (not uploaded)", "name_conflicts"))
    for title, key in sections:
        if plan.get(key):
            lines += [f"## {title}", ""]
            lines += [
                f"- `File:{i['wiki_file']}` ({i['texture']})" + (f": {i['reason']}" if i.get("reason") else "")
                for i in plan[key]
            ] + [""]
    if plan.get("not_textures"):
        lines += ["## Ignored: no image exported (materials, widgets, ...)", ""]
        lines += [f"- `{t}`" for t in plan["not_textures"]] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="assetsync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan")
    p.add_argument("paks", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("processor")
    p.set_defaults(func=cmd_processor)

    p = sub.add_parser("finalize")
    p.add_argument("--partials-dir", type=Path, required=True)
    p.add_argument("--build")
    p.add_argument("--version", help="Changelog version label, e.g. V3.0.26.156677")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("redirects")
    p.add_argument("--data-root", type=Path, required=True, help="InGameFiles directory")
    p.add_argument("--build")
    p.set_defaults(func=cmd_redirects)

    p = sub.add_parser("publish")
    p.add_argument("--live", action="store_true", help="Edit the wiki (default is a dry run)")
    p.add_argument("--review-out", type=Path, help="Write the review checklist here (for a GitHub issue)")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("resolve", help="Decide what happens to a texture in review")
    p.add_argument("texture", help="e.g. Assets/2DArt/UI/Icons/MapIcons/T_Icon_Map_NPC_Farmer")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--upload", action="store_true",
                        help="Upload it on the next run, overwriting any wiki file of that name")
    action.add_argument("--adopt", action="store_true",
                        help="The wiki file already is this texture; keep it and sync future changes")
    action.add_argument("--ignore", action="store_true", help="Never upload it")
    p.add_argument("--as", dest="as_name", metavar="NAME.png",
                   help="Wiki file name to use instead of the texture name (for name clashes)")
    p.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
