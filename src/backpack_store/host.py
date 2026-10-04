"""Local application binding; no default path, database discovery or model imports."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from .store import Backpack

if TYPE_CHECKING:
    from hidden_moves.adapters import CapabilityCatalog


def build_catalog(
    store: Backpack, names: Iterable[str], *, allow_writes: bool = False
) -> CapabilityCatalog:
    """Select operations on a configured store; writes require separate host intent."""
    from hidden_moves import Moves, discover_providers, load_provider
    from hidden_moves.adapters import CapabilityCatalog

    from .integration import WRITE_CAPABILITIES

    names = tuple(names)
    if not names:
        raise ValueError("Select at least one Backpack capability.")
    if not allow_writes and any(name in WRITE_CAPABILITIES for name in names):
        raise ValueError(
            "Select the local write profile explicitly with allow_writes=True."
        )
    entries = [entry for entry in discover_providers() if entry.name == "backpack"]
    if len(entries) != 1:
        raise ValueError("Install exactly one Backpack provider.")
    moves = Moves(target=store)
    load_provider(entries[0], moves.registry)
    return CapabilityCatalog(moves, names)


async def serve_store(
    store: Backpack, names: Iterable[str], *, allow_writes: bool = False
) -> None:
    """Serve selected tools over stdio; the calling application owns cleanup."""
    from hidden_moves_mcp import MCPAdapter, serve_stdio

    catalog = build_catalog(store, names, allow_writes=allow_writes)
    await serve_stdio(MCPAdapter(catalog).server("backpack"))
