from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from eljeffe.server import LocalServer
from eljeffe.service import Workspace


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name))
        self.server = LocalServer(("127.0.0.1", 0), self.workspace)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None, raw=False):
        headers = dict(headers or {})
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(data).encode()
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        body = response.read()
        status, result_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, body if raw else json.loads(body), result_headers

    def token_headers(self):
        return {"X-CSRF-Token": self.server.csrf}

    def test_empty_start_demo_details_and_export_bundle(self):
        self.assertEqual(self.request("GET", "/api/stats")[1]["total"], 0)
        status, data, _ = self.request("POST", "/api/demo", {}, self.token_headers())
        self.assertEqual(status, 200)
        self.assertEqual(data["inserted"], 5)
        status, data, _ = self.request("GET", "/api/entities?kind=wanted")
        self.assertEqual(data["total"], 1)
        record = self.request("GET", "/api/entities/demo-company-002?source=demo")[1]
        self.assertEqual(record["related"][0]["schema"], "Sanction")
        status, body, headers = self.request("GET", "/api/export?source=demo&bundle=1", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            lines = archive.read("eljeffe.ftm.json").splitlines()
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(len(lines), 5)
        self.assertEqual(manifest["count"], 5)
        self.assertEqual(manifest["sources"][0]["id"], "demo")
        import hashlib
        self.assertEqual(manifest["sha256"], hashlib.sha256(b"\n".join(lines) + b"\n").hexdigest())

    def test_host_origin_and_token_protect_local_writes(self):
        self.assertEqual(self.request("GET", "/api/config", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", {})[0], 403)
        headers = {**self.token_headers(), "Origin": "https://evil.example"}
        self.assertEqual(self.request("POST", "/api/demo", {}, headers)[0], 403)
        headers = {**self.token_headers(), "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("POST", "/api/demo", {}, headers)[0], 403)
        self.assertEqual(self.workspace.store.stats()["total"], 0)

    def test_input_errors_return_json_and_keep_server_working(self):
        for path in ("/api/entities?limit=-1", "/api/entities?offset=no", "/api/entities?inactive=true"):
            status, result, _ = self.request("GET", path)
            self.assertEqual(status, 400)
            self.assertIn("error", result)
        status, _, _ = self.request("POST", "/api/import", {"filename": "oops", "content": "broken"}, self.token_headers())
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/stats")[1]["total"], 0)

    def test_readonly_files_never_expose_database(self):
        status, _, _ = self.request("GET", "/files/%2e%2e/records.sqlite3")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/../records.sqlite3")
        self.assertEqual(status, 404)
        with self.assertRaises(ValueError):
            LocalServer(("0.0.0.0", 0), self.workspace)

    def test_sync_job_errors_are_visible_without_destroying_data(self):
        self.workspace.demo()
        with patch.object(self.workspace, "sync", side_effect=RuntimeError("Source returned HTTP 403")):
            status, data, _ = self.request("POST", "/api/sync", {"source": "fbi"}, self.token_headers())
            self.assertEqual(status, 202)
            job_id = data["job"]["id"]
            for _ in range(30):
                job = self.request("GET", "/api/jobs/" + job_id)[1]
                if job["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(job["status"], "failed")
        self.assertIn("403", job["error"])
        self.assertEqual(self.workspace.store.stats()["active"], 5)

    def test_plain_ftm_export_and_historical_export_guard(self):
        self.workspace.demo()
        status, body, _ = self.request("GET", "/api/export?kind=pep", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["properties"]["topics"], ["role.pep"])
        self.assertEqual(self.request("GET", "/api/export?inactive=1")[0], 400)


if __name__ == "__main__":
    unittest.main()
