#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The champ-select session, and the memo in front of the guide fetch.

The memo is what stops switching between the enemies in one champ select
refetching the build over the network, and it is bounded so a long evening
does not accumulate every guide it has ever shown.
"""

from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class Memo(unittest.TestCase):

    def test_it_returns_what_it_stored(self):
        memo = OrderedDict()
        self.assertEqual(main.memoise(memo, "k", {"a": 1}, 4), {"a": 1})
        self.assertEqual(memo["k"], {"a": 1})

    def test_the_oldest_go_first(self):
        memo = OrderedDict()
        for i in range(10):
            main.memoise(memo, i, i, 4)
        self.assertEqual(list(memo), [6, 7, 8, 9])

    def test_a_key_stored_again_is_not_a_second_entry(self):
        memo = OrderedDict()
        main.memoise(memo, "k", 1, 4)
        main.memoise(memo, "k", 2, 4)
        self.assertEqual(list(memo), ["k"])
        self.assertEqual(memo["k"], 2)

    def test_the_two_bounds_are_the_ones_the_guide_uses(self):
        self.assertEqual((main.GUIDE_MEMO_MAX, main.SHARED_MEMO_MAX), (8, 4))


class SessionDefaults(unittest.TestCase):
    """Two instances must not share the memos."""

    def test_each_session_gets_its_own_memos(self):
        one, two = main.Session(), main.Session()
        one.guide_memo["k"] = 1
        one.shared_memo["k"] = 1
        self.assertEqual(two.guide_memo, {})
        self.assertEqual(two.shared_memo, {})

    def test_it_starts_with_nothing_selected(self):
        session = main.Session()
        self.assertIsNone(session.selected)
        self.assertIsNone(session.chroma)
        self.assertIsNone(session.champion_id)
        self.assertIsNone(session.auto_imported)
        self.assertIsNone(session.guide_key)
        self.assertFalse(session.was_locked)
        self.assertFalse(session.was_in_select)
        self.assertFalse(session.opponent_by_hand)
        self.assertEqual(session.guide_generation, 0)

    def test_a_mistyped_field_is_an_error_rather_than_a_new_one(self):
        """The whole reason this is not a dict: the old one grew a key and
        carried on."""
        session = main.Session()
        with self.assertRaises(AttributeError):
            session.was_lockd = True


class Arming(unittest.TestCase):
    """The worker that arms the patcher runs one build at a time and always
    the newest request. A pick made while a build was in flight used to be
    dropped, and stepping through the rail armed the first skin passed over.
    """

    def worker(self):
        import threading
        ran, gate = [], threading.Event()
        started = threading.Event()

        def run(request):
            started.set()
            gate.wait(2)
            ran.append(request)

        return main.LatestOnly(run), ran, gate, started

    def settle(self, worker):
        for _ in range(200):
            if not worker.busy():
                return
            import time
            time.sleep(0.01)
        self.fail("the worker never went idle")

    def test_a_request_made_during_a_build_runs_after_it(self):
        worker, ran, gate, started = self.worker()
        self.assertTrue(worker.submit("first"))
        started.wait(2)
        self.assertFalse(worker.submit("second"))
        self.assertTrue(worker.has_pending())
        gate.set()
        self.settle(worker)
        self.assertEqual(ran, ["first", "second"])

    def test_only_the_newest_of_several_survives_the_wait(self):
        """Arrow through five skins: the first builds, the fifth is what ends
        up armed, and the three in between are never built at all."""
        worker, ran, gate, started = self.worker()
        worker.submit(1)
        started.wait(2)
        for request in (2, 3, 4, 5):
            worker.submit(request)
        gate.set()
        self.settle(worker)
        self.assertEqual(ran, [1, 5])
        self.assertFalse(worker.has_pending())

    def test_a_stop_can_be_queued_behind_a_build(self):
        worker, ran, gate, started = self.worker()
        worker.submit(("mod", {}))
        started.wait(2)
        worker.submit(None)
        gate.set()
        self.settle(worker)
        self.assertEqual(ran, [("mod", {}), None])

    def test_a_build_that_raises_does_not_take_the_next_one_with_it(self):
        import threading
        ran = []
        first = threading.Event()

        def run(request):
            if request == "bad":
                first.set()
                raise RuntimeError("mkoverlay fell over")
            ran.append(request)

        worker = main.LatestOnly(run)
        worker.submit("bad")
        first.wait(2)
        self.settle(worker)
        worker.submit("good")
        self.settle(worker)
        self.assertEqual(ran, ["good"])

    def test_the_worker_is_idle_again_once_the_queue_drains(self):
        worker, ran, gate, started = self.worker()
        gate.set()
        worker.submit("a")
        self.settle(worker)
        self.assertFalse(worker.busy())
        self.assertTrue(worker.submit("b"), "a fresh run, not a queued one")
        self.settle(worker)
        self.assertEqual(ran, ["a", "b"])


class RememberedChroma(unittest.TestCase):
    """A skin picked on its own brings back the chroma remembered for it --
    but only a chroma that exists on disk, or the restore fails outright."""

    SKINS = [
        {"id": 103005, "available": True,
         "chromas": [{"id": 103006, "available": True},
                     {"id": 103007, "available": False}]},
        {"id": 103001, "available": True, "chromas": []},
    ]

    def test_a_remembered_chroma_with_a_mod_comes_back(self):
        self.assertEqual(main.remembered_chroma(self.SKINS, 103005, 103006),
                         103006)

    def test_a_remembered_chroma_with_no_mod_falls_back_to_the_skin(self):
        self.assertIsNone(main.remembered_chroma(self.SKINS, 103005, 103007))

    def test_a_chroma_of_another_skin_is_not_carried_over(self):
        self.assertIsNone(main.remembered_chroma(self.SKINS, 103001, 103006))

    def test_nothing_remembered_is_nothing(self):
        self.assertIsNone(main.remembered_chroma(self.SKINS, 103005, None))
        self.assertIsNone(main.remembered_chroma(self.SKINS, None, 103006))

    def test_the_keep_marker_is_not_a_chroma_and_not_none(self):
        self.assertIsNot(main.KEEP_CHROMA, None)
        self.assertFalse(isinstance(main.KEEP_CHROMA, int))


if __name__ == "__main__":
    unittest.main()
