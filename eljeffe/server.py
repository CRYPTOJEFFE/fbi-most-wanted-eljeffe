"""Loopback-only HTTP interface; no external runtime or remote database needed."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as package_files
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from . import __version__
from .service import DISCLAIMER, SOURCE_PRESETS, Workspace
from .store import utcnow


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace: Workspace):
        if address[0] != "127.0.0.1":
            raise ValueError("The browser workbench must bind to 127.0.0.1")
        self.workspace = workspace
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.mutation_lock = threading.Lock()
        super().__init__(address, Handler)

    def start_job(self, action):
        # One mutation at a time prevents overlapping snapshots for one source.
        if not self.mutation_lock.acquire(blocking=False):
            raise ValueError("Another import or download is running; wait for it to finish")
        identifier = secrets.token_hex(12)
        job = {"id": identifier, "status": "running", "message": "Starting…", "started_at": utcnow()}
        with self.jobs_lock:
            self.jobs[identifier] = job
            # Retain only bounded job history, and never discard the running job.
            for key in list(self.jobs)[:-30]:
                if self.jobs[key]["status"] != "running":
                    del self.jobs[key]

        def update(message):
            with self.jobs_lock:
                job["message"] = str(message)

        def worker():
            try:
                result = action(update)
                with self.jobs_lock:
                    job.update(status="complete", message="Completed", result=result, completed_at=utcnow())
            except Exception as exc:
                with self.jobs_lock:
                    job.update(status="failed", message="Source update failed", error=str(exc), completed_at=utcnow())
            finally:
                self.mutation_lock.release()

        threading.Thread(target=worker, daemon=True, name="eljeffe-import").start()
        return {"job": {"id": identifier, "status": "running"}}


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "ElJeffe/" + __version__
    sys_version = ""

    def log_message(self, fmt, *args):
        # Avoid putting names and query strings in terminal access logs.
        return

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, data, status=200, content_type="application/json; charset=utf-8", filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _error(self, message, status=400):
        self._send({"error": message}, status)

    @staticmethod
    def _filters(query):
        value = lambda key, default="": query.get(key, [default])[0]
        if value("inactive", "0") not in ("0", "1"):
            raise ValueError("inactive must be 0 or 1")
        return {"q": value("q"), "source": value("source"), "kind": value("kind"),
                "schema": value("schema"), "inactive": value("inactive", "0") == "1"}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            self._error("Unrecognized Host", 403)
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        store = self.server.workspace.store
        try:
            if path == "/api/config":
                self._send({"csrfToken": self.server.csrf, "version": __version__,
                            "sourcePresets": SOURCE_PRESETS, "disclaimer": DISCLAIMER})
            elif path == "/api/stats":
                self._send(store.stats())
            elif path == "/api/history":
                self._send({"items": store.history()})
            elif path == "/api/entities":
                filters = self._filters(query)
                self._send(store.search(**filters, limit=int(query.get("limit", [25])[0]),
                                        offset=int(query.get("offset", [0])[0])))
            elif path.startswith("/api/entities/"):
                identifier = path[len("/api/entities/"):]
                result = store.get(identifier, query.get("source", [""])[0])
                if result:
                    self._send(result)
                else:
                    self._error("Record not found", 404)
            elif path == "/api/export":
                # Spool to disk and stream so full-collection exports do not fill memory.
                self._export(self._filters(query), bundle=query.get("bundle", ["0"])[0] == "1")
            elif path == "/api/manifest":
                self._send({"format": "FollowTheMoney JSONL", "generated_at": utcnow(),
                            "filters": self._filters(query), "sources": store.stats()["sources"],
                            "notice": DISCLAIMER}, filename="eljeffe.manifest.json")
            elif path.startswith("/api/jobs/"):
                with self.server.jobs_lock:
                    job = self.server.jobs.get(path.rsplit("/", 1)[-1])
                    self._send(dict(job)) if job else self._error("Job not found", 404)
            elif path.startswith("/files/"):
                self._file(path[len("/files/"):])
            elif path in ("/", "/index.html", "/app.js", "/styles.css", "/icon.svg", "/favicon.svg", "/manifest.webmanifest"):
                name = "index.html" if path == "/" else path[1:]
                asset = package_files("eljeffe").joinpath("web", name)
                if not asset.is_file():
                    self._error("Not found", 404)
                    return
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send(asset.read_bytes(), content_type=mime + ("; charset=utf-8" if mime.startswith("text/") or mime in ("application/javascript", "application/json") else ""))
            else:
                self._error("Not found", 404)
        except (ValueError, OSError) as exc:
            self._error(str(exc))

    def _export(self, filters, bundle=False):
        import hashlib
        import tempfile
        import zipfile
        with tempfile.TemporaryFile() as stream:
            checksum, count = hashlib.sha256(), 0
            with self.server.workspace.store.export_snapshot(**filters) as (entities, source_metadata):
                for entity in entities:
                    line = (json.dumps(entity, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    stream.write(line)
                    checksum.update(line)
                    count += 1
            stream.seek(0)
            if bundle:
                manifest = {"format": "FollowTheMoney JSONL", "generated_at": utcnow(),
                            "filters": filters, "sources": source_metadata, "count": count,
                            "sha256": checksum.hexdigest(), "notice": DISCLAIMER}
                with tempfile.TemporaryFile() as archive:
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                        with zipped.open("eljeffe.ftm.json", "w") as target:
                            while chunk := stream.read(65536):
                                target.write(chunk)
                        zipped.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    self._stream_download(archive, "application/zip", "eljeffe-ftm-export.zip")
            else:
                self._stream_download(stream, "application/x-ndjson; charset=utf-8", "eljeffe.ftm.json")

    def _stream_download(self, stream, mime, filename):
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            try:
                while chunk := stream.read(65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _file(self, relative):
        base = self.server.workspace.attachments.resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            self._error("File not found", 404)
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"):
            mime = "application/octet-stream"
        self._send(path.read_bytes(), content_type=mime, filename=path.name)

    def do_POST(self):
        if not self._host_ok():
            self._error("Unrecognized Host", 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self._error("Cross-origin writes are not allowed", 403)
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error("Cross-site writes are not allowed", 403)
            return
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            self._error("Missing or invalid request token", 403)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._error("Use application/json", 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 25 * 1024 * 1024:
                self._error("Request must contain 1 byte to 25 MB of JSON", 413)
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object")
            path = urlsplit(self.path).path
            workspace = self.server.workspace
            if path == "/api/sync":
                source = payload.get("source", "opensanctions")
                dataset = payload.get("dataset", "us_fbi_most_wanted")
                if source not in ("fbi", "opensanctions") or not isinstance(dataset, str):
                    raise ValueError("Invalid source or dataset")
                self._send(self.server.start_job(lambda progress: workspace.sync(source, dataset, progress=progress)), 202)
            elif path == "/api/files":
                identifier, source = payload.get("id"), payload.get("source", "")
                if not isinstance(identifier, str) or not isinstance(source, str):
                    raise ValueError("id and source must be strings")
                self._send(self.server.start_job(lambda progress: workspace.fetch_files(identifier, source, progress)), 202)
            elif path in ("/api/demo", "/api/import"):
                if not self.server.mutation_lock.acquire(blocking=False):
                    self._error("Another import is running; wait for it to finish", 409)
                    return
                try:
                    result = workspace.demo() if path == "/api/demo" else workspace.import_content(payload.get("filename"), payload.get("content"))
                    self._send(result)
                finally:
                    self.server.mutation_lock.release()
            else:
                self._error("Not found", 404)
        except (ValueError, OSError, RuntimeError) as exc:
            self._error(str(exc))


def serve(workspace, port=8765, open_browser=False):
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    with LocalServer(("127.0.0.1", port), workspace) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"EL JEFFE {__version__} → {url}", flush=True)
        print(f"Local data: {workspace.path}\nPress Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
