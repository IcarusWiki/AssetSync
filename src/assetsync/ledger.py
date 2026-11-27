"""The git-tracked inventory of every in-scope texture and what the wiki holds for it.

``pixel_sha256`` describes the game; ``wiki_pixel_sha256`` describes the file last
uploaded. A synced texture whose two hashes differ needs an upload, so a failed publish
is simply retried on the next run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

SYNCED = "synced"      # on the wiki under its texture name; kept up to date
# In the game but not on the wiki yet. Without a review_reason it is uploaded on the next
# publish if the wiki has nothing under its name; with one, a human decides (`resolve`).
REVIEW = "review"
REMOVED = "removed"    # gone from the game; wiki file kept and marked historical
IGNORED = "ignored"    # not a texture, or reviewed and deliberately not uploaded


@dataclass
class TextureRecord:
    pak_sha1: str
    pixel_sha256: str | None = None
    wiki_file: str | None = None
    wiki_pixel_sha256: str | None = None
    status: str = REVIEW
    first_seen: str = ""
    last_changed: str = ""
    removed_in: str | None = None
    review_reason: str | None = None  # why a human has to decide; None = upload automatically


@dataclass
class Ledger:
    textures: dict[str, TextureRecord] = field(default_factory=dict)
    redirects: dict[str, str] = field(default_factory=dict)
    # Redirect names already sent to review, with the candidates they were reported with.
    redirect_review: dict[str, list[str]] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Ledger:
        def read(name: str) -> dict:
            path = root / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

        return cls(
            textures={k: TextureRecord(**v) for k, v in read("textures.json").items()},
            redirects=read("redirects.json"),
            redirect_review=read("redirect_review.json"),
            state=read("state.json"),
        )

    def save(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)

        def write(name: str, data: dict) -> None:
            text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
            (root / name).write_text(text, encoding="utf-8", newline="\n")

        def record(r: TextureRecord) -> dict:
            data = asdict(r)
            if data["review_reason"] is None:
                del data["review_reason"]  # keep the 7,800-row file free of an always-empty field
            return data

        write("textures.json", {k: record(v) for k, v in self.textures.items()})
        write("redirects.json", self.redirects)
        write("redirect_review.json", self.redirect_review)
        write("state.json", self.state)


def synced_textures(ledger: Ledger) -> set[str]:
    """Texture paths whose file is on the wiki under its texture name."""
    return {t for t, r in ledger.textures.items() if r.status == SYNCED and r.wiki_file}


def wiki_file_name(texture: str) -> str:
    return PurePosixPath(texture).name + ".png"


def title_key(name: str) -> str:
    """How MediaWiki sees a file name: underscores are spaces, the first letter is capitalised."""
    name = " ".join(name.replace("_", " ").split())
    return name[:1].upper() + name[1:]


def target_name(texture: str, record: TextureRecord) -> str:
    return record.wiki_file or wiki_file_name(texture)


def is_pending_new(record: TextureRecord) -> bool:
    return record.status == REVIEW and not record.review_reason


def pending_export(ledger: Ledger) -> list[str]:
    """Textures whose PNG a publish still needs although the pak may not have changed:
    new ones waiting for their first upload, and synced ones whose upload never landed."""
    return sorted(
        t
        for t, r in ledger.textures.items()
        if is_pending_new(r)
        or (r.status == SYNCED and r.pixel_sha256 and r.pixel_sha256 != r.wiki_pixel_sha256)
    )


def category_for(texture: str) -> str:
    return str(PurePosixPath(texture).parent)


@dataclass
class Changes:
    changed: list[str] = field(default_factory=list)    # pak fingerprint differs
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Ignored assets (materials, widgets, ...) whose pak fingerprint differs. They are
    # never exported: many aren't textures at all, and Ue4Export fails on those.
    ignored: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def to_export(self) -> list[str]:
        return self.changed + self.added


def diff(ledger: Ledger, scanned: dict[str, str]) -> Changes:
    changes = Changes()
    for texture, pak_sha1 in scanned.items():
        record = ledger.textures.get(texture)
        if record is None or record.status == REMOVED:
            changes.added.append(texture)
        elif record.pak_sha1 != pak_sha1:
            (changes.ignored if record.status == IGNORED else changes.changed).append(texture)
        else:
            changes.unchanged += 1
    for texture, record in ledger.textures.items():
        if texture not in scanned and record.status != REMOVED:
            changes.removed.append(texture)
    for bucket in (changes.changed, changes.added, changes.removed, changes.ignored):
        bucket.sort()
    return changes


def plan_update(
    ledger: Ledger,
    scanned: dict[str, str],
    changes: Changes,
    pixels: dict[str, str],
    *,
    build: str,
    version: str | None,
    not_textures: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Apply a scan to the ledger and return the publish plan.

    ``pixels`` holds decoded pixel hashes for every exported texture (changed, added, and
    those in ``pending_export``). ``not_textures`` lists exported assets that produced no
    image; new ones are ignored.

    New textures go to ``new`` and are uploaded unless the wiki already has a file by that
    name (checked at publish time). Two textures that would share a wiki file name go to
    ``review`` instead.
    """
    hash_only, historical, ignored_new = [], [], []
    for texture in changes.ignored:
        ledger.textures[texture].pak_sha1 = scanned[texture]
    for texture in changes.changed:
        record = ledger.textures[texture]
        record.pak_sha1 = scanned[texture]
        new_pixels = pixels.get(texture)
        if new_pixels is None:
            continue  # export failed; keep the old pixel hash so the change is retried
        if new_pixels == record.pixel_sha256:
            hash_only.append(texture)
        else:
            record.pixel_sha256 = new_pixels
            record.last_changed = build
    for texture in changes.added:
        previous = ledger.textures.get(texture)
        not_texture = texture in not_textures and texture not in pixels
        ledger.textures[texture] = TextureRecord(
            pak_sha1=scanned[texture],
            pixel_sha256=pixels.get(texture),
            wiki_file=previous.wiki_file if previous else None,
            wiki_pixel_sha256=previous.wiki_pixel_sha256 if previous else None,
            status=IGNORED if not_texture else REVIEW,
            first_seen=previous.first_seen if previous else build,
            last_changed=build,
        )
        if not_texture:
            ignored_new.append(texture)
    for texture in changes.removed:
        record = ledger.textures[texture]
        record.status = REMOVED
        record.removed_in = version or build
        if record.wiki_file:
            historical.append(texture)

    # Pending new textures re-exported this run (their pak may be unchanged).
    for texture, record in ledger.textures.items():
        if is_pending_new(record) and pixels.get(texture):
            record.pixel_sha256 = pixels[texture]

    # Who holds, or is about to take, each wiki file name.
    claims: dict[str, list[str]] = {}
    for texture, record in sorted(ledger.textures.items()):
        if record.status == SYNCED or is_pending_new(record):
            claims.setdefault(title_key(target_name(texture, record)), []).append(texture)

    review, new = [], []
    for texture, record in sorted(ledger.textures.items()):
        if not is_pending_new(record):
            continue
        others = [t for t in claims[title_key(target_name(texture, record))] if t != texture]
        if others:
            record.review_reason = "name clash with " + ", ".join(f"`{t}`" for t in others)
            review.append(texture)
        elif record.pixel_sha256:
            new.append(texture)

    # Two synced records on one wiki file would overwrite each other; upload neither.
    uploads, name_conflicts = [], []
    for texture, record in sorted(ledger.textures.items()):
        if record.status == SYNCED and record.pixel_sha256 and record.pixel_sha256 != record.wiki_pixel_sha256:
            synced_owners = [
                t for t in claims[title_key(target_name(texture, record))] if ledger.textures[t].status == SYNCED
            ]
            (name_conflicts if len(synced_owners) > 1 else uploads).append(texture)
    ledger.state.update({"build": build, "version": version or ""})

    def entry(texture: str) -> dict:
        record = ledger.textures[texture]
        item = {
            "texture": texture,
            "wiki_file": target_name(texture, record),
            "category": category_for(texture),
            "pixel_sha256": record.pixel_sha256,
        }
        if record.review_reason:
            item["reason"] = record.review_reason
        return item

    return {
        "build": build,
        "version": version,
        "uploads": [entry(t) for t in uploads],
        "new": [entry(t) for t in new],
        "historical": [entry(t) for t in historical],
        "review": [entry(t) for t in review],
        "name_conflicts": [entry(t) for t in name_conflicts],
        "hash_only": hash_only,
        "not_textures": ignored_new,
        "ignored_recooked": len(changes.ignored),
        "unchanged": changes.unchanged,
    }
