#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What the importer would send, checked without a client.

Two kinds of case here. The synthetic ones pin the rules -- perk order, shard
rows, the summoner-spell key habit, which sets survive a merge. The one at the
end runs a real u.gg build out of the on-disk cache through the same decode
path the picker uses, so the shape being asserted is the shape that actually
arrives rather than one written to match the assertions.

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import importer  # noqa: E402

#: The three stat shard rows, as the client publishes them. Two of them
#: contain 5001 and two contain 5008, which is the whole reason ordering
#: shards needs the rows.
STAT_ROWS = [[5008, 5005, 5007], [5008, 5010, 5001], [5011, 5013, 5001]]


def tree(style_id, rows):
    """A resolved tree block, shaped like `guide.Guide._tree_block`."""
    return {"id": style_id, "name": "", "icon": "",
            "rows": [[{"id": p, "name": "", "icon": "", "picked": picked}
                      for p, picked in row] for row in rows]}


def a_build():
    """Precision keystone, Sorcery secondary -- the Team Advisor page's shape."""
    return {
        "runes": {
            "primary": tree(8000, [
                [(8005, True), (8008, False), (8021, False), (8010, False)],
                [(9101, False), (9111, True), (8009, False)],
                [(9104, False), (9105, True), (9103, False)],
                [(8014, False), (8017, True), (8299, False)],
            ]),
            "secondary": tree(8200, [
                [(8214, False), (8229, False), (8230, False)],
                [(8224, False), (8226, False), (8275, False)],
                [(8210, False), (8234, True), (8233, False)],
                [(8237, False), (8232, False), (8236, True)],
            ]),
            "keystoneId": 8005,
        },
        "shards": [{"id": 5001}, {"id": 5008}, {"id": 5010}],
        "spells": {"spells": [{"id": 4, "name": "Flash"},
                              {"id": 14, "name": "Ignite"}]},
        "start": {"winRate": 54.2, "items": [{"id": 1055}, {"id": 2003},
                                             {"id": 2003}]},
        "core": {"winRate": 51.9, "items": [{"id": 6672}, {"id": 3031},
                                            {"id": 3036}]},
        "fourth": [{"id": 3072}, {"id": 6673}, {"id": 3095}, {"id": 9999}],
        "fifth": [{"id": 3026}],
        "sixth": [],
    }


class RunePage(unittest.TestCase):

    def test_nine_perks_in_the_client_s_order(self):
        page = importer.rune_page(a_build(), "Tristana", STAT_ROWS)
        self.assertEqual(page["primaryStyleId"], 8000)
        self.assertEqual(page["subStyleId"], 8200)
        self.assertEqual(page["selectedPerkIds"],
                         # keystone, three primary rows, two secondary, shards
                         [8005, 9111, 9105, 8017, 8234, 8236, 5008, 5010, 5001])
        self.assertEqual(len(page["selectedPerkIds"]), 9)

    def test_the_name_is_the_champion_and_fits(self):
        self.assertEqual(importer.page_name("Shyvana"), "Tibbers: Shyvana")
        longest = importer.page_name("Nunu & Willump")
        self.assertEqual(longest, "Tibbers: Nunu & Willump")
        self.assertLessEqual(len(longest), importer.NAME_LIMIT)
        self.assertLessEqual(len(importer.page_name("A" * 40)),
                             importer.NAME_LIMIT)

    def test_only_our_own_pages_are_ours(self):
        self.assertTrue(importer.owns_page({"name": "Tibbers: Jhin"}))
        self.assertFalse(importer.owns_page({"name": "the best caitlyn runes"}))
        self.assertFalse(importer.owns_page(
            {"name": "Porofessor: Tristana - Diamond+"}))

    def test_a_build_without_runes_is_refused_not_guessed(self):
        with self.assertRaises(importer.Incomplete):
            importer.rune_page({"shards": []}, "Jhin", STAT_ROWS)

    def test_a_short_tree_is_refused(self):
        build = a_build()
        build["runes"]["secondary"]["rows"][2][1]["picked"] = False
        with self.assertRaises(importer.Incomplete):
            importer.rune_page(build, "Jhin", STAT_ROWS)


