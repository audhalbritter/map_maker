# Map maker (NatuRA Workshop 2026)

Print-ready **Norgeskart** maps (Kartverket) for Aurland sites, as A0/A1 PDFs for a print shop.

The script downloads colour topo tiles from `cache.kartverket.no`, stitches them for a named extent, and writes a PDF with title, scale bar, and attribution.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Map extents and print settings live in `maps.yaml`.

```bash
python make_map.py list                 # show available maps
python make_map.py vikesland            # write vikesland_A0.pdf
python make_map.py aurland_kommune      # write aurland_kommune_A1.pdf
python make_map.py stemmerdalen --dpi 250
python make_map.py rausmesdalen --label-size xlarge
```

Useful flags (override `maps.yaml`): `--dpi`, `--paper`, `--orientation`, `--label-size`, `-o`.

`label_size` controls how large place names appear (`detail` / `medium` / `large` / `xlarge`). Larger labels use a slightly coarser zoom.

## Adding a map

Add an entry under `maps:` in `maps.yaml` with a WGS84 bounding box:

```yaml
  my_site:
    title: My site
    paper: A0
    orientation: portrait
    bbox:
      west: 7.10
      south: 60.80
      east: 7.25
      north: 60.90
```

Then run `python make_map.py my_site`.

## Notes

- Output PDFs, screenshots (`maps/`), and the tile cache (`.tile_cache/`) are gitignored.
- Kartdata: © Kartverket (`norgeskart.no` / `cache.kartverket.no`).
