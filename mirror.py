#!/usr/bin/env python3
"""Entrypoint: mirror the podcast feed to R2, or verify the published mirror."""

import sys

from podcast_mirror.cli import main

if __name__ == "__main__":
    sys.exit(main())