class Shards(unittest.TestCase):

    def test_rows_are_filled_in_order(self):
        self.assertEqual(importer.order_shards([5001, 5008, 5010], STAT_ROWS),
                         [5008, 5010, 5001])

    def test_duplicates_land_on_different_rows(self):
        # The live "the best caitlyn runes" page: attack speed, then health
        # scaling twice -- flex and defence, which is the only valid reading.
        self.assertEqual(importer.order_shards([5005, 5001, 5001], STAT_ROWS),
                         [5005, 5001, 5001])
        self.assertEqual(importer.order_shards([5001, 5001, 5005], STAT_ROWS),
                         [5005, 5001, 5001])

    def test_an_unplaceable_set_is_left_alone(self):
        self.assertEqual(importer.order_shards([1, 2, 3], STAT_ROWS), [1, 2, 3])

    def test_without_rows_the_order_is_kept(self):
        self.assertEqual(importer.order_shards([5010, 5008, 5011], None),
                         [5010, 5008, 5011])


class Spells(unittest.TestCase):

    def test_flash_keeps_its_key(self):
        # Flash on D (second slot); the build wants flash + ignite.
        self.assertEqual(importer.place_spells((4, 14), (12, 4)), (14, 4))
        # Flash on F (first slot).
        self.assertEqual(importer.place_spells((4, 14), (4, 12)), (4, 14))

    def test_a_spell_already_held_keeps_its_key(self):
        self.assertEqual(importer.place_spells((12, 14), (11, 14)), (12, 14))
        self.assertEqual(importer.place_spells((12, 14), (14, 11)), (14, 12))

    def test_nothing_in_common_takes_the_build_s_order(self):
        self.assertEqual(importer.place_spells((4, 14), (11, 12)), (4, 14))

    def test_already_set_writes_nothing(self):
        self.assertIsNone(importer.place_spells((4, 14), (4, 14)))
        self.assertIsNone(importer.place_spells((4, 14), (14, 4)))

    def test_a_build_without_spells_is_refused(self):
        with self.assertRaises(importer.Incomplete):
            importer.spell_pair({"spells": {"spells": [{"id": 4}]}})


class ItemSet(unittest.TestCase):

    def setUp(self):
        self.set = importer.item_set(a_build(), 18, "Tristana", "rift", 11)

    def test_it_has_every_field_the_client_writes(self):
        # Measured against the sets already on the account, which were written
        # by the client and by Porofessor.
        self.assertEqual(sorted(self.set), [
            "associatedChampions", "associatedMaps", "blocks", "map", "mode",
            "preferredItemSlots", "sortrank", "startedFrom", "title", "type",
            "uid"])
        self.assertEqual(sorted(self.set["blocks"][0]), [
            "hideIfSummonerSpell", "items", "showIfSummonerSpell", "type"])
        self.assertEqual(self.set["type"], "custom")
        self.assertEqual(self.set["map"], "any")
        self.assertEqual(self.set["mode"], "any")
        self.assertEqual(self.set["associatedChampions"], [18])
        self.assertEqual(self.set["associatedMaps"], [11])
        self.assertEqual(self.set["title"], "Tibbers: Tristana Rift")

    def test_item_ids_are_strings_and_runs_are_counted(self):
        start = self.set["blocks"][0]
        self.assertTrue(start["type"].startswith("Starting items"))
        self.assertEqual(start["items"],
                         [{"count": 1, "id": "1055"}, {"count": 2, "id": "2003"}])

    def test_the_core_keeps_its_order(self):
        core = self.set["blocks"][1]
        self.assertEqual([i["id"] for i in core["items"]],
                         ["6672", "3031", "3036"])

    def test_the_option_slots_are_capped_at_three(self):
        fourth = next(b for b in self.set["blocks"] if b["type"] == "4th item")
        self.assertEqual([i["id"] for i in fourth["items"]],
                         ["3072", "6673", "3095"])
        self.assertNotIn("6th item", [b["type"] for b in self.set["blocks"]])

    def test_an_unknown_map_falls_back_to_the_rift(self):
        other = importer.item_set(a_build(), 18, "Tristana", "nexusblitz", 21)
        self.assertEqual(other["associatedMaps"], [11])
        aram = importer.item_set(a_build(), 18, "Tristana", "aram", 12)
        self.assertEqual(aram["associatedMaps"], [12])
        self.assertEqual(aram["title"], "Tibbers: Tristana ARAM")

    def test_a_rift_build_has_no_boots_tab(self):
        """u.gg publishes no boots slot outside Arena, so the block that
        looked for one never filled -- and a stray key must not revive it."""
        build = a_build()
        build["boots"] = {"winRate": 51.0, "items": [{"id": 3020}]}
        made = importer.item_set(build, 18, "Tristana", "rift", 11)
        types = [b["type"] for b in made["blocks"]]
        self.assertNotIn("Boots", types)
        self.assertEqual(types, [t for t in types if "Boots" not in t])
        self.assertEqual(types[1], "Core build · 51.9% win")

    def test_a_build_with_no_items_is_refused(self):
        with self.assertRaises(importer.Incomplete):
            importer.item_set({}, 18, "Tristana", "rift", 11)


