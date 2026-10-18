"""assetsync command line.

  scan       fingerprint in-scope textures in local paks (diagnostics)
  processor  [CI, inside IcarusWiki/Data's pak pipeline] diff one pak against the ledger, export changed PNGs
  finalize   [CI] apply processor output to the ledger and queue a publish plan
  redirects  queue display-name redirects from D_Itemable
  publish    carry out queued plans on the wiki (dry run unless --live)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import ledger as L
from .export import export_textures, hash_exports
from .filepage import mark_historical, new_file_page, redirect_page
from .redirects import load_itemable, plan_redirects, wanted_redirects
from .wiki import MediaWikiClient, MediaWikiError

# Finalizers run from the Data workspace; it tells us where this repo is checked out.
REPO_ROOT = Path(os.environ.get("ICARUS_CONSUMER_REPO_DIR") or Path.cwd())
API_URL = "https://icarus.wiki.gg/api.php"
USER_AGENT = "IcarusWiki-AssetSync/0.1 (https://github.com/IcarusWiki/AssetSync)"
BOT_MIN_WRITE_INTERVAL_SECONDS = 2.0


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return Path(value)


def _build_label(explicit: str | None) -> str:
    return explicit or os.environ.get("ICARUS_BUILD_ID") or dt.date.today().isoformat()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


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
        f"{len(changes.changed)} changed, {len(changes.added)} added, {len(changes.removed)} removed"
    )
    exported = export_textures(
        changes.changed + changes.added,
        ue4export=ue4export,
        paks_dir=pak_file.parent,
        out_dir=artifact / "export",
    )
    _write_json(artifact / "scan.json", scanned)
    _write_json(artifact / "pixels.json", hash_exports(exported))


def cmd_finalize(args: argparse.Namespace) -> None:
    partials = args.partials_dir
    scanned = json.loads((partials / "scan.json").read_text(encoding="utf-8"))
    pixels = json.loads((partials / "pixels.json").read_text(encoding="utf-8"))
    ledger_dir = REPO_ROOT / "ledger"
    ledger = L.Ledger.load(ledger_dir)
    build = _build_label(args.build)
    version = args.version or latest_changelog_version(MediaWikiClient(API_URL, user_agent=USER_AGENT))

    changes = L.diff(ledger, scanned)
    plan = L.plan_update(ledger, scanned, changes, pixels, build=build, version=version)

    queue = REPO_ROOT / "queue" / build
    for item in plan["uploads"] + plan["review"]:
        png = partials / "export" / "Icarus" / "Content" / f"{item['texture']}.png"
        if png.exists():
            item["png"] = f"png/{item['texture']}.png"
            dest = queue / item["png"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(png, dest)
    _write_json(queue / "plan.json", plan)
    (queue / "REPORT.md").write_text(render_report(plan), encoding="utf-8")
    ledger.save(ledger_dir)
    print(
        f"Queued build {build}: {len(plan['uploads'])} uploads, {len(plan['historical'])} removed, "
        f"{len(plan['review'])} for review, {len(plan['hash_only'])} re-cooked without pixel changes"
    )


def cmd_redirects(args: argparse.Namespace) -> None:
    ledger = L.Ledger.load(REPO_ROOT / "ledger")
    found = wanted_redirects(load_itemable(args.data_root))
    plan = plan_redirects(ledger.redirects, found.wanted)
    plan["conflicts"] = found.conflicts
    plan["skipped"] = found.skipped
    queue = REPO_ROOT / "queue" / _build_label(args.build)
    _write_json(queue / "redirects.json", plan)
    print(
        f"{len(found.wanted)} display-name redirects wanted: {len(plan['create'])} to create, "
        f"{len(plan['retarget'])} to retarget, {len(found.conflicts)} conflicts"
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
        args.review_out.write_text("\n".join(review_lines) + "\n", encoding="utf-8")


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
        def mark(title: str, label: str) -> None:
            page = client.fetch_page(title)
            if page.exists:
                client.edit_page(title, mark_historical(page.text, label), summary, nocreate=True)

        for item in plan["historical"]:
            title = f"File:{item['wiki_file']}"
            act(f"mark historical {title}", mark, title, version or plan["build"])
        for item in plan["review"]:
            review.append(f"- [ ] `{item['texture']}` -> `File:{item['wiki_file']}` (build {plan['build']})")

    redirects_path = batch / "redirects.json"
    if redirects_path.exists():
        plan = json.loads(redirects_path.read_text(encoding="utf-8"))
        summary = "Automated display-name redirect for {{Item icon}}"
        for item in plan["create"] + plan["retarget"]:
            title = f"File:{item['redirect']}"
            if act(
                f"redirect {title} -> File:{item['target']}",
                client and client.edit_page,
                title,
                redirect_page(item["target"]),
                summary,
                nocreate=False,
            ):
                ledger.redirects[item["redirect"]] = item["target"]
    return failures


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
        f"| Removed from game (mark historical) | {len(plan['historical'])} |",
        f"| New textures for review | {len(plan['review'])} |",
        "",
    ]
    for title, key in (("Uploads", "uploads"), ("Removed", "historical"), ("For review", "review")):
        if plan[key]:
            lines += [f"## {title}", ""] + [f"- `File:{i['wiki_file']}` ({i['texture']})" for i in plan[key]] + [""]
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

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
