"""ReleaseGuard: offline release readiness auditing.

The package is deliberately layered. ``metadata`` reads a manifest, ``artifacts`` reads a build
directory, and ``audit`` is the only module that knows both exist. Importing this package imports
nothing that touches the filesystem.
"""

from __future__ import annotations

__all__: list[str] = []
