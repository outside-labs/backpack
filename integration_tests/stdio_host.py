"""Offline test application: explicit path, model registration and read selection."""

import asyncio
import sys

from fixture_models import Note

from backpack_store import Backpack, DataclassCodec
from backpack_store.host import serve_store
from backpack_store.integration import READ_CAPABILITIES

with Backpack.open(sys.argv[1]) as store:
    store.register(DataclassCodec(Note, type_key="notes.note"))
    asyncio.run(serve_store(store, READ_CAPABILITIES))
