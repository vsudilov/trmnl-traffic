#!/usr/bin/env python3
"""
Render a traffic-density map (default: Boston, MA) for a TRMNL e-ink display.

Providers:
  google (default)  Google Map Tiles API: a styled roadmap with the live
                    traffic layer. Needs GOOGLE_MAPS_API_KEY.
  tomtom            TomTom base map plus raster traffic-flow tiles.
                    Needs TOMTOM_API_KEY.

Pipeline:
  1. Work out which 256px web-mercator tiles cover an 800x480 window
     centred on --lat/--lon at --zoom.
  2. Fetch the tiles. For Google, the base map is styled in flat "key" colours
     (white land, pure-blue water, black labels) so that only traffic is
     coloured.
  3. Collapse the colours into four grey levels that read well on e-ink:
        black       heavy / stopped (red, dark red)
        dark grey   slow (orange / yellow); also map labels
        light grey  free flowing (green)            [--hide-free drops it]
        white       background; water gets a light checker texture
  4. Draw a title, timestamp, legend and the provider's attribution, then
     write a 2-bit greyscale PNG (TRMNL OG, firmware >= 1.6) or a 1-bit PNG
     (--bits 1).

Usage:
  GOOGLE_MAPS_API_KEY=... python render.py --out site/boston-traffic.png
  TOMTOM_API_KEY=...      python render.py --provider tomtom
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import png  # pypng: writes true 1-/2-bit greyscale PNGs
import requests
from PIL import Image, ImageDraw, ImageFont

TILE = 256
W, H = 800, 480  # TRMNL OG resolution

# Grey levels (also the 2-bit PNG sample values: 0 = black ... 3 = white)
BLACK, DARK, LIGHT, WHITE = 0, 1, 2, 3


# --------------------------------------------------------------------------
# Geometry + tile fetching
# --------------------------------------------------------------------------
def latlon_to_global_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def global_px_to_latlon(x: float, y: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def fetch_tile(session: requests.Session, url: str, params: dict,
               cache_file: Path | None) -> Image.Image:
    if cache_file and cache_file.exists():
        return Image.open(cache_file).convert("RGBA")
    last_err = None
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(r.content)
            return Image.open(BytesIO(r.content)).convert("RGBA")
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    # Never print the URL with the key in it.
    raise RuntimeError(f"tile fetch failed for {url.split('?')[0]}: {last_err}")


def mosaic(session, url_tmpl, params, z, x0, y0, cache_dir: Path | None, tag: str):
    """Stitch the tiles covering [x0, x0+W) x [y0, y0+H) and crop to W x H."""
    tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
    tx1, ty1 = int((x0 + W - 1) // TILE), int((y0 + H - 1) // TILE)
    canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (0, 0, 0, 0))
    jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

    def get(t):
        tx, ty = t
        cf = cache_dir / f"{tag}_{z}_{tx}_{ty}.png" if cache_dir else None
        return t, fetch_tile(session, url_tmpl.format(z=z, x=tx, y=ty), params, cf)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for (tx, ty), img in ex.map(get, jobs):
            canvas.paste(img, ((tx - tx0) * TILE, (ty - ty0) * TILE))

    ox, oy = int(round(x0 - tx0 * TILE)), int(round(y0 - ty0 * TILE))
    return canvas.crop((ox, oy, ox + W, oy + H))


def _hsv_arrays(img: Image.Image):
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    hsv = np.asarray(img.convert("RGB").convert("HSV")).astype(np.float32)
    hue = hsv[..., 0] * 360.0 / 255.0
    sat = hsv[..., 1] / 255.0
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    return rgb, hue, sat, lum


def _checker():
    yy, xx = np.mgrid[0:H, 0:W]
    return ((xx + yy) % 2) == 0


# --------------------------------------------------------------------------
# Provider: Google Map Tiles API
# --------------------------------------------------------------------------
GOOGLE_TILE_URL = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
GOOGLE_SESSION_URL = "https://tile.googleapis.com/v1/createSession"
GOOGLE_VIEWPORT_URL = "https://tile.googleapis.com/tile/v1/viewport"

# Flat "key" colours, so that after rendering the only saturated non-blue
# pixels are traffic. Land/roads white, water pure blue, labels black.
GOOGLE_STYLES = [
    {"elementType": "geometry", "stylers": [{"color": "#ffffff"}]},
    {"featureType": "water", "elementType": "geometry", "stylers": [{"color": "#0000ff"}]},
    {"featureType": "poi", "stylers": [{"visibility": "off"}]},
    {"featureType": "transit", "stylers": [{"visibility": "off"}]},
    {"featureType": "road", "elementType": "labels", "stylers": [{"visibility": "off"}]},
    {"featureType": "administrative", "elementType": "geometry", "stylers": [{"visibility": "off"}]},
    {"elementType": "labels.icon", "stylers": [{"visibility": "off"}]},
    {"elementType": "labels.text.fill", "stylers": [{"color": "#000000"}]},
    {"elementType": "labels.text.stroke", "stylers": [{"color": "#ffffff"}, {"weight": 3}]},
]


def render_google(s: requests.Session, args, x0, y0):
    key = os.environ.get("GOOGLE_MAPS_API_KEY") or sys.exit("Set GOOGLE_MAPS_API_KEY")

    r = s.post(GOOGLE_SESSION_URL, params={"key": key}, timeout=15, json={
        "mapType": "roadmap",
        "language": "en-US",
        "region": "US",
        "layerTypes": ["layerTraffic"],
        "styles": GOOGLE_STYLES,
    })
    if not r.ok:
        sys.exit(f"createSession failed: HTTP {r.status_code} {r.text[:300]}")
    token = r.json()["session"]

    # No disk cache: Google's terms only allow caching as the HTTP headers permit.
    img = mosaic(s, GOOGLE_TILE_URL, {"session": token, "key": key},
                 args.zoom, x0, y0, None, "google")

    # Attribution text for exactly this viewport (required by Google's policies).
    north, west = global_px_to_latlon(x0, y0, args.zoom)
    south, east = global_px_to_latlon(x0 + W, y0 + H, args.zoom)
    attribution = "Google Maps"
    try:
        v = s.get(GOOGLE_VIEWPORT_URL, timeout=10, params={
            "session": token, "key": key, "zoom": args.zoom,
            "north": north, "south": south, "east": east, "west": west})
        if v.ok and v.json().get("copyright"):
            attribution = f"Google Maps · {v.json()['copyright']}"
    except (requests.RequestException, ValueError):
        pass

    return img, classify_google(img, args), attribution


def classify_google(img: Image.Image, args) -> np.ndarray:
    rgb, hue, sat, lum = _hsv_arrays(img)
    r_, g_, b_ = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lvl = np.full((H, W), WHITE, dtype=np.uint8)

    water = (b_ > 150) & (r_ < 110) & (g_ < 110)
    lvl[water & _checker()] = LIGHT

    labels = (sat < 0.25) & (lum < args.label_lum)
    lvl[labels] = DARK

    coloured = (sat > 0.35) & ~water
    green = coloured & (hue >= 70) & (hue < 170)
    orange = coloured & (hue >= 15) & (hue < 70)            # "slow"
    red = coloured & ((hue < 15) | (hue >= 330))            # red + dark red
    if not args.hide_free:
        lvl[green] = LIGHT
    lvl[orange] = DARK
    lvl[red] = BLACK
    return lvl


# --------------------------------------------------------------------------
# Provider: TomTom
# --------------------------------------------------------------------------
TOMTOM_BASE_URL = "https://api.tomtom.com/map/1/tile/basic/main/{z}/{x}/{y}.png"
TOMTOM_FLOW_URL = "https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png"


def render_tomtom(s: requests.Session, args, x0, y0):
    key = os.environ.get("TOMTOM_API_KEY") or sys.exit("Set TOMTOM_API_KEY")
    base = mosaic(s, TOMTOM_BASE_URL, {"key": key}, args.zoom, x0, y0, args.cache_dir, "tt-base")
    flow = mosaic(s, TOMTOM_FLOW_URL, {"key": key, "thickness": args.thickness},
                  args.zoom, x0, y0, None, "tt-flow")
    debug = Image.alpha_composite(base, flow)
    return debug, classify_tomtom(base, flow, args), "© TomTom"


def classify_tomtom(base: Image.Image, flow: Image.Image, args) -> np.ndarray:
    lvl = np.full((H, W), WHITE, dtype=np.uint8)

    rgb, _, _, lum = _hsv_arrays(base)
    r_, g_, b_ = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    water = (b_ > r_ + 20) & (b_ >= g_) & (lum > 110)
    lvl[water & _checker()] = LIGHT
    lvl[lum < args.label_lum] = DARK

    alpha = np.asarray(flow)[..., 3] > 100
    _, hue, sat, _ = _hsv_arrays(flow)
    closed = alpha & (sat < 0.18)
    green = alpha & ~closed & (hue >= 75) & (hue < 170)
    yellow = alpha & ~closed & (hue >= 42) & (hue < 75)
    red_orange = alpha & ~closed & ~green & ~yellow
    if not args.hide_free:
        lvl[green] = LIGHT
    lvl[yellow] = DARK
    lvl[red_orange | closed] = BLACK
    return lvl


PROVIDERS = {"google": render_google, "tomtom": render_tomtom}


# --------------------------------------------------------------------------
# Annotation + output
# --------------------------------------------------------------------------
def load_font(size: int, bold: bool = False):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def annotate(lvl: np.ndarray, title: str, tz: str, attribution: str) -> np.ndarray:
    img = Image.fromarray(lvl, mode="L")
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # no anti-aliasing: crisp on e-ink

    stamp = dt.datetime.now(ZoneInfo(tz)).strftime("%a %-I:%M %p")

    f_title, f_small = load_font(22, bold=True), load_font(15)
    box_w = int(max(d.textlength(title, font=f_title), d.textlength(stamp, font=f_small))) + 20
    d.rectangle([0, 0, box_w, 56], fill=WHITE, outline=BLACK, width=2)
    d.text((10, 6), title, font=f_title, fill=BLACK)
    d.text((10, 34), stamp, font=f_small, fill=BLACK)

    items = [(BLACK, "Heavy / stopped"), (DARK, "Slow"), (LIGHT, "Free flow")]
    lw, lh = 170, 18 * len(items) + 14
    x0, y0 = W - lw - 6, H - lh - 22
    d.rectangle([x0, y0, x0 + lw, y0 + lh], fill=WHITE, outline=BLACK, width=2)
    for i, (shade, label) in enumerate(items):
        y = y0 + 8 + i * 18
        d.rectangle([x0 + 10, y + 3, x0 + 38, y + 11], fill=shade, outline=BLACK)
        d.text((x0 + 46, y - 1), label, font=f_small, fill=BLACK)

    # Attribution strip, bottom-right, kept clear of everything else.
    f_tiny = load_font(12)
    aw = int(d.textlength(attribution, font=f_tiny))
    d.rectangle([W - aw - 10, H - 17, W, H], fill=WHITE)
    d.text((W - aw - 5, H - 16), attribution, font=f_tiny, fill=BLACK)

    return np.asarray(img, dtype=np.uint8)


def write_png(lvl: np.ndarray, out: Path, bits: int):
    out.parent.mkdir(parents=True, exist_ok=True)
    if bits == 2:
        rows = lvl  # 0..3 is exactly the 2-bit grey scale
    else:
        yy, xx = np.mgrid[0:H, 0:W]
        even = (xx % 2 == 0) & (yy % 2 == 0)
        rows = np.ones((H, W), dtype=np.uint8)
        rows[lvl == BLACK] = 0
        rows[(lvl == DARK) & ~even] = 0     # ~75% black
        rows[(lvl == LIGHT) & even] = 0     # ~25% black
    with open(out, "wb") as fh:
        png.Writer(W, H, greyscale=True, bitdepth=bits).write(fh, rows.tolist())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=PROVIDERS, default="google")
    ap.add_argument("--lat", type=float, default=42.355)
    ap.add_argument("--lon", type=float, default=-71.075)
    ap.add_argument("--zoom", type=int, default=12, help="12 ≈ 22 km wide in Boston; 13 ≈ 11 km")
    ap.add_argument("--title", default="Boston traffic")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--out", type=Path, default=Path("site/boston-traffic.png"))
    ap.add_argument("--bits", type=int, choices=[1, 2], default=2,
                    help="2 = 4-grey PNG (OG fw >= 1.6), 1 = black/white")
    ap.add_argument("--hide-free", action="store_true", help="don't draw free-flowing roads")
    ap.add_argument("--label-lum", type=int, default=90,
                    help="pixels darker than this (and unsaturated) become grey labels")
    ap.add_argument("--thickness", type=int, default=8, help="[tomtom] flow line thickness 1-20")
    ap.add_argument("--cache-dir", type=Path, default=Path(".tile-cache"),
                    help="[tomtom] base-map tile cache")
    ap.add_argument("--debug", action="store_true", help="also save the raw colour mosaic")
    args = ap.parse_args()

    cx, cy = latlon_to_global_px(args.lat, args.lon, args.zoom)
    x0, y0 = cx - W / 2, cy - H / 2

    with requests.Session() as s:
        s.headers["User-Agent"] = "trmnl-traffic-map/1.1"
        raw, lvl, attribution = PROVIDERS[args.provider](s, args, x0, y0)

    if args.debug:
        raw.save(args.out.with_name("debug-raw.png"))

    write_png(annotate(lvl, args.title, args.tz, attribution), args.out, args.bits)
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes, {args.bits}-bit, {args.provider})")


if __name__ == "__main__":
    main()
