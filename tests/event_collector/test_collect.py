"""Synthetic regression cases for archive boundaries and preservation of local data."""

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from http.cookiejar import Cookie
from urllib.error import HTTPError
from urllib.request import Request

from tools.event_collector.collect import (
    ARCHIVE, ArchiveRedirect, CollectionError, Fetcher, PublicRedirects, collect, main, parse_index, parse_message,
    recent_month,
)


SEPTEMBER = ARCHIVE + "2026-09/"
OCTOBER = ARCHIVE + "2026-10/"
MESSAGE = SEPTEMBER + "msg00000.html"


def index(count, links, month="2026/09"):
    return "<strong>{} {} mails</strong>".format(month, count) + "".join(
        '<a href="{}">link</a>'.format(link) for link in links
    )


def message(body=None, date="Thu, 3 Sep 2026 14:44:49 +0000"):
    body = body or '<p>Example school, 14–19 June 2027.</p><p>Deadline: 1 May.</p><a href="https://example.org/school">Website</a>'
    return (
        '<title>Archive</title><!--X-Subject: [latticenews&#45;l] Example school -->'
        '<!--X-Date: ' + date + ' --><!--X-Message-Id: example-only -->'
        '<!--X-Content-Type: text/html -->'
        '<!--X-Head-of-Message--><ul><li>From: personal-contact@example.org</li>'
        '<li><strong>Date</strong>: ' + date + '</li></ul><!--X-Head-of-Message-End-->'
        '<!--X-Body-of-Message-->'
        + body + '<!--X-Body-of-Message-End--><div>Archive navigation</div>'
    )


class FakeFetcher:
    offline = False
    delay = 0
    cache_hits = 0

    def __init__(self, pages):
        self.pages = pages
        self.requests = 0

    def get(self, url):
        self.requests += 1
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result


