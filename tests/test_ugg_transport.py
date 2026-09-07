#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Where a u.gg file is read from, and for how long it is trusted.

The mirror goes first because u.gg's CDN refuses most non-browser clients;
u.gg itself is for what the mirror does not carry and for when it is down.
Each source's copy expires on that source's own terms and is revalidated
only against the source that issued its ETag.
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

MIRROR = "https://mirror.test"
FILE = f"{ugg.BASE}/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
PAIR = f"{ugg.BASE}/overview/16_17/ranked_solo_5x5/matchups/18_51/1.5.0.json"


class Response(io.BytesIO):
    """Enough of an HTTP response for `_get`: a body, headers, a status."""

    def __init__(self, body, etag=None):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag} if etag else {}
        self.status = 200

    def __enter__(self):
        self.seek(0)   # one scripted answer can serve several requests
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, body=b""):
    return urllib.error.HTTPError(url, code, "", {}, io.BytesIO(body))


class Network:
    """A scripted network: URL -> what urlopen does, plus what curl returns.

    Each answer is a value to return, an exception to raise, or a callable
    taking the request. Every request is recorded, headers included.
    """

    def __init__(self, answers, curl=None):
        self.answers = answers
        self.curl = curl
        self.requests = []

    def urlopen(self, request, timeout=0):
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        answer = self.answers.get(url)
        if answer is None:
            raise urllib.error.URLError(f"unscripted {url}")
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _curl(self, url):
        self.requests.append((url, {"curl": True}))
        return self.curl(url) if callable(self.curl) else self.curl


class TransportCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(ugg, "MIRROR", MIRROR),
            mock.patch.object(ugg.system, "data_dir",
                              lambda: Path(self.home.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.home.cleanup)

    def wire(self, answers, curl=None):
        net = Network(answers, curl)
        for target in (mock.patch.object(ugg.urllib.request, "urlopen", net.urlopen),
                       mock.patch.object(ugg, "_curl", net._curl)):
            target.start()
            self.addCleanup(target.stop)
        return net

    @staticmethod
    def later(seconds):
        """A clock running `seconds` ahead, for `with`."""
        real = time.time
        return mock.patch.object(ugg.time, "time", lambda: real() + seconds)

    @staticmethod
    def status(next_run_in=3600):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                              time.gmtime(time.time() + next_run_in))
        return Response({"nextRunAt": stamp})


