#!/usr/bin/env python3
"""Backward-compatible wrapper — prefer: python make_map.py aurland_kommune """

from make_map import main

if __name__ == "__main__":
    raise SystemExit(main(["aurland_kommune", *(__import__("sys").argv[1:])]))
