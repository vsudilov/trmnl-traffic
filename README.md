# TRMNL traffic map (Boston)

Renders an 800×480 live traffic-density map for a TRMNL e-ink display every
15 minutes and publishes it on GitHub Pages. The TRMNL **Alias** plugin then
shows the published image.

Traffic data comes from the **Google Map Tiles API** by default, or from
TomTom (`--provider tomtom`).

```
GitHub Actions (cron) ──> render.py ──> Google Map Tiles API
          │                              (styled roadmap + live traffic layer)
          └──> GitHub Pages: https://<you>.github.io/<repo>/boston-traffic.png
                                   ▲
TRMNL device ── Alias plugin ──────┘  (device downloads the image directly)
```

## Deploy

1. **Google key.**
   - In the Google Cloud console, create a project or pick an existing one,
     and attach billing to it. Map Tiles API requires a billing account even
     on the free tier.
   - Enable **Map Tiles API**.
   - Create an API key and, under API restrictions, limit it to Map Tiles API.
   - Optional safety net: under Map Tiles API → Quotas, cap daily tile
     requests at about 3,000. One run uses at most 15 tiles, so 96 runs a day
     is about 1,440 tiles, or about 43k a month. The 2D Map Tiles free cap is
     100k a month.
2. **Repo.** Create a **public** GitHub repo (Pages from a private repo needs
   a paid plan) and push these files, including `.github/workflows/render.yml`.
3. **Secret.** Go to Settings → Secrets and variables → Actions and add
   `GOOGLE_MAPS_API_KEY`.
4. **Pages.** Go to Settings → Pages → Source and choose **GitHub Actions**.
5. **First run.** On the Actions tab, open "Render TRMNL traffic map" and click
   **Run workflow**. Then open
   `https://<you>.github.io/<repo>/boston-traffic.png` to check the image.
6. **TRMNL.** In the TRMNL web app, open Plugins → **Alias** and set:
   - Type: Plain Text
   - URL: the Pages URL above
   - Enable cache?: **No** (the filename never changes)

   Add it to a playlist, drag it below the "Displayed now" item, and press
   the button on the back of the device to skip to it.

## How the Google rendering works
The Map Tiles session asks for a `roadmap` with `layerTypes: ["layerTraffic"]`
and a custom style. The style paints land and roads white, water pure blue
and labels black, and hides POIs, transit and road labels. After rendering,
the only colored pixels left are Google's traffic colors, and the script
sorts them by hue:

| Google color | Meaning | On the display |
|---|---|---|
| green | normal speed | light grey (or hidden with `--hide-free`) |
| orange | slow | dark grey |
| red / dark red | heavy / stopped | black |

The attribution string is fetched from the viewport endpoint for exactly this
map area and printed in the bottom-right corner, as Google's policies require.

## Tuning
| Flag | Default | Notes |
|---|---|---|
| `--provider` | google | or `tomtom` (needs `TOMTOM_API_KEY`) |
| `--lat/--lon` | 42.355, -71.075 | map centre (Back Bay) |
| `--zoom` | 12 | 12 ≈ 22 km across, 13 ≈ 11 km (downtown/Cambridge/Brookline) |
| `--bits` | 2 | 4 grays; needs OG firmware 1.6+. Use `1` for older firmware |
| `--hide-free` | off | show only congested roads |
| `--label-lum` | 90 | raise it if place labels disappear |
| `--debug` | off | also saves the raw color mosaic as `debug-raw.png` |

**Local test:** `pip install -r requirements.txt && GOOGLE_MAPS_API_KEY=... python render.py --debug`

**Run it on a home machine instead:** a cron job running `render.py` plus any
static web server on your LAN works too. Alias accepts `http://192.168.x.x/...`
URLs because the device fetches the image itself.

## Notes
- Google's Map Tiles policies forbid storing or caching tiles beyond what the
  HTTP headers allow, and forbid using tiles for "image analysis / machine
  interpretation". This script only recolors a map for display, stores no
  tiles, and keeps the attribution. Even so, republishing a recolored Google
  map is a gray area under those terms. It's meant for a personal display;
  don't redistribute the image. TomTom (`--provider tomtom`) has no such
  constraints.
- GitHub disables scheduled workflows in a repo after 60 days with no commits.
  Re-enable from the Actions tab, or push any commit.
- How often the screen updates is set by the device's playlist refresh
  interval in TRMNL, not by this cron schedule.