class Merge(unittest.TestCase):

    def test_other_sets_survive_untouched(self):
        foreign = {"title": "Porofessor - Diana (Mid)", "uid": "abc",
                   "blocks": [], "sortrank": 10027}
        before = json.dumps(foreign, sort_keys=True)
        document = {"accountId": 42, "itemSets": [foreign], "timestamp": 1}
        out = importer.merge_sets(document, importer.item_set(
            a_build(), 18, "Tristana", "rift", 11))
        self.assertEqual(out["accountId"], 42)
        self.assertEqual(len(out["itemSets"]), 2)
        self.assertEqual(json.dumps(out["itemSets"][0], sort_keys=True), before)
        self.assertNotEqual(out["timestamp"], 1)

    def test_our_own_set_is_replaced_not_added(self):
        mine = importer.item_set(a_build(), 18, "Tristana", "rift", 11)
        document = {"accountId": 42, "itemSets": [mine], "timestamp": 1}
        out = importer.merge_sets(document, importer.item_set(
            a_build(), 103, "Ahri", "aram", 12))
        self.assertEqual(len(out["itemSets"]), 1)
        self.assertEqual(out["itemSets"][0]["title"], "Tibbers: Ahri ARAM")

    def test_a_stray_set_under_an_old_uid_is_swept_up(self):
        stray = {"title": "Tibbers: Jhin Rift", "uid": "something-older",
                 "blocks": []}
        document = {"accountId": 42, "itemSets": [stray], "timestamp": 1}
        out = importer.merge_sets(document, importer.item_set(
            a_build(), 18, "Tristana", "rift", 11))
        self.assertEqual(len(out["itemSets"]), 1)


class Arena(unittest.TestCase):

    BUILD = {"mode": "arena", "arenaItems": {
        "prismatic": [{"items": [{"id": 4646}, {"id": 3089}]},
                      {"items": [{"id": 4646}, {"id": 3157}]}],
        "core": [{"items": [{"id": 6653}, {"id": 3157}]}],
        "boots": [{"items": [{"id": 3020}]}],
    }}

    def test_the_blocks_are_the_distinct_items_of_the_best_rows(self):
        made = importer.item_set(self.BUILD, 54, "Malphite", "arena", 30,
                                 arena=True)
        self.assertEqual(made["associatedMaps"], [30])
        self.assertEqual(made["title"], "Tibbers: Malphite Arena")
        # Arena keeps its boots tab: op.gg does publish the slot.
        self.assertEqual([b["type"] for b in made["blocks"]],
                         ["Prismatic", "Core build", "Boots"])
        # 4646 appears in both prismatic rows and is written once.
        self.assertEqual([i["id"] for i in made["blocks"][0]["items"]],
                         ["4646", "3089", "3157"])


class Dispatch(unittest.TestCase):
    """`run` decides what to attempt; nothing here reaches a client."""

    class NoClient(importer.Importer):
        def __init__(self, **kw):
            super().__init__(lambda: None, **kw)

    def test_arena_skips_runes_and_spells(self):
        out = self.NoClient().run(Arena.BUILD, 54, "Malphite", kind="arena",
                                  map_id=30, arena=True)
        self.assertNotIn("runes", out)
        self.assertNotIn("spells", out)
        self.assertIn("items", out)

    def test_the_spells_switch_is_obeyed(self):
        out = self.NoClient().run(a_build(), 18, "Tristana", spells=False)
        self.assertNotIn("spells", out)

    def test_one_target_at_a_time(self):
        out = self.NoClient().run(a_build(), 18, "Tristana", what="runes")
        self.assertIn("runes", out)
        self.assertNotIn("items", out)


