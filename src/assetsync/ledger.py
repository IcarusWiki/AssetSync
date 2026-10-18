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
REVIEW = "review"      # in the game but not (knowingly) on the wiki; needs a human decision
REMOVED = "removed"    # gone from the game; wiki file kept and marked historical
IGNORED = "ignored"    # reviewed and deliberately not uploaded


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


@dataclass
class Ledger:
    textures: dict[str, TextureRecord] = field(default_factory=dict)
    redirects: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Ledger:
        def read(name: str) -> dict:
            path = root / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

        return cls(
            textures={k: TextureRecord(**v) for k, v in read("textures.json").items()},
            redirects=read("redirects.json"),
            state=read("state.json"),
        )

    def save(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)

        def write(name: str, data: dict) -> None:
            text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
            (root / name).write_text(text, encoding="utf-8", newline="\n")

        write("textures.json", {k: asdict(v) for k, v in self.textures.items()})
        write("redirects.json", self.redirects)
        write("state.json", self.state)


def wiki_file_name(texture: str) -> str:
    return PurePosixPath(texture).name + ".png"


def category_for(texture: str) -> str:
    return str(PurePosixPath(texture).parent)


@dataclass
class Changes:
    changed: list[str] = field(default_factory=list)    # pak fingerprint differs
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0


def diff(ledger: Ledger, scanned: dict[str, str]) -> Changes:
    changes = Changes()
    for texture, pak_sha1 in scanned.items():
        record = ledger.textures.get(texture)
        if record is None or record.status == REMOVED:
            changes.added.append(texture)
        elif record.pak_sha1 != pak_sha1:
            changes.changed.append(texture)
        else:
            changes.unchanged += 1
    for texture, record in ledger.textures.items():
        if texture not in scanned and record.status != REMOVED:
            changes.removed.append(texture)
    for bucket in (changes.changed, changes.added, changes.removed):
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
) -> dict:
    """Apply a scan to the ledger and return the publish plan.

    ``pixels`` holds decoded pixel hashes for every changed or added texture.
    """
    hash_only, review, historical = [], [], []
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
        ledger.textures[texture] = TextureRecord(
            pak_sha1=scanned[texture],
            pixel_sha256=pixels.get(texture),
            wiki_file=previous.wiki_file if previous else None,
            wiki_pixel_sha256=previous.wiki_pixel_sha256 if previous else None,
            status=REVIEW,
            first_seen=previous.first_seen if previous else build,
            last_changed=build,
        )
        review.append(texture)
    for texture in changes.removed:
        record = ledger.textures[texture]
        record.status = REMOVED
        record.removed_in = version or build
        if record.wiki_file:
            historical.append(texture)

    uploads = [
        texture
        for texture, record in sorted(ledger.textures.items())
        if record.status == SYNCED and record.pixel_sha256 and record.pixel_sha256 != record.wiki_pixel_sha256
    ]
    ledger.state.update({"build": build, "version": version or ""})

    def entry(texture: str) -> dict:
        record = ledger.textures[texture]
        return {
            "texture": texture,
            "wiki_file": record.wiki_file or wiki_file_name(texture),
            "category": category_for(texture),
            "pixel_sha256": record.pixel_sha256,
        }

    return {
        "build": build,
        "version": version,
        "uploads": [entry(t) for t in uploads],
        "historical": [entry(t) for t in historical],
        "review": [entry(t) for t in review],
        "hash_only": hash_only,
        "unchanged": changes.unchanged,
    }
