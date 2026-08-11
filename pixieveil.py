#!/usr/bin/env python3.12

"""
PixieVeil Application Entry Point
=================================

Thin executable shim around :func:`pixieveil.cli.run`. Kept as a top-level
script (rather than folded into the ``pixieveil`` package) so it can be run
directly with ``python3 pixieveil.py`` without requiring the package to be
installed.
"""

from pixieveil.cli import run

if __name__ == "__main__":
    run()
