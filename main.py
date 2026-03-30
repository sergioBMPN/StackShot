#!/usr/bin/env python3
"""
Sony A7 III Remote Control — Focus Bracket Tool
================================================
Controls a Sony Alpha 7 III camera via USB (gphoto2).
Features:
  - Live view preview
  - ISO / Aperture / Shutter Speed / WB adjustment
  - Manual focus drive (Near/Far with configurable step size)
  - Automated focus bracketing between two user-defined points

Requirements:
  - macOS with Python 3.10+
  - pip install gphoto2 Pillow
  - Camera set to USB mode: "PC Remote"
  - Camera in MF or DMF focus mode for focus bracket

Usage:
  python main.py
"""

import logging
import sys


def main():
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Optionally enable gphoto2 debug logging
    if "--debug" in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            import gphoto2 as gp
            gp.use_python_logging()
        except ImportError:
            pass

    from gui import App

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
