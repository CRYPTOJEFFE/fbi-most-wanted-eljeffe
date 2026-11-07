"""Small, bounded HTTPS client for the official public data sources.

The allowlist is deliberately enforced on *every redirect*, not only the first
URL. Runtime traffic uses Python's inherited proxy and certificate settings.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO
from urllib import error, parse, request, robotparser


OFFICIAL_HOSTS = frozenset({
    "www.opensanctions.org", "data.opensanctions.org",
    "api.fbi.gov", "www.fbi.gov", "fbi.gov",
})
USER_AGENT = (
    "ElJeffePublicRecords/1.0 "
    "(+https://github.com/CRYPTOJEFFE/fbi-most-wanted-eljeffe)"
)
ROBOT_AGENT = "ElJeffePublicRecords"
MAX_DATA_BYTES = 8 * 1024**3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class NetworkError(RuntimeError):
    def __init__(self, message: str, *, url: str = "", status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


def validate_url(url: str, allowed_hosts=OFFICIAL_HOSTS) -> str:
    """Require a credential-free HTTPS URL on an explicitly trusted host."""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Source URL contains whitespace or control characters")
    parts = parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid source URL port") from exc
    if (parts.scheme != "https" or parts.hostname not in allowed_hosts
            or parts.username is not None or parts.password is not None
            or port not in (None, 443) or "\\" in parts.netloc):
        raise ValueError("Only HTTPS URLs on the configured official source hosts are allowed")
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


class _CheckedRedirect(request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = validate_url(newurl, self.hosts)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # No authorization is currently sent, but do not let a future
            # credential addition silently leak across an allowed redirect.
            if parse.urlsplit(req.full_url).hostname != parse.urlsplit(newurl).hostname:
                redirected.remove_header("Authorization")
                redirected.remove_header("Cookie")
        return redirected


@dataclass
class Download:
    url: str
    size: int
    sha256: str
    sha1: str
    fetched_at: str
    content_type: str
    headers: dict
    body: bytes | None = None
    path: str | None = None

    def manifest(self) -> dict:
        result = {
            "url": self.url, "size": self.size, "sha256": self.sha256,
            "sha1": self.sha1, "fetched_at": self.fetched_at,
            "content_type": self.content_type,
        }
        if self.path is not None:
            result["path"] = self.path
        return result


class Client:
    """Streaming downloads, conservative retries, pacing, and robots checks."""

    def __init__(self, *, timeout: float = 20, retries: int = 2,
                 interval: float = 1.0, allowed_hosts=OFFICIAL_HOSTS):
        self.timeout = timeout
        self.retries = retries
        self.interval = interval
        self.allowed_hosts = frozenset(allowed_hosts)
        self.opener = request.build_opener(_CheckedRedirect(self.allowed_hosts))
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._crawl_delays: dict[str, float] = {}

    def _pace(self, url: str) -> None:
        host = parse.urlsplit(url).hostname or ""
        minimum = max(self.interval, self._crawl_delays.get(host, 0))
        delay = self._last_request.get(host, 0) + minimum - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last_request[host] = time.monotonic()

    @staticmethod
    def _retry_delay(headers, attempt: int) -> float:
        value = headers.get("Retry-After") if headers else None
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    dt = parsedate_to_datetime(value)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    delay = (dt - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 2**attempt
            if delay > 60:
                raise NetworkError(f"Source requests Retry-After {value}; retry later")
            return max(0, delay)
        return min(2**attempt, 10)

    def _read_into(self, url: str, output: BinaryIO, max_bytes: int) -> Download:
        url = validate_url(url, self.allowed_hosts)
        last_error = None
        for attempt in range(self.retries + 1):
            output.seek(0)
            output.truncate()
            self._pace(url)
            req = request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            })
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    final_url = validate_url(response.geturl(), self.allowed_hosts)
                    headers = dict(response.headers.items())
                    declared = response.headers.get("Content-Length")
                    try:
                        declared_size = int(declared) if declared is not None else None
                    except ValueError as exc:
                        raise NetworkError("Invalid Content-Length from source", url=url) from exc
                    if declared_size is not None and (declared_size < 0 or declared_size > max_bytes):
                        raise NetworkError(f"Source exceeds the {max_bytes:,}-byte download limit", url=url)
                    sha256 = hashlib.sha256()
                    sha1 = hashlib.sha1()
                    size = 0
                    while True:
                        chunk = response.read(min(65536, max_bytes - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise NetworkError(f"Source exceeds the {max_bytes:,}-byte download limit", url=url)
                        output.write(chunk)
                        sha256.update(chunk)
                        sha1.update(chunk)
                    if declared_size is not None and size != declared_size:
                        raise NetworkError("Incomplete source download (Content-Length mismatch)", url=url)
                    return Download(
                        url=final_url, size=size, sha256=sha256.hexdigest(),
                        sha1=sha1.hexdigest(), fetched_at=utc_now(),
                        content_type=response.headers.get_content_type(), headers=headers,
                    )
            except error.HTTPError as exc:
                last_error = NetworkError(
                    f"HTTP {exc.code} from {parse.urlsplit(url).hostname}; "
                    "check source availability or your authorized data access",
                    url=url, status=exc.code,
                )
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise last_error from exc
                delay = self._retry_delay(exc.headers, attempt)
                exc.close()
                time.sleep(delay)
            except (error.URLError, TimeoutError, ConnectionError, OSError,
                    http.client.HTTPException) as exc:
                last_error = NetworkError(f"Unable to retrieve {parse.urlsplit(url).hostname}: {exc}", url=url)
                if attempt >= self.retries:
                    raise last_error from exc
                time.sleep(self._retry_delay(None, attempt))
        raise last_error or NetworkError("Download failed", url=url)

    def get(self, url: str, *, max_bytes: int = 8 * 1024**2) -> Download:
        buffer = io.BytesIO()
        result = self._read_into(url, buffer, max_bytes)
        result.body = buffer.getvalue()
        return result

    def download(self, url: str, destination: Path, *, max_bytes: int = MAX_DATA_BYTES) -> Download:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w+b") as output:
                result = self._read_into(url, output, max_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            result.path = str(destination.resolve())
            return result
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def robots_allowed(self, url: str) -> bool:
        """Check robots before HTML scraping; inability to check fails closed.

        Published JSON APIs/bulk files are accessed separately, under their
        documented download terms, even if optional HTML discovery is skipped.
        """
        url = validate_url(url, self.allowed_hosts)
        parts = parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            robots_url = origin + "/robots.txt"
            try:
                response = self.get(robots_url, max_bytes=1024**2)
                parser = robotparser.RobotFileParser(robots_url)
                parser.parse((response.body or b"").decode("utf-8", "replace").splitlines())
                self._robots[origin] = parser
            except NetworkError as exc:
                if exc.status in {404, 410}:
                    self._robots[origin] = None
                elif exc.status in {401, 403}:
                    parser = robotparser.RobotFileParser(robots_url)
                    parser.parse(["User-agent: *", "Disallow: /"])
                    self._robots[origin] = parser
                else:
                    raise NetworkError("Unable to verify robots.txt; HTML scraping was skipped", url=url) from exc
        parser = self._robots[origin]
        if parser is None:
            return True
        delay = parser.crawl_delay(ROBOT_AGENT) or parser.crawl_delay("*")
        if delay is not None:
            if delay > 60:
                raise NetworkError("robots.txt asks for a long crawl delay; HTML scraping was skipped", url=url)
            self._crawl_delays[parts.hostname or ""] = float(delay)
        return parser.can_fetch(ROBOT_AGENT, url)


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
