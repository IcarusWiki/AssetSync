"""Wikitext for asset file pages, matching the conventions already used on icarus.wiki.gg."""

from __future__ import annotations

import re

REDIRECT_CATEGORY = "Image Redirects"


def new_file_page(category: str) -> str:
    return f"== Licensing ==\n{{{{License|game}}}}\n\n[[Category:{category}]]"


def _category_pattern(category: str) -> re.Pattern[str]:
    # MediaWiki treats spaces and underscores in titles as the same character.
    words = [re.escape(w) for w in re.split(r"[ _]+", category.strip())]
    return re.compile(r"\[\[\s*Category\s*:\s*" + r"[ _]+".join(words) + r"\s*(\|[^\]]*)?\]\]", re.IGNORECASE)


def has_category(text: str, category: str) -> bool:
    return bool(_category_pattern(category).search(text))


def ensure_category(text: str, category: str) -> str:
    if has_category(text, category):
        return text
    return text.rstrip() + f"\n[[Category:{category}]]"


def mark_historical(text: str, version: str) -> str:
    """Add {{Historical content}} to the top of a file page, once."""
    if re.search(r"\{\{\s*Historical[ _]content\b", text, re.IGNORECASE):
        return text
    label = version if version.upper().startswith("V") else f"V{version}"
    template = f"{{{{Historical content|Removed in [[Changelog:{label}]]}}}}"
    return f"{template}\n{text.lstrip()}"


def redirect_page(target_file: str) -> str:
    return f"#REDIRECT [[File:{target_file}]]\n[[Category:{REDIRECT_CATEGORY}]]"


def redirect_target(text: str) -> str | None:
    match = re.match(r"\s*#REDIRECT\s*\[\[\s*(?:File|Image)\s*:\s*([^\]|#]+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None
