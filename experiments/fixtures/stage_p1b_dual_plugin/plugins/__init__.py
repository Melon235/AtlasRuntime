"""Vendored channel plugins.

Each package under this directory is an independent producer adapter: it parses its own transport
and exposes its own native record. Neither package imports ``core``.
"""

from __future__ import annotations

__all__: list[str] = []