class CollectionTests(unittest.TestCase):
    def test_calendar_month_windows_and_january_rollover(self):
        october = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.assertEqual(recent_month(now=october), "2026-10")
        self.assertEqual(recent_month("previous", october), "2026-09")
        january = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(recent_month("previous", january), "2026-12")

    def test_default_run_requests_only_latest_month(self):
        pages = {"https://list.iu.edu/robots.txt": "", OCTOBER: index(
            1, ["msg00000.html", SEPTEMBER, "../", SEPTEMBER + "msg00000.html"], "2026/10"),
            OCTOBER + "msg00000.html": message()}
        fetcher = FakeFetcher(pages)
        with tempfile.TemporaryDirectory() as temporary, \
                patch("tools.event_collector.collect.recent_month", return_value="2026-10"), \
                patch("tools.event_collector.collect.Fetcher", return_value=fetcher), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--output", temporary]), 0)
            report = json.loads((Path(temporary) / "last_run.json").read_text())
            self.assertEqual(report["months"], ["2026-10"])
            self.assertEqual(fetcher.requests, 3)

    def test_previous_month_cli_selection(self):
        fetcher = FakeFetcher({"https://list.iu.edu/robots.txt": "", SEPTEMBER: index(0, [])})
        with tempfile.TemporaryDirectory() as temporary, \
                patch("tools.event_collector.collect.recent_month", return_value="2026-09") as selector, \
                patch("tools.event_collector.collect.Fetcher", return_value=fetcher), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--month-window", "previous", "--output", temporary]), 0)
            selector.assert_called_once_with("previous")
            self.assertEqual(fetcher.requests, 2)

    def test_message_body_has_no_navigation_or_header_contacts(self):
        record = parse_message(message(), MESSAGE)
        self.assertEqual(record["subject"], "[latticenews-l] Example school")
        self.assertEqual(record["posted_at"], "2026-09-03T14:44:49+00:00")
        self.assertIn("14–19 June 2027", record["body"])
        self.assertIn("\n", record["body"])
        self.assertNotIn("personal-contact", record["body"])
        self.assertNotIn("Archive navigation", record["body"])
        self.assertEqual(record["links"], ["https://example.org/school"])

    def test_plain_text_pre_and_link_target_are_preserved(self):
        record = parse_message(message('<pre>Title\nDate: TBD\n  indented text</pre>'
                                       '<a href="https://example.org/?x=1&amp;y=2">Register</a>'), MESSAGE)
        self.assertIn("Title\nDate: TBD\n  indented text", record["body"])
        self.assertEqual(record["links"], ["https://example.org/?x=1&y=2"])

    def test_authentication_200_is_not_empty_archive(self):
        with self.assertRaisesRegex(CollectionError, "Authentication"):
            parse_index('<title>IU Login</title>', SEPTEMBER, "2026-09")

    def test_unknown_markup_fails_visibly(self):
        with self.assertRaises(CollectionError):
            parse_index('<h1>No archive marker</h1>', SEPTEMBER, "2026-09")
        with self.assertRaises(CollectionError):
            parse_message('<p>School announced</p>', MESSAGE)

    def test_explicit_empty_month(self):
        self.assertEqual(parse_index(index(0, []), SEPTEMBER, "2026-09"), ([], [], 0))

    def test_missing_timezone_is_not_assumed(self):
        with self.assertRaises(CollectionError):
            parse_message(message(date="Thu, 3 Sep 2026 14:44:49"), MESSAGE)

    def test_link_without_url_is_flagged_without_inventing_target(self):
        record = parse_message(message('<p>Lattice school.</p><a>Conference website</a>'), MESSAGE)
        self.assertEqual(record["links"], [])
        self.assertTrue(any("no URL" in warning for warning in record["warnings"]))

    def test_sender_and_archive_timestamps_are_distinct(self):
        text = message().replace('<!--X-Date: Thu, 3 Sep 2026 14:44:49 +0000 -->',
                                 '<!--X-Date: Thu, 3 Sep 2026 10:45:12 &#45;0400 -->')
        record = parse_message(text, MESSAGE)
        self.assertEqual(record["posted_at"], "2026-09-03T14:44:49+00:00")
        self.assertEqual(record["archived_at"], "2026-09-03T10:45:12-04:00")

    def test_unsafe_links_and_other_months_not_followed(self):
        text = index(1, ["msg00000.html", "mail2.html", "https://example.org/msg00001.html",
                         OCTOBER + "msg00000.html", "../../info/latticenews-l", "msg00000.html#00000"])
        messages, pages, count = parse_index(text, SEPTEMBER, "2026-09")
        self.assertEqual(messages, [MESSAGE])
        self.assertEqual(pages, [SEPTEMBER + "mail2.html"])
        self.assertEqual(count, 1)

    def test_login_redirect_rejected_before_following(self):
        with self.assertRaises(CollectionError):
            PublicRedirects().redirect_request(Request(SEPTEMBER), None, 302, "Found", {},
                                               "https://idp.login.iu.edu/idp/profile/cas/login")

    def test_pagination_and_repeated_run(self):
        pages = {"https://list.iu.edu/robots.txt": "", SEPTEMBER: index(2, ["msg00000.html", "mail2.html"]),
                 SEPTEMBER + "mail2.html": index(2, ["msg00001.html", "mail1.html"]),
                 MESSAGE: message(), SEPTEMBER + "msg00001.html": message('<p>Another school.</p>')}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = collect(["2026-09"], directory, FakeFetcher(pages))
            self.assertEqual((first["status"], first["new"]), ("success", 2))
            second = collect(["2026-09"], directory, FakeFetcher(pages))
            self.assertEqual((second["new"], second["unchanged"]), (0, 2))
            self.assertEqual(len(list((directory / "announcements").glob("*.json"))), 2)

    def test_failed_run_retains_previous_announcements(self):
        pages = {"https://list.iu.edu/robots.txt": "", SEPTEMBER: index(1, ["msg00000.html"]), MESSAGE: message()}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            collect(["2026-09"], directory, FakeFetcher(pages))
            before = next((directory / "announcements").glob("*.json")).read_bytes()
            pages[SEPTEMBER] = CollectionError("Login redirect")
            failed = collect(["2026-09"], directory, FakeFetcher(pages))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(next((directory / "announcements").glob("*.json")).read_bytes(), before)

    def test_changes_keep_id_and_preserve_previous_version(self):
        pages = {"https://list.iu.edu/robots.txt": "", SEPTEMBER: index(1, ["msg00000.html"]), MESSAGE: message()}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            collect(["2026-09"], directory, FakeFetcher(pages))
            previous = json.loads(next((directory / "announcements").glob("*.json")).read_text())
            pages[MESSAGE] = message('<p>Corrected school dates: 15–19 June 2027.</p>')
            report = collect(["2026-09"], directory, FakeFetcher(pages))
            latest = json.loads(next((directory / "announcements").glob("*.json")).read_text())
            self.assertEqual(report["changed"], 1)
            self.assertEqual(previous["id"], latest["id"])
            self.assertTrue((directory / "versions" / previous["id"] / (previous["content_sha256"] + ".json")).exists())

    def test_robots_blocks_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = collect(["2026-09"], Path(temporary), FakeFetcher({
                "https://list.iu.edu/robots.txt": "User-agent: *\nDisallow: /sympa/arc/\n"}))
            self.assertEqual(report["status"], "failed")
            self.assertIn("disallows", report["failures"][0]["error"])

    def test_count_mismatch_and_limit_are_incomplete(self):
        pages = {"https://list.iu.edu/robots.txt": "", SEPTEMBER: index(2, ["msg00000.html"]), MESSAGE: message()}
        with tempfile.TemporaryDirectory() as temporary:
            report = collect(["2026-09"], Path(temporary), FakeFetcher(pages))
            self.assertEqual(report["status"], "partial")
            self.assertTrue(any("count mismatch" in e["error"] for e in report["failures"]))
        pages[SEPTEMBER] = index(2, ["msg00000.html", "msg00001.html"])
        with tempfile.TemporaryDirectory() as temporary:
            report = collect(["2026-09"], Path(temporary), FakeFetcher(pages), max_messages=1)
            self.assertEqual(report["status"], "partial")
            self.assertTrue(any("limit reached" in e["error"] for e in report["failures"]))

    def test_304_uses_cache_and_offline_requires_no_network(self):
        class Headers(dict):
            def get_content_type(self): return "text/html"
            def get_content_charset(self): return "utf-8"

        class Response:
            headers = Headers({"ETag": '"example"'})
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return message().encode()

        class Opener:
            requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1: return Response()
                raise HTTPError(MESSAGE, 304, "Not Modified", {}, None)

        with tempfile.TemporaryDirectory() as temporary:
            fetcher = Fetcher(Path(temporary), delay=0)
            fetcher.opener = Opener()
            original = fetcher.get(MESSAGE)
            self.assertEqual(fetcher.get(MESSAGE), original)
            self.assertEqual(fetcher.opener.requests[-1].get_header("If-none-match"), '"example"')
            offline = Fetcher(Path(temporary), offline=True)
            self.assertEqual(offline.get(MESSAGE), original)
            self.assertEqual(offline.requests, 0)

    def test_first_anonymous_cookie_retries_same_url_without_login(self):
        class Response:
            headers = type("Headers", (dict,), {
                "get_content_type": lambda self: "text/html",
                "get_content_charset": lambda self: "utf-8"})()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return message().encode()

        with tempfile.TemporaryDirectory() as temporary:
            fetcher = Fetcher(Path(temporary), delay=0)
            class Opener:
                urls = []
                def open(self, request, timeout):
                    self.urls.append(request.full_url)
                    if len(self.urls) == 1:
                        fetcher.cookies.set_cookie(Cookie(
                            0, "anonymous", "synthetic", None, False, "list.iu.edu", True,
                            False, "/", True, True, None, True, None, None, {}, False))
                        raise ArchiveRedirect("Login redirect not followed")
                    return Response()
            fetcher.opener = Opener()
            self.assertEqual(fetcher.get(MESSAGE), message())
            self.assertEqual(fetcher.opener.urls, [MESSAGE, MESSAGE])

    def test_unchanged_session_does_not_retry_login_redirect(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise ArchiveRedirect("Login redirect not followed")
        with tempfile.TemporaryDirectory() as temporary:
            fetcher = Fetcher(Path(temporary), delay=0)
            fetcher.opener = Opener()
            with self.assertRaises(ArchiveRedirect):
                fetcher.get(MESSAGE)
            self.assertEqual(fetcher.opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
