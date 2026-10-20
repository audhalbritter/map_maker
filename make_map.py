#!/usr/bin/env python3
"""
Print-ready Norgeskart PDFs (Kartverket) for arbitrary named map extents.

Define maps in maps.yaml, then:

  python make_map.py list
  python make_map.py vikesland
  python make_map.py vikesland --dpi 250 --orientation portrait
  python make_map.py aurland_kommune --paper A1

CLI flags override values from maps.yaml.
"""

from __future__ import annotations

import argparse
import io
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A0, A1, A2, A3, A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# A0 @ high zoom produces large mosaics; allow up to ~500 MP
Image.MAX_IMAGE_PIXELS = 500_000_000

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "maps.yaml"
TILE_CACHE = ROOT / ".tile_cache"

TILE_URL = (
    "https://cache.kartverket.no/v1/wmts/1.0.0/"
    "{layer}/default/webmercator/{z}/{y}/{x}.png"
)
KOMMUNEINFO = "https://ws.geonorge.no/kommuneinfo/v1/kommuner/{nr}"

TILE_SIZE = 256
EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS
USER_AGENT = "NatuRA-map-maker/2.0 (workshop print maps)"

LAYERS = {
    "topo": "Norgeskart (farge)",
    "topograatone": "Norgeskart (gråtone)",
    "toporaster": "Turkart",
    "sjokartraster": "Sjøkart",
}

PAPERS = {
    "A0": A0,
    "A1": A1,
    "A2": A2,
    "A3": A3,
    "A4": A4,
}


@dataclass
class BBox:
    west: float
    south: float
    east: float
    north: float

    def padded(self, fraction: float) -> "BBox":
        if fraction <= 0:
            return self
        lon_pad = (self.east - self.west) * fraction
        lat_pad = (self.north - self.south) * fraction
        return BBox(
            self.west - lon_pad,
            self.south - lat_pad,
            self.east + lon_pad,
            self.north + lat_pad,
        )

    def width_m(self) -> float:
        mid_lat = (self.south + self.north) / 2
        return (self.east - self.west) * (math.pi / 180) * EARTH_RADIUS * math.cos(
            math.radians(mid_lat)
        )

    def height_m(self) -> float:
        return (self.north - self.south) * (math.pi / 180) * EARTH_RADIUS

    def expand_to_aspect(self, target_h_over_w: float) -> "BBox":
        """Pad east-west or north-south so ground aspect matches paper."""
        w_m, h_m = self.width_m(), self.height_m()
        if w_m <= 0 or h_m <= 0:
            return self
        current = h_m / w_m
        mid_lon = (self.west + self.east) / 2
        mid_lat = (self.south + self.north) / 2
        if abs(current - target_h_over_w) < 0.01:
            return self
        if current < target_h_over_w:
            # need taller — pad latitude
            new_h_m = w_m * target_h_over_w
            half_lat = (new_h_m / 2) / ((math.pi / 180) * EARTH_RADIUS)
            return BBox(self.west, mid_lat - half_lat, self.east, mid_lat + half_lat)
        # need wider — pad longitude
        new_w_m = h_m / target_h_over_w
        m_per_deg_lon = (math.pi / 180) * EARTH_RADIUS * math.cos(math.radians(mid_lat))
        half_lon = (new_w_m / 2) / m_per_deg_lon
        return BBox(mid_lon - half_lon, self.south, mid_lon + half_lon, self.north)


def lonlat_to_meters(lon: float, lat: float) -> tuple[float, float]:
    x = lon * ORIGIN_SHIFT / 180.0
    y = math.log(math.tan((90 + lat) * math.pi / 360.0)) * EARTH_RADIUS
    return x, y


def lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    x, y = lonlat_to_meters(lon, lat)
    res = (2 * ORIGIN_SHIFT) / (TILE_SIZE * 2**zoom)
    return (x + ORIGIN_SHIFT) / res, (ORIGIN_SHIFT - y) / res


# Lower zoom → fewer/larger Kartverket labels when printed (tiles are upscaled).
# Higher zoom → more detail, smaller place-name text.
LABEL_SIZE_BIAS = {
    "detail": 0,   # sharpest / smallest labels (old behaviour)
    "small": 0,
    "medium": -1,
    "large": -2,   # default: readable labels on poster prints
    "xlarge": -3,
}


