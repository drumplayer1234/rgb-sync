"""Champion colors: a primary and secondary LED color per champion skin.

Colors are extracted from the Data Dragon loading-screen art with Pillow,
cached in ``%LOCALAPPDATA%\\RGBSync\\champion_colors.json``, and can be
overridden by hand in ``champion_overrides.json`` in the same folder::

    {"Ahri": {"primary": "#ff4fa3", "secondary": "#7a3cff"},
     "Ahri_3": {"primary": "#ffffff", "secondary": "#000000"}}

Network failures never raise: the caller gets a cached or default palette.
The effects layer must not call ``get`` in its frame loop; it uses
``prefetch`` (background thread) and ``peek`` (instant, cache only).
"""
from __future__ import annotations

import colorsys
import io
import json
import logging
import os
import pathlib
import threading
import time
from typing import Any

import requests
from PIL import Image

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]
Palette = tuple[RGB, RGB]

DDRAGON = "https://ddragon.leagueoflegends.com"
DEFAULT_PALETTE: Palette = ((255, 255, 255), (0, 100, 255))
VERSION_TTL = 24 * 3600.0   # re-check the latest Data Dragon version daily
RETRY_AFTER = 60.0          # don't hammer the network after a failure
HTTP_TIMEOUT = 6.0


def data_dir() -> pathlib.Path:
    """``%LOCALAPPDATA%\\RGBSync`` (created if missing)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = pathlib.Path(base) / "RGBSync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def to_hex(color: RGB) -> str:
    return "#%02x%02x%02x" % tuple(color)


def from_hex(text: Any) -> RGB | None:
    """Parse ``#rrggbb`` (or ``rrggbb``); None if invalid."""
    if not isinstance(text, str):
        return None
    t = text.strip().lstrip("#")
    if len(t) != 6:
        return None
    try:
        return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
    except ValueError:
        return None


# ------------------------------------------------------------- extraction


