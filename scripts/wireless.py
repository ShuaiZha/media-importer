#!/usr/bin/env python3
"""Run on the Mac, outside Docker; see docs/wireless.md."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_importer.wireless_host import main

if __name__ == '__main__':
    main()
