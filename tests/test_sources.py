"""Offline source-contract, normalization, provenance, and download tests."""

import gzip
import hashlib
import http.client
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from eljeffe.ftm import classification, fbi_entity, iso_date, plain_text, validate_entity
from eljeffe.network import Client, Download, NetworkError, _CheckedRedirect, utc_now, validate_url
from eljeffe.model import schema_errors
from eljeffe.sources import (
    DatasetPageParser, SourceError, _FBIFileParser, discover_dataset,
    download_attachments, fetch_fbi, fetch_opensanctions, read_ftm_file,
)


def record(identifier="person-1", **extra):
    return {"id": identifier, "schema": "Person", "properties": {"name": ["Fixture Person"]}, **extra}


def response(url, body):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    if isinstance(body, str):
        body = body.encode()
    return Download(url=url, body=body, size=len(body), sha256=hashlib.sha256(body).hexdigest(),
                    sha1=hashlib.sha1(body).hexdigest(), fetched_at=utc_now(),
                    content_type="application/json", headers={})


class FakeClient:
    def __init__(self, mapping=None, robots=True):
        self.mapping = mapping or {}
        self.robots = robots
        self.calls = []
        self.downloads = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Download) else response(url, value)

    def download(self, url, path, **kwargs):
        self.downloads.append(url)
        value = self.get(url)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(value.body)
        value.path = str(Path(path).resolve())
        return value

    def robots_allowed(self, url):
        if isinstance(self.robots, Exception):
            raise self.robots
        return self.robots


DATASET = "us_fbi_most_wanted"
INDEX = f"https://data.opensanctions.org/datasets/latest/{DATASET}/index.json"
PAGE = f"https://www.opensanctions.org/datasets/{DATASET}/"
DATA = f"https://data.opensanctions.org/artifacts/{DATASET}/test-version/entities.ftm.json"


def opensanctions_client(entities=None, *, nested=False, count=None, checksum=None, robots=True):
    entities = entities if entities is not None else [record()]
    content = "".join(json.dumps(entity) + "\n" for entity in entities).encode()
    metadata = {
        "name": DATASET, "title": "Fixture wanted source", "version": "test-version",
        "entity_count": len(entities) if count is None else count,
        "resources": [{"name": "entities.ftm.json", "url": DATA, "size": len(content),
                       "checksum": checksum or hashlib.sha1(content).hexdigest()}],
    }
    if nested:
        metadata = {"dataset": metadata}
    return FakeClient({INDEX: metadata, PAGE: f'<a href="{DATA}">FtM</a>', DATA: content}, robots=robots)


