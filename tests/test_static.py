"""Cheap guards for the single-page UI: every button id in the page is wired in app.js."""

import re
from pathlib import Path

STATIC = Path(__file__).parents[1] / "mobile_rpa" / "static"


def test_every_button_id_is_used_by_the_script():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    ids = re.findall(r'<button[^>]*\bid="([^"]+)"', html)
    assert ids, "no buttons found"
    unused = [i for i in ids if f"'{i}'" not in js and f'"{i}"' not in js]
    assert not unused, f"buttons with no handler: {unused}"
