"""Provider adapters: one module per delegate target."""

from __future__ import annotations

from .base import Event, Job, Provider
from .claude import Claude
from .codex import Codex
from .cursor import Cursor
from .grok import Grok
from .pi import Pi

REGISTRY: dict[str, Provider] = {
    p.name: p for p in (Grok(), Claude(), Codex(), Cursor(), Pi())
}

NAMES = tuple(REGISTRY.keys())


def get(name: str) -> "Provider | None":
    prototype = REGISTRY.get(name)
    return type(prototype)() if prototype is not None else None


__all__ = ["Event", "Job", "Provider", "REGISTRY", "NAMES", "get"]
