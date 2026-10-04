import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import patch

from fixture_models import Note
from hidden_moves import Moves, UnknownMoveError, discover_providers, load_provider
from hidden_moves.adapters import (
    CapabilityArgumentError,
    CapabilityCatalog,
    CapabilityExposureError,
)
from hidden_moves_mcp import MCPAdapter
from hidden_moves_openai import FunctionToolAdapter
from mcp import Client, MCPError, StdioServerParameters

from backpack_store import (
    Backpack,
    DataclassCodec,
    Provenance,
    UnknownTypeError,
    ValidationError,
)
from backpack_store.host import build_catalog
from backpack_store.integration import (
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
    get_record,
    provide_moves,
)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-configured-path.sqlite"
        self.store = Backpack.open(self.path)
        self.addCleanup(self.store.close)
        self.store.register(DataclassCodec(Note, type_key="notes.note"))

    def test_installed_discovery_and_factory_open_no_database(self):
        (entry,) = [entry for entry in discover_providers() if entry.name == "backpack"]
        with patch(
            "backpack_store.Backpack.open",
            side_effect=AssertionError("database access"),
        ):
            self.assertEqual(len(provide_moves()), 4)
            moves = Moves()
            load_provider(entry, moves.registry)
            for name in (*READ_CAPABILITIES, *WRITE_CAPABILITIES):
                definition = moves.describe(name)
                self.assertFalse(definition.available)
                self.assertEqual(definition.schema_errors, ())
                self.assertNotIn("store", definition.input_schema["properties"])
                self.assertNotIn("path", definition.input_schema["properties"])
                self.assertFalse(definition.annotations.external)
            with self.assertRaises(CapabilityExposureError):
                CapabilityCatalog(moves, [READ_CAPABILITIES[0]])

    def test_selected_read_and_write_profiles_and_effects(self):
        written = self.store.put(Note("before", "local"))
        read = build_catalog(self.store, READ_CAPABILITIES)
        definition = read.describe(READ_CAPABILITIES[0])
        self.assertTrue(definition.annotations.read_only)
        self.assertNotIn(str(self.path), json.dumps(definition.to_dict()))
        with self.assertRaises(UnknownMoveError):
            read.invoke(WRITE_CAPABILITIES[1], {"record_id": written.id})
        with self.assertRaises(ValueError):
            build_catalog(self.store, WRITE_CAPABILITIES)
        write = build_catalog(self.store, WRITE_CAPABILITIES, allow_writes=True)
        inserted = write.invoke(
            WRITE_CAPABILITIES[0],
            {
                "type_key": "notes.note",
                "payload": {"title": "after", "body": "local"},
                "tags": ["saved"],
                "provenance": {"source_system": "fixture"},
            },
        )
        self.assertEqual(self.store.get(inserted.id), Note("after", "local"))
        self.assertEqual(
            self.store.get_raw(inserted.id).provenance,
            Provenance(source_system="fixture"),
        )
        self.assertFalse(write.describe(WRITE_CAPABILITIES[0]).annotations.idempotent)
        self.assertTrue(write.describe(WRITE_CAPABILITIES[1]).annotations.destructive)
        self.assertTrue(write.invoke(WRITE_CAPABILITIES[1], {"record_id": inserted.id}))
        self.assertEqual(self.store.get(written.id).title, "before")

    def test_registered_types_validation_and_bounded_find(self):
        catalog = build_catalog(
            self.store, (*READ_CAPABILITIES, *WRITE_CAPABILITIES), allow_writes=True
        )
        with self.assertRaises(UnknownTypeError):
            catalog.invoke(
                WRITE_CAPABILITIES[0], {"type_key": "untrusted.module", "payload": {}}
            )
        with self.assertRaises(ValidationError):
            catalog.invoke(
                WRITE_CAPABILITIES[0],
                {"type_key": "notes.note", "payload": {"title": 3, "body": "b"}},
            )
        with self.assertRaises(CapabilityArgumentError):
            catalog.invoke(
                READ_CAPABILITIES[1], {"type_key": "notes.note", "path": "/arbitrary"}
            )
        for index in range(3):
            self.store.put(Note(str(index), "text"), tags=["selected"])
        page = catalog.invoke(
            READ_CAPABILITIES[1],
            {"type_key": "notes.note", "tags": ["selected"], "limit": 2},
        )
        self.assertEqual(len(page.records), 2)
        self.assertIsNotNone(page.next_cursor)
        with self.assertRaises(ValidationError):
            catalog.invoke(
                READ_CAPABILITIES[1], {"type_key": "notes.note", "limit": 1001}
            )

    def test_host_requires_unique_provider_and_explicit_selection(self):
        with self.assertRaises(ValueError):
            build_catalog(self.store, ())
        for entries in (
            (),
            (
                EntryPoint("backpack", "a:factory", "hidden_moves.moves"),
                EntryPoint("backpack", "b:factory", "hidden_moves.moves"),
            ),
        ):
            with (
                patch("hidden_moves.discover_providers", return_value=entries),
                self.assertRaises(ValueError),
            ):
                build_catalog(self.store, READ_CAPABILITIES)


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "records.sqlite"
        self.store = Backpack.open(self.path)
        self.addCleanup(self.store.close)
        self.store.register(DataclassCodec(Note, type_key="notes.note"))
        self.written = self.store.put(Note("fixture", "offline"), tags=["saved"])

    async def test_python_catalog_mcp_and_function_tools_agree(self):
        name, arguments = READ_CAPABILITIES[0], {"record_id": self.written.id}
        expected = asdict(get_record(self.store, self.written.id))
        catalog = build_catalog(self.store, [name])
        self.assertEqual(
            catalog.serialize_result(name, catalog.invoke(name, arguments)), expected
        )
        async with Client(MCPAdapter(catalog).server()) as client:
            listing = await client.list_tools()
            self.assertEqual([tool.name for tool in listing.tools], [name])
            result = await client.call_tool(name, arguments)
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, {"result": expected})
        adapter = FunctionToolAdapter(catalog)
        (tool,) = adapter.tools()
        result = await adapter.call_output("fixture-call", tool["name"], arguments)
        self.assertEqual(json.loads(result["output"]), expected)

    async def test_explicit_mcp_write_profile_inserts_and_deletes_locally(self):
        catalog = build_catalog(self.store, WRITE_CAPABILITIES, allow_writes=True)
        async with Client(MCPAdapter(catalog).server()) as client:
            inserted = await client.call_tool(
                WRITE_CAPABILITIES[0],
                {
                    "type_key": "notes.note",
                    "payload": {"title": "written", "body": "local"},
                    "tags": ["new"],
                },
            )
            self.assertFalse(inserted.is_error)
            write = inserted.structured_content["result"]
            self.assertEqual(write["revision"], 1)
            self.assertEqual(self.store.get(write["id"]), Note("written", "local"))
            deleted = await client.call_tool(
                WRITE_CAPABILITIES[1], {"record_id": write["id"]}
            )
            self.assertEqual(deleted.structured_content, {"result": True})
            self.assertEqual(self.store.find(Note, tags=["new"]).records, ())
            with self.assertRaises(MCPError):
                await client.call_tool(
                    READ_CAPABILITIES[0], {"record_id": self.written.id}
                )

    async def test_real_stdio_host_reopens_database_and_rejects_unselected_writes(self):
        expected = asdict(get_record(self.store, self.written.id))
        self.store.close()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("stdio_host.py")), str(self.path)],
        )
        async with (
            asyncio.timeout(20),
            Client(parameters, read_timeout_seconds=10) as client,
        ):
            listing = await client.list_tools()
            self.assertEqual(
                {tool.name for tool in listing.tools}, set(READ_CAPABILITIES)
            )
            result = await client.call_tool(
                READ_CAPABILITIES[0], {"record_id": self.written.id}
            )
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, {"result": expected})
            page = await client.call_tool(
                READ_CAPABILITIES[1],
                {"type_key": "notes.note", "tags": ["saved"], "limit": 1},
            )
            self.assertEqual(page.structured_content["result"]["records"], [expected])
            with self.assertRaises(MCPError):
                await client.call_tool(
                    WRITE_CAPABILITIES[1], {"record_id": self.written.id}
                )
        with Backpack.open(self.path) as reopened:
            reopened.register(DataclassCodec(Note, type_key="notes.note"))
            self.assertEqual(reopened.get(self.written.id), Note("fixture", "offline"))