def choose_zoom(
    bbox: BBox,
    target_width_px: int,
    max_zoom: int = 17,
    zoom_bias: int = 0,
) -> int:
    base = max_zoom
    for z in range(8, max_zoom + 1):
        px0, _ = lonlat_to_pixel(bbox.west, bbox.north, z)
        px1, _ = lonlat_to_pixel(bbox.east, bbox.north, z)
        if (px1 - px0) >= target_width_px:
            base = z
            break
    return max(8, min(max_zoom, base + zoom_bias))


def chrome_font_sizes(paper: str) -> dict[str, float]:
    """Title / footer sizes scaled to paper (points)."""
    scale = {"A4": 0.7, "A3": 0.85, "A2": 1.0, "A1": 1.15, "A0": 1.35}.get(
        paper.upper(), 1.0
    )
    return {
        "title": 22 * scale,
        "subtitle": 11 * scale,
        "scale": 9 * scale,
        "attr": 8 * scale,
        "meta": 7 * scale,
    }


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def merge_map_config(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    maps = cfg.get("maps") or {}
    if name not in maps:
        known = ", ".join(sorted(maps)) or "(none)"
        raise SystemExit(f"Unknown map {name!r}. Known maps: {known}")
    merged = dict(cfg.get("defaults") or {})
    merged.update(maps[name])
    merged["name"] = name
    return merged


def fetch_kommune_bbox(s: requests.Session, kommune_nr: str) -> BBox:
    r = s.get(KOMMUNEINFO.format(nr=kommune_nr), timeout=30)
    r.raise_for_status()
    coords = r.json()["avgrensningsboks"]["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return BBox(min(lons), min(lats), max(lons), max(lats))


def fetch_kommune_outline(
    s: requests.Session, kommune_nr: str
) -> list[list[tuple[float, float]]]:
    r = s.get(f"{KOMMUNEINFO.format(nr=kommune_nr)}/omrade", timeout=60)
    r.raise_for_status()
    geom = r.json()["omrade"]
    rings: list[list[tuple[float, float]]] = []

    def walk(node: Any) -> None:
        if not node:
            return
        if isinstance(node[0][0], (int, float)):
            rings.append([(p[0], p[1]) for p in node])
        else:
            for child in node:
                walk(child)

    walk(geom["coordinates"])
    return [ring for ring in rings if len(ring) >= 20]


def resolve_bbox(s: requests.Session, conf: dict[str, Any]) -> BBox:
    if conf.get("source") == "kommune":
        nr = str(conf["kommune_nr"])
        print(f"Fetching kommune {nr} extent from Geonorge…")
        bbox = fetch_kommune_bbox(s, nr)
    else:
        b = conf["bbox"]
        bbox = BBox(float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))
    return bbox.padded(float(conf.get("padding") or 0))


def page_size(paper: str, orientation: str) -> tuple[float, float]:
    base = PAPERS[paper.upper()]
    return landscape(base) if orientation == "landscape" else base


def download_tile(
    s: requests.Session, layer: str, z: int, x: int, y: int, retries: int = 3
) -> Image.Image:
    cache_path = TILE_CACHE / layer / str(z) / str(x) / f"{y}.png"
    if cache_path.exists():
        try:
            return Image.open(cache_path).convert("RGB")
        except OSError:
            cache_path.unlink(missing_ok=True)

    url = TILE_URL.format(layer=layer, z=z, x=x, y=y)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 404:
                img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (240, 240, 240))
            else:
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(cache_path)
            return img
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"Failed tile z={z} x={x} y={y}: {last_err}")