def _hue_dist(a: float, b: float) -> float:
    """Circular distance between two hues in degrees (0-180)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def boost(color: RGB, min_sat: float = 0.7, min_val: float = 0.9) -> RGB:
    """Make a color LED-friendly: LEDs wash out dark or pale colors."""
    r, g, b = (c / 255.0 for c in color)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s = max(s, min_sat)
    v = max(v, min_val)
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))  # type: ignore[return-value]


def _hue_shift(color: RGB, degrees: float) -> RGB:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in color))
    h = ((h * 360.0 + degrees) % 360.0) / 360.0
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))  # type: ignore[return-value]


BINS = 24  # 15 degrees each


def _vivid_bins(pixels: list[RGB], min_sat: float, min_val: float) -> tuple[list[float], list[list[float]]]:
    """Histogram vivid pixels by hue. Returns (weights, [sum_r, sum_g, sum_b, n] per bin)."""
    weights = [0.0] * BINS
    sums = [[0.0, 0.0, 0.0, 0.0] for _ in range(BINS)]
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s < min_sat or v < min_val:
            continue
        idx = int(h * BINS) % BINS
        w = s * v  # favour vivid, bright pixels
        weights[idx] += w
        acc = sums[idx]
        acc[0] += r * w
        acc[1] += g * w
        acc[2] += b * w
        acc[3] += w
    return weights, sums


def _bin_color(sums: list[list[float]], idx: int) -> RGB:
    """Weighted mean color of bin ``idx`` (merged with its neighbours)."""
    tot = [0.0, 0.0, 0.0, 0.0]
    for j in (idx - 1, idx, idx + 1):
        acc = sums[j % BINS]
        weight = 1.0 if j % BINS == idx else 0.5
        for k in range(4):
            tot[k] += acc[k] * weight
    if tot[3] <= 0:
        return DEFAULT_PALETTE[0]
    return round(tot[0] / tot[3]), round(tot[1] / tot[3]), round(tot[2] / tot[3])


def _hue_of(color: RGB) -> float:
    return colorsys.rgb_to_hsv(*(c / 255.0 for c in color))[0] * 360.0


def palette_from_image(img: Image.Image) -> Palette:
    """Pick a (primary, secondary) LED color pair from an image.

    Downscales, ignores near-black / near-white / grey pixels, buckets the
    rest by hue, takes the heaviest vivid hue as primary and the heaviest
    hue that is clearly different from it as secondary, then boosts
    saturation/value so the LEDs look good. Never raises for valid images.
    """
    small = img.convert("RGB")
    small.thumbnail((96, 96))
    flat = getattr(small, "get_flattened_data", small.getdata)  # getdata is deprecated in new Pillow
    pixels = list(flat())
    total = max(1, len(pixels))

    weights, sums = _vivid_bins(pixels, min_sat=0.30, min_val=0.25)
    if sum(1 for w in weights if w > 0) == 0 or sum(weights) < 0.01 * total:
        # Dark or muted art: relax the thresholds so something still comes out.
        weights, sums = _vivid_bins(pixels, min_sat=0.15, min_val=0.05)
    if sum(weights) <= 0:
        return DEFAULT_PALETTE

    # Smooth each bin with its neighbours so one colour split across a bin edge isn't penalised.
    smooth = [weights[i] + 0.5 * (weights[(i - 1) % BINS] + weights[(i + 1) % BINS])
              for i in range(BINS)]
    p_idx = max(range(BINS), key=lambda i: smooth[i])
    primary = _bin_color(sums, p_idx)
    p_hue = _hue_of(primary)

    best, best_score = None, 0.0
    for i in range(BINS):
        if smooth[i] < 0.12 * smooth[p_idx]:
            continue
        cand = _bin_color(sums, i)
        dist = _hue_dist(_hue_of(cand), p_hue)
        if dist < 40:
            continue
        score = smooth[i] * min(dist, 120.0) / 120.0
        if score > best_score:
            best, best_score = cand, score
    secondary = best if best is not None else _hue_shift(primary, 40)
    return boost(primary), boost(secondary)


# ----------------------------------------------------------- the cache layer


class ChampionColors:
    """Per-skin champion palettes with disk cache, user overrides and offline fallback."""

    def __init__(self, directory: pathlib.Path | str | None = None) -> None:
        self.dir = pathlib.Path(directory) if directory else data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.dir / "champion_colors.json"
        self.overrides_path = self.dir / "champion_overrides.json"
        self.version_path = self.dir / "ddragon_version.json"
        self._lock = threading.RLock()
        self._cache: dict[str, Palette] = self._load_cache()
        self._defaults: dict[str, float] = {}   # key -> retry-after time for failed lookups
        self._ids: dict[int, str] = {}          # champion key -> Data Dragon id
        self._ids_loaded = False
        self._in_flight: set[tuple[str, int]] = set()
        self._overrides: dict[str, Palette] = {}
        self._overrides_mtime: float | None = None
        self._net_retry_at = 0.0

    # -- small json helpers ----------------------------------------------
    @staticmethod
    def _read_json(path: pathlib.Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: pathlib.Path, obj: Any) -> None:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not write %s: %s", path.name, exc)

    def _load_cache(self) -> dict[str, Palette]:
        out: dict[str, Palette] = {}
        raw = self._read_json(self.cache_path)
        if isinstance(raw, dict):
            for key, val in raw.items():
                p = from_hex(val.get("primary")) if isinstance(val, dict) else None
                s = from_hex(val.get("secondary")) if isinstance(val, dict) else None
                if p and s:
                    out[key] = (p, s)
        return out

    def _save_cache(self) -> None:
        self._write_json(self.cache_path, {
            k: {"primary": to_hex(p), "secondary": to_hex(s)} for k, (p, s) in self._cache.items()})

    # -- overrides ---------------------------------------------------------
    def _refresh_overrides(self) -> None:
        try:
            mtime = self.overrides_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._overrides_mtime:
            return
        self._overrides_mtime = mtime
        self._overrides = {}
        raw = self._read_json(self.overrides_path) if mtime is not None else None
        if isinstance(raw, dict):
            for key, val in raw.items():
                if not isinstance(val, dict):
                    continue
                p, s = from_hex(val.get("primary")), from_hex(val.get("secondary"))
                if p and s:
                    self._overrides[str(key).lower()] = (p, s)
                elif p:
                    self._overrides[str(key).lower()] = (p, boost(_hue_shift(p, 40)))

    def _override_for(self, champ: str, skin: int) -> Palette | None:
        with self._lock:
            self._refresh_overrides()
            return (self._overrides.get(f"{champ}_{skin}".lower())
                    or self._overrides.get(champ.lower()))

    # -- champion id map ---------------------------------------------------
    def _id_map_path(self, version: str) -> pathlib.Path:
        return self.dir / f"champion_ids_{version}.json"

    def _load_ids_from_disk(self) -> None:
        """Load the newest cached key->id map (no network)."""
        with self._lock:
            if self._ids_loaded:
                return
            files = sorted(self.dir.glob("champion_ids_*.json"), key=lambda p: p.stat().st_mtime)
            for path in reversed(files):
                raw = self._read_json(path)
                if isinstance(raw, dict) and raw:
                    self._ids = {int(k): str(v) for k, v in raw.items() if str(k).isdigit()}
                    self._ids_loaded = True
                    return

    def resolve_id(self, champion: str | int | None) -> str | None:
        """Data Dragon id for a champion id/name/numeric key, from cache only (no network)."""
        if champion is None:
            return None
        self._load_ids_from_disk()
        if isinstance(champion, int) or (isinstance(champion, str) and champion.isdigit()):
            return self._ids.get(int(champion))
        # Normalise case ("Fiddlesticks" vs "FiddleSticks") against the known ids.
        wanted = champion.lower()
        for known in self._ids.values():
            if known.lower() == wanted:
                return known
        return champion

    def _latest_version(self) -> str | None:
        cached = self._read_json(self.version_path)
        now = time.time()
        if isinstance(cached, dict) and now - float(cached.get("checked", 0)) < VERSION_TTL \
                and cached.get("version"):
            return str(cached["version"])
        try:
            resp = requests.get(f"{DDRAGON}/api/versions.json", timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            version = str(resp.json()[0])
            self._write_json(self.version_path, {"version": version, "checked": now})
            return version
        except Exception as exc:
            log.debug("ddragon versions unavailable: %s", exc)
            return str(cached["version"]) if isinstance(cached, dict) and cached.get("version") else None

    def _ensure_ids(self, need_key: int | None = None) -> None:
        """Make sure the key->id map is loaded, downloading champion.json if needed."""
        self._load_ids_from_disk()
        with self._lock:
            if self._ids_loaded and (need_key is None or need_key in self._ids):
                return
            if time.time() < self._net_retry_at:
                return
            self._net_retry_at = time.time() + RETRY_AFTER
        version = self._latest_version()
        if not version:
            return
        path = self._id_map_path(version)
        raw = self._read_json(path)
        if not isinstance(raw, dict) or not raw:
            try:
                resp = requests.get(f"{DDRAGON}/cdn/{version}/data/en_US/champion.json",
                                    timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                data = resp.json().get("data", {})
                raw = {str(v["key"]): v["id"] for v in data.values()}
                self._write_json(path, raw)
            except Exception as exc:
                log.debug("champion.json unavailable: %s", exc)
                return
        with self._lock:
            self._ids = {int(k): str(v) for k, v in raw.items() if str(k).isdigit()}
            self._ids_loaded = True
            self._net_retry_at = 0.0

    # -- public API ----------------------------------------------------------
    def peek(self, champion: str | int | None, skin_num: int | None = 0) -> Palette | None:
        """Instant, no I/O beyond a stat: override or cached palette, else None.

        Returns the built-in default for a champion whose download recently failed,
        so callers are not stuck waiting forever while offline.
        """
        champ = self.resolve_id(champion)
        if not champ:
            return None
        skin = int(skin_num or 0)
        override = self._override_for(champ, skin)
        if override:
            return override
        with self._lock:
            hit = self._cache.get(f"{champ}_{skin}")
            if hit:
                return hit
            if self._defaults.get(f"{champ}_{skin}", 0) > 0:
                return DEFAULT_PALETTE
        return None

    def get(self, champion: str | int | None, skin_num: int | None = 0) -> Palette:
        """Palette for a champion skin; may download. Never raises."""
        try:
            return self._get(champion, int(skin_num or 0))
        except Exception as exc:  # last-resort guard: colors are cosmetic
            log.debug("palette lookup failed: %s", exc)
            return DEFAULT_PALETTE

    def _get(self, champion: str | int | None, skin: int) -> Palette:
        known = self.peek(champion, skin)
        if known is not None and known is not DEFAULT_PALETTE:
            return known
        need = int(champion) if isinstance(champion, int) or (
            isinstance(champion, str) and champion.isdigit()) else None
        self._ensure_ids(need)
        champ = self.resolve_id(champion)
        if not champ:
            return DEFAULT_PALETTE
        if self._override_for(champ, skin):
            return self._override_for(champ, skin)  # type: ignore[return-value]
        key = f"{champ}_{skin}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if time.time() < self._defaults.get(key, 0.0):
                return DEFAULT_PALETTE
        img = self._download_art(champ, skin)
        if img is None and skin != 0:
            # No art for this skin: reuse the base skin if we have it.
            base = self._cache.get(f"{champ}_0")
            if base:
                return base
            img = self._download_art(champ, 0)
        if img is None:
            with self._lock:
                self._defaults[key] = time.time() + RETRY_AFTER
            return self._cache.get(f"{champ}_0") or DEFAULT_PALETTE
        palette = palette_from_image(img)
        with self._lock:
            self._cache[key] = palette
            self._defaults.pop(key, None)
            self._save_cache()
        return palette

    def _download_art(self, champ: str, skin: int) -> Image.Image | None:
        url = f"{DDRAGON}/cdn/img/champion/loading/{champ}_{skin}.jpg"
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None
            img = Image.open(io.BytesIO(resp.content))
            img.load()
            return img
        except Exception as exc:
            log.debug("loading art unavailable for %s_%s: %s", champ, skin, exc)
            return None

    def prefetch(self, champion: str | int | None, skin_num: int | None = 0) -> None:
        """Make sure a palette will be cached soon; runs in a background thread. Cheap to repeat."""
        if champion is None:
            return
        skin = int(skin_num or 0)
        label = str(champion)
        with self._lock:
            if (label, skin) in self._in_flight:
                return
            champ = self.resolve_id(champion)
            if champ:
                key = f"{champ}_{skin}"
                if key in self._cache or time.time() < self._defaults.get(key, 0.0) \
                        or self._override_for(champ, skin):
                    return
            self._in_flight.add((label, skin))
        threading.Thread(target=self._prefetch_worker, args=(champion, skin, label),
                         name="ChampionColors", daemon=True).start()

    def _prefetch_worker(self, champion: str | int, skin: int, label: str) -> None:
        try:
            self.get(champion, skin)
        finally:
            with self._lock:
                self._in_flight.discard((label, skin))
