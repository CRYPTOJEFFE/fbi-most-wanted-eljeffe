import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from eljeffe.demo import demo_snapshot
from eljeffe.model import schema_errors
from eljeffe.service import Workspace, default_data_dir


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eljeffe path ")
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "eljeffe", "--data-dir", str(self.path), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_end_to_end_cli_export_and_reimport(self):
        init = self.command("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(json.loads(init.stdout)["total"], 0)
        demo = self.command("demo")
        self.assertEqual(demo.returncode, 0, demo.stderr)
        search = self.command("search", "fictional", "--kind", "wanted", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        found = json.loads(search.stdout)
        self.assertEqual(found["total"], 1)
        show = self.command("show", "demo-person-001", "--source", "demo")
        self.assertEqual(show.returncode, 0, show.stderr)
        output = self.path / "exports" / "records.ftm.json"
        exported = self.command("export", "--output", str(output), "--source", "demo")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        manifest = json.loads(Path(str(output) + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["count"], 5)
        self.assertEqual(len(manifest["sha256"]), 64)
        other = Workspace(self.path / "second workspace")
        result = other.import_file(output)
        self.assertEqual(result["inserted"], 5)

    def test_missing_file_and_bad_data_fail_cleanly(self):
        result = self.command("import", str(self.path / "missing.ftm.json"))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        bad = self.path / "bad.ftm.json"
        bad.write_text('{"id":"x","schema":"Person","properties":{"name":"bad"}}\n', encoding="utf-8")
        result = self.command("import", str(bad))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(self.command("stats").stdout)["total"], 0)

    def test_database_cannot_be_export_target(self):
        self.command("demo")
        result = self.command("export", "-o", str(self.path / "records.sqlite3"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(self.command("stats").stdout)["total"], 5)

    def test_demo_entities_have_real_schema_property_names(self):
        for entity in demo_snapshot().entities:
            self.assertEqual(schema_errors(entity), [], entity["id"])

    def test_import_filename_never_escapes_workspace(self):
        workspace = Workspace(self.path)
        content = json.dumps({"id": "demo", "schema": "Person", "properties": {"name": ["Example"]}})
        workspace.import_content("../../escape.ftm.json", content)
        self.assertEqual(workspace.store.stats()["total"], 1)
        self.assertEqual(len(list(workspace.imports.iterdir())), 1)

    def test_redirected_cli_output_uses_utf8_even_under_legacy_encoding(self):
        self.command("demo")
        result = subprocess.run(
            [sys.executable, "-m", "eljeffe", "--data-dir", str(self.path), "search", "--json"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "ascii"}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(data["total"], 5)

    def test_attachment_request_for_former_id_uses_resolved_canonical_id(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        workspace = Workspace(self.path)
        value = {"id": "new-id", "schema": "Person", "properties": {"name": ["Example"]},
                 "referents": ["old-id"]}
        workspace.store.ingest(SimpleNamespace(source="test", entities=[value], complete=True, metadata={}))
        with patch("eljeffe.service.download_attachments", return_value=[{"url": "https://www.fbi.gov/x.pdf", "status": "downloaded"}]):
            result = workspace.fetch_files("old-id", "test")
        self.assertEqual(result["id"], "new-id")
        self.assertEqual(workspace.store.get("new-id")["entity"]["files"][0]["status"], "downloaded")


if __name__ == "__main__":
    unittest.main()
