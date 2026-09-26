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


def test_v2_adds_phones_through_the_app_only():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    for gone in ("api/pair/qr", "api('api/phones', {method: 'POST'", "/prepare", 'data-m="qr"', 'data-m="code"', 'data-m="addr"'):
        assert gone not in js, gone
    assert "v-prepare" not in html and "v-prepare" not in js
    assert "FastAutomate v2" in js
    assert ">v2<" in html


def test_v2_screens():
    js = (STATIC / "app.js").read_text()
    assert "new LiveView(tile" not in js and "syncTileStreams" not in js  # no video on task tiles
    assert "shot.jpg" in js  # final screenshot on tiles and in history
    for field in ("management_key", "daily_cap_usd"):
        assert field in js
    assert "s-format" not in js and "FORMAT_PRESETS" not in js  # OpenRouter only
    assert "10000" in js  # card thumbnails every 10 s
