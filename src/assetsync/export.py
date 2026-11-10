"""Decode textures to PNG with Ue4Export (CrystalFerrai/Ue4Export, CUE4Parse-based)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import CONTENT_PREFIX
from .pixels import pixel_hash

ENGINE_VERSION = "UE4_27"
# Ue4Export exits 2 when it ran but "one or more assets failed to export" (e.g. a
# material listed as a texture). The PNGs it did write are fine.
PARTIAL_FAILURE = 2


def export_textures(
    textures: list[str],
    *,
    ue4export: Path,
    paks_dir: Path,
    out_dir: Path,
) -> dict[str, Path]:
    """Export ``textures`` (content-relative paths) and return the PNG written for each.

    Assets that produce no PNG are simply missing from the result.
    """
    if not textures:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    # Ue4Export clears its output directory first, so the list must live beside it.
    asset_list = out_dir.with_name(out_dir.name + "-assetlist.txt")
    asset_list.write_text(
        "[Texture]\n" + "".join(f"{CONTENT_PREFIX}{t}\n" for t in textures), encoding="utf-8"
    )
    command = [str(ue4export), "--quiet", str(paks_dir), ENGINE_VERSION, str(asset_list), str(out_dir)]
    result = subprocess.run(command)
    if result.returncode not in (0, PARTIAL_FAILURE):
        raise subprocess.CalledProcessError(result.returncode, command)
    exported = {}
    for texture in textures:
        png = out_dir / f"{CONTENT_PREFIX}{texture}.png"
        if png.exists():
            exported[texture] = png
    return exported


def hash_exports(exported: dict[str, Path]) -> dict[str, str]:
    return {texture: pixel_hash(png) for texture, png in exported.items()}