def stitch_map(
    s: requests.Session,
    layer: str,
    bbox: BBox,
    zoom: int,
    workers: int,
) -> tuple[Image.Image, tuple[float, float, float, float]]:
    px_w, py_n = lonlat_to_pixel(bbox.west, bbox.north, zoom)
    px_e, py_s = lonlat_to_pixel(bbox.east, bbox.south, zoom)

    tx0 = int(math.floor(px_w / TILE_SIZE))
    ty0 = int(math.floor(py_n / TILE_SIZE))
    tx1 = int(math.floor((px_e - 1e-9) / TILE_SIZE))
    ty1 = int(math.floor((py_s - 1e-9) / TILE_SIZE))

    n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    print(f"  Zoom {zoom}: fetching {n_tiles} tiles ({tx1 - tx0 + 1}×{ty1 - ty0 + 1})…")

    mosaic = Image.new(
        "RGB",
        ((tx1 - tx0 + 1) * TILE_SIZE, (ty1 - ty0 + 1) * TILE_SIZE),
        (255, 255, 255),
    )
    jobs = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    done = 0

    def task(xy: tuple[int, int]) -> tuple[int, int, Image.Image]:
        x, y = xy
        return x, y, download_tile(s, layer, zoom, x, y)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, j) for j in jobs]
        for fut in as_completed(futures):
            x, y, tile = fut.result()
            mosaic.paste(tile, ((x - tx0) * TILE_SIZE, (y - ty0) * TILE_SIZE))
            done += 1
            if done % 50 == 0 or done == n_tiles:
                print(f"    {done}/{n_tiles}", flush=True)

    left = px_w - tx0 * TILE_SIZE
    upper = py_n - ty0 * TILE_SIZE
    right = px_e - tx0 * TILE_SIZE
    lower = py_s - ty0 * TILE_SIZE
    cropped = mosaic.crop(
        (int(left), int(upper), int(math.ceil(right)), int(math.ceil(lower)))
    )
    return cropped, (px_w, py_n, px_e, py_s)


def draw_boundary(
    img: Image.Image,
    rings: Iterable[list[tuple[float, float]]],
    zoom: int,
    pixel_bbox: tuple[float, float, float, float],
) -> None:
    px_w, py_n, _, _ = pixel_bbox
    draw = ImageDraw.Draw(img)
    for ring in rings:
        pts = []
        for lon, lat in ring:
            px, py = lonlat_to_pixel(lon, lat, zoom)
            pts.append((px - px_w, py - py_n))
        if len(pts) >= 3:
            # Subtle outline — thin enough not to dominate the print
            draw.line(pts + [pts[0]], fill=(160, 40, 40), width=2, joint="curve")


