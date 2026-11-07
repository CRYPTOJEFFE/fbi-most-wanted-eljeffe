"""Transactional, source-separated snapshots; no automatic identity resolution."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unicodedata
from typing import Iterable

from .ftm import classification, validate_entity
from .model import references


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def folded(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold()
                   if not unicodedata.combining(c))


def canonical(entity: dict) -> dict:
    """Strip local annotations from a standard FtM entity stream."""
    keys = ("id", "schema", "properties", "caption", "datasets", "referents",
            "first_seen", "last_seen", "last_change", "target")
    return {key: entity[key] for key in keys if key in entity}


def categories(entity: dict) -> list[str]:
    topics = set(entity["properties"].get("topics", []))
    found = []
    if "wanted" in topics:
        found.append("wanted")
    if "sanction" in topics or entity["schema"] == "Sanction":
        found.append("sanction")
    if topics.intersection({"role.pep", "role.pep.frmr"}):
        found.append("pep")
    if any(v.casefold() in ("missing", "missing person", "missing-person")
           for v in entity["properties"].get("classification", [])):
        found.append("missing")
    return found or ["other"]


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, updated_at TEXT NOT NULL,
                    complete INTEGER NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entities (
                    source TEXT NOT NULL, id TEXT NOT NULL, schema TEXT NOT NULL,
                    caption TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL,
                    added_at TEXT NOT NULL, observed_at TEXT NOT NULL,
                    ftm TEXT NOT NULL, search_text TEXT NOT NULL, files TEXT NOT NULL,
                    PRIMARY KEY(source, id),
                    FOREIGN KEY(source) REFERENCES sources(id)
                );
                CREATE INDEX IF NOT EXISTS entity_filters
                    ON entities(active, source, kind, schema, caption);
                CREATE INDEX IF NOT EXISTS entity_id ON entities(id);
                CREATE TABLE IF NOT EXISTS relations (
                    source TEXT NOT NULL, from_id TEXT NOT NULL, property TEXT NOT NULL,
                    to_id TEXT NOT NULL, PRIMARY KEY(source,from_id,property,to_id)
                );
                CREATE INDEX IF NOT EXISTS relation_target ON relations(source,to_id);
                CREATE TABLE IF NOT EXISTS entity_kinds (
                    source TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
                    PRIMARY KEY(source,entity_id,kind)
                );
                CREATE INDEX IF NOT EXISTS kinds_filter ON entity_kinds(kind,source,entity_id);
                CREATE TABLE IF NOT EXISTS referents (
                    source TEXT NOT NULL, entity_id TEXT NOT NULL, referent TEXT NOT NULL,
                    PRIMARY KEY(source,entity_id,referent)
                );
                CREATE INDEX IF NOT EXISTS referents_lookup ON referents(source,referent);
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL, count INTEGER NOT NULL,
                    complete INTEGER NOT NULL, metadata TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ingest(self, snapshot, *, allow_empty: bool = False) -> dict:
        """Apply only an entirely parsed snapshot; rollback on malformed input.

        Missing IDs become 'not in latest snapshot' only for complete fetches.
        This observation is not a statement about legal or wanted status.
        """
        source = str(snapshot.source)
        if not source or len(source) > 200:
            raise ValueError("A source identifier of 1–200 characters is required")
        now = utcnow()
        metadata = dict(snapshot.metadata)
        metadata.setdefault("fetched_at", now)
        label = str(metadata.get("label") or source)
        count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO sources VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET label=excluded.label,
                updated_at=excluded.updated_at, complete=excluded.complete,
                metadata=excluded.metadata""",
                         (source, label, now, int(snapshot.complete), json.dumps(metadata)))
            conn.execute("CREATE TEMP TABLE seen_ids (id TEXT PRIMARY KEY)")
            for incoming in snapshot.entities:
                entity = validate_entity(incoming)
                identifier = entity["id"]
                try:
                    conn.execute("INSERT INTO seen_ids VALUES (?)", (identifier,))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"Duplicate entity ID in snapshot: {identifier}") from exc
                item = canonical(entity)
                caption = str(item.get("caption") or next(iter(
                    item["properties"].get("name", [])), identifier))
                kind = classification(item)
                text = folded(" ".join([identifier, caption] + item.get("referents", []) + [
                    v for values in item["properties"].values() for v in values]))
                files = incoming.get("_files", [])
                if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
                    raise ValueError("Attachment metadata must be a list of objects")
                previous = conn.execute("SELECT files FROM entities WHERE source=? AND id=?",
                                        (source, identifier)).fetchone()
                if previous:
                    existing = {f.get("url"): f for f in json.loads(previous["files"])}
                    if "_files" not in incoming:
                        files = json.loads(previous["files"])
                    else:
                        files = [{**existing.get(f.get("url"), {}), **f} for f in files]
                conn.execute("""INSERT INTO entities
                    (source,id,schema,caption,kind,active,added_at,observed_at,ftm,search_text,files)
                    VALUES (?,?,?,?,?,1,?,?,?,?,?)
                    ON CONFLICT(source,id) DO UPDATE SET schema=excluded.schema,
                    caption=excluded.caption,kind=excluded.kind,active=1,
                    observed_at=excluded.observed_at,ftm=excluded.ftm,
                    search_text=excluded.search_text,files=excluded.files""",
                             (source, identifier, item["schema"], caption, kind, now, now,
                              json.dumps(item, ensure_ascii=False), text, json.dumps(files)))
                conn.execute("DELETE FROM relations WHERE source=? AND from_id=?", (source, identifier))
                conn.executemany("INSERT OR IGNORE INTO relations VALUES (?,?,?,?)",
                                 [(source, identifier, name, value) for name, value in references(item)])
                conn.execute("DELETE FROM entity_kinds WHERE source=? AND entity_id=?", (source, identifier))
                conn.executemany("INSERT INTO entity_kinds VALUES (?,?,?)",
                                 [(source, identifier, kind) for kind in categories(item)])
                conn.execute("DELETE FROM referents WHERE source=? AND entity_id=?", (source, identifier))
                conn.executemany("INSERT OR IGNORE INTO referents VALUES (?,?,?)",
                                 [(source, identifier, value) for value in item.get("referents", [])])
                count += 1
            if not count and not allow_empty:
                raise ValueError("Empty snapshot rejected; existing records were preserved")
            if snapshot.complete:
                conn.execute("""UPDATE entities SET active=0 WHERE source=?
                    AND id NOT IN (SELECT id FROM seen_ids)""", (source,))
            # Some streaming readers finalize a checksum as the iterator ends.
            metadata.update(snapshot.metadata)
            conn.execute("UPDATE sources SET metadata=? WHERE id=?",
                         (json.dumps(metadata), source))
            conn.execute("""INSERT INTO history
                (source,started_at,completed_at,count,complete,metadata) VALUES (?,?,?,?,?,?)""",
                         (source, now, utcnow(), count, int(snapshot.complete), json.dumps(metadata)))
            inactive = conn.execute("SELECT COUNT(*) FROM entities WHERE source=? AND active=0",
                                    (source,)).fetchone()[0]
        return {"source": source, "inserted": count, "inactive": inactive,
                "complete": bool(snapshot.complete), "updated_at": now}

    @staticmethod
    def _where(q="", source="", kind="", schema="", inactive=False):
        conditions, params = [], []
        if not inactive:
            conditions.append("active=1")
        for key, value in (("source", source), ("schema", schema)):
            if value:
                conditions.append(key + "=?")
                params.append(value)
        if kind:
            if kind not in ("wanted", "sanction", "pep", "missing", "other"):
                raise ValueError("Unknown record category")
            conditions.append("EXISTS (SELECT 1 FROM entity_kinds k WHERE k.source=entities.source AND k.entity_id=entities.id AND k.kind=?)")
            params.append(kind)
        if len(q) > 300:
            raise ValueError("Search text must be at most 300 characters")
        for token in folded(q).split():
            conditions.append("search_text LIKE ? ESCAPE '\\'")
            params.append("%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        return (" WHERE " + " AND ".join(conditions)) if conditions else "", params

    @staticmethod
    def _row(row):
        item = json.loads(row["ftm"])
        item.update({"source": row["source"], "kind": row["kind"],
                     "categories": categories(item),
                     "active": bool(row["active"]), "caption": row["caption"],
                     "observed_at": row["observed_at"], "added_at": row["added_at"],
                     "files": json.loads(row["files"])})
        return item

    def search(self, *, q="", source="", kind="", schema="", inactive=False,
               limit=25, offset=0):
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Limit must be 1–500 and offset must be non-negative")
        where, params = self._where(q, source, kind, schema, inactive)
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM entities" + where, params).fetchone()[0]
            rows = conn.execute("SELECT * FROM entities" + where +
                                " ORDER BY caption COLLATE NOCASE,source,id LIMIT ? OFFSET ?",
                                params + [limit, offset]).fetchall()
        return {"items": [self._row(row) for row in rows], "total": total,
                "offset": offset, "limit": limit}

    def get(self, identifier, source=""):
        with self.connect() as conn:
            params = [identifier]
            sql = "SELECT * FROM entities WHERE id=?"
            if source:
                sql += " AND source=?"
                params.append(source)
            rows = conn.execute(sql + " ORDER BY source LIMIT 2", params).fetchall()
            resolved_from = None
            if not rows:
                params = [identifier]
                sql = """SELECT e.* FROM entities e JOIN referents r
                    ON r.source=e.source AND r.entity_id=e.id WHERE e.active=1 AND r.referent=?"""
                if source:
                    sql += " AND e.source=?"
                    params.append(source)
                rows = conn.execute(sql + " ORDER BY e.source LIMIT 2", params).fetchall()
                if not rows:
                    return None
                resolved_from = identifier
                identifier = rows[0]["id"]
            if len(rows) > 1:
                raise ValueError("This ID appears in multiple sources; specify --source")
            item = self._row(rows[0])
            src = conn.execute("SELECT * FROM sources WHERE id=?", (item["source"],)).fetchone()
            item["source_metadata"] = json.loads(src["metadata"])
            item["source_updated_at"] = src["updated_at"]
            if not item["active"]:
                successors = conn.execute("""SELECT e.id,e.caption,e.source FROM entities e
                    JOIN referents r ON r.source=e.source AND r.entity_id=e.id
                    WHERE e.source=? AND e.active=1 AND r.referent=? AND e.id!=?""",
                    (item["source"], identifier, identifier))
                item["replaced_by"] = [dict(row) for row in successors]
            # Only schema-defined entity references can form links. Names and
            # notes equal to an ID never become asserted relationships.
            related_clause = """
                WHERE source=? AND id!=? AND id IN (
                    SELECT to_id FROM relations WHERE source=? AND from_id=?
                    UNION SELECT from_id FROM relations WHERE source=? AND to_id=?
                )"""
            related_params = (item["source"], identifier, item["source"], identifier, item["source"], identifier)
            related_total = conn.execute("SELECT COUNT(*) FROM entities" + related_clause, related_params).fetchone()[0]
            related_rows = conn.execute("SELECT * FROM entities" + related_clause + " ORDER BY caption LIMIT 100", related_params)
            related = [self._row(row) for row in related_rows]
        result = {"entity": item, "related": related, "related_total": related_total,
                  "related_truncated": related_total > len(related)}
        if resolved_from:
            result["resolved_from"] = resolved_from
        return result

    def export(self, **filters) -> Iterable[dict]:
        with self.export_snapshot(**filters) as (entities, _):
            yield from entities

    @contextmanager
    def export_snapshot(self, **filters):
        """Yield the entity stream and matching provenance from one read snapshot."""
        if filters.get("inactive"):
            raise ValueError("FtM snapshots export current observations only. Clear 'include inactive' to export; historical records remain available in search and show.")
        where, params = self._where(**filters)
        with self.connect() as conn:
            conn.execute("BEGIN")
            duplicate = conn.execute("SELECT id FROM entities" + where +
                                     " GROUP BY id HAVING COUNT(*)>1 LIMIT 1", params).fetchone()
            if duplicate:
                raise ValueError("The selected sources share an entity ID. Select one source for a lossless FtM export; source records are never implicitly merged.")
            selected = [row[0] for row in conn.execute("SELECT DISTINCT source FROM entities" + where, params)]
            source_metadata = []
            for source in selected:
                row = conn.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()
                source_metadata.append({"id": row["id"], "label": row["label"],
                                        "updated_at": row["updated_at"], "complete": bool(row["complete"]),
                                        "metadata": json.loads(row["metadata"])})
            cursor = conn.execute("SELECT ftm FROM entities" + where + " ORDER BY id,source", params)
            yield (json.loads(row["ftm"]) for row in cursor), source_metadata

    def stats(self):
        with self.connect() as conn:
            total, active = conn.execute("SELECT COUNT(*),COALESCE(SUM(active),0) FROM entities").fetchone()
            sources = []
            for row in conn.execute("SELECT * FROM sources ORDER BY label"):
                n, a = conn.execute("SELECT COUNT(*),COALESCE(SUM(active),0) FROM entities WHERE source=?",
                                    (row["id"],)).fetchone()
                sources.append({"id": row["id"], "label": row["label"], "entity_count": n,
                                "active_count": a, "updated_at": row["updated_at"],
                                "complete": bool(row["complete"]), "metadata": json.loads(row["metadata"])})
            kinds = {key: 0 for key in ("wanted", "sanction", "pep", "missing", "other")}
            kinds.update(dict(conn.execute("""SELECT k.kind,COUNT(*) FROM entity_kinds k JOIN entities e
                ON e.source=k.source AND e.id=k.entity_id WHERE e.active=1 GROUP BY k.kind""")))
            schemas = dict(conn.execute("SELECT schema,COUNT(*) FROM entities WHERE active=1 GROUP BY schema"))
        return {"total": total, "active": active, "sources": sources, "kinds": kinds,
                "schemas": schemas, "last_sync": max((s["updated_at"] for s in sources), default=None)}

    def set_files(self, source, identifier, files):
        with self.connect() as conn:
            conn.execute("UPDATE entities SET files=? WHERE source=? AND id=?",
                         (json.dumps(files), source, identifier))

    def history(self, limit=20):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,source,started_at,completed_at,count,complete FROM history ORDER BY id DESC LIMIT ?",
                (limit,))]
