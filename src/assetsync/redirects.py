"""Display-name redirects: File:<PREFIX>_<DisplayName>.png -> File:<TextureName>.png.

Templates look images up by display name, while each texture is uploaded once under its
own name:

  ITEM_  every D_Itemable row ({{Item icon}} / Module:Link icon)
  ACH_   Steam achievements in D_Accolades (achievement icons)
  CRE_   bestiary creatures: their vestige, else the bestiary image (Module:Link icon)

Old redirects are never removed, so a renamed item keeps working under both names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import SCOPE_PREFIX

ICON_TYPE = "ITEM"
ACHIEVEMENT_TYPE = "ACH"
CREATURE_TYPE = "CRE"
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
    # Names a human has to look at: redirect file -> candidate target files.
    review: dict[str, list[str]] = field(default_factory=dict)

    def update(self, other: RedirectSet) -> None:
        self.wanted.update(other.wanted)
        self.conflicts.update(other.conflicts)
        self.skipped += other.skipped
        self.review.update(other.review)


def _rows(table: dict) -> Iterable[tuple[dict, dict]]:
    defaults = table.get("Defaults", {})
    for row in table.get("Rows", []):
        yield row, defaults


def build_redirects(
    prefix: str,
    entries: Iterable[tuple[str, str | None, str | None]],
    synced: set[str] | None = None,
) -> RedirectSet:
    """Turn (row name, display name, texture) entries into <prefix>_<name>.png redirects.

    With ``synced`` (texture paths known to be on the wiki), a target that isn't there goes
    to review instead of becoming a broken redirect.
    """
    result = RedirectSet()
    targets: dict[str, set[str]] = {}
    for row_name, name, texture in entries:
        if not name or not texture:
            continue
        if INVALID_TITLE_CHARS & set(name):
            result.skipped.append(f"{row_name}: {name}")
            continue
        redirect = f"{prefix}_{name}.png"
        target = PurePosixPath(texture).name + ".png"
        if synced is not None and texture not in synced:
            result.review[redirect] = [target]
            continue
        if normalize_title(redirect) == normalize_title(target):
            continue
        targets.setdefault(redirect, set()).add(target)
    for redirect, files in sorted(targets.items()):
        if len(files) == 1:
            result.wanted[redirect] = files.pop()
        else:
            result.conflicts[redirect] = sorted(files)
    return result


def wanted_redirects(itemable: dict) -> RedirectSet:
    return build_redirects(
        ICON_TYPE,
        (
            (row.get("Name"),
             display_name(row.get("DisplayName", defaults.get("DisplayName"))),
             icon_texture(row.get("Icon", defaults.get("Icon"))))
            for row, defaults in _rows(itemable)
        ),
    )


def achievement_redirects(accolades: dict, synced: set[str]) -> RedirectSet:
    """ACH_<DisplayName>.png for every accolade that is a Steam achievement (not ribbons or medals)."""
    return build_redirects(
        ACHIEVEMENT_TYPE,
        (
            (row.get("Name"),
             display_name(row.get("DisplayName", defaults.get("DisplayName"))),
             icon_texture(row.get("Icon", defaults.get("Icon"))))
            for row, defaults in _rows(accolades)
            if row.get("SteamAchievementId", defaults.get("SteamAchievementId", "None")) not in ("", "None")
        ),
        synced,
    )


CREATURE_TABLES = (
    "AI/D_AISetup",
    "Bestiary/D_BestiaryData",
    "Items/D_ItemRewards",
    "Items/D_ItemTemplate",
    "Items/D_ItemsStatic",
    "Traits/D_Itemable",
)


def _by_name(table: dict) -> dict[str, dict]:
    # Row names are FNames: case-insensitive (D_AISetup asks for AlphaWolf_Head, the row is Alphawolf_Head).
    return {row.get("Name", "").lower(): row for row in table.get("Rows", [])}


def _ref(row: dict, key: str) -> str:
    return (row.get(key) or {}).get("RowName") or "None"


def creature_redirects(tables: dict[str, dict], synced: set[str]) -> RedirectSet:
    """CRE_<CreatureName>.png for every bestiary entry: its vestige, else the bestiary image.

    The vestige is the D_Itemable row called "<CreatureName> Vestige", or else the one
    trophy item every D_AISetup creature in the bestiary group drops
    (AISetup.Trophy -> D_ItemRewards -> D_ItemTemplate -> D_ItemsStatic -> D_Itemable).
    A group whose creatures drop several vestiges (Chicken, Dog, ...) gets the bestiary
    image and goes to review, so Module:Link icon can be given an override.
    """
    itemable = tables["Traits/D_Itemable"]
    item_defaults = itemable.get("Defaults", {})
    items = _by_name(itemable)
    statics = _by_name(tables["Items/D_ItemsStatic"])
    templates = _by_name(tables["Items/D_ItemTemplate"])
    rewards = _by_name(tables["Items/D_ItemRewards"])

    def icon_of(item: dict | None) -> str | None:
        return icon_texture(item.get("Icon", item_defaults.get("Icon"))) if item else None

    by_display: dict[str, set[str]] = {}
    for row, defaults in _rows(itemable):
        name, texture = display_name(row.get("DisplayName", defaults.get("DisplayName"))), icon_of(row)
        if name and texture:
            by_display.setdefault(name.lower(), set()).add(texture)

    def trophy_icon(trophy: str) -> str | None:
        reward = rewards.get(trophy.lower(), {}).get("Rewards") or []
        if not reward:
            return None
        item = _ref(reward[0], "Item")
        static = _ref(templates.get(item.lower(), {}), "ItemStaticData")
        static_row = statics.get((item if static == "None" else static).lower())
        return icon_of(items.get(_ref(static_row, "Itemable").lower())) if static_row else None

    trophies: dict[str, set[str]] = {}
    for row, _ in _rows(tables["AI/D_AISetup"]):
        group, trophy = _ref(row, "BestiaryGroup"), _ref(row, "Trophy")
        if group != "None" and trophy != "None":
            texture = trophy_icon(trophy)
            if texture:
                trophies.setdefault(group.lower(), set()).add(texture)

    def file(texture: str) -> str:
        return PurePosixPath(texture).name + ".png"

    entries, review = [], {}
    for row, defaults in _rows(tables["Bestiary/D_BestiaryData"]):
        name = display_name(row.get("CreatureName", defaults.get("CreatureName")))
        if not name:
            continue
        image = icon_texture(row.get("Image", defaults.get("Image")))
        exact = by_display.get(f"{name} vestige".lower(), set())
        chain = trophies.get(row.get("Name", "").lower(), set())
        redirect = f"{CREATURE_TYPE}_{name}.png"
        if len(exact) == 1 and exact <= synced:
            target = next(iter(exact))
        elif len(exact) <= 1 and len(chain) == 1 and chain <= synced:
            target = next(iter(chain))
        else:
            target = image if image in synced else None
            vestiges = exact | chain
            if len(vestiges) > 1:
                review[redirect] = sorted(file(t) for t in vestiges)
            elif target is None:
                review[redirect] = sorted(file(t) for t in vestiges | ({image} if image else set()))
        entries.append((row.get("Name"), name, target))
    result = build_redirects(CREATURE_TYPE, entries)
    result.review.update(review)
    return result


def all_redirects(data_root: Path, synced: set[str]) -> RedirectSet:
    """Every display-name redirect the game data asks for, across all prefixes."""
    result = wanted_redirects(load_table(data_root, "Traits/D_Itemable"))
    result.update(achievement_redirects(load_table(data_root, "Accolades/D_Accolades"), synced))
    tables = {name: load_table(data_root, name) for name in CREATURE_TABLES}
    result.update(creature_redirects(tables, synced))
    return result


def new_reviews(review: dict[str, list[str]], reported: dict[str, list[str]]) -> dict[str, list[str]]:
    """Review items not yet reported with the same candidates (a changed list is reported again)."""
    return {name: files for name, files in sorted(review.items()) if reported.get(name) != files}


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


def load_table(data_root: Path, name: str) -> dict:
    """``name`` like 'Traits/D_Itemable', relative to InGameFiles."""
    return json.loads((data_root / f"{name}.json").read_text(encoding="utf-8"))


def load_itemable(data_root: Path) -> dict:
    return load_table(data_root, "Traits/D_Itemable")