class FakeClient:
    """An LCU that records what it was asked to write, and answers plausibly.

    Shaped from the real responses: the page list and the item set document
    below are the ones this account actually returns, trimmed to the fields
    the importer reads.
    """

    def __init__(self, pages=None, can_add=False, session=None):
        self.pages = list(pages if pages is not None else [
            {"id": 1, "name": "Shyvana - Conqueror", "current": True,
             "isDeletable": True, "pageKeystone": {"name": "Conqueror"}},
            {"id": 2, "name": "the best caitlyn runes", "current": False,
             "isDeletable": True, "pageKeystone": {"name": "Grasp"}},
            {"id": 3, "name": "Team Advisor", "current": False,
             "isDeletable": True, "pageKeystone": {"name": "Press the Attack"}},
        ])
        self.can_add = can_add
        self.session = session
        self.sets = {"accountId": 42, "timestamp": 1,
                     "itemSets": [{"title": "Porofessor - Diana (Mid)",
                                   "uid": "abc", "blocks": []}]}
        self.sent = []

    def get(self, endpoint, timeout=5.0):
        if endpoint == "/lol-perks/v1/pages":
            return self.pages
        if endpoint == "/lol-perks/v1/inventory":
            return {"canAddCustomPage": self.can_add}
        if endpoint.endswith("/sets"):
            return self.sets
        return None

    def current_summoner(self):
        return {"summonerId": 42, "accountId": 42}

    def champ_select(self):
        return self.session

    def send(self, method, endpoint, body=None, timeout=8.0):
        self.sent.append((method, endpoint, body))
        if method == "POST" and endpoint == "/lol-perks/v1/pages":
            return 200, {"id": 99, **(body or {})}
        return 200, None

    def calls(self):
        return [(m, e) for m, e, _ in self.sent]


