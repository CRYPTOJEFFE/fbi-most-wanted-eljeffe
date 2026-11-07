"""Shared CLI/browser operations. All state is local to the selected data folder."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .demo import demo_snapshot
from .sources import download_attachments, fetch_fbi, fetch_opensanctions, read_ftm_file
from .store import Store, utcnow


SOURCE_PRESETS = [
    {"id": "us_fbi_most_wanted", "label": "FBI Most Wanted", "description": "FBI person notices via OpenSanctions"},
    {"id": "us_dea_fugitives", "label": "DEA Fugitives", "description": "US Drug Enforcement Administration"},
    {"id": "us_ss_wanted", "label": "Secret Service Fugitives", "description": "US Secret Service wanted notices"},
    {"id": "us_ice_wanted", "label": "ICE Most Wanted", "description": "US Immigration and Customs Enforcement"},
    {"id": "eu_europol_wanted", "label": "Europe Most Wanted", "description": "European national wanted notices"},
    {"id": "interpol_red_notices", "label": "INTERPOL Red Notices", "description": "International alerts; not international arrest warrants"},
    {"id": "us_ofac_sdn", "label": "OFAC sanctions", "description": "Sanctions designations, not wanted-person notices"},
    {"id": "default", "label": "OpenSanctions complete collection", "description": "Large multi-source dataset; includes relationships, PEPs and organizations"},
]

DISCLAIMER = ("Public-source research records. A search result is not an identity match or proof of guilt. "
              "Verify current status with the publishing authority. PEP status does not imply wrongdoing.")


def default_data_dir() -> Path:
    override = os.environ.get("ELJEFFE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ElJeffe"
    if __import__("sys").platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ElJeffe"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "eljeffe"


@dataclass
class Workspace:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path).expanduser().resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.cache = self.path / "cache"
        self.attachments = self.path / "attachments"
        self.imports = self.path / "imports"
        for folder in (self.cache, self.attachments, self.imports):
            folder.mkdir(exist_ok=True)
        self.store = Store(self.path / "records.sqlite3")

    def sync(self, source, dataset="us_fbi_most_wanted", *, max_pages=None, progress=None):
        if source == "opensanctions":
            snapshot = fetch_opensanctions(dataset=dataset, cache_dir=self.cache, progress=progress)
        elif source == "fbi":
            snapshot = fetch_fbi(max_pages=max_pages, cache_dir=self.cache, progress=progress)
        else:
            raise ValueError("Source must be 'fbi' or 'opensanctions'")
        if progress:
            progress("Validating and committing source snapshot")
        result = self.store.ingest(snapshot)
        result["metadata"] = snapshot.metadata
        return result

    def import_file(self, path, source=None):
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Import file does not exist")
        source = source or "import:" + (re.sub(r"[^a-zA-Z0-9_.-]", "_", path.name)[:120] or "local")
        return self.store.ingest(read_ftm_file(path, source=source))

    def import_content(self, filename, content):
        if not isinstance(filename, str) or not isinstance(content, str):
            raise ValueError("Filename and content must be strings")
        name = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)[:120].strip(".") or "upload.ftm.json"
        raw = content.encode("utf-8")
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Browser imports are limited to 20 MB; use the CLI for larger files")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.imports / (digest[:16] + "-" + name)
        # A failed import is retained as raw evidence, never activated in the DB.
        path.write_bytes(raw)
        return self.import_file(path, source="import:" + name)

    def demo(self):
        return self.store.ingest(demo_snapshot())

    def fetch_files(self, identifier, source="", progress=None):
        record = self.store.get(identifier, source)
        if not record:
            raise ValueError("Record not found")
        entity = record["entity"]
        identifier = entity["id"]
        key = hashlib.sha256((entity["source"] + "\0" + identifier).encode()).hexdigest()[:24]
        dest = self.attachments / key
        dest.mkdir(exist_ok=True)
        entity["_files"] = entity.get("files", [])
        if progress:
            progress("Downloading source-provided posters and images")
        files = download_attachments(entity, dest)
        for item in files:
            if item.get("path"):
                file_path = Path(item["path"]).resolve()
                try:
                    relative = file_path.relative_to(self.attachments)
                except ValueError as exc:
                    raise ValueError("Attachment escaped the workspace") from exc
                item["local_url"] = "/files/" + relative.as_posix()
        self.store.set_files(entity["source"], identifier, files)
        return {"id": identifier, "source": entity["source"], "files": files,
                "downloaded": sum(f.get("status") in ("downloaded", "cached", "ok") for f in files)}

    def export_file(self, output, **filters):
        output = Path(output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Prevent clobbering the database or its WAL/SHM sidecars with a user path.
        if output in {self.store.path, Path(str(self.store.path) + "-wal"), Path(str(self.store.path) + "-shm")}:
            raise ValueError("Export must not replace the database")
        count = 0
        checksum = hashlib.sha256()
        temporary = None
        source_metadata = []
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=output.parent, delete=False) as handle:
                temporary = Path(handle.name)
                with self.store.export_snapshot(**filters) as (entities, source_metadata):
                    for entity in entities:
                        line = (json.dumps(entity, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                        handle.write(line)
                        checksum.update(line)
                        count += 1
            temporary.replace(output)
            temporary = None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        manifest = {"format": "FollowTheMoney JSONL", "generated_at": utcnow(), "count": count,
                    "sha256": checksum.hexdigest(), "filters": filters,
                    "sources": source_metadata, "notice": DISCLAIMER}
        manifest_path = output.with_name(output.name + ".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"path": str(output), "manifest": str(manifest_path), "count": count,
                "sha256": checksum.hexdigest()}
