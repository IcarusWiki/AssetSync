"""Display-name redirects: File:ITEM_<DisplayName>.png -> File:<TextureName>.png.

{{Item icon}} (Module:Link icon) looks for ITEM_<display name>.png, while each texture is
uploaded once under its own name. Old redirects are never removed, so a renamed item
keeps working under both names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import SCOPE_PREFIX

ICON_TYPE = "ITEM"
# Characters MediaWiki does not allow in page titles.
INVALID_TITLE_CHARS = set("#<>[]|{}")

_LOCTEXT = re.compile(r'^NSLOCTEXT\(\s*"(?:[^"\\]|\\.)*"\s*,\s*"(?:[^"\\]|\\.)*"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)$')


def display_name(raw: str | None) -> str | None:
    """Text of an NSLOCTEXT value. INVTEXT marks untranslated dev/test items, which are skipped."""
    match = _LOCTEXT.match((raw or "").strip())
    if not match:
        return None
    return re.sub(r"\\(.)", r"\1", match.group(1)).strip() or None


def icon_texture(raw: str | None) -> str | None:
    """'/Game/Assets/2DArt/UI/.../ITEM_X.ITEM_X' -> 'Assets/2DArt/UI/.../ITEM_X' (in scope only)."""
    if not raw or not raw.startswith("/Game/"):
        return None
    path = raw[len("/Game/"):].rsplit(".", 1)[0]
    return path if path.startswith(SCOPE_PREFIX) else None


def normalize_title(title: str) -> str:
    """Compare titles the way MediaWiki does: underscores are spaces, first letter capitalised."""
    title = re.sub(r"[ _]+", " ", title).strip()
    return title[:1].upper() + title[1:]


@dataclass
class RedirectSet:
    wanted: dict[str, str] = field(default_factory=dict)       # redirect file -> target file
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def wanted_redirects(itemable: dict) -> RedirectSet:
    defaults = itemable.get("Defaults", {})
    result = RedirectSet()
    targets: dict[str, set[str]] = {}
    for row in itemable.get("Rows", []):
        name = display_name(row.get("DisplayName", defaults.get("DisplayName")))
        texture = icon_texture(row.get("Icon", defaults.get("Icon")))
        if not name or not texture:
            continue
        if INVALID_TITLE_CHARS & set(name):
            result.skipped.append(f"{row.get('Name')}: {name}")
            continue
        redirect = f"{ICON_TYPE}_{name}.png"
        target = PurePosixPath(texture).name + ".png"
        if normalize_title(redirect) == normalize_title(target):
            continue
        targets.setdefault(redirect, set()).add(target)
    for redirect, files in sorted(targets.items()):
        if len(files) == 1:
            result.wanted[redirect] = files.pop()
        else:
            result.conflicts[redirect] = sorted(files)
    return result


def plan_redirects(known: dict[str, str], wanted: dict[str, str]) -> dict[str, list[dict]]:
    """Redirects to create or retarget; ``known`` (the ledger) is never pruned."""
    by_norm = {normalize_title(k): (k, v) for k, v in known.items()}
    create, retarget = [], []
    for redirect, target in sorted(wanted.items()):
        existing = by_norm.get(normalize_title(redirect))
        if existing is None:
            create.append({"redirect": redirect, "target": target})
        elif normalize_title(existing[1]) != normalize_title(target):
            retarget.append({"redirect": existing[0], "target": target, "previous": existing[1]})
    return {"create": create, "retarget": retarget}


WRITE, DONE, KEEP = "write", "done", "keep"


def redirect_action(exists: bool, current_target: str | None, item: dict) -> str:
    """Decide whether writing a planned redirect is safe.

    Only a missing page, or a redirect still pointing where this tool last pointed it, is
    written. Anything an editor set up differently (another target, or a real file) is kept.
    """
    if not exists:
        return WRITE
    if current_target and normalize_title(current_target) == normalize_title(item["target"]):
        return DONE
    previous = item.get("previous")
    if current_target and previous and normalize_title(current_target) == normalize_title(previous):
        return WRITE
    return KEEP


def load_itemable(data_root: Path) -> dict:
    return json.loads((data_root / "Traits" / "D_Itemable.json").read_text(encoding="utf-8"))
