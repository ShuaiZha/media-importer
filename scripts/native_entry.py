"""PyInstaller console entry; deliberately no GUI/.app bundle."""
from media_importer.native.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
