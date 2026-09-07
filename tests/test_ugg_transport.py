#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The u.gg transport: which client fetches a file, and what happens on a refusal.

u.gg's CDN refuses `urllib` every time and challenges even an accepted curl a
fraction of the time, so curl goes first and is retried, the header sets are a
last resort, and a held copy is revalidated conditionally. A file that does
not exist (S3's `AccessDenied`) is told from a bot challenge and never
retried; a bot challenge that outlasts every attempt still yields to a stale
copy when one is held.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import ugg  # noqa: E402

FILE = f"{ugg.BASE}/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
CHALLENGE = b"<!DOCTYPE html><html>Just a moment..."
ACCESS_DENIED = b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>"


def http_error(url, code, body=b""):
    return urllib.error.HTTPError(url, code, "", {}, io.BytesIO(body))


class Response(io.BytesIO):
    """Enough of an HTTP response for the header-set fallback."""

    def __init__(self, body, etag=None):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag} if etag else {}
        self.status = 200

    def __enter__(self):
        self.seek(0)
        return self

    def __exit__(self, *exc):
        return False


class TransportCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch = mock.patch.object(ugg.system, "data_dir",
                                  lambda: Path(self.home.name))
        patch.start()
        self.addCleanup(patch.stop)

    def wire_curl(self, *results):
        """Script `_curl` to return each of *results* in turn (a callable is
        called with (url, etag)); the last result repeats."""
        calls = []

        def fake(url, etag=None):
            calls.append((url, etag))
            r = results[min(len(calls) - 1, len(results) - 1)]
            return r(url, etag) if callable(r) else r

        p = mock.patch.object(ugg, "_curl", fake)
        p.start()
        self.addCleanup(p.stop)
        self.curl_calls = calls

    def wire_urllib(self, answers):
        seen = []

        def urlopen(request, timeout=0):
            seen.append(request.full_url)
            a = answers.get(request.full_url)
            if a is None:
                raise urllib.error.URLError("unscripted")
            if isinstance(a, BaseException):
                raise a
            return a

        p = mock.patch.object(ugg.urllib.request, "urlopen", urlopen)
        p.start()
        self.addCleanup(p.stop)
        self.urllib_calls = seen


class CurlFirst(TransportCase):

    def test_a_200_is_decoded_and_cached(self):
        self.wire_curl((200, b'{"v": 1}', '"a"'))
        self.wire_urllib({})
        client = ugg.UGG()
        self.assertEqual(client._get(FILE, "k"), {"v": 1})
        # No fallback to urllib once curl succeeds.
        self.assertEqual(self.urllib_calls, [])
        # A second read is served from memory, no second fetch.
        self.assertEqual(client._get(FILE, "k"), {"v": 1})
        self.assertEqual(len(self.curl_calls), 1)

    def test_no_sleep_between_the_first_and_a_retry(self):
        # A challenge then success: two curl calls, and the backoff is honoured.
        slept = []
        self.wire_curl((403, CHALLENGE, None), (200, b'{"v": 2}', '"b"'))
        self.wire_urllib({})
        with mock.patch.object(ugg.time, "sleep", slept.append):
            self.assertEqual(ugg.UGG()._get(FILE, "k"), {"v": 2})
        self.assertEqual(len(self.curl_calls), 2)
        self.assertEqual(slept, [ugg.CURL_BACKOFF[1]])

    def test_a_missing_file_is_unavailable_and_not_retried(self):
        self.wire_curl((403, ACCESS_DENIED, None))
        self.wire_urllib({})
        with self.assertRaises(ugg.Unavailable) as caught:
            ugg.UGG()._get(FILE, "k")
        self.assertIn("no data", str(caught.exception))
        self.assertEqual(len(self.curl_calls), 1)   # not retried

    def test_a_persistent_challenge_is_retried_then_falls_to_header_sets(self):
        self.wire_curl((403, CHALLENGE, None))            # every attempt challenged
        self.wire_urllib({FILE: Response({"v": 3}, etag='"e"')})
        with mock.patch.object(ugg.time, "sleep", lambda s: None):
            self.assertEqual(ugg.UGG()._get(FILE, "k"), {"v": 3})
        self.assertEqual(len(self.curl_calls), ugg.CURL_ATTEMPTS)
        self.assertEqual(self.urllib_calls, [FILE])

    def test_the_configured_curl_is_what_runs(self):
        with mock.patch.dict(ugg.os.environ, {"TIBBERS_CURL": "/opt/x/curl --impersonate chrome"}):
            self.assertEqual(ugg._curl_argv()[:3],
                             ["/opt/x/curl", "--impersonate", "chrome"])

    def test_a_supplied_browser_curl_is_preferred_over_path(self):
        with mock.patch.dict(ugg.os.environ, {}, clear=False):
            ugg.os.environ.pop("TIBBERS_CURL", None)
            with mock.patch.object(ugg.system, "browser_curl",
                                   lambda: ["C:/t/curl.exe", "--impersonate", "chrome131"]):
                self.assertEqual(ugg._curl_argv(),
                                 ["C:/t/curl.exe", "--impersonate", "chrome131"])
            with mock.patch.object(ugg.system, "browser_curl", lambda: None):
                self.assertEqual(ugg._curl_argv(), ["curl"])


