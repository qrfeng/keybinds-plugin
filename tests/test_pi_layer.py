"""The pi layer must show pi's *effective* keys, not a hand-copied guess.

pi owns its own keymap (`~/.pi/agent/keybindings.json`, applied with `/reload`) and
documents its defaults as a markdown table. The panel mirrors the docs section for
transcript scrolling with the user's file on top, because pi is the innermost layer:
a chord the user believes scrolls the transcript but that herdr grabbed is dead, and
a Windows-only default listed as live is a lie about this machine.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import keybinds_manager as km


DOCS = """# Keybindings Reference

## Assign keybindings

| Keybinding id | Default | Description |
|---|---|---|
| `editor.newline` | `enter` | Insert a newline |

#### Fullscreen

| Keybinding id | Default | Description |
|---|---|---|
| `tui.altScreen.pageUp` | `pageUp` | Scroll the transcript up by one page |
| `tui.altScreen.lineUp` | None | Scroll the transcript up by one line |
| `tui.altScreen.previousPrompt` | `ctrl+shift+up`, `ctrl+up` (`ctrl+up` only on Windows and WSL) | Jump to the previous marked message |
| `tui.altScreen.search` | `ctrl+shift+f` (`ctrl+f` on Windows and WSL) | Search the rendered transcript |

### Application

| Keybinding id | Default | Description |
|---|---|---|
| `app.interrupt` | `escape` | Cancel / abort |
"""


def _pi_rows(user_bindings):
    with tempfile.TemporaryDirectory() as tmp:
        docs = os.path.join(tmp, "keybindings.md")
        cfg = os.path.join(tmp, "keybindings.json")
        with open(docs, "w", encoding="utf-8") as handle:
            handle.write(DOCS)
        with open(cfg, "w", encoding="utf-8") as handle:
            handle.write(user_bindings)
        old = (km.PI_DOCS_PATH, km.PI_KEYBINDINGS_PATH)
        km.PI_DOCS_PATH, km.PI_KEYBINDINGS_PATH = docs, cfg
        try:
            return km.parse_pi_keybinds()
        finally:
            km.PI_DOCS_PATH, km.PI_KEYBINDINGS_PATH = old


def _by_action(rows):
    out = {}
    for row in rows:
        out.setdefault(row["pi_action"], []).append(row)
    return out


class PiLayerRows(unittest.TestCase):
    def test_only_the_transcript_section_is_mirrored(self):
        actions = _by_action(_pi_rows("{}"))
        self.assertNotIn("editor.newline", actions)
        self.assertNotIn("app.interrupt", actions)
        self.assertIn("tui.altScreen.pageUp", actions)

    def test_windows_only_alternates_are_dropped(self):
        actions = _by_action(_pi_rows("{}"))
        self.assertEqual([r["key"] for r in actions["tui.altScreen.search"]],
                         ["CTRL + SHIFT + F"])
        self.assertEqual([r["key"] for r in actions["tui.altScreen.previousPrompt"]],
                         ["CTRL + SHIFT + UP"])

    def test_user_binding_replaces_the_default(self):
        actions = _by_action(_pi_rows('{"tui.altScreen.lineUp": "alt+j"}'))
        rows = actions["tui.altScreen.lineUp"]
        self.assertEqual([r["key"] for r in rows], ["ALT + J"])
        self.assertEqual(rows[0]["file"], "pi/keybindings.json")
        self.assertEqual(rows[0]["description_zh"], "对话上滑一行")

    def test_empty_list_shows_the_action_as_disabled(self):
        actions = _by_action(_pi_rows('{"tui.altScreen.pageUp": []}'))
        row = actions["tui.altScreen.pageUp"][0]
        self.assertEqual(row["status"], "disabled")
        self.assertIn("已关闭", row["description_zh"])

    def test_binding_without_a_doc_row_still_shows_up(self):
        actions = _by_action(_pi_rows('{"app.session.new": "ctrl+shift+n"}'))
        self.assertEqual([r["key"] for r in actions["app.session.new"]], ["CTRL + SHIFT + N"])

    def test_pi_rows_keep_their_own_bucket(self):
        row = _by_action(_pi_rows('{"tui.altScreen.lineUp": "alt+j"}'))["tui.altScreen.lineUp"][0]
        self.assertEqual(km.classify_binding(row), km.CATEGORY_PI)


if __name__ == "__main__":
    unittest.main()
