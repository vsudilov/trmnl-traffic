#!/usr/bin/env python3
"""
Render a live traffic map (default: Boston, MA) for a TRMNL e-ink display,
using the Google Map Tiles API.

  1. Create a Map Tiles session: styled roadmap + live traffic layer. The style
     paints land/roads white, water pure blue and labels black, so traffic is
     the only colour left. On TRMNL X, tiles are requested at 2x (high-DPI).
  2. Fetch and stitch the tiles covering the display.
  3. Sort pixels into classes (heavy / slow / free / road / water / label /
     background); roads are styled pure cyan so the street network can be
     drawn faintly under the traffic
     and paint each class with the device's grey palette:
        TRMNL OG  800x480,   4 greys (2-bit PNG, firmware >= 1.6)
        TRMNL X   1872x1404, 16 greys (4-bit PNG)
  4. Add title, timestamp, legend and Google attribution; write the PNG.

Usage:
  GOOGLE_MAPS_API_KEY=... python render.py --device x --out site/boston-traffic.png
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import png  # pypng: writes true 1/2/4-bit greyscale PNGs
import requests
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
DEVICES = {
    #        width  height  bits  hi-DPI tiles  UI scale  max PNG bytes
    "og": dict(w=800, h=480, bits=2, hidpi=False, ui=1.0, limit=90_000),
    "x": dict(w=1872, h=1404, bits=4, hidpi=True, ui=1.8, limit=750_000),
}

# Set in main() from the device preset.
W, H = 800, 480
TILE = 256

# Pixel classes (what a pixel *means*; the palette decides its grey).
HEAVY, SLOW, FREE, BG, WATER, LABEL, ROAD = 0, 1, 2, 3, 4, 5, 6

# Grey value per class, by bit depth (0 = black, max = white).
PALETTE_4BIT = {HEAVY: 0, SLOW: 5, FREE: 9, ROAD: 12, BG: 15, WATER: 14, LABEL: 2}
PALETTE_2BIT = {HEAVY: 0, SLOW: 1, FREE: 2, BG: 3, LABEL: 1}  # water + roads: checker of 2/3


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


def fetch_tile(session: requests.Session, url: str, params: dict) -> Image.Image:
    last_err = None
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return Image.open(BytesIO(r.content)).convert("RGBA")
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    # Never print the URL with the key in it.
    raise RuntimeError(f"tile fetch failed for {url.split('?')[0]}: {last_err}")


def mosaic(session, url_tmpl, params, z, x0, y0, w, h):
    """Stitch the tiles covering [x0, x0+w) x [y0, y0+h) and crop to w x h."""
    tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
    tx1, ty1 = int((x0 + w - 1) // TILE), int((y0 + h - 1) // TILE)
    canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (0, 0, 0, 0))
    jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

    def get(t):
        tx, ty = t
        img = fetch_tile(session, url_tmpl.format(z=z, x=tx, y=ty), params)
        if img.size != (TILE, TILE):
            img = img.resize((TILE, TILE), Image.LANCZOS)
        return t, img

    with ThreadPoolExecutor(max_workers=8) as ex:
        for (tx, ty), img in ex.map(get, jobs):
            canvas.paste(img, ((tx - tx0) * TILE, (ty - ty0) * TILE))

    ox, oy = int(round(x0 - tx0 * TILE)), int(round(y0 - ty0 * TILE))
    return canvas.crop((ox, oy, ox + w, oy + h)), len(jobs)


# --------------------------------------------------------------------------
# Google Map Tiles API
# --------------------------------------------------------------------------
GOOGLE_TILE_URL = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
GOOGLE_SESSION_URL = "https://tile.googleapis.com/v1/createSession"
GOOGLE_VIEWPORT_URL = "https://tile.googleapis.com/tile/v1/viewport"

# Flat "key" colours, so that after rendering the only saturated non-blue
# pixels are traffic. Land/roads white, water pure blue, labels black.
# Road network (all roads, incl. small streets) in pure cyan: a hue Google's
# traffic colours never use, so it can be told apart and drawn faint grey.
ROAD_STYLE = {"featureType": "road", "elementType": "geometry", "stylers": [{"color": "#00ffff"}]}

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


def render_google(s: requests.Session, args, hidpi: bool):
    global TILE
    key = os.environ.get("GOOGLE_MAPS_API_KEY") or sys.exit("Set GOOGLE_MAPS_API_KEY")

    body = {
        "mapType": "roadmap",
        "language": "en-US",
        "region": "US",
        "layerTypes": ["layerTraffic"],
        "styles": GOOGLE_STYLES + ([] if args.no_roads else [ROAD_STYLE]),
    }
    if hidpi:
        # Same ground coverage per tile, twice the pixels in each direction.
        body.update({"scale": "scaleFactor2x", "highDpi": True})

    r = s.post(GOOGLE_SESSION_URL, params={"key": key}, timeout=15, json=body)
    if not r.ok:
        sys.exit(f"createSession failed: HTTP {r.status_code} {r.text[:300]}")
    sess = r.json()
    token = sess["session"]
    TILE = int(sess.get("tileWidth") or (512 if hidpi else 256))

    # Fractional zoom: fetch tiles at the nearest whole zoom level (zt), covering
    # the same ground area, then rescale by f = 2^(zoom - zt) to W x H.
    # f is between ~0.71 and ~1.41, so labels stay a sensible size.
    zt = max(0, min(22, round(args.zoom)))
    f = 2 ** (args.zoom - zt)
    fw, fh = math.ceil(W / f), math.ceil(H / f)

    cx, cy = latlon_to_global_px(args.lat, args.lon, zt)
    x0, y0 = cx - fw / 2, cy - fh / 2

    # No disk cache: Google's terms only allow caching as the HTTP headers permit.
    img, n_tiles = mosaic(s, GOOGLE_TILE_URL, {"session": token, "key": key}, zt, x0, y0, fw, fh)
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)
    print(f"zoom {args.zoom:g}: {n_tiles} tiles at z{zt}, scaled x{f:.3f}")

    # Attribution text for exactly this viewport (required by Google's policies).
    north, west = global_px_to_latlon(x0, y0, zt)
    south, east = global_px_to_latlon(x0 + fw, y0 + fh, zt)
    attribution = "Google Maps"
    try:
        v = s.get(GOOGLE_VIEWPORT_URL, timeout=10, params={
            "session": token, "key": key, "zoom": zt,
            "north": north, "south": south, "east": east, "west": west})
        if v.ok and v.json().get("copyright"):
            attribution = f"Google Maps · {v.json()['copyright']}"
    except (requests.RequestException, ValueError):
        pass

    return img, classify(img, args), attribution


def classify(img: Image.Image, args) -> np.ndarray:
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    hsv = np.asarray(img.convert("RGB").convert("HSV")).astype(np.float32)
    hue = hsv[..., 0] * 360.0 / 255.0
    sat = hsv[..., 1] / 255.0
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    r_, g_, b_ = rgb[..., 0], rgb[..., 1], rgb[..., 2]

    cls = np.full((H, W), BG, dtype=np.uint8)
    water = (b_ > 150) & (r_ < 110) & (g_ < 110)
    cls[water] = WATER
    # Road network (cyan). Low saturation threshold keeps thin anti-aliased streets.
    cls[(hue >= 165) & (hue < 200) & (sat > 0.15)] = ROAD
    cls[(sat < 0.25) & (lum < args.label_lum)] = LABEL

    coloured = (sat > 0.35) & ~water
    green = coloured & (hue >= 70) & (hue < 165)
    orange = coloured & (hue >= 15) & (hue < 70)            # "slow"
    red = coloured & ((hue < 15) | (hue >= 330))            # red + dark red
    if not args.hide_free:
        cls[green] = FREE
    cls[orange] = SLOW
    cls[red] = HEAVY
    return cls


# --------------------------------------------------------------------------
# Annotation (drawn in class space, so it follows the palette)
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


def annotate(cls: np.ndarray, title: str, tz: str, attribution: str, u: float) -> np.ndarray:
    img = Image.fromarray(cls, mode="L")
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # no anti-aliasing: pixels must stay valid class ids

    def S(v):  # scale a size by the device UI factor
        return int(round(v * u))

    stamp = dt.datetime.now(ZoneInfo(tz)).strftime("%a %-I:%M %p")

    f_title, f_small, f_tiny = load_font(S(22), bold=True), load_font(S(15)), load_font(S(12))
    pad = S(10)
    box_w = int(max(d.textlength(title, font=f_title), d.textlength(stamp, font=f_small))) + 2 * pad
    d.rectangle([0, 0, box_w, S(56)], fill=BG, outline=HEAVY, width=max(2, S(2)))
    d.text((pad, S(6)), title, font=f_title, fill=HEAVY)
    d.text((pad, S(34)), stamp, font=f_small, fill=HEAVY)

    items = [(HEAVY, "Heavy / stopped"), (SLOW, "Slow"), (FREE, "Free flow")]
    row = S(18)
    lw, lh = S(190), row * len(items) + S(14)
    x0, y0 = W - lw - S(6), H - lh - S(22)
    d.rectangle([x0, y0, x0 + lw, y0 + lh], fill=BG, outline=HEAVY, width=max(2, S(2)))
    for i, (shade, label) in enumerate(items):
        y = y0 + S(8) + i * row
        d.rectangle([x0 + S(10), y + S(3), x0 + S(38), y + S(11)], fill=shade, outline=HEAVY)
        d.text((x0 + S(46), y - S(1)), label, font=f_small, fill=HEAVY)

    # Attribution strip, bottom-right, kept clear of everything else.
    aw = int(d.textlength(attribution, font=f_tiny))
    d.rectangle([W - aw - S(10), H - S(17), W, H], fill=BG)
    d.text((W - aw - S(5), H - S(16)), attribution, font=f_tiny, fill=HEAVY)

    return np.asarray(img, dtype=np.uint8)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def to_grey(cls: np.ndarray, bits: int) -> np.ndarray:
    yy, xx = np.mgrid[0:H, 0:W]
    out = np.zeros((H, W), dtype=np.uint8)
    if bits == 4:
        for c, g in PALETTE_4BIT.items():
            out[cls == c] = g
    elif bits == 2:
        for c, g in PALETTE_2BIT.items():
            out[cls == c] = g
        checker = ((xx + yy) % 2) == 0
        for c in (WATER, ROAD):
            out[cls == c] = np.where(checker[cls == c], 2, 3)
    else:  # 1-bit: ordered patterns approximate the greys
        even = (xx % 2 == 0) & (yy % 2 == 0)
        sparse = (xx % 4 == 0) & (yy % 4 == 0)
        out[:] = 1
        out[(cls == HEAVY) | (cls == LABEL)] = 0
        out[(cls == SLOW) & ~even] = 0        # ~75% black
        out[(cls == FREE) & ((xx + yy) % 2 == 0)] = 0   # ~50% black
        out[(cls == ROAD) & even] = 0         # ~25% black
        out[(cls == WATER) & sparse] = 0      # ~6% black
    return out


def encode_png(grey: np.ndarray, bits: int) -> bytes:
    buf = io.BytesIO()
    png.Writer(W, H, greyscale=True, bitdepth=bits, compression=9).write(buf, grey.tolist())
    return buf.getvalue()


def main():
    global W, H
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=DEVICES, default="x", help="og (800x480) or x (1872x1404)")
    ap.add_argument("--portrait", action="store_true", help="render in portrait orientation")
    ap.add_argument("--lat", type=float, default=42.355)
    ap.add_argument("--lon", type=float, default=-71.075)
    ap.add_argument("--zoom", type=float, default=12,
                    help="map zoom, fractions allowed (e.g. 12.5); 12 ≈ 22-26 km wide around Boston, +1 halves it")
    ap.add_argument("--title", default="Boston traffic")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--out", type=Path, default=Path("site/boston-traffic.png"))
    ap.add_argument("--bits", type=int, choices=[1, 2, 4],
                    help="override grey depth (default: 2 for og, 4 for x)")
    ap.add_argument("--hide-free", action="store_true", help="don't draw free-flowing traffic")
    ap.add_argument("--no-roads", action="store_true",
                    help="don't draw the street network under the traffic")
    ap.add_argument("--label-lum", type=int, default=90,
                    help="pixels darker than this (and unsaturated) become labels")
    ap.add_argument("--debug", action="store_true", help="also save the raw colour mosaic")
    args = ap.parse_args()

    dev = DEVICES[args.device]
    W, H = (dev["h"], dev["w"]) if args.portrait else (dev["w"], dev["h"])
    bits = args.bits or dev["bits"]

    with requests.Session() as s:
        s.headers["User-Agent"] = "trmnl-traffic-map/1.2"
        raw, cls, attribution = render_google(s, args, dev["hidpi"])

    if args.debug:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        raw.save(args.out.with_name("debug-raw.png"))

    cls = annotate(cls, args.title, args.tz, attribution, dev["ui"])
    data = encode_png(to_grey(cls, bits), bits)
    if len(data) > dev["limit"]:
        # The device silently refuses oversized images; fail loudly instead
        # so the previously published image stays live.
        sys.exit(f"PNG is {len(data):,} bytes, over the {args.device} limit of "
                 f"{dev['limit']:,}. Try a lower --bits, --hide-free or a higher --zoom.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"wrote {args.out} ({W}x{H}, {bits}-bit, {len(data):,} bytes)")


if __name__ == "__main__":
    main()
