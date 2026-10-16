"""The terminal layer must always say *who* ends up answering a forwarded key.

`keybind = <chord>=unbind` in ~/.config/ghostty/config does not disable the key,
it hands it inward. The panel's value to the user is the answer to "so what
happens when I press it now?", which is why a disabled ghostty row carries the
inner layer's action instead of a bare "已解绑".
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import keybinds_manager as km


def _ghostty_row(chord, status="default", action="new_window", raw=None):
    return {
        "source": "ghostty", "layer": "terminal", "key": chord,
        "raw_key": raw or chord.lower(), "action": action, "status": status,
        "description": "ghostty: " + action, "description_zh": "新建 ghostty 窗口",
    }


def _herdr_row(chord, zh, action="split_vertical"):
    return {
        "source": "herdr", "layer": "herdr", "key": chord, "raw_key": chord.lower(),
        "action": action, "status": "default", "description": action,
        "description_zh": zh,
    }


class ForwardedKeyAttribution(unittest.TestCase):
    def test_unbound_ghostty_chord_named_owner(self):
        g = _ghostty_row("ALT + SHIFT + RETURN", status="disabled", action="unbind")
        h = _herdr_row("ALT + SHIFT + RETURN", "纵向分屏")
        km.annotate_layer_shadows([g, h])
        self.assertEqual(g.get("handed_to"), "herdr")
        self.assertIn("纵向分屏", g["description_zh"])
        self.assertNotIn("shadowed_by", h)  # the key really does reach herdr

    def test_owner_not_claimed_when_herdr_row_is_dead(self):
        g = _ghostty_row("ALT + 1", status="disabled", action="unbind")
        h = _herdr_row("ALT + 1", "跳到第 N 个标签页", action="switch_tab")
        outer = {"source": "ghostty", "layer": "terminal", "key": "ALT + 1",
                 "raw_key": "alt+digit_1", "action": "goto_tab:1", "status": "default",
                 "description": "ghostty: goto_tab:1", "description_zh": "跳到第 N 个 ghostty 标签页"}
        km.annotate_layer_shadows([g, h, outer])
        self.assertEqual(h.get("shadowed_by"), "ghostty")
        self.assertIsNone(g.get("handed_to"))  # a dead row owns nothing

    def test_hyprland_claim_is_reported_too(self):
        g = _ghostty_row("SUPER + T", status="disabled", action="unbind")
        hypr = {"key": "SUPER + T", "raw_key": "SUPER+T", "action": "togglefloating",
                "status": "default", "description": "toggle float",
                "description_zh": "切换窗口浮动/平铺"}
        km.annotate_layer_shadows([g, hypr])
        self.assertEqual(g.get("handed_to"), "Hyprland")

    def test_unbind_without_a_built_in_default_says_so(self):
        """"原本是 ghostty 内置动作" must not be printed for a chord ghostty never bound."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write("keybind = alt+shift+enter=unbind\n")
            orig_cli, orig_path = km._run_cli, km.GHOSTTY_CONFIG_PATH
            km._run_cli = lambda argv, timeout=8: ""       # no CLI -> file only
            km.GHOSTTY_CONFIG_PATH = cfg
            try:
                rows = km.parse_ghostty_keybinds()
            finally:
                km._run_cli, km.GHOSTTY_CONFIG_PATH = orig_cli, orig_path
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "disabled")
        self.assertIn("本来不绑", rows[0]["description_zh"])
        self.assertNotIn("内置动作", rows[0]["description_zh"])


if __name__ == "__main__":
    unittest.main()
