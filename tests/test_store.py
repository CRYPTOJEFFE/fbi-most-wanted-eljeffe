import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from eljeffe.model import schema_errors
from eljeffe.store import Store


def entity(identifier="a", name="Example Person", schema="Person", **properties):
    return {"id": identifier, "schema": schema,
            "properties": {"name": [name], **properties}}


def snapshot(entities, source="test", complete=True):
    return SimpleNamespace(source=source, entities=entities,
                           complete=complete, metadata={"label": "Test source"})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "records.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_invalid_snapshot_rolls_back_including_retirements(self):
        self.store.ingest(snapshot([entity("a"), entity("b")]))
        def broken():
            yield entity("a", "Changed")
            yield {"id": "c", "schema": "Person", "properties": {"name": "not a list"}}
        with self.assertRaises(ValueError):
            self.store.ingest(snapshot(broken()))
        self.assertEqual(self.store.search()["total"], 2)
        self.assertEqual(self.store.get("a")["entity"]["caption"], "Example Person")
        self.assertEqual(len(self.store.history()), 1)

    def test_full_snapshot_marks_missing_as_historical(self):
        self.store.ingest(snapshot([entity("a"), entity("b")]))
        result = self.store.ingest(snapshot([entity("a")]))
        self.assertEqual(result["inactive"], 1)
        self.assertEqual(self.store.search()["total"], 1)
        self.assertEqual(self.store.search(inactive=True)["total"], 2)
        self.assertFalse(self.store.get("b")["entity"]["active"])

    def test_partial_snapshot_never_retires_unseen_records(self):
        self.store.ingest(snapshot([entity("a"), entity("b")]))
        self.store.ingest(snapshot([entity("a", "New name")], complete=False))
        self.assertEqual(self.store.search()["total"], 2)
        self.assertFalse(self.store.stats()["sources"][0]["complete"])

    def test_empty_and_duplicate_snapshots_preserve_data(self):
        self.store.ingest(snapshot([entity()]))
        for data in ([], [entity("b"), entity("b", "collision")]):
            with self.assertRaises(ValueError):
                self.store.ingest(snapshot(data))
        self.assertEqual(self.store.search()["total"], 1)

    def test_same_id_is_separate_per_source_and_requires_scoped_export(self):
        self.store.ingest(snapshot([entity("a", "Source one")], source="one"))
        self.store.ingest(snapshot([entity("a", "Source two")], source="two"))
        self.assertEqual(self.store.stats()["active"], 2)
        with self.assertRaisesRegex(ValueError, "multiple sources"):
            self.store.get("a")
        with self.assertRaisesRegex(ValueError, "share an entity ID"):
            list(self.store.export())
        self.assertEqual(list(self.store.export(source="one"))[0]["properties"]["name"], ["Source one"])
        self.store.ingest(snapshot([entity("b")], source="one"))
        self.assertTrue(self.store.get("a", "two")["entity"]["active"])

    def test_search_handles_unicode_aliases_and_literal_wildcards(self):
        self.store.ingest(snapshot([
            entity("a", "José Rivera", alias=["Jóse 100%_literal"]),
            entity("b", "Unrelated"),
        ]))
        for query in ("JOSE", "rivera", "100%_literal"):
            self.assertEqual(self.store.search(q=query)["total"], 1)
        self.assertEqual(self.store.search(q="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.store.search(q="%")["total"], 1)

    def test_topics_never_make_pep_into_wanted(self):
        self.store.ingest(snapshot([
            entity("a", topics=["role.pep"]), entity("b", topics=["wanted"]),
            entity("c", topics=["sanction.linked"]), entity("d", topics=["sanction"]),
        ]))
        self.assertEqual(self.store.search(kind="wanted")["total"], 1)
        self.assertEqual(self.store.search(kind="sanction")["total"], 1)
        self.assertEqual(self.store.search(kind="pep")["total"], 1)

    def test_relations_use_schema_types_and_literal_ids(self):
        self.store.ingest(snapshot([
            entity("person_%1"), entity("fake", name="person_%1", notes=["person_%1"]),
            {"id": "listing", "schema": "Sanction", "properties": {"entity": ["person_%1"], "reason": ["Example"]}},
        ]))
        related = self.store.get("person_%1")["related"]
        self.assertEqual([item["id"] for item in related], ["listing"])
        self.assertEqual(self.store.get("listing")["related"][0]["id"], "person_%1")

    def test_export_keeps_native_metadata_but_not_local_envelope(self):
        value = entity(topics=["wanted"])
        value.update(datasets=["test"], referents=["old-a"], first_seen="2024", last_change="2025", _files=[{"url": "https://www.fbi.gov/x.pdf"}])
        self.store.ingest(snapshot([value]))
        exported = list(self.store.export())[0]
        self.assertNotIn("_files", exported)
        self.assertNotIn("active", exported)
        self.assertEqual(exported["referents"], ["old-a"])
        self.assertEqual(schema_errors(exported), [])
        with self.assertRaisesRegex(ValueError, "current observations only"):
            list(self.store.export(inactive=True))

    def test_filters_pagination_and_deterministic_sort(self):
        self.store.ingest(snapshot([entity("b", "Bravo"), entity("a", "Alpha")]))
        self.assertEqual(self.store.search(limit=1, offset=1)["items"][0]["id"], "b")
        self.assertEqual(self.store.search(schema="Company")["total"], 0)
        for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"q": "x" * 301}):
            with self.assertRaises(ValueError):
                self.store.search(**kwargs)

    def test_multiple_topics_are_searchable_under_each_applicable_category(self):
        self.store.ingest(snapshot([entity(topics=["wanted", "sanction", "role.pep"])]))
        for kind in ("wanted", "sanction", "pep"):
            self.assertEqual(self.store.search(kind=kind)["total"], 1)
            self.assertEqual(self.store.stats()["kinds"][kind], 1)

    def test_source_declared_referents_resolve_without_name_matching(self):
        value = entity("new-id")
        value["referents"] = ["old-id"]
        self.store.ingest(snapshot([value]))
        result = self.store.get("old-id", "test")
        self.assertEqual(result["resolved_from"], "old-id")
        self.assertEqual(result["entity"]["id"], "new-id")
        self.assertEqual(self.store.search(q="old-id")["total"], 1)

    def test_existing_historical_id_points_to_source_declared_successor(self):
        self.store.ingest(snapshot([entity("old-id")]))
        value = entity("new-id")
        value["referents"] = ["old-id"]
        self.store.ingest(snapshot([value]))
        original = self.store.get("old-id")["entity"]
        self.assertFalse(original["active"])
        self.assertEqual(original["replaced_by"][0]["id"], "new-id")

    def test_export_provenance_and_entities_share_one_database_snapshot(self):
        self.store.ingest(snapshot([entity("before")]))
        with self.store.export_snapshot() as (stream, sources):
            # A WAL writer can commit while the exporter retains its read view.
            other = Store(self.store.path)
            other.ingest(snapshot([entity("after")]))
            self.assertEqual([row["id"] for row in stream], ["before"])
            self.assertEqual(sources[0]["id"], "test")
        self.assertEqual(self.store.search()["items"][0]["id"], "after")

    def test_downloaded_file_metadata_survives_source_without_attachment_fields(self):
        self.store.ingest(snapshot([entity()]))
        files = [{"url": "https://www.fbi.gov/example.pdf", "status": "downloaded", "sha256": "abc"}]
        self.store.set_files("test", "a", files)
        self.store.ingest(snapshot([entity()]))
        self.assertEqual(self.store.get("a")["entity"]["files"], files)
        value = entity()
        value["_files"] = []
        self.store.ingest(snapshot([value]))
        self.assertEqual(self.store.get("a")["entity"]["files"], [])

    def test_former_pep_is_searchable_without_making_associates_peps(self):
        self.store.ingest(snapshot([entity("a", topics=["role.pep.frmr"]), entity("b", topics=["role.rca"])]))
        self.assertEqual(self.store.search(kind="pep")["total"], 1)
        self.assertEqual(self.store.search(kind="other")["total"], 1)


if __name__ == "__main__":
    unittest.main()
