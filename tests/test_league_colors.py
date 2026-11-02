"""Champion color tests: extraction on synthetic images, overrides, caching, offline fallback.

No network (requests.get is faked) and nothing is written outside temp dirs.
Run: python tests/test_league_colors.py   (plain asserts, no pytest needed)
"""
import colorsys
import io
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from rgbsync.league import colors  # noqa: E402
from rgbsync.league.colors import (  # noqa: E402
    DEFAULT_PALETTE, ChampionColors, from_hex, palette_from_image, to_hex)


def hsv(color):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in color))
    return h * 360, s, v


def hue_near(color, target, tol=25):
    d = abs(hsv(color)[0] - target) % 360
    return min(d, 360 - d) <= tol


def solid(color, size=(80, 80)):
    return Image.new("RGB", size, color)


def pink_with_blue() -> Image.Image:
    img = solid((230, 60, 150))
    for x in range(60, 80):                # 25% blue accent
        for y in range(80):
            img.putpixel((x, y), (40, 90, 230))
    return img


def test_palette_pink_and_blue() -> None:
    primary, secondary = palette_from_image(pink_with_blue())
    assert hue_near(primary, 330, 20), primary         # pink / magenta
    assert hue_near(secondary, 220, 20), secondary     # blue
    for c in (primary, secondary):
        _h, s, v = hsv(c)
        assert s >= 0.69 and v >= 0.89, c               # LED friendly


def test_palette_ignores_black_white_and_grey() -> None:
    img = solid((10, 10, 10), (100, 100))
    for x in range(100):
        for y in range(100):
            if x < 30:
                img.putpixel((x, y), (250, 250, 250))      # white
            elif x < 55:
                img.putpixel((x, y), (120, 120, 125))      # grey
            elif x < 70:
                img.putpixel((x, y), (30, 200, 60))        # the only vivid color: green
    primary, _secondary = palette_from_image(img)
    assert hue_near(primary, 130, 15), primary


def test_palette_dark_image_is_boosted() -> None:
    primary, secondary = palette_from_image(solid((25, 8, 45)))
    assert hue_near(primary, 270, 25), primary
    assert hsv(primary)[2] >= 0.89 and hsv(primary)[1] >= 0.69
    assert hsv(secondary)[2] >= 0.89


def test_palette_no_color_returns_default() -> None:
    assert palette_from_image(solid((128, 128, 128))) == DEFAULT_PALETTE
    assert palette_from_image(solid((0, 0, 0))) == DEFAULT_PALETTE


def test_palette_single_color_gets_a_distinct_secondary() -> None:
    primary, secondary = palette_from_image(solid((200, 30, 30)))
    assert hue_near(primary, 0, 15)
    assert secondary != primary


# ---- fake network -------------------------------------------------------

