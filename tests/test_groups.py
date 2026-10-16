"""`groups` is the contract the island's keybinding view builds its tabs from.

The island list only shows chords you can actually press, so a tab's number has to
be the number of rows under it: an unbound (struck-out) chord must not be counted,
or the view advertises a row it refuses to draw. The panel keeps the *other* count
(`category_counts`, unbound included) because there a chip is a filter over a list
that does show unbound rows. Both numbers come out of the same model, and a change
to one silently fixing or breaking the other is exactly what these invariants guard.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import keybinds_manager as km


MODEL = km.build_complete_model()


class TestGroups(unittest.TestCase):
    def setUp(self):
        self.rows = MODEL["active"]
        self.groups = MODEL["groups"]

    def test_every_row_is_labelled(self):
        # A row without a category falls into a tab the view cannot name.
        for row in self.rows:
            self.assertTrue(row.get("category"), row)
            self.assertTrue(row.get("category_zh"), row)

    def test_group_counts_exclude_unbound(self):
        live = {}
        for row in self.rows:
            if row.get("status") == "disabled":
                continue
            live[row["category"]] = live.get(row["category"], 0) + 1
        self.assertEqual({g["id"]: g["count"] for g in self.groups}, live)

    def test_empty_groups_are_dropped(self):
        # A tab with nothing under it is a click that shows an empty pane.
        for group in self.groups:
            self.assertGreater(group["count"], 0, group)
        self.assertEqual(len(self.groups), len({g["id"] for g in self.groups}))

    def test_group_order_follows_category_order(self):
        order = MODEL["category_order"]
        ids = [g["id"] for g in self.groups]
        self.assertEqual(ids, [cat for cat in order if cat in ids])
        for group in self.groups:
            self.assertIn(group["id"], order)

    def test_tab_labels_are_short_and_speak_the_right_dimension(self):
        # Two kinds of bucket, two kinds of label, and each must stay in its lane:
        # the Omarchy buckets say what a shortcut *does* (Chinese, because he reads
        # this list to learn the chords), the program-owned buckets say *who owns
        # the key* (the program's own name - "终端"/"分屏"/"AI" were three angles on
        # one window, so each read wrong for the other two).
        self.assertEqual(km.CATEGORY_ZH[km.CATEGORY_HERDR], "herdr")
        self.assertEqual(km.CATEGORY_ZH[km.CATEGORY_TERMINAL], "ghostty")
        self.assertEqual(km.CATEGORY_ZH[km.CATEGORY_PI], "pi")
        self.assertEqual(km.CATEGORY_ZH[km.CATEGORY_ISLAND], "island")
        functional = (
            km.CATEGORY_WORKSPACE, km.CATEGORY_WINDOW, km.CATEGORY_APP, km.CATEGORY_SYSTEM,
            km.CATEGORY_DISPLAY, km.CATEGORY_CAPTURE, km.CATEGORY_CLIPBOARD, km.CATEGORY_MEDIA,
            km.CATEGORY_NOTIFICATION,
        )
        for cat in functional:
            self.assertRegex(km.CATEGORY_ZH[cat], "[\u4e00-\u9fff]", cat)
        for group in self.groups:
            # A tab strip is ~9 cells wide before the count gets clipped.
            self.assertLessEqual(len(group["label"]), 8, group)

    def test_action_labels_avoid_english_jargon(self):
        # "pane" / "zoom" / "resize" were the words he could not map to anything.
        for action, label in km.HERDR_ACTION_ZH.items():
            for jargon in ("pane", "zoom", "resize", "detach"):
                self.assertNotIn(jargon, label, (action, label))

if __name__ == "__main__":
    unittest.main()
