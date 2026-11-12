"""Agreed local category order and compatibility mapping."""

import json
from pathlib import Path


def test_ten_categories_in_display_order():
    """
    Keep the agreed storefront order and the legacy meeting alias explicit.

    This is the offline author-validation copy of the backend categories.
    """
    rows = json.loads(
        (Path(__file__).parents[1] / "schema/categories.json").read_text()
    )["categories"]
    assert [row["id"] for row in rows] == [
        "productivity",
        "clocks",
        "info-feeds",
        "social",
        "dev-monitoring",
        "music",
        "art",
        "games",
        "smart-home",
        "utilities",
    ]
    assert rows[0]["aliases"] == ["meetings"]