def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Resp:
    def __init__(self, status=200, body=None, content=b"") -> None:
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeNet:
    """Replaces requests.get inside rgbsync.league.colors."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.down = False
        self.missing_skins: set[str] = set()   # image urls that 404
        self.pink = png_bytes(pink_with_blue())
        self.green = png_bytes(solid((40, 210, 90)))

    def __call__(self, url, **kw):
        self.calls.append(url)
        if self.down:
            raise ConnectionError("network down")
        if url.endswith("/api/versions.json"):
            return Resp(body=["99.1.1", "99.0.9"])
        if url.endswith("/data/en_US/champion.json"):
            assert "/cdn/99.1.1/" in url
            return Resp(body={"data": {"Ahri": {"key": "103", "id": "Ahri"},
                                       "Fiddlesticks": {"key": "9", "id": "Fiddlesticks"},
                                       "Jinx": {"key": "222", "id": "Jinx"}}})
        if "/img/champion/loading/" in url:
            if url in self.missing_skins:
                return Resp(404)
            return Resp(content=self.green if "Jinx" in url else self.pink)
        raise AssertionError(f"unexpected url {url}")


def with_net(net, fn):
    real = colors.requests.get
    colors.requests.get = net
    try:
        fn()
    finally:
        colors.requests.get = real


def test_full_path_cache_and_disk() -> None:
    net = FakeNet()

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cc = ChampionColors(tmp)
            assert cc.peek("Ahri", 0) is None            # nothing cached yet, no network on peek
            assert net.calls == []
            primary, secondary = cc.get("Ahri", 0)
            assert hue_near(primary, 330, 20) and hue_near(secondary, 220, 20)
            n = len(net.calls)
            assert cc.get("Ahri", 0) == (primary, secondary) and len(net.calls) == n  # cached
            assert cc.peek("Ahri", 0) == (primary, secondary)
            saved = json.loads((pathlib.Path(tmp) / "champion_colors.json").read_text("utf-8"))
            assert saved["Ahri_0"]["primary"] == to_hex(primary)
            # numeric key resolves through champion.json (cached per version)
            assert cc.get(222, 0)[0] != primary and hue_near(cc.get(222, 0)[0], 130, 20)
            assert (pathlib.Path(tmp) / "champion_ids_99.1.1.json").exists()
            # a fresh instance works offline from disk
            net.down = True
            cc2 = ChampionColors(tmp)
            assert cc2.peek("Ahri", 0) == (primary, secondary)
            assert cc2.peek(103, 0) == (primary, secondary)      # key -> id from disk
            assert cc2.resolve_id("FiddleSticks") == "Fiddlesticks"   # case-normalised
            net.down = False

    with_net(net, run)


def test_skin_falls_back_to_base_art() -> None:
    net = FakeNet()
    net.missing_skins.add("https://ddragon.leagueoflegends.com/cdn/img/champion/loading/Ahri_9.jpg")

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cc = ChampionColors(tmp)
            base = cc.get("Ahri", 0)
            assert cc.get("Ahri", 9) == base        # no skin 9 art -> base skin
            assert cc.get("Ahri", 9) == base

    with_net(net, run)


def test_override_wins_and_needs_no_network() -> None:
    net = FakeNet()
    net.down = True

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "champion_overrides.json"
            path.write_text(json.dumps({
                "Ahri": {"primary": "#ff4fa3", "secondary": "#7a3cff"},
                "Ahri_3": {"primary": "#00ff00", "secondary": "#0000ff"},
                "Zed": {"primary": "not-a-color", "secondary": "#7a3cff"},
            }), "utf-8")
            cc = ChampionColors(tmp)
            assert cc.peek("Ahri", 0) == ((255, 79, 163), (122, 60, 255))
            assert cc.get("Ahri", 0) == ((255, 79, 163), (122, 60, 255))
            assert cc.get("Ahri", 1) == ((255, 79, 163), (122, 60, 255))     # champion-wide
            assert cc.get("Ahri", 3) == ((0, 255, 0), (0, 0, 255))           # skin-specific wins
            assert net.calls == []
            assert cc.peek("Zed", 0) is None                                 # invalid entry ignored
            # edits to the file are picked up without restarting
            time.sleep(0.05)
            path.write_text(json.dumps({"Ahri": {"primary": "#112233", "secondary": "#445566"}}), "utf-8")
            import os
            os.utime(path, (time.time() + 5, time.time() + 5))
            assert cc.peek("Ahri", 0) == ((17, 34, 51), (68, 85, 102))

    with_net(net, run)

    # an override also beats an already-cached extraction
    net2 = FakeNet()

    def run2() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cc = ChampionColors(tmp)
            extracted = cc.get("Ahri", 0)
            (pathlib.Path(tmp) / "champion_overrides.json").write_text(
                json.dumps({"Ahri": {"primary": "#ff4fa3", "secondary": "#7a3cff"}}), "utf-8")
            assert cc.get("Ahri", 0) != extracted and cc.get("Ahri", 0)[0] == (255, 79, 163)

    with_net(net2, run2)


def test_network_failure_falls_back() -> None:
    net = FakeNet()
    net.down = True

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cc = ChampionColors(tmp)
            assert cc.get("Ahri", 0) == DEFAULT_PALETTE            # never raises
            assert cc.get(103, 0) == DEFAULT_PALETTE               # unknown key, still no raise
            assert cc.peek("Ahri", 0) == DEFAULT_PALETTE           # peek no longer blocks effects
            assert not (pathlib.Path(tmp) / "champion_colors.json").exists()  # failures not cached
            # a cached base skin is preferred over the default when a skin lookup fails
            pathlib.Path(tmp, "champion_colors.json").write_text(
                json.dumps({"Ahri_0": {"primary": "#ff4fa3", "secondary": "#7a3cff"}}), "utf-8")
            cc2 = ChampionColors(tmp)
            assert cc2.get("Ahri", 4) == ((255, 79, 163), (122, 60, 255))
            # corrupt cache file is tolerated
            pathlib.Path(tmp, "champion_colors.json").write_text("{not json", "utf-8")
            assert ChampionColors(tmp).get("Ahri", 0) == DEFAULT_PALETTE

    with_net(net, run)


def test_prefetch_runs_in_background() -> None:
    net = FakeNet()

    def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cc = ChampionColors(tmp)
            cc.prefetch(103, 0)       # by numeric key: needs champion.json first
            cc.prefetch(103, 0)       # repeat is a cheap no-op while in flight
            deadline = time.time() + 5
            while cc.peek(103, 0) is None and time.time() < deadline:
                time.sleep(0.02)
            pal = cc.peek("Ahri", 0)
            assert pal is not None and hue_near(pal[0], 330, 20)
            before = len(net.calls)
            cc.prefetch("Ahri", 0)    # already cached: no work
            time.sleep(0.1)
            assert len(net.calls) == before

    with_net(net, run)


def test_hex_helpers() -> None:
    assert to_hex((255, 79, 163)) == "#ff4fa3"
    assert from_hex("#FF4fa3") == (255, 79, 163) and from_hex("ff4fa3") == (255, 79, 163)
    assert from_hex("#12") is None and from_hex(None) is None and from_hex("#gggggg") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("league colors tests passed")