class MirrorFirst(TransportCase):

    def test_a_file_the_mirror_has_never_touches_ugg(self):
        net = self.wire({
            f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json":
                Response({"ok": 1}, etag='"m1"'),
            f"{MIRROR}/status.json": lambda r: self.status(),
        })
        self.assertEqual(ugg.UGG()._get(FILE, "k"), {"ok": 1})
        self.assertFalse([u for u, _ in net.requests if "u.gg" in u])
        headers = [h for u, h in net.requests if u.endswith("/1.5.0.json")][0]
        self.assertEqual(headers.get("Accept-encoding"), "gzip")
        self.assertTrue(headers.get("User-agent", "").startswith("tibbers/"))

    def test_the_manifest_maps_to_the_mirror_s_manifest(self):
        net = self.wire({f"{MIRROR}/manifest.json": Response({"16_17": {}}),
                         f"{MIRROR}/status.json": lambda r: self.status()})
        self.assertEqual(ugg.UGG()._get(ugg.VERSIONS_URL, "versions", 3600),
                         {"16_17": {}})
        self.assertEqual(net.requests[0][0], f"{MIRROR}/manifest.json")

    def test_a_file_the_mirror_lacks_comes_from_ugg(self):
        pair_on_mirror = (f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/"
                          "matchups/18_51/1.5.0.json")
        net = self.wire({pair_on_mirror: http_error(pair_on_mirror, 404)},
                        curl=(200, json.dumps({"pair": 1}).encode()))
        self.assertEqual(ugg.UGG()._get(PAIR, "pair"), {"pair": 1})
        self.assertEqual([u for u, h in net.requests if h.get("curl")], [PAIR])

    def test_a_mirror_that_is_down_falls_back_to_ugg(self):
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        net = self.wire({mirror_file: urllib.error.URLError("refused")},
                        curl=(200, json.dumps({"direct": 1}).encode()))
        self.assertEqual(ugg.UGG()._get(FILE, "k"), {"direct": 1})
        self.assertIn(FILE, [u for u, _ in net.requests])

    def test_with_no_mirror_configured_ugg_is_asked_directly(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            net = self.wire({}, curl=(200, json.dumps({"direct": 1}).encode()))
            self.assertEqual(ugg.UGG()._get(FILE, "k"), {"direct": 1})
        self.assertEqual([u for u, _ in net.requests], [FILE])


class UggDirect(TransportCase):

    def test_curl_goes_before_the_header_sets(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            net = self.wire({}, curl=(200, b'{"a": 1}'))
            ugg.UGG()._get(FILE, "k")
        self.assertEqual(len(net.requests), 1)
        self.assertTrue(net.requests[0][1].get("curl"))

    def test_a_missing_file_is_unavailable_not_retried(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            net = self.wire({}, curl=(403, b"<Error><Code>AccessDenied</Code>"))
            with self.assertRaises(ugg.Unavailable) as caught:
                ugg.UGG()._get(FILE, "k")
        self.assertIn("no data", str(caught.exception))
        self.assertEqual(len(net.requests), 1)

    def test_a_challenge_from_every_client_reads_as_refused(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            self.wire({FILE: http_error(FILE, 403, b"<html>Just a moment")},
                      curl=(403, b"<html>Just a moment"))
            with self.assertRaises(ugg.Unavailable) as caught:
                ugg.UGG()._get(FILE, "k")
        self.assertIn("refused", str(caught.exception))
        self.assertNotIn("HTTP Error", str(caught.exception))

    def test_a_stale_copy_beats_a_refusal(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            self.wire({}, curl=(200, b'{"v": 1}'))
            client = ugg.UGG()
            client._get(FILE, "k")
            # Time passes; every transport now refuses.
            with self.later(ugg.CACHE_SECONDS + 1):
                self.wire({FILE: http_error(FILE, 403, b"<html>")},
                          curl=(403, b"<html>"))
                self.assertEqual(client._get(FILE, "k"), {"v": 1})


class Lifetimes(TransportCase):

    def test_a_mirror_file_expires_just_after_the_next_refresh(self):
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        self.wire({mirror_file: Response({"ok": 1}, etag='"m1"'),
                   f"{MIRROR}/status.json": lambda r: self.status(3600)})
        client = ugg.UGG()
        client._get(FILE, "k")
        entry = client._memory["k"]
        self.assertEqual(entry["source"], "mirror")
        low = time.time() + 3600 + ugg.STATUS_SLACK
        self.assertGreaterEqual(entry["expires"], low - 2)
        self.assertLessEqual(entry["expires"], low + ugg.STATUS_SLACK + 2)

    def test_the_schedule_is_read_once_per_window(self):
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        net = self.wire({mirror_file: Response({"ok": 1}),
                         f"{MIRROR}/status.json": lambda r: self.status()})
        client = ugg.UGG()
        for key in ("a", "b", "c"):
            client._get(FILE, key)
        self.assertEqual(
            len([u for u, _ in net.requests if u.endswith("status.json")]), 1)

    def test_without_a_schedule_the_cdn_s_lifetime_applies(self):
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        self.wire({mirror_file: Response({"ok": 1}),
                   f"{MIRROR}/status.json": http_error("s", 500)})
        client = ugg.UGG()
        client._get(FILE, "k")
        expires = client._memory["k"]["expires"]
        self.assertAlmostEqual(expires, time.time() + ugg.CACHE_SECONDS, delta=5)

    def test_an_expired_mirror_file_is_revalidated_with_its_own_etag(self):
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        net = self.wire({mirror_file: Response({"ok": 1}, etag='"m1"'),
                         f"{MIRROR}/status.json": lambda r: self.status(-1)})
        client = ugg.UGG()
        client._get(FILE, "k")
        expires = client._memory["k"]["expires"]

        net.answers[mirror_file] = http_error(mirror_file, 304)
        with mock.patch.object(ugg.time, "time", lambda: expires + 1):
            self.assertEqual(client._get(FILE, "k"), {"ok": 1})
        sent = [h for u, h in net.requests if u == mirror_file][-1]
        self.assertEqual(sent.get("If-none-match"), '"m1"')
        # No u.gg traffic at any point.
        self.assertFalse([u for u, _ in net.requests if "u.gg" in u])

    def test_a_ugg_etag_is_never_offered_to_the_mirror(self):
        with mock.patch.object(ugg, "MIRROR", ""):
            self.wire({FILE: Response({"v": 1}, etag='"u1"')}, curl=None)
            client = ugg.UGG()
            client._get(FILE, "k")
            self.assertEqual(client._memory["k"]["source"], "ugg")
        mirror_file = f"{MIRROR}/lol/1.5/overview/16_17/ranked_solo_5x5/18/1.5.0.json"
        net = self.wire({mirror_file: Response({"v": 2}),
                         f"{MIRROR}/status.json": lambda r: self.status()})
        with self.later(ugg.CACHE_SECONDS + 1):
            self.assertEqual(client._get(FILE, "k"), {"v": 2})
        sent = [h for u, h in net.requests if u == mirror_file][0]
        self.assertNotIn("If-none-match", sent)


if __name__ == "__main__":
    unittest.main()
