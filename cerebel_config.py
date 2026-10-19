"""Shared CeRebel runtime flags (env + optional CLI helpers)."""

from __future__ import annotations

import os


def _truthy(value: str | None, default: str = "0") -> bool:
    return (value if value is not None else default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def fake_egocentric_enabled() -> bool:
    """When set, UIs hide the raw input video and show the GEM-X SOMA mesh instead."""
    return _truthy(os.environ.get("CEREBEL_FAKE_EGOCENTRIC"))


def apply_cli_flags(argv: list[str] | None = None) -> list[str]:
    """Consume known CeRebel flags from argv; return the remaining args.

    Recognized:
      --fake-egocentric / --fake-ego
      --no-fake-egocentric
    """
    import sys

    if argv is None:
        argv = sys.argv[1:]
    remaining: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--fake-egocentric", "--fake-ego"):
            os.environ["CEREBEL_FAKE_EGOCENTRIC"] = "1"
        elif arg in ("--no-fake-egocentric", "--no-fake-ego"):
            os.environ["CEREBEL_FAKE_EGOCENTRIC"] = "0"
        else:
            remaining.append(arg)
        i += 1
    return remaining