class FallbackAndCache(TransportCase):

    def test_urllib_is_used_when_curl_is_absent(self):
        self.wire_curl(None)                        # curl cannot run
        self.wire_urllib({FILE: Response({"v": 4})})
        self.assertEqual(ugg.UGG()._get(FILE, "k"), {"v": 4})
        self.assertEqual(self.urllib_calls, [FILE])

    def test_a_challenge_everywhere_reads_as_refused_not_a_missing_file(self):
        self.wire_curl((403, CHALLENGE, None))
        self.wire_urllib({FILE: http_error(FILE, 403, CHALLENGE)})
        with mock.patch.object(ugg.time, "sleep", lambda s: None):
            with self.assertRaises(ugg.Unavailable) as caught:
                ugg.UGG()._get(FILE, "k")
        self.assertIn("refused", str(caught.exception))
        self.assertNotIn("HTTP Error", str(caught.exception))

    def test_a_stale_copy_beats_a_refusal(self):
        self.wire_curl((200, b'{"v": 5}', '"e5"'))
        self.wire_urllib({})
        client = ugg.UGG()
        client._get(FILE, "k")
        real = time.time
        with mock.patch.object(ugg.time, "time",
                               lambda: real() + ugg.CACHE_SECONDS + 1):
            self.wire_curl((403, CHALLENGE, None))
            self.wire_urllib({FILE: http_error(FILE, 403, CHALLENGE)})
            with mock.patch.object(ugg.time, "sleep", lambda s: None):
                self.assertEqual(client._get(FILE, "k"), {"v": 5})

    def test_an_expired_file_is_revalidated_with_its_etag(self):
        # First fetch through the header sets, which carry an ETag.
        self.wire_curl(None)
        self.wire_urllib({FILE: Response({"v": 6}, etag='"E"')})
        client = ugg.UGG()
        client._get(FILE, "k")
        self.assertEqual(client._memory["k"]["etag"], '"E"')

        real = time.time
        conditional = []

        def curl(url, etag=None):
            conditional.append(etag)
            return (304, b"", None)

        with mock.patch.object(ugg.time, "time",
                               lambda: real() + ugg.CACHE_SECONDS + 1):
            with mock.patch.object(ugg, "_curl", curl):
                self.assertEqual(client._get(FILE, "k"), {"v": 6})
        self.assertEqual(conditional, ['"E"'])      # the stored ETag was sent


class Config(unittest.TestCase):

    def test_cache_hours_can_be_overridden(self):
        with mock.patch.dict(ugg.os.environ, {"TIBBERS_UGG_CACHE_HOURS": "12"}):
            self.assertEqual(ugg._cache_hours(), 12.0)
        with mock.patch.dict(ugg.os.environ, {"TIBBERS_UGG_CACHE_HOURS": "nonsense"}):
            self.assertEqual(ugg._cache_hours(), 8.0)
        with mock.patch.dict(ugg.os.environ, {"TIBBERS_UGG_CACHE_HOURS": "0"}):
            self.assertEqual(ugg._cache_hours(), 0.5)   # floored, never zero


if __name__ == "__main__":
    unittest.main()
