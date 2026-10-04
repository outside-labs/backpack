"""A local application with its own trusted model and explicit exposure profile."""

import argparse
import asyncio
from dataclasses import dataclass

from backpack_store import Backpack, DataclassCodec
from backpack_store.host import serve_store
from backpack_store.integration import READ_CAPABILITIES, WRITE_CAPABILITIES


@dataclass(frozen=True)
class Note:
    title: str
    body: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve typed notes from an explicit local SQLite file."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--profile", choices=("read", "write"), required=True)
    args = parser.parse_args()
    names = (
        READ_CAPABILITIES
        if args.profile == "read"
        else (*READ_CAPABILITIES, *WRITE_CAPABILITIES)
    )
    with Backpack.open(args.database) as store:
        store.register(DataclassCodec(Note, type_key="notes.note"))
        asyncio.run(serve_store(store, names, allow_writes=args.profile == "write"))


if __name__ == "__main__":
    main()
