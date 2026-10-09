"""Bounded, anonymous Lattice News retrieval using the Python standard library.

Run from the repository root with ``python3 -m tools.event_collector --help``.
Raw HTML and announcement records remain in ignored, local storage.
"""

import argparse
import hashlib
import html
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from http.cookiejar import CookieJar
from http.client import HTTPException


HOST = "list.iu.edu"
ARCHIVE = "https://list.iu.edu/sympa/arc/latticenews-l/"
USER_AGENT = "EuroLFTEventCollector/0.1 (public archive research)"
MAX_BYTES = 2 * 1024 * 1024
MESSAGE_PATH = re.compile(r"^/sympa/arc/latticenews-l/\d{4}-\d{2}/msg\d+\.html$")
PAGE_NAME = re.compile(r"(?:mail|thrd)\d+\.html$")


class CollectionError(Exception):
    """An access, content, cache or parsing failure requiring visible reporting."""


class ArchiveRedirect(CollectionError):
    """A redirect refused before leaving the permitted anonymous archive."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def archive_url(url, month=None):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != HOST or parts.port not in (None, 443)
            or parts.username or parts.password or parts.query):
        raise CollectionError("URL is outside the permitted public archive")
    prefix = "/sympa/arc/latticenews-l/" + (month + "/" if month else "")
    if not parts.path.startswith(prefix) or ".." in parts.path or "%" in parts.path:
        raise CollectionError("URL is outside the requested archive path")
    return urlunsplit(("https", HOST, parts.path, "", ""))


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        # Do not follow authentication flows or cross-host redirects.
        target = urlsplit(newurl)
        if (target.scheme != "https" or target.hostname != HOST
                or target.port not in (None, 443) or target.username or target.password
                or not (target.path.startswith("/sympa/arc/latticenews-l/")
                        or target.path == "/robots.txt")):
            raise ArchiveRedirect("Request redirected outside the public archive (possibly to login)")
        if target.path != "/robots.txt":
            archive_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


class Fetcher:
    def __init__(self, directory, offline=False, delay=1.0, timeout=20):
        self.directory = directory
        self.offline = offline
        self.delay = delay
        self.timeout = timeout
        self.last_request = None
        self.cookies = CookieJar()
        self.opener = build_opener(PublicRedirects(), HTTPCookieProcessor(self.cookies))
        self.requests = 0
        self.cache_hits = 0

    def get(self, url):
        key = digest(url.encode())
        path = self.directory / (key + ".json")
        previous = None
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous["url"] != url or digest(previous["text"].encode()) != previous["sha256"]:
                    raise ValueError("cache integrity mismatch")
            except (ValueError, KeyError, TypeError) as exc:
                raise CollectionError("Invalid cache entry; original preserved for inspection") from exc
        if self.offline:
            if previous is None:
                raise CollectionError("No cached response available in offline mode")
            self.cache_hits += 1
            return previous["text"]
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}
        if previous:
            for cached, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
                if previous.get(cached):
                    headers[header] = previous[cached]
        session_retry_used = False
        for attempt in range(3):
            if self.last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            cookies_before = {(c.domain, c.path, c.name, c.value) for c in self.cookies}
            try:
                with self.opener.open(Request(url, headers=headers), timeout=self.timeout) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in ("text/html", "text/plain"):
                        raise CollectionError("Unexpected response content type: " + content_type)
                    raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise CollectionError("Response exceeds the 2 MiB collection limit")
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        text = raw.decode(charset)
                    except (UnicodeError, LookupError) as exc:
                        raise CollectionError("Response encoding cannot be decoded without data loss") from exc
                    assert_public_content(text)
                    save_json(path, {"url": url, "text": text, "sha256": digest(text.encode()),
                                     "retrieved_at": utc_now(), "etag": response.headers.get("ETag"),
                                     "last_modified": response.headers.get("Last-Modified")})
                    return text
            except ArchiveRedirect:
                cookies_after = {(c.domain, c.path, c.name, c.value) for c in self.cookies}
                # Sympa can set an anonymous cookie on its first redirect. Retry the
                # identical public URL once; never follow or perform authentication.
                if cookies_after != cookies_before and not session_retry_used and attempt < 2:
                    session_retry_used = True
                    continue
                raise
            except HTTPError as exc:
                if exc.code == 304 and previous:
                    self.cache_hits += 1
                    return previous["text"]
                if url.endswith("/robots.txt") and exc.code in (404, 410):
                    save_json(path, {"url": url, "text": "", "sha256": digest(b""),
                                     "retrieved_at": utc_now(), "status": exc.code})
                    return ""
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_after = exc.headers.get("Retry-After", "")
                    if retry_after.isdigit() and int(retry_after) > 30:
                        raise CollectionError("Server requests a longer delay; retry in a later run") from exc
                    time.sleep(max(attempt + 1, int(retry_after) if retry_after.isdigit() else 0))
                    continue
                raise CollectionError("HTTP response " + str(exc.code)) from exc
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                if attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise CollectionError("Network request failed after bounded retries") from exc


def assert_public_content(text):
    if re.search(r"<title[^>]*>[^<]*(?:IU Login|Sign[ -]?in|Access denied)", text, re.I):
        raise CollectionError("Authentication/access page returned instead of archive content")


class TextAndLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.ignored = 0
        self.in_pre = 0
        self.missing_link_targets = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        if self.ignored:
            return
        if tag == "pre":
            self.in_pre += 1
        if tag in ("br", "p", "div", "li", "tr", "pre", "h1", "h2"):
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
            else:
                self.missing_link_targets += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        if self.ignored:
            return
        if tag in ("p", "div", "li", "tr", "pre", "h1", "h2"):
            self.parts.append("\n")
        elif tag in ("a", "strong"):
            self.parts.append(" ")
        if tag == "pre":
            self.in_pre = max(0, self.in_pre - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data if self.in_pre else re.sub(r"\s+", " ", data))

    def text(self):
        value = "".join(self.parts).replace("\xa0", " ")
        return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value).strip()


def parse_index(text, url, month):
    assert_public_content(text)
    parser = TextAndLinks()
    parser.feed(text)
    visible = parser.text()
    match = re.search(re.escape(month.replace("-", "/")) + r"\s+(\d+)\s+mails?\b", visible)
    if not match:
        raise CollectionError("Archive month/count marker missing; refusing to assume an empty month")
    messages, pages = set(), set()
    for href in parser.links:
        candidate = urljoin(url, href)
        try:
            candidate = archive_url(candidate, month)
        except (CollectionError, ValueError):
            continue
        if MESSAGE_PATH.fullmatch(urlsplit(candidate).path):
            messages.add(candidate)
        elif PAGE_NAME.fullmatch(urlsplit(candidate).path.rsplit("/", 1)[-1]):
            pages.add(candidate)
    return sorted(messages), sorted(pages), int(match.group(1))


def parse_message(text, url):
    url = archive_url(url)
    if not MESSAGE_PATH.fullmatch(urlsplit(url).path):
        raise CollectionError("Not an archive message URL")
    assert_public_content(text)
    def metadata(name):
        found = re.search(r"<!--" + re.escape(name) + r":\s*([\s\S]*?)-->", text)
        return html.unescape(found.group(1)).strip() if found else None
    subject, archived = metadata("X-Subject"), metadata("X-Date")
    body = re.search(r"<!--X-Body-of-Message-->\s*([\s\S]*?)<!--X-Body-of-Message-End-->", text)
    if not subject or not archived or body is None:
        raise CollectionError("Required message metadata/body boundaries missing")
    try:
        archived_timestamp = parsedate_to_datetime(archived)
        if archived_timestamp.tzinfo is None:
            raise ValueError("Archive timezone missing")
        header = re.search(r"<!--X-Head-of-Message-->\s*([\s\S]*?)<!--X-Head-of-Message-End-->", text)
        header_parser = TextAndLinks()
        if header:
            header_parser.feed(header.group(1))
        posted = re.search(r"(?:^|\n)Date\s*:\s*([^\n]+)", header_parser.text())
        timestamp = parsedate_to_datetime(posted.group(1)) if posted else None
        if timestamp is not None and timestamp.tzinfo is None:
            raise ValueError("Sender timezone missing")
    except (ValueError, TypeError) as exc:
        raise CollectionError("Message posting timestamp is invalid or has no timezone") from exc
    parser = TextAndLinks()
    parser.feed(body.group(1))
    content = parser.text()
    links = []
    for href in parser.links:
        link = urljoin(url, href)
        if urlsplit(link).scheme in ("http", "https") and link not in links:
            links.append(link)
    warnings = []
    if timestamp is None:
        warnings.append("Sender Date header unavailable; archive timestamp retained separately.")
    if not content:
        warnings.append("Message body contains no readable text; attachments may require inspection.")
    if parser.missing_link_targets:
        warnings.append("Message contains a link label with no URL; no target was inferred.")
    record = {"schema_version": 1, "source_id": "latticenews-l", "id": "latticenews-" + digest(url.encode())[:24],
              "url": url, "subject": subject, "posted_at": timestamp.isoformat() if timestamp else None,
              "archived_at": archived_timestamp.isoformat(),
              "message_id": metadata("X-Message-Id"), "content_type": metadata("X-Content-Type"),
              "body": content, "links": links, "warnings": warnings}
    record["content_sha256"] = digest(json.dumps(record, sort_keys=True, ensure_ascii=False).encode())
    record["retrieved_at"] = utc_now()
    return record


def collect(months, directory, fetcher, max_messages=50, max_pages=8):
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    report = {"run_id": str(uuid.uuid4()), "started_at": utc_now(), "months": months,
              "mode": "offline" if fetcher.offline else "anonymous_http", "status": "success",
              "new": 0, "changed": 0, "unchanged": 0, "failures": [], "month_counts": {}}
    seen_messages = set()
    try:
        robots_text = fetcher.get("https://" + HOST + "/robots.txt")
        robots = RobotFileParser()
        robots.parse(robots_text.splitlines())
        crawl_delay = robots.crawl_delay(USER_AGENT)
        if crawl_delay is not None:
            fetcher.delay = max(fetcher.delay, crawl_delay)
        report["robots"] = "directives_checked" if robots_text else "not_provided"
    except CollectionError as exc:
        report["failures"].append({"url": "https://" + HOST + "/robots.txt", "error": str(exc)})
        robots = None
    if robots is not None:
        for month in months:
            queue = [ARCHIVE + month + "/"]
            visited, discovered = set(), set()
            expected = None
            while queue and (expected is None or len(discovered) < expected):
                if len(visited) >= max_pages:
                    report["failures"].append({"url": queue[0], "error": "Index page limit reached"})
                    break
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    if not robots.can_fetch(USER_AGENT, url):
                        raise CollectionError("robots.txt disallows this archive URL")
                    messages, pages, count = parse_index(fetcher.get(url), url, month)
                    if expected is not None and count != expected:
                        raise CollectionError("Archive count changed during run; rescan next run")
                    expected = count
                    discovered.update(messages)
                    queue.extend(p for p in pages if p not in visited and p not in queue)
                except CollectionError as exc:
                    report["failures"].append({"url": url, "error": str(exc)})
            report["month_counts"][month] = {"expected": expected, "discovered": len(discovered)}
            if expected is not None and len(discovered) != expected:
                report["failures"].append({"url": ARCHIVE + month + "/", "error": "Index discovery count mismatch"})
            for url in sorted(discovered):
                if url in seen_messages:
                    continue
                if len(seen_messages) >= max_messages:
                    report["failures"].append({"url": url, "error": "Message limit reached; run is incomplete"})
                    break
                seen_messages.add(url)
                try:
                    if not robots.can_fetch(USER_AGENT, url):
                        raise CollectionError("robots.txt disallows this message URL")
                    record = parse_message(fetcher.get(url), url)
                    destination = directory / "announcements" / (record["id"] + ".json")
                    previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else None
                    if previous and previous["content_sha256"] == record["content_sha256"]:
                        report["unchanged"] += 1
                    else:
                        if previous:
                            save_json(directory / "versions" / record["id"] / (previous["content_sha256"] + ".json"), previous)
                        save_json(destination, record)
                        report["changed" if previous else "new"] += 1
                except (CollectionError, ValueError, KeyError, OSError) as exc:
                    report["failures"].append({"url": url, "error": str(exc)})
    successes = report["new"] + report["changed"] + report["unchanged"]
    if report["failures"]:
        report["status"] = "partial" if successes else "failed"
    report.update(finished_at=utc_now(), requests=fetcher.requests, cache_hits=fetcher.cache_hits)
    save_json(directory / "runs" / (report["run_id"] + ".json"), report)
    save_json(directory / "last_run.json", report)
    return report


def recent_month(window="current", now=None):
    """Select one calendar archive month using UTC, including January rollover."""
    if window not in ("current", "previous"):
        raise ValueError("Unknown archive month window")
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    if window == "previous":
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    return "{:04d}-{:02d}".format(year, month)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect announcements only; no LLM or publication.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--month-window", choices=("current", "previous"), default="current",
                           help="One UTC calendar month: current (default) or previous completed month")
    selection.add_argument("--months", nargs="+", help="Explicit YYYY-MM months for manual research, maximum 12")
    parser.add_argument("--output", type=Path, default=Path("_event_collector/local"), help="Ignored local storage directory")
    parser.add_argument("--offline", action="store_true", help="Use saved responses without network access")
    parser.add_argument("--max-messages", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=8, help="Maximum index pages per month")
    args = parser.parse_args(argv)
    months = args.months if args.months is not None else [recent_month(args.month_window)]
    if len(months) > 12 or any(not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m) for m in months):
        parser.error("Provide at most 12 valid YYYY-MM months")
    if not 1 <= args.max_messages <= 500 or not 1 <= args.max_pages <= 50:
        parser.error("Message limit must be 1–500; page limit must be 1–50")
    os.umask(0o077)
    try:
        fetcher = Fetcher(args.output / "cache", offline=args.offline)
        report = collect(sorted(set(months)), args.output, fetcher, args.max_messages, args.max_pages)
    except OSError as exc:
        parser.exit(1, "Local storage error: " + str(exc) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("run_id", "started_at", "finished_at")}, indent=2))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
