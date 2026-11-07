"""Official source discovery, auditable snapshots, and optional wanted files."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin, urlsplit

from .ftm import fbi_entity, validate_entity
from .network import Client, Download, MAX_DATA_BYTES, NetworkError, atomic_json, utc_now, validate_url


OPEN_SANCTIONS_HOSTS = frozenset({"www.opensanctions.org", "data.opensanctions.org"})
FBI_HOSTS = frozenset({"api.fbi.gov", "www.fbi.gov", "fbi.gov"})
FBI_ENDPOINT = "https://api.fbi.gov/wanted/v1/list"
MAX_LINE_BYTES = 16 * 1024**2
MAX_ATTACHMENT_BYTES = 25 * 1024**2


class SourceError(RuntimeError):
    pass


@dataclass
class SourceSnapshot:
    source: str
    entities: Iterable[dict]
    metadata: dict
    complete: bool = True


def _progress(callback, message: str) -> None:
    if callback is not None:
        callback(message)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:100] or "snapshot"


def _dataset_name(dataset: str) -> str:
    if not isinstance(dataset, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", dataset):
        raise ValueError("Dataset must be a lowercase OpenSanctions identifier, e.g. us_fbi_most_wanted")
    return dataset


def _cache_root(cache_dir: Path | None) -> Path:
    root = Path(cache_dir) if cache_dir is not None else Path(tempfile.mkdtemp(prefix="eljeffe-sources-"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _hash_file(path: Path, max_bytes: int = MAX_DATA_BYTES) -> dict:
    sha256, sha1, size = hashlib.sha256(), hashlib.sha1(), 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024**2)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise SourceError(f"File exceeds the {max_bytes:,}-byte limit")
            sha256.update(chunk)
            sha1.update(chunk)
    return {"sha256": sha256.hexdigest(), "sha1": sha1.hexdigest(), "size": size}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _json(data: str | bytes):
    return json.loads(data, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)


class _HashedReader(io.RawIOBase):
    def __init__(self, raw):
        super().__init__()
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.raw.readinto(buffer)
        if count:
            self.size += count
            if self.size > MAX_DATA_BYTES:
                raise SourceError("FtM source data exceeds the size limit")
            self.digest.update(memoryview(buffer)[:count])
        return count


def _ftm_lines(path: Path, *, expected_sha256: str | None = None):
    """Stream flat FtM JSONL, checking decompressed size and per-record size."""
    count, total = 0, 0
    try:
        with path.open("rb", buffering=0) as raw:
            hashed = _HashedReader(raw)
            with io.BufferedReader(hashed) as buffer:
                compressed = buffer.peek(2)[:2] == b"\x1f\x8b"
                stream = gzip.GzipFile(fileobj=buffer, mode="rb") if compressed else buffer
                try:
                    line_number = 0
                    while True:
                        line = stream.readline(MAX_LINE_BYTES + 1)
                        if not line:
                            break
                        line_number += 1
                        total += len(line)
                        if len(line) > MAX_LINE_BYTES:
                            raise SourceError(f"FtM line {line_number} exceeds the record size limit")
                        if total > MAX_DATA_BYTES:
                            raise SourceError("FtM decompressed data exceeds the size limit")
                        if not line.strip():
                            continue
                        try:
                            text = line.decode("utf-8-sig" if line_number == 1 else "utf-8")
                            entity = validate_entity(_json(text))
                        except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
                            raise SourceError(f"Invalid FtM record at line {line_number}: {exc}") from exc
                        count += 1
                        yield entity
                finally:
                    if compressed:
                        stream.close()
            if expected_sha256 is not None and hashed.digest.hexdigest() != expected_sha256:
                raise SourceError("Source file changed between fingerprinting and parsing; import aborted")
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise SourceError("Invalid or truncated compressed FtM input") from exc
    if count == 0:
        raise SourceError("Source contains no FtM entities; an empty file will not replace stored records")


def read_ftm_file(path: Path, source: str = "local") -> SourceSnapshot:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise SourceError(f"FtM file does not exist: {path}")
    digest = _hash_file(path)
    metadata = {
        "label": path.name, "source_url": str(path), "fetched_at": utc_now(),
        "license": "Imported file: retain and verify the original publisher's terms",
        "attribution": "As supplied in the imported FtM entities and their datasets",
        "resources": [{"name": path.name, "path": str(path), **digest}],
        **digest,
    }

    def entities():
        count = 0
        for entity in _ftm_lines(path, expected_sha256=digest["sha256"]):
            count += 1
            yield entity
        metadata["entity_count"] = count

    return SourceSnapshot(source=source, entities=entities(), metadata=metadata)


class DatasetPageParser(HTMLParser):
    """Extract published bulk links rather than scraping search result profiles."""

    def __init__(self, page_url: str, dataset: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.dataset = dataset
        self.resources: list[dict] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urljoin(self.page_url, href)
        if not _resource_url(url, self.dataset):
            return
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        if name and url not in self._seen and name.endswith((".json", ".json.gz", ".csv", ".txt")):
            self.resources.append({"name": name, "url": url, "discovered_via": "dataset_page"})
            self._seen.add(url)


def _resource_url(url, dataset: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        validate_url(url, {"data.opensanctions.org"})
    except ValueError:
        return False
    path = urlsplit(url).path
    return path.startswith((f"/datasets/latest/{dataset}/", f"/artifacts/{dataset}/"))


def discover_dataset(dataset: str, *, client: Client | None = None,
                     cache_dir: Path | None = None, progress=None) -> dict:
    dataset = _dataset_name(dataset)
    client = client or Client(allowed_hosts=OPEN_SANCTIONS_HOSTS)
    page_url = f"https://www.opensanctions.org/datasets/{dataset}/"
    index_url = f"https://data.opensanctions.org/datasets/latest/{dataset}/index.json"
    _progress(progress, f"Reading OpenSanctions metadata for {dataset}")
    response = client.get(index_url, max_bytes=20 * 1024**2)
    try:
        raw_metadata = _json(response.body or b"")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError("OpenSanctions metadata is not valid JSON") from exc
    if not isinstance(raw_metadata, dict):
        raise SourceError("OpenSanctions metadata must be an object")
    # Current exports are flat; older documented indexes may wrap a dataset.
    inner = raw_metadata.get("dataset")
    source_metadata = inner if isinstance(inner, dict) else raw_metadata
    if source_metadata.get("name", dataset) != dataset:
        raise SourceError("OpenSanctions metadata identifies a different dataset")
    if source_metadata.get("deprecated") or source_metadata.get("disabled"):
        raise SourceError("This OpenSanctions dataset is deprecated or disabled; choose a maintained source")
    version = str(source_metadata.get("version") or "unversioned")
    folder = _cache_root(cache_dir) / "opensanctions" / dataset / _slug(version)
    folder.mkdir(parents=True, exist_ok=True)
    index_path = folder / "index.json"
    _write_bytes(index_path, response.body or b"")
    index_manifest = {"name": "index.json", **response.manifest(), "path": str(index_path.resolve())}
    warnings, scraped, page_resources = [], False, []
    retrieved = [index_manifest]
    try:
        if client.robots_allowed(page_url):
            _progress(progress, "Discovering published files on the OpenSanctions dataset page")
            page = client.get(page_url, max_bytes=8 * 1024**2)
            parser = DatasetPageParser(page_url, dataset)
            parser.feed((page.body or b"").decode("utf-8", "replace"))
            page_resources = parser.resources
            page_path = folder / "dataset-page.html"
            _write_bytes(page_path, page.body or b"")
            retrieved.append({"name": "dataset-page.html", **page.manifest(), "path": str(page_path.resolve())})
            scraped = True
        else:
            warnings.append("Dataset-page HTML scraping is disallowed by robots.txt; used the documented bulk metadata")
    except (NetworkError, ValueError) as exc:
        warnings.append(f"Optional HTML discovery skipped: {exc}. Used the documented bulk metadata")
    raw_resources = source_metadata.get("resources", raw_metadata.get("resources", []))
    if not isinstance(raw_resources, list):
        raise SourceError("OpenSanctions resources must be an array")
    resources, seen = [], set()
    for resource in [*raw_resources, *page_resources]:
        if not isinstance(resource, dict):
            raise SourceError("Invalid OpenSanctions resource entry")
        url = resource.get("url")
        if not _resource_url(url, dataset):
            continue
        if url not in seen:
            resources.append(dict(resource))
            seen.add(url)
    ftm_resources = [resource for resource in resources if resource.get("name") in {
        "entities.ftm.json", "entities.ftm.json.gz",
    }]
    if not ftm_resources:
        raise SourceError("Dataset publishes no recognized flat FollowTheMoney export")
    ftm_resource = ftm_resources[0]
    return {
        "label": str(source_metadata.get("title") or dataset),
        "dataset": dataset, "source_url": page_url, "metadata_url": index_url,
        "fetched_at": utc_now(), "version": version,
        "license": "CC BY-NC 4.0 for non-commercial use; commercial use requires an OpenSanctions data license",
        "license_url": "https://www.opensanctions.org/licensing/",
        "attribution": "OpenSanctions and the original publishers identified in the dataset metadata",
        "resources": resources, "ftm_resource": ftm_resource,
        "retrieved_resources": retrieved, "page_scraped": scraped, "warnings": warnings,
        "source_metadata": raw_metadata, "cache_path": str(folder.resolve()),
        "expected_entity_count": source_metadata.get("entity_count"),
    }


def _expected_sha1(resource: dict) -> str | None:
    checksum = resource.get("checksum")
    if checksum is None:
        return None
    if not isinstance(checksum, str):
        raise SourceError("Invalid OpenSanctions resource checksum")
    checksum = checksum.removeprefix("sha1:").casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", checksum):
        raise SourceError("Expected a SHA1 checksum in OpenSanctions metadata")
    return checksum


def fetch_opensanctions(dataset: str = "us_fbi_most_wanted", cache_dir: Path | None = None,
                       progress=None, *, client: Client | None = None) -> SourceSnapshot:
    client = client or Client(allowed_hosts=OPEN_SANCTIONS_HOSTS)
    metadata = discover_dataset(dataset, client=client, cache_dir=cache_dir, progress=progress)
    resource = metadata["ftm_resource"]
    expected_sha1 = _expected_sha1(resource)
    filename = "entities.ftm.json.gz" if str(resource["name"]).endswith(".gz") else "entities.ftm.json"
    path = Path(metadata["cache_path"]) / filename
    digest = _hash_file(path) if path.exists() and expected_sha1 is not None else None
    if digest and digest["sha1"] == expected_sha1:
        metadata["cache_hit"] = True
        downloaded = {"name": filename, "url": resource["url"], "path": str(path.resolve()),
                      "verified_at": metadata["fetched_at"], **digest}
        previous_manifest = Path(metadata["cache_path"]) / "manifest.json"
        if previous_manifest.exists():
            try:
                previous = _json(previous_manifest.read_bytes())
                for previous_resource in previous.get("retrieved_resources", []):
                    if (previous_resource.get("sha256") == digest["sha256"]
                            and previous_resource.get("fetched_at")):
                        downloaded["fetched_at"] = previous_resource["fetched_at"]
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        _progress(progress, "Verified the cached source file against the publisher checksum")
    else:
        _progress(progress, f"Downloading {metadata['label']} as FollowTheMoney JSON")
        result = client.download(resource["url"], path, max_bytes=MAX_DATA_BYTES)
        downloaded = {"name": filename, **result.manifest()}
        metadata["cache_hit"] = False
        if expected_sha1 is not None and result.sha1 != expected_sha1:
            path.unlink(missing_ok=True)
            raise SourceError("OpenSanctions checksum mismatch; the existing database was not replaced")
    size = resource.get("size")
    if isinstance(size, int) and size >= 0 and downloaded["size"] != size:
        raise SourceError("OpenSanctions resource size does not match its metadata")
    metadata["retrieved_resources"].append(downloaded)
    metadata["sha256"] = downloaded["sha256"]
    expected = metadata.get("expected_entity_count")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        expected = None
    metadata["complete"] = expected is not None

    def entities():
        count = 0
        for entity in _ftm_lines(path, expected_sha256=downloaded["sha256"]):
            count += 1
            yield entity
        if expected is not None and count != expected:
            raise SourceError(f"OpenSanctions entity count mismatch: metadata says {expected:,}, file has {count:,}")
        metadata["entity_count"] = count
        atomic_json(Path(metadata["cache_path"]) / "manifest.json", metadata)
        _progress(progress, f"Validated {count:,} OpenSanctions entities")

    return SourceSnapshot(source=f"opensanctions:{dataset}", entities=entities(),
                          metadata=metadata, complete=expected is not None)


def fetch_fbi(max_pages: int | None = None, cache_dir: Path | None = None,
              progress=None, *, client: Client | None = None) -> SourceSnapshot:
    if max_pages is not None and (isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1):
        raise ValueError("max_pages must be a positive integer")
    client = client or Client(allowed_hosts=FBI_HOSTS)
    started = utc_now()
    folder = _cache_root(cache_dir) / "fbi" / (_slug(started) + "-" + os.urandom(4).hex())
    folder.mkdir(parents=True, exist_ok=True)
    pages, seen, resources = [], set(), []
    total = None
    complete = False
    for page in range(1, min(max_pages or 1000, 1000) + 1):
        url = FBI_ENDPOINT + "?" + urlencode({"page": page, "pageSize": 50,
                                               "sort_on": "modified", "sort_order": "desc"})
        _progress(progress, f"Retrieving FBI public notices, page {page}")
        response = client.get(url, max_bytes=16 * 1024**2)
        try:
            data = _json(response.body or b"")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(f"Invalid FBI JSON response on page {page}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError(f"FBI page {page} has no items array")
        reported_total = data.get("total")
        if not isinstance(reported_total, int) or isinstance(reported_total, bool) or reported_total < 0:
            raise SourceError("FBI response has no valid total record count")
        if data.get("page", page) != page:
            raise SourceError("FBI API returned a different page than requested")
        if total is None:
            total = reported_total
        elif total != reported_total:
            raise SourceError("FBI record count changed during pagination; retry a fresh snapshot")
        path = folder / f"page-{page:06d}.json"
        _write_bytes(path, response.body or b"")
        resources.append({"name": path.name, **response.manifest(), "path": str(path.resolve())})
        for item in data["items"]:
            # Validate before calling this a complete snapshot; raw originals
            # remain in the page cache even for unmapped source fields.
            entity = fbi_entity(item, retrieved_at=started)
            if entity["id"] in seen:
                raise SourceError("FBI pagination returned duplicate IDs; retry to avoid an inconsistent snapshot")
            seen.add(entity["id"])
            pages.append(entity)
        if len(seen) == total:
            complete = True
            break
        if len(seen) > total:
            raise SourceError("FBI returned more unique records than its reported total")
        if not data["items"]:
            raise SourceError("FBI pagination ended before its reported total; snapshot not imported")
    if not complete and max_pages is None:
        raise SourceError("FBI pagination safety limit reached before the complete snapshot")
    if not pages:
        raise SourceError("FBI returned no notices; an empty response will not replace stored records")
    metadata = {
        "label": "FBI public wanted and information notices", "source_url": "https://www.fbi.gov/wanted/",
        "api_url": FBI_ENDPOINT, "fetched_at": utc_now(), "retrieval_started_at": started,
        "license": "FBI public-source material; third-party images may have separate rights",
        "license_url": "https://www.fbi.gov/legalnotices",
        "attribution": "Federal Bureau of Investigation; descriptions and statuses are source assertions",
        "entity_count": len(pages), "reported_total": total, "pages": len(resources),
        "complete": complete, "resources": resources, "cache_path": str(folder.resolve()),
        "warnings": [] if complete else ["Page-limited snapshot: unobserved existing records must not be retired"],
    }
    atomic_json(folder / "manifest.json", metadata)
    return SourceSnapshot(source="fbi", entities=pages, metadata=metadata, complete=complete)


class _FBIFileParser(HTMLParser):
    def __init__(self, source_url: str):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.source_path = urlsplit(source_url).path.rstrip("/") + "/"
        self.files, self._seen = [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        raw = attrs.get("href") if tag == "a" else attrs.get("src") if tag == "img" else None
        if not raw:
            return
        url = urljoin(self.source_url, raw)
        try:
            url = validate_url(url, FBI_HOSTS)
        except ValueError:
            return
        path = urlsplit(url).path
        # Restrict to files belonging to this notice; never collect site logos,
        # adjacent notices, third-party pixels, or arbitrary page links.
        if not path.startswith(self.source_path):
            return
        kind = None
        if tag == "a" and (path.casefold().endswith(".pdf") or "@@download.pdf" in path):
            kind = "pdf"
        elif tag == "img" or (tag == "a" and re.search(r"\.(?:jpe?g|png|gif|webp)(?:/|$)", path, re.I)):
            kind = "image"
        if kind and url not in self._seen:
            self.files.append({"url": url, "kind": kind,
                               "name": attrs.get("alt") or attrs.get("title") or f"FBI {kind}"})
            self._seen.add(url)


def _discover_fbi_files(entity: dict, client: Client) -> tuple[list[dict], list[dict]]:
    files, problems = [], []
    eligible = []
    for source_url in entity.get("properties", {}).get("sourceUrl", []):
        try:
            source_url = validate_url(source_url, {"www.fbi.gov", "fbi.gov"})
        except ValueError:
            continue
        if not urlsplit(source_url).path.startswith("/wanted/"):
            continue
        if source_url not in eligible:
            eligible.append(source_url)
        if len(eligible) >= 3:
            break
    for source_url in eligible:
        try:
            if not client.robots_allowed(source_url):
                problems.append({"url": source_url, "kind": "discovery", "stage": "discovery",
                                 "status": "error", "error": "robots.txt disallows HTML file discovery"})
                continue
            response = client.get(source_url, max_bytes=8 * 1024**2)
            final_url = validate_url(response.url, {"www.fbi.gov", "fbi.gov"})
            if not urlsplit(final_url).path.startswith("/wanted/"):
                raise SourceError("FBI profile redirected outside the wanted notice collection")
            parser = _FBIFileParser(final_url)
            parser.feed((response.body or b"").decode("utf-8", "replace"))
            files.extend(parser.files)
        except (NetworkError, SourceError, ValueError) as exc:
            problems.append({"url": source_url, "kind": "discovery", "stage": "discovery",
                             "status": "error", "error": str(exc)})
    return files, problems


def _media_type(header: bytes) -> tuple[str, str] | None:
    if header.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def download_attachments(entity: dict, destination: Path, *, client: Client | None = None) -> list[dict]:
    """Download bounded official PDFs/photos and retain a per-file SHA256.

    API file links are preferred. For OpenSanctions entities without media
    metadata, discover file links on at most three official FBI profile pages.
    HTML/SVG/executables are never accepted as downloaded attachments.
    """
    client = client or Client(allowed_hosts=FBI_HOSTS)
    identifier = str(entity.get("id") or "record")
    folder = Path(destination).resolve() / (_slug(identifier)[:60] + "-" + hashlib.sha256(identifier.encode()).hexdigest()[:8])
    folder.mkdir(parents=True, exist_ok=True)
    files = entity.get("_files") or []
    if not isinstance(files, list):
        raise ValueError("Attachment metadata must be a list")
    # Stored discovery failures are diagnostics, not media candidates. Retrying
    # a blocked profile must re-run discovery instead of downloading its HTML.
    files = [entry for entry in files if not isinstance(entry, dict)
             or (entry.get("kind") != "discovery" and entry.get("stage") != "discovery")]
    results = []
    if not files:
        files, results = _discover_fbi_files(entity, client)
    if not files:
        return results
    seen = set()
    for entry in files[:40]:
        if not isinstance(entry, dict):
            results.append({"status": "error", "error": "Invalid attachment metadata"})
            continue
        output = {key: entry[key] for key in ("url", "name", "kind") if key in entry}
        temporary = None
        try:
            url = validate_url(entry.get("url"), FBI_HOSTS)
            if url in seen:
                continue
            seen.add(url)
            key = hashlib.sha256(url.encode()).hexdigest()[:24]
            manifest_path = folder / (key + ".json")
            if manifest_path.exists():
                try:
                    cached = _json(manifest_path.read_bytes())
                    cached_path = Path(cached["path"]).resolve()
                    fetched = datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - fetched).total_seconds()
                    if (cached.get("url") == url and cached_path.is_relative_to(folder)
                            and cached_path.is_file() and 0 <= age < 24 * 3600):
                        digest = _hash_file(cached_path, MAX_ATTACHMENT_BYTES)
                        if digest["sha256"] == cached.get("sha256"):
                            results.append({**cached, "status": "cached"})
                            continue
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            temporary = folder / (key + ".pending")
            downloaded = client.download(url, temporary, max_bytes=MAX_ATTACHMENT_BYTES)
            with temporary.open("rb") as stream:
                detected = _media_type(stream.read(16))
            if detected is None:
                raise SourceError("Attachment is not a supported PDF/image; possible HTML error page")
            suffix, media_type = detected
            if entry.get("kind") == "pdf" and suffix != ".pdf":
                raise SourceError("The published PDF URL returned a different file type")
            if entry.get("kind") == "image" and not media_type.startswith("image/"):
                raise SourceError("The published image URL returned a different file type")
            final_path = folder / (key + "-" + downloaded.sha256[:12] + suffix)
            os.replace(temporary, final_path)
            output.update({"url": url, "resolved_url": downloaded.url, "path": str(final_path),
                           "sha256": downloaded.sha256, "size": downloaded.size,
                           "content_type": media_type, "fetched_at": downloaded.fetched_at,
                           "status": "downloaded"})
            atomic_json(manifest_path, output)
            results.append(output)
        except (NetworkError, SourceError, ValueError, OSError) as exc:
            output.update({"status": "error", "error": str(exc)})
            results.append(output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if len(files) > 40:
        results.append({"status": "error", "error": "40-file per-record safety limit reached; remaining files were not downloaded"})
    return results