class FtMTests(unittest.TestCase):
    def test_official_fugitive_leaf_categories_and_mixed_terrorism_notices(self):
        for category in ("cei", "counterintelligence", "murders", "additional", "wcc", "cac", "human-trafficking", "wanted_terrorists", "dt"):
            item = {"uid": "test-leaf", "title": "Fictional test", "sex": "Male",
                    "url": f"https://www.fbi.gov/wanted/{category}/example", "status": "na"}
            self.assertEqual(classification(fbi_entity(item)), "wanted", category)
            item["status"] = "captured"
            self.assertEqual(classification(fbi_entity(item)), "other", category)
        for category in ("terrorism", "terrorinfo", "seeking-info", "law-enforcement-assistance"):
            item = {"uid": "test-mixed", "title": "Fictional test", "sex": "Male",
                    "url": f"https://www.fbi.gov/wanted/{category}/example"}
            self.assertEqual(classification(fbi_entity(item)), "other", category)

    def test_preserves_full_properties_and_provenance(self):
        entity = record(datasets=["source_a"], referents=["old-id"], first_seen="2021-01-01",
                        last_seen="2026-11-07", last_change="2026-10-30", target=True)
        entity["properties"]["alias"] = ["Alternate name", "Another name"]
        normalized = validate_entity(entity)
        self.assertEqual(entity, normalized)
        normalized["properties"]["name"].append("Different")
        self.assertEqual(len(entity["properties"]["name"]), 1)

    def test_rejects_non_ftm_values(self):
        for value in ("a name", 4, None, [{"id": "nested"}], [4]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_entity({"id": "p1", "schema": "Person", "properties": {"name": value}})
        for value in (None, [], {"id": "", "schema": "Person", "properties": {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_entity(value)

    def test_classification_does_not_equate_targets_and_wanted_people(self):
        self.assertEqual(classification(record(target=True)), "other")
        self.assertEqual(classification(record(properties={"topics": ["role.pep"]})), "pep")
        self.assertEqual(classification(record(properties={"topics": ["role.pep.frmr"]})), "pep")
        self.assertEqual(classification(record(properties={"topics": ["role.rca"]})), "other")
        self.assertEqual(classification(record(properties={"topics": ["sanction"]})), "sanction")
        self.assertEqual(classification(record(properties={"topics": ["sanction.linked"]})), "other")
        self.assertEqual(classification(record(properties={"topics": ["wanted"]})), "wanted")

    def test_dates_preserve_precision_and_do_not_guess(self):
        cases = {"1984": "1984", "1984-03": "1984-03", "March 5, 1984": "1984-03-05",
                 "Mar. 5, 1984": "1984-03-05", "February 30, 1984": None,
                 "03/05/1984": None, "1984-99": None, "0000": None}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(iso_date(value), expected)

    def test_plain_text_removes_executable_markup(self):
        self.assertEqual(plain_text('<p>Public <b>notice</b></p><script>bad()</script> &amp; data'),
                         "Public notice & data")

    def test_fbi_person_mapping_has_no_invented_warning_status_or_location(self):
        source = {"uid": "fixture", "title": "EXAMPLE PERSON", "sex": "Male",
                  "url": "https://www.fbi.gov/wanted/fugitives/example-person",
                  "aliases": ["Alias One", "Alias Two"], "warning_message": None,
                  "dates_of_birth_used": ["March 5, 1984", "1985", "03/05/1984"],
                  "field_offices": ["newark"], "nationality": "American",
                  "modified": "2026-01-01T10:00:00-05:00"}
        entity = fbi_entity(source, retrieved_at="2026-11-07T12:00:00Z")
        self.assertEqual(entity["schema"], "Person")
        self.assertEqual(classification(entity), "wanted")
        self.assertEqual(entity["properties"]["birthDate"], ["1984-03-05", "1985"])
        self.assertEqual(entity["properties"]["gender"], ["male"])
        self.assertEqual(entity["properties"]["modifiedAt"], ["2026-01-01T15:00:00Z"])
        self.assertNotIn("status", entity["properties"])
        self.assertNotIn("address", entity["properties"])
        self.assertNotIn("nationality", entity["properties"])
        self.assertNotIn("armed", json.dumps(entity).casefold())
        self.assertIn("FBI field office (not whereabouts): newark", entity["properties"]["notes"])

    def test_fbi_missing_victim_and_captured_records_are_not_new_wanted_assertions(self):
        base = {"uid": "fixture", "title": "FIXTURE", "sex": "Female"}
        missing = fbi_entity({**base, "url": "https://www.fbi.gov/wanted/kidnap/fixture"})
        victim = fbi_entity({**base, "person_classification": "victim",
                             "url": "https://www.fbi.gov/wanted/fugitives/fixture"})
        captured = fbi_entity({**base, "status": "captured",
                               "url": "https://www.fbi.gov/wanted/topten/fixture"})
        self.assertEqual(classification(missing), "missing")
        self.assertEqual(classification(victim), "other")
        self.assertEqual(classification(captured), "other")
        self.assertEqual(captured["properties"]["status"], ["captured"])

    def test_fbi_group_and_seeking_information_notice_are_not_assumed_people(self):
        entity = fbi_entity({"uid": "group", "title": "EXAMPLE GROUP", "person_classification": "main",
                             "url": "https://www.fbi.gov/wanted/seeking-info/example-group"})
        self.assertEqual(entity["schema"], "Thing")
        self.assertEqual(classification(entity), "other")

    def test_fbi_files_are_separate_from_ftm_properties(self):
        entity = fbi_entity({"uid": "fixture", "title": "EXAMPLE", "sex": "Male",
                             "files": [{"url": "https://www.fbi.gov/wanted/test/@@download.pdf", "name": "English"}],
                             "images": [{"original": "https://www.fbi.gov/wanted/test/photo.jpg"}]})
        self.assertEqual(len(entity["_files"]), 2)
        self.assertNotIn("image", entity["properties"])
        self.assertNotIn("images", entity["properties"])

    def test_generated_fbi_properties_conform_to_bundled_upstream_ontology(self):
        inputs = [
            {"uid": "person", "title": "EXAMPLE PERSON", "sex": "Male", "status": "na",
             "person_classification": "main", "hair": "Brown", "eyes": "Brown",
             "aliases": ["Example Alias"], "dates_of_birth_used": ["May 3, 1980"],
             "place_of_birth": "Source city", "modified": "2026-01-01T12:00:00Z",
             "publication": "2025-01-01", "url": "https://www.fbi.gov/wanted/fugitives/example",
             "description": "Source description", "warning_message": "Source warning"},
            {"uid": "group", "title": "EXAMPLE GROUP", "status": "Unknown",
             "url": "https://www.fbi.gov/wanted/seeking-info/example"},
        ]
        for item in inputs:
            with self.subTest(uid=item["uid"]):
                self.assertEqual(schema_errors(fbi_entity(item)), [])

    def test_fbi_country_normalization_uses_exact_official_model_labels(self):
        entity = fbi_entity({"uid": "country-fixture", "title": "EXAMPLE", "sex": "Male",
                             "nationality": "Mexico", "citizenship": ["US", "Unknown"]})
        self.assertEqual(entity["properties"]["nationality"], ["mx"])
        self.assertEqual(entity["properties"]["citizenship"], ["us"])
        self.assertIn("Nationality (source): Mexico", entity["properties"]["notes"])
        self.assertIn("Citizenship (source): Unknown", entity["properties"]["notes"])


class FileImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_jsonl_and_gzip_import(self):
        raw = json.dumps(record()).encode() + b"\n"
        for name, body in (("entities.jsonl", raw), ("entities.jsonl.gz", gzip.compress(raw))):
            path = self.root / name
            path.write_bytes(body)
            snapshot = read_ftm_file(path, source="import:fixture")
            self.assertEqual(list(snapshot.entities), [record()])
            self.assertEqual(snapshot.source, "import:fixture")
            self.assertEqual(snapshot.metadata["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(snapshot.metadata["entity_count"], 1)

    def test_truncated_second_line_raises_after_first_for_transaction_rollback(self):
        path = self.root / "bad.jsonl"
        path.write_text(json.dumps(record()) + '\n{"id":"truncated"', encoding="utf-8")
        snapshot = read_ftm_file(path)
        iterator = iter(snapshot.entities)
        self.assertEqual(next(iterator)["id"], "person-1")
        with self.assertRaisesRegex(SourceError, "line 2"):
            next(iterator)

    def test_empty_duplicate_keys_and_nonfinite_json_rejected(self):
        for index, body in enumerate((b"\n", b'{"id":"p1","id":"p2","schema":"Person","properties":{}}',
                                      b'{"id":"p1","schema":"Person","properties":{},"score":NaN}')):
            path = self.root / f"bad-{index}.jsonl"
            path.write_bytes(body)
            with self.subTest(index=index), self.assertRaises(SourceError):
                list(read_ftm_file(path).entities)

    def test_truncated_gzip_rejected(self):
        path = self.root / "bad.gz"
        path.write_bytes(gzip.compress(json.dumps(record()).encode())[:-5])
        with self.assertRaises(SourceError):
            list(read_ftm_file(path).entities)

    def test_changed_source_after_hashing_aborts_import(self):
        path = self.root / "changing.jsonl"
        path.write_text(json.dumps(record("original")) + "\n", encoding="utf-8")
        snapshot = read_ftm_file(path)
        path.write_text(json.dumps(record("changed")) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(SourceError, "changed between"):
            list(snapshot.entities)


class OpenSanctionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_flat_and_nested_metadata_and_html_discovery(self):
        for nested in (False, True):
            metadata = discover_dataset(DATASET, client=opensanctions_client(nested=nested), cache_dir=self.root)
            self.assertTrue(metadata["page_scraped"])
            self.assertEqual(metadata["ftm_resource"]["url"], DATA)
            self.assertEqual(metadata["expected_entity_count"], 1)

    def test_scraper_ignores_external_and_other_dataset_links(self):
        parser = DatasetPageParser(PAGE, DATASET)
        parser.feed(f'<a href="{DATA}">valid</a>'
                    '<a href="https://evil.example/entities.ftm.json">bad</a>'
                    '<a href="https://data.opensanctions.org/datasets/latest/another/entities.ftm.json">other</a>')
        self.assertEqual([entry["url"] for entry in parser.resources], [DATA])

    def test_robots_rejection_keeps_documented_bulk_endpoint_usable(self):
        client = opensanctions_client(robots=False)
        snapshot = fetch_opensanctions(DATASET, self.root, client=client)
        self.assertEqual(len(list(snapshot.entities)), 1)
        self.assertFalse(snapshot.metadata["page_scraped"])
        self.assertNotIn(PAGE, client.calls)
        self.assertTrue(snapshot.metadata["warnings"])

    def test_complete_hashed_snapshot_and_verified_cache_reuse(self):
        client = opensanctions_client()
        snapshot = fetch_opensanctions(DATASET, self.root, client=client)
        self.assertEqual(snapshot.source, "opensanctions:" + DATASET)
        self.assertTrue(snapshot.complete)
        self.assertEqual(list(snapshot.entities), [record()])
        self.assertEqual(snapshot.metadata["entity_count"], 1)
        again = fetch_opensanctions(DATASET, self.root, client=client)
        self.assertEqual(list(again.entities), [record()])
        self.assertTrue(again.metadata["cache_hit"])
        self.assertEqual(len(client.downloads), 1)

    def test_wrong_checksum_and_wrong_count_rejected(self):
        with self.assertRaisesRegex(SourceError, "checksum mismatch"):
            fetch_opensanctions(DATASET, self.root, client=opensanctions_client(checksum="0" * 40))
        snapshot = fetch_opensanctions(DATASET, self.root, client=opensanctions_client(count=2))
        with self.assertRaisesRegex(SourceError, "count mismatch"):
            list(snapshot.entities)

    def test_unsafe_dataset_identifier_rejected_before_network(self):
        for name in ("../default", "default?x=1", "https://example.test", "foo/bar", "US_FBI"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                discover_dataset(name, client=FakeClient(), cache_dir=self.root)


class FBIPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def client(pages):
        base = "https://api.fbi.gov/wanted/v1/list?page={}&pageSize=50&sort_on=modified&sort_order=desc"
        return FakeClient({base.format(index): page for index, page in enumerate(pages, 1)})

    @staticmethod
    def item(uid):
        return {"uid": uid, "title": "FIXTURE " + uid, "sex": "Male"}

    def test_all_pages_and_raw_cache(self):
        client = self.client([{"total": 3, "page": 1, "items": [self.item("a"), self.item("b")]},
                              {"total": 3, "page": 2, "items": [self.item("c")]}])
        snapshot = fetch_fbi(cache_dir=self.root, client=client)
        self.assertTrue(snapshot.complete)
        self.assertEqual(len(list(snapshot.entities)), 3)
        self.assertEqual(snapshot.metadata["pages"], 2)
        self.assertTrue(all(Path(resource["path"]).exists() for resource in snapshot.metadata["resources"]))

    def test_limited_snapshot_is_marked_incomplete(self):
        snapshot = fetch_fbi(max_pages=1, cache_dir=self.root, client=self.client([
            {"total": 3, "items": [self.item("a")]}]))
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.metadata["warnings"])

    def test_duplicate_ids_count_change_and_early_end_are_errors(self):
        variants = [
            [{"total": 2, "items": [self.item("a")]}, {"total": 2, "items": [self.item("a")]}],
            [{"total": 2, "items": [self.item("a")]}, {"total": 3, "items": [self.item("b")]}],
            [{"total": 2, "items": [self.item("a")]}, {"total": 2, "items": []}],
        ]
        for pages in variants:
            with self.subTest(pages=pages), self.assertRaises(SourceError):
                fetch_fbi(cache_dir=self.root, client=self.client(pages))


class NetworkTests(unittest.TestCase):
    def test_only_official_https_urls_without_credentials_or_other_ports(self):
        self.assertEqual(validate_url("https://www.fbi.gov/wanted/#x"), "https://www.fbi.gov/wanted/")
        for url in ("http://www.fbi.gov/", "https://www.fbi.gov.evil.example/", "https://127.0.0.1/",
                    "https://user:password@www.fbi.gov/", "https://www.fbi.gov:9999/", "file:///tmp/a",
                    "https://www.fbi.gov/\nanything", "https://www.fbi.gov./"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_redirect_is_validated(self):
        redirect = _CheckedRedirect({"www.fbi.gov"})
        with self.assertRaises(ValueError):
            redirect.redirect_request(Request("https://www.fbi.gov/wanted/"), None, 302, "", {},
                                      "https://127.0.0.1/private")

    def test_incomplete_download_does_not_replace_existing_file(self):
        class Truncated(io.BytesIO):
            def __init__(self):
                super().__init__(b"abc")
                self.headers = Message()
                self.headers["Content-Length"] = "10"
                self.headers["Content-Type"] = "text/plain"

            def geturl(self):
                return "https://www.fbi.gov/wanted/test"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            destination.write_bytes(b"existing")
            client = Client(retries=0, interval=0)
            with patch.object(client.opener, "open", return_value=Truncated()):
                with self.assertRaisesRegex(NetworkError, "Incomplete"):
                    client.download("https://www.fbi.gov/wanted/test", destination)
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_dropped_http_body_raises_actionable_network_error(self):
        class Dropped(io.BytesIO):
            def __init__(self):
                super().__init__(b"")
                self.headers = Message()

            def geturl(self):
                return "https://www.fbi.gov/wanted/test"

            def read(self, length=-1):
                raise http.client.IncompleteRead(b"partial", 100)

        client = Client(retries=0, interval=0)
        with patch.object(client.opener, "open", return_value=Dropped()):
            with self.assertRaises(NetworkError):
                client.get("https://www.fbi.gov/wanted/test")


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = "https://www.fbi.gov/wanted/fugitives/fixture/@@download.pdf"
        self.entity = record(_files=[{"url": self.url, "name": "../../poster", "kind": "pdf"}])

    def tearDown(self):
        self.temp.cleanup()

    def test_pdf_download_hash_safe_filename_and_cache(self):
        client = FakeClient({self.url: b"%PDF-1.7\nfixture"})
        result = download_attachments(self.entity, self.root, client=client)
        self.assertEqual(result[0]["status"], "downloaded")
        path = Path(result[0]["path"])
        # The downloader returns resolved paths. TemporaryDirectory may use a
        # symlink/junction or shortened path spelling on macOS and Windows.
        # Compare existing physical paths so aliases pass and escapes still fail.
        self.assertTrue(path.resolve(strict=True).is_relative_to(self.root.resolve(strict=True)))
        self.assertEqual(result[0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        again = download_attachments(self.entity, self.root, client=client)
        self.assertEqual(again[0]["status"], "cached")
        self.assertEqual(len(client.downloads), 1)

    def test_external_url_and_html_response_are_not_saved_as_media(self):
        bad = download_attachments(record(_files=[{"url": "https://evil.example/a.pdf", "kind": "pdf"}]),
                                   self.root, client=FakeClient())
        self.assertEqual(bad[0]["status"], "error")
        html = download_attachments(self.entity, self.root, client=FakeClient({self.url: b"<html>error</html>"}))
        self.assertEqual(html[0]["status"], "error")
        self.assertFalse(list(self.root.rglob("*.pdf")))
        self.assertFalse(list(self.root.rglob("*.pending")))

    def test_fbi_file_scraper_keeps_only_this_notices_pdf_and_images(self):
        page = self.url.removesuffix("/@@download.pdf")
        parser = _FBIFileParser(page)
        parser.feed(f'<a href="{self.url}">English</a>'
                    '<img src="/wanted/fugitives/fixture/photo.jpg">'
                    '<img src="/assets/logo.png"><img src="/wanted/fugitives/other/photo.jpg">'
                    '<a href="https://evil.example/a.pdf">Bad</a>')
        self.assertEqual(len(parser.files), 2)

    def test_discovery_failure_can_be_retried_after_service_stores_it(self):
        page = self.url.removesuffix("/@@download.pdf")
        entity = record(properties={"sourceUrl": [page]})
        first = download_attachments(entity, self.root, client=FakeClient(robots=False))
        self.assertEqual(first[0]["stage"], "discovery")
        entity["_files"] = first
        client = FakeClient({page: f'<a href="{self.url}">Poster</a>', self.url: b"%PDF-1.7\nfixture"})
        second = download_attachments(entity, self.root, client=client)
        self.assertEqual(second[0]["status"], "downloaded")

    def test_profile_redirect_uses_new_profile_path_for_discovery(self):
        old = "https://www.fbi.gov/wanted/fugitives/old-fixture"
        new = "https://www.fbi.gov/wanted/fugitives/new-fixture"
        media = new + "/@@download.pdf"
        client = FakeClient({old: response(new, f'<a href="{media}">Poster</a>'), media: b"%PDF-1.7\nfixture"})
        result = download_attachments(record(properties={"sourceUrl": [old]}), self.root, client=client)
        self.assertEqual(result[0]["status"], "downloaded")

    def test_eligible_fbi_links_are_filtered_before_three_page_limit(self):
        page = self.url.removesuffix("/@@download.pdf")
        urls = ["https://example.test/1", "https://example.test/2", "https://example.test/3", page]
        client = FakeClient({page: f'<a href="{self.url}">Poster</a>', self.url: b"%PDF-1.7\nfixture"})
        result = download_attachments(record(properties={"sourceUrl": urls}), self.root, client=client)
        self.assertEqual(result[0]["status"], "downloaded")


if __name__ == "__main__":
    unittest.main()
