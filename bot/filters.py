"""Title-based filters for subscription downloads.

Two complementary filters per subscription:
- white_filter: comma-separated list of substrings. If non-empty, a video
  title MUST contain at least one of these substrings (case-insensitive)
  to be downloaded. Empty = no whitelist restriction.
- black_filter: comma-separated list of substrings. If a video title
  contains ANY of these substrings (case-insensitive), it is NEVER
  downloaded. Empty = no blacklist restriction.

Both filters can be combined: white restricts what's eligible, black
removes specific items from the eligible set.

Each filter is a simple comma-separated list. Whitespace around each
item is stripped. Empty items are ignored. Matching is case-insensitive
substring containment (Python `in` operator).

Examples:
    white_filter = "tutorial,обзор,распаковка"
    → only videos whose title contains "tutorial" OR "обзор" OR "распаковка"

    black_filter = "shorts,short,премьера"
    → skip any video whose title contains "shorts", "short", or "премьера"

    white_filter = "" (empty)
    → no whitelist; all videos pass this filter

    black_filter = "" (empty)
    → no blacklist; no videos blocked by this filter
"""

from __future__ import annotations

from typing import Iterable


def parse_filter(raw: str) -> list[str]:
    """Parse a comma-separated filter string into a list of lowercased substrings.

    Empty items are dropped. Whitespace is stripped from each item.
    """
    if not raw:
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def matches_white_list(title: str, white_filter: str) -> bool:
    """Return True if the title passes the white list.

    - If white_filter is empty → all titles pass (no restriction).
    - Otherwise → title must contain at least one of the filter substrings
      (case-insensitive).
    """
    items = parse_filter(white_filter)
    if not items:
        return True
    title_lower = title.lower()
    return any(item in title_lower for item in items)


def matches_black_list(title: str, black_filter: str) -> bool:
    """Return True if the title is BLOCKED by the black list.

    - If black_filter is empty → no titles are blocked.
    - Otherwise → True if title contains ANY of the filter substrings
      (case-insensitive). The caller should skip such videos.
    """
    items = parse_filter(black_filter)
    if not items:
        return False
    title_lower = title.lower()
    return any(item in title_lower for item in items)


def should_download(title: str, white_filter: str, black_filter: str) -> bool:
    """Combined check: True if the video should be downloaded under both filters.

    Rule:
      - Must pass white list (must contain at least one whitelisted substring,
        unless white list is empty).
      - Must NOT be on black list (must not contain any blacklisted substring).
    """
    if not matches_white_list(title, white_filter):
        return False
    if matches_black_list(title, black_filter):
        return False
    return True


def format_filter_for_display(raw: str, max_items: int = 5) -> str:
    """Render a filter string for display in Telegram messages.

    Returns:
        '—' if the filter is empty (disabled).
        Otherwise: comma-separated list of items (truncated to max_items + '…').
    """
    items = parse_filter(raw)
    if not items:
        return "—"
    if len(items) > max_items:
        return ", ".join(items[:max_items]) + ", …"
    return ", ".join(items)