class Writes(unittest.TestCase):

    def importer_for(self, client):
        return importer.Importer(lambda: client, stat_rows=lambda: STAT_ROWS)

    def test_an_existing_tibbers_page_is_updated_in_place(self):
        client = FakeClient(pages=[
            {"id": 7, "name": "Tibbers: Jhin", "current": False,
             "isDeletable": True},
            {"id": 8, "name": "mine", "current": True, "isDeletable": True},
        ])
        out = self.importer_for(client).import_runes(a_build(), "Tristana")
        self.assertTrue(out["ok"])
        self.assertEqual(out["how"], "updated")
        self.assertEqual(client.calls(), [("PUT", "/lol-perks/v1/pages/7"),
                                          ("PUT", "/lol-perks/v1/currentpage")])
        self.assertEqual(client.sent[0][2]["name"], "Tibbers: Tristana")
        self.assertEqual(client.sent[1][2], 7)

    def test_a_free_slot_is_used_rather_than_anything_deleted(self):
        client = FakeClient(pages=[{"id": 8, "name": "mine", "current": True,
                                    "isDeletable": True}], can_add=True)
        out = self.importer_for(client).import_runes(a_build(), "Tristana")
        self.assertEqual(out["how"], "created")
        self.assertEqual(client.calls(), [("POST", "/lol-perks/v1/pages"),
                                          ("PUT", "/lol-perks/v1/currentpage")])
        self.assertEqual(client.sent[1][2], 99)

    def test_a_full_account_asks_before_it_deletes(self):
        client = FakeClient()
        out = self.importer_for(client).import_runes(a_build(), "Tristana")
        self.assertFalse(out["ok"])
        self.assertTrue(out["needsSlot"])
        self.assertEqual([p["name"] for p in out["pages"]],
                         ["Shyvana - Conqueror", "the best caitlyn runes",
                          "Team Advisor"])
        self.assertTrue(out["pages"][0]["current"])
        self.assertEqual(client.sent, [], "nothing may be written unasked")

    def test_a_named_page_is_deleted_only_once_confirmed(self):
        client = FakeClient()
        out = self.importer_for(client).import_runes(a_build(), "Tristana", 3)
        self.assertEqual(out["how"], "replaced")
        self.assertEqual(client.calls(), [("DELETE", "/lol-perks/v1/pages/3"),
                                          ("POST", "/lol-perks/v1/pages"),
                                          ("PUT", "/lol-perks/v1/currentpage")])

    def test_an_undeletable_page_is_refused(self):
        client = FakeClient(pages=[
            {"id": 1, "name": "a", "isDeletable": False},
            {"id": 2, "name": "b", "isDeletable": True},
            {"id": 3, "name": "c", "isDeletable": True}])
        out = self.importer_for(client).import_runes(a_build(), "Tristana", 1)
        self.assertFalse(out["ok"])
        self.assertEqual(client.sent, [])

    def test_a_page_that_vanished_is_refused(self):
        client = FakeClient()
        out = self.importer_for(client).import_runes(a_build(), "Tristana", 404)
        self.assertEqual(out["error"], "that rune page is gone")
        self.assertEqual(client.sent, [])

    def test_spells_are_patched_onto_the_keys_already_in_use(self):
        client = FakeClient(session={
            "localPlayerCellId": 2,
            "myTeam": [{"cellId": 2, "spell1Id": 12, "spell2Id": 4}]})
        out = self.importer_for(client).import_spells(a_build())
        self.assertTrue(out["ok"])
        self.assertEqual(client.calls(),
                         [("PATCH", "/lol-champ-select/v1/session/my-selection")])
        self.assertEqual(client.sent[0][2], {"spell1Id": 14, "spell2Id": 4})

    def test_spells_already_right_send_nothing(self):
        client = FakeClient(session={
            "localPlayerCellId": 2,
            "myTeam": [{"cellId": 2, "spell1Id": 14, "spell2Id": 4}]})
        out = self.importer_for(client).import_spells(a_build())
        self.assertTrue(out["unchanged"])
        self.assertEqual(client.sent, [])

    def test_spells_outside_champ_select_are_not_a_failure(self):
        client = FakeClient(session=None)
        out = self.importer_for(client).run(a_build(), 18, "Tristana",
                                            what="spells")
        self.assertTrue(out["ok"])
        self.assertTrue(out["spells"]["skipped"])
        self.assertIsNone(out["error"])

    def test_the_item_set_write_carries_the_whole_document(self):
        client = FakeClient()
        out = self.importer_for(client).import_items(a_build(), 18, "Tristana",
                                                     "rift", 11)
        self.assertTrue(out["ok"])
        method, endpoint, body = client.sent[0]
        self.assertEqual((method, endpoint),
                         ("PUT", "/lol-item-sets/v1/item-sets/42/sets"))
        self.assertEqual(body["accountId"], 42)
        self.assertEqual(len(body["itemSets"]), 2)
        self.assertEqual(body["itemSets"][0]["title"], "Porofessor - Diana (Mid)")
        self.assertEqual(out["kept"], 1)

    def test_a_dry_run_reads_but_never_writes(self):
        client = FakeClient(can_add=True, session={
            "localPlayerCellId": 2,
            "myTeam": [{"cellId": 2, "spell1Id": 12, "spell2Id": 11}]})
        dry = importer.Importer(lambda: client, stat_rows=lambda: STAT_ROWS,
                                dry_run=True)
        out = dry.run(a_build(), 18, "Tristana")
        self.assertTrue(out["ok"])
        self.assertTrue(out["dryRun"])
        self.assertEqual(client.sent, [])

    def test_no_client_is_reported_not_crashed(self):
        out = importer.Importer(lambda: None).run(a_build(), 18, "Tristana")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "the League client is not running")


