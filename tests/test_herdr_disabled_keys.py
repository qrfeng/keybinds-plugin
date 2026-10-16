"""An action herdr switched off with `x = ""` must still appear in the panel.

`~/.config/herdr/config.toml` disables a built-in binding by binding it to an
empty chord (verified against `herdr config check`: `""` validates clean, while
`"none"` / `"unbind"` parse as invalid). Such an action owns no chord, so a
parser that only walks chords drops it in silence and the panel stops being the
place the user can audit "what did we turn off, and why". The invariant behind
these tests: every action listed in `[keys]` yields exactly one row, either a
chord row or a disabled row.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import keybinds_manager as km


def _rows_from(config_body):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(config_body)
        old = km.HERDR_CONFIG_PATH
        km.HERDR_CONFIG_PATH = path
        try:
            return km.parse_herdr_keybinds()
        finally:
            km.HERDR_CONFIG_PATH = old


CONFIG = """
[keys]
prefix = "ctrl+space"
zoom = "ctrl+alt+z"
swap_pane_up = ""
remote_image_paste = ""
move_tab_next = ["alt+shift+right"]
half_empty = ["", "alt+k"]
"""


class HerdrDisabledRows(unittest.TestCase):
    def setUp(self):
        self.rows = _rows_from(CONFIG)
        self.by_action = {}
        for row in self.rows:
            self.by_action.setdefault(row["action"], []).append(row)

    def test_disabled_action_owns_one_row_with_no_chord(self):
        rows = self.by_action.get("swap_pane_up", [])
        self.assertEqual(len(rows), 1, "a disabled action must appear exactly once")
        row = rows[0]
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(row["raw_key"], "")
        self.assertTrue(row["key"], "the panel needs a non-empty left column")
        self.assertIn("已关闭", row["description_zh"])

    def test_only_actions_without_any_chord_count_as_disabled(self):
        actions = {r["action"] for r in self.rows if r["status"] == "disabled"}
        self.assertEqual(actions, {"swap_pane_up", "remote_image_paste"})

    def test_bound_actions_keep_their_chord_rows(self):
        self.assertEqual([r["key"] for r in self.by_action["zoom"]], ["CTRL + ALT + Z"])
        self.assertEqual([r["key"] for r in self.by_action["move_tab_next"]],
                         ["ALT + SHIFT + RIGHT"])
        self.assertEqual([r["key"] for r in self.by_action["half_empty"]], ["ALT + K"])

    def test_every_listed_action_is_accounted_for(self):
        # `prefix` is itself a [keys] entry, so it shows up as the prefix chord.
        listed = {"prefix", "zoom", "swap_pane_up", "remote_image_paste",
                  "move_tab_next", "half_empty"}
        self.assertEqual({r["action"] for r in self.rows}, listed)


if __name__ == "__main__":
    unittest.main()


class IndexedRangeLabels(unittest.TestCase):
    def test_placeholder_label_is_filled_instead_of_doubled(self):
        rows = _rows_from('[keys]\nfocus_agent = "ctrl+alt+shift+1..9"\n')
        labels = [r["description_zh"] for r in rows]
        self.assertEqual(labels[2], "直达第 N 个 AI 对话".replace("第 N 个", "第 3 个"))
        self.assertTrue(all("（第" not in label for label in labels), labels)

    def test_plain_label_still_gets_the_position_suffix(self):
        rows = _rows_from('[keys]\nnext_tab = "alt+1..3"\n')
        self.assertEqual([r["description_zh"] for r in rows],
                         ["下一个标签页（第 1 个）", "下一个标签页（第 2 个）", "下一个标签页（第 3 个）"])