def fit_to_frame(img: Image.Image, frame_w: int, frame_h: int) -> Image.Image:
    scale = min(frame_w / img.width, frame_h / img.height)
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    out = Image.new("RGB", (frame_w, frame_h), (255, 255, 255))
    out.paste(resized, ((frame_w - new_w) // 2, (frame_h - new_h) // 2))
    return out


def nice_scale_m(bbox: BBox, map_width_px: int, bar_target_px: int = 450) -> float:
    m_per_px = bbox.width_m() / max(1, map_width_px)
    raw = bar_target_px * m_per_px
    nice = [200, 500, 1000, 2000, 2500, 5000, 10000, 15000, 20000, 25000, 50000]
    return min(nice, key=lambda n: abs(n - raw))


def write_pdf(
    map_img: Image.Image,
    bbox: BBox,
    *,
    title: str,
    subtitle: str,
    layer: str,
    paper: str,
    orientation: str,
    dpi: int,
    out_path: Path,
) -> None:
    page = page_size(paper, orientation)
    page_w, page_h = page
    margin = 12 * mm
    title_h = 18 * mm
    footer_h = 14 * mm
    map_frame_w = page_w - 2 * margin
    map_frame_h = page_h - 2 * margin - title_h - footer_h

    frame_px_w = int(round(map_frame_w / 72 * dpi))
    frame_px_h = int(round(map_frame_h / 72 * dpi))
    framed = fit_to_frame(map_img, frame_px_w, frame_px_h)

    buf = io.BytesIO()
    framed.save(buf, format="JPEG", quality=92, optimize=True)
    buf.seek(0)

    c = canvas.Canvas(str(out_path), pagesize=page)
    c.setTitle(f"{title} — Norgeskart")
    c.setAuthor("Kartverket / NatuRA map_maker")
    c.setSubject(f"{paper} print map: {title}")

    fonts = chrome_font_sizes(paper)
    c.setFont("Helvetica-Bold", fonts["title"])
    c.drawString(margin, page_h - margin - 12 * mm, title)
    c.setFont("Helvetica", fonts["subtitle"])
    right = " · ".join(
        p for p in (subtitle, LAYERS.get(layer, layer)) if p
    )
    c.drawRightString(page_w - margin, page_h - margin - 12 * mm, right)

    c.drawImage(
        ImageReader(buf),
        margin,
        margin + footer_h,
        width=map_frame_w,
        height=map_frame_h,
        preserveAspectRatio=True,
        anchor="c",
    )

    scale_ratio = min(frame_px_w / map_img.width, frame_px_h / map_img.height)
    drawn_map_w_px = map_img.width * scale_ratio
    drawn_map_w_pt = drawn_map_w_px / dpi * 72
    ground_m = bbox.width_m()
    rf = ground_m / (drawn_map_w_pt / 72 * 0.0254)
    rf_round = int(round(rf, -3 if rf >= 100_000 else -2))
    rf_label = f"1:{rf_round:,}".replace(",", " ")

    bar_m = nice_scale_m(bbox, max(1, int(drawn_map_w_px)))
    bar_pt = (bar_m / ground_m) * drawn_map_w_pt
    bar_x, bar_y = margin, margin + 4 * mm
    c.setFillColorRGB(0, 0, 0)
    c.rect(bar_x, bar_y, bar_pt, 2.5 * mm, stroke=1, fill=1)
    c.setFont("Helvetica", fonts["scale"])
    bar_label = f"{int(bar_m / 1000)} km" if bar_m >= 1000 else f"{int(bar_m)} m"
    c.drawString(bar_x, bar_y + 4 * mm, f"Målestokk ca. {rf_label}   |   {bar_label}")

    c.setFont("Helvetica", fonts["attr"])
    c.drawRightString(
        page_w - margin,
        margin + 4 * mm,
        "Kartdata: © Kartverket  ·  norgeskart.no / cache.kartverket.no",
    )
    c.setFont("Helvetica", fonts["meta"])
    c.drawRightString(
        page_w - margin,
        margin + 1 * mm,
        f"{dpi} dpi  ·  {paper} {orientation}  ·  "
        f"{bbox.west:.4f},{bbox.south:.4f} – {bbox.east:.4f},{bbox.north:.4f}",
    )
    c.showPage()
    c.save()


def build_map(conf: dict[str, Any], overrides: argparse.Namespace) -> int:
    layer = overrides.layer or conf.get("layer") or "topo"
    paper = (overrides.paper or conf.get("paper") or "A0").upper()
    orientation = overrides.orientation or conf.get("orientation") or "portrait"
    dpi = int(overrides.dpi if overrides.dpi is not None else conf.get("dpi") or 200)
    workers = int(overrides.workers or conf.get("workers") or 10)
    fit_aspect = (
        conf.get("fit_paper_aspect", True)
        if overrides.fit_paper_aspect is None
        else overrides.fit_paper_aspect
    )
    draw_boundary_flag = bool(conf.get("draw_boundary"))
    if overrides.no_boundary:
        draw_boundary_flag = False
    if overrides.boundary:
        draw_boundary_flag = True

    if paper not in PAPERS:
        raise SystemExit(f"Unknown paper {paper!r}. Choose from {', '.join(PAPERS)}")
    if dpi < 72 or dpi > 400:
        raise SystemExit("DPI should be between 72 and 400.")

    out = overrides.output
    if out is None:
        out = ROOT / f"{conf['name']}_{paper}.pdf"

    s = session()
    bbox = resolve_bbox(s, conf)
    print(
        f"  BBox: {bbox.west:.5f},{bbox.south:.5f} → {bbox.east:.5f},{bbox.north:.5f} "
        f"(~{bbox.width_m()/1000:.1f} × {bbox.height_m()/1000:.1f} km)"
    )

    page_w, page_h = page_size(paper, orientation)
    margin = 12 * mm
    title_h = 18 * mm
    footer_h = 14 * mm
    map_frame_w = page_w - 2 * margin
    map_frame_h = page_h - 2 * margin - title_h - footer_h

    if fit_aspect:
        bbox = bbox.expand_to_aspect(map_frame_h / map_frame_w)
        print(
            f"  Fitted to {paper} {orientation}: "
            f"~{bbox.width_m()/1000:.1f} × {bbox.height_m()/1000:.1f} km"
        )

    rings: list[list[tuple[float, float]]] = []
    if draw_boundary_flag and conf.get("source") == "kommune":
        print("Fetching municipality boundary…")
        rings = fetch_kommune_outline(s, str(conf["kommune_nr"]))
        print(f"  {len(rings)} ring(s)")

    target_w = int(round(map_frame_w / 72 * dpi))
    max_zoom = int(overrides.max_zoom or conf.get("max_zoom") or 17)
    label_size = (
        overrides.label_size or conf.get("label_size") or "large"
    ).lower()
    if label_size not in LABEL_SIZE_BIAS:
        raise SystemExit(
            f"Unknown label_size {label_size!r}. "
            f"Choose from: {', '.join(LABEL_SIZE_BIAS)}"
        )
    zoom_bias = LABEL_SIZE_BIAS[label_size]
    if overrides.zoom_bias is not None:
        zoom_bias = int(overrides.zoom_bias)
    zoom = choose_zoom(bbox, target_w, max_zoom=max_zoom, zoom_bias=zoom_bias)
    print(
        f"Building {conf['name']} ({LAYERS[layer]}, ~{target_w}px wide, "
        f"label_size={label_size}, zoom={zoom})…"
    )
    map_img, pixel_bbox = stitch_map(s, layer, bbox, zoom, workers=workers)

    if rings:
        print("Drawing boundary…")
        draw_boundary(map_img, rings, zoom, pixel_bbox)

    # Downscale to print target if mosaic is larger (saves RAM / PDF size)
    target_h = int(round(map_frame_h / 72 * dpi))
    if map_img.width > target_w * 1.05 or map_img.height > target_h * 1.05:
        scale = min(target_w / map_img.width, target_h / map_img.height)
        new_size = (
            max(1, int(round(map_img.width * scale))),
            max(1, int(round(map_img.height * scale))),
        )
        print(f"  Downscaling {map_img.width}×{map_img.height} → {new_size[0]}×{new_size[1]}")
        map_img = map_img.resize(new_size, Image.Resampling.LANCZOS)

    if overrides.png:
        print(f"Writing PNG {overrides.png} ({map_img.width}×{map_img.height})…")
        map_img.save(overrides.png, optimize=True)

    title = conf.get("title") or conf["name"]
    subtitle = conf.get("subtitle") or ""
    print(f"Writing PDF {out} ({paper} {orientation}, {dpi} dpi)…")
    write_pdf(
        map_img,
        bbox,
        title=title,
        subtitle=subtitle,
        layer=layer,
        paper=paper,
        orientation=orientation,
        dpi=dpi,
        out_path=Path(out),
    )
    size_mb = Path(out).stat().st_size / (1024 * 1024)
    print(f"Done. {Path(out).resolve()} ({size_mb:.1f} MB)")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "map",
        nargs="?",
        help="Map name from maps.yaml (or 'list' to show available maps)",
    )
    p.add_argument("-c", "--config", type=Path, default=CONFIG_PATH, help="Path to maps.yaml")
    p.add_argument("-o", "--output", type=Path, default=None, help="Output PDF path")
    p.add_argument("--dpi", type=int, default=None, help="Override print DPI")
    p.add_argument("--paper", choices=list(PAPERS), default=None, help="Override paper size")
    p.add_argument(
        "--orientation",
        choices=("landscape", "portrait"),
        default=None,
        help="Override orientation",
    )
    p.add_argument("--layer", choices=list(LAYERS), default=None, help="Override map layer")
    p.add_argument("--workers", type=int, default=None, help="Parallel tile downloads")
    p.add_argument("--max-zoom", type=int, default=None, help="Cap WMTS zoom (default 17)")
    p.add_argument(
        "--label-size",
        choices=list(LABEL_SIZE_BIAS),
        default=None,
        help="Map label size: large/xlarge = bigger place names (default: large)",
    )
    p.add_argument(
        "--zoom-bias",
        type=int,
        default=None,
        help="Manual zoom offset (negative = larger labels), overrides --label-size",
    )
    p.add_argument("--png", type=Path, default=None, help="Also write a PNG")
    p.add_argument("--no-boundary", action="store_true", help="Skip kommune outline")
    p.add_argument("--boundary", action="store_true", help="Force kommune outline")
    p.add_argument(
        "--fit-paper-aspect",
        dest="fit_paper_aspect",
        action="store_true",
        default=None,
        help="Expand bbox to fill the page",
    )
    p.add_argument(
        "--no-fit-paper-aspect",
        dest="fit_paper_aspect",
        action="store_false",
        help="Keep exact bbox (may letterbox)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    maps = cfg.get("maps") or {}

    if not args.map or args.map == "list":
        print("Available maps:")
        for name, m in maps.items():
            paper = m.get("paper", "?")
            ori = m.get("orientation", "?")
            title = m.get("title", name)
            print(f"  {name:20s}  {paper} {ori:10s}  {title}")
        print("\nRun:  python make_map.py <name> [--dpi 200] [--orientation portrait]")
        return 0

    conf = merge_map_config(cfg, args.map)
    return build_map(conf, args)


if __name__ == "__main__":
    raise SystemExit(main())