class RealBuild(unittest.TestCase):
    """One cached u.gg build, decoded exactly as the picker decodes it."""

    CACHE = (Path.home() / "Library" / "Application Support" / "tibbers" / "ugg")

    def setUp(self):
        if not self.CACHE.is_dir():
            self.skipTest("no u.gg cache on this machine")
        files = sorted(self.CACHE.glob("overview-*-ranked_solo_5x5.json"))
        if not files:
            self.skipTest("no cached overview to read")
        self.path = files[-1]

    def test_a_cached_build_produces_a_valid_page_and_set(self):
        from tibbers.ugg import UGG

        payload = json.loads(self.path.read_text())["data"]
        cell = UGG._cell(payload, None)
        self.assertIsNotNone(cell, f"{self.path.name} holds no usable cell")

        # The tree map the picker builds from the client's perkstyles. Read
        # from the same cache if it is there; otherwise derive it from the
        # decoded runes, which is enough to exercise the ordering.
        trees = _trees_from_cache()
        raw = UGG().decode(cell, trees)
        if not raw.get("runes"):
            self.skipTest("this cached cell carries no runes")

        build = _resolve(raw, trees)
        page = importer.rune_page(build, "Shyvana", _stat_rows_from_cache())
        self.assertEqual(len(page["selectedPerkIds"]), 9)
        self.assertEqual(len(set(page["selectedPerkIds"][:6])), 6)
        self.assertEqual(page["primaryStyleId"], raw["runes"]["primaryTree"])
        self.assertEqual(page["subStyleId"], raw["runes"]["secondaryTree"])
        self.assertEqual(page["selectedPerkIds"][0], raw["runes"]["keystone"])
        for perk in page["selectedPerkIds"][:4]:
            self.assertEqual(trees.get(perk), page["primaryStyleId"])
        for perk in page["selectedPerkIds"][4:6]:
            self.assertEqual(trees.get(perk), page["subStyleId"])
        for shard in page["selectedPerkIds"][6:]:
            self.assertIn(shard, range(5001, 5014))

        made = importer.item_set(build, 102, "Shyvana", "rift", 11)
        self.assertTrue(made["blocks"])
        for block in made["blocks"]:
            for item in block["items"]:
                self.assertIsInstance(item["id"], str)
                self.assertGreaterEqual(item["count"], 1)


def _perkstyles() -> dict:
    root = (Path.home() / "Library" / "Application Support" / "tibbers"
            / "gamedata")
    for path in sorted(root.glob("*/perkstyles.json"), reverse=True):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _trees_from_cache() -> dict:
    out = {}
    for style in (_perkstyles().get("styles") or []):
        for slot in style.get("slots") or []:
            for perk in slot.get("perks") or []:
                out[int(perk)] = int(style["id"])
    return out


def _stat_rows_from_cache():
    for style in (_perkstyles().get("styles") or []):
        rows = [[int(p) for p in (slot.get("perks") or [])]
                for slot in style.get("slots") or []
                if (slot.get("type") or "") == "kStatMod"]
        if len(rows) == 3:
            return rows
    return STAT_ROWS


def _resolve(raw: dict, trees: dict) -> dict:
    """The half of `Guide.build` the importer reads, without a GameData."""
    styles = {int(s["id"]): s for s in (_perkstyles().get("styles") or [])}

    def rows_for(style_id, chosen):
        picked = set(chosen)
        rows = []
        for slot in (styles.get(int(style_id)) or {}).get("slots") or []:
            if (slot.get("type") or "") == "kStatMod":
                continue
            rows.append([{"id": int(p), "picked": int(p) in picked}
                         for p in slot.get("perks") or []])
        return {"id": int(style_id), "rows": rows}

    runes = raw["runes"]
    primary = [runes["keystone"]] + list(runes.get("primary") or [])
    out = {
        "runes": {"primary": rows_for(runes["primaryTree"], primary),
                  "secondary": rows_for(runes["secondaryTree"],
                                        runes.get("secondary") or []),
                  "keystoneId": runes["keystone"]},
        "shards": [{"id": s} for s in (raw.get("shards") or {}).get("ids") or []],
        "spells": {"spells": [{"id": s}
                              for s in (raw.get("spells") or {}).get("ids") or []]},
    }
    for name in ("start", "core"):
        block = raw.get(name) or {}
        out[name] = {"winRate": block.get("winRate", 0.0),
                     "items": [{"id": i} for i in block.get("items") or []]}
    for name in ("fourth", "fifth", "sixth"):
        out[name] = [{"id": r["itemId"]} for r in raw.get(name) or []]
    return out


if __name__ == "__main__":
    unittest.main()
