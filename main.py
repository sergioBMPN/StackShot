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
import tkinter as tk

MIN_PYTHON = (3, 10)
MIN_TK = "8.6"


def main():
    # Configure logging
    debug = "--debug" in sys.argv
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)
    logger.info("StackShot starting...  (debug=%s)", debug)
    logger.info("Python %s on %s", sys.version, sys.platform)

    # Check Python version
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(you have {sys.version}). "
            f"Install Python 3.13 from https://www.python.org/downloads/macos/"
        )

    # Check Tk version
    _root = tk.Tk()
    _root.withdraw()
    tk_version = _root.tk.call("info", "patchlevel")
    logger.info("Tcl/Tk version: %s", tk_version)
    if not str(tk_version).startswith(MIN_TK):
        logger.warning(
            "Tk %s detected — Tk 8.6+ required for proper rendering. "
            "Install Python from python.org (includes modern Tk).",
            tk_version,
        )
    _root.destroy()

    # Optionally enable gphoto2 debug logging
    if debug:
        try:
            import gphoto2 as gp
            gp.use_python_logging()
            logger.debug("gphoto2 debug logging enabled")
        except ImportError:
            logger.debug("gphoto2 not available (skip debug logging)")

    logger.debug("Importing gui.App...")
    from gui import App

    logger.info("Creating App window...")
    app = App()
    logger.info("GUI ready — entering mainloop")
    app.mainloop()


if __name__ == "__main__":
    main()
