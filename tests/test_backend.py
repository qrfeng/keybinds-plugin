#!/usr/bin/env python3
import unittest
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))
# Importing the backend writes backend/__pycache__/*.pyc, and the running shell
# watches this whole plugin folder: a new .pyc reads as "the plugin changed"
# and reloads the desktop bar. Tests are not a plugin change, so keep the
# compiler from leaving anything behind.
sys.dont_write_bytecode = True
import keybinds_manager


class TestKeybindsBackend(unittest.TestCase):

    def test_normalize_key_chord(self):
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + A"), "SUPER + A")
        self.assertEqual(keybinds_manager.normalize_key_chord("CTRL + ALT + DELETE"), "CTRL + ALT + DELETE")
        self.assertEqual(keybinds_manager.normalize_key_chord("ctrl+a"), "CTRL + A")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER+SHIFT+RETURN"), "SUPER + SHIFT + RETURN")
        self.assertEqual(keybinds_manager.normalize_key_chord("SHIFT+SUPER+ctrl+w"), "SUPER + SHIFT + CTRL + W")
        self.assertEqual(keybinds_manager.normalize_key_chord("XF86AudioMute"), "XF86AudioMute")

    def test_parse_default_bindings(self):
        binds = keybinds_manager.parse_default_bindings()
        self.assertGreater(len(binds), 20)
        keys = [b["key"] for b in binds]
        self.assertIn("SUPER + RETURN", keys)
        self.assertIn("SUPER + SPACE", keys)

    def test_skip_comments_in_user_bindings(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".lua") as tmp:
            tmp_path = tmp.name

        try:
            old_path = keybinds_manager.USER_BINDINGS_PATH
            keybinds_manager.USER_BINDINGS_PATH = tmp_path

            # Write commented out lines
            with open(tmp_path, "w") as f:
                f.write('-- hl.unbind("SUPER + SHIFT + B")\n-- o.bind("SUPER + X", "Test", "test-cmd")\n')

            parsed = keybinds_manager.parse_user_bindings()
            self.assertNotIn("SUPER + SHIFT + B", parsed["unbinds"])
            self.assertEqual(len(parsed["binds"]), 0)

        finally:
            keybinds_manager.USER_BINDINGS_PATH = old_path
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_write_and_parse_user_bindings(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".lua") as tmp:
            tmp_path = tmp.name

        try:
            old_path = keybinds_manager.USER_BINDINGS_PATH
            keybinds_manager.USER_BINDINGS_PATH = tmp_path

            # Initial state
            with open(tmp_path, "w") as f:
                f.write('-- Header\n-- [[ OMARCHY_MOUSE_BINDINGS_START ]]\no.bind("mouse:275", "Prev", "prev_ws")\n-- [[ OMARCHY_MOUSE_BINDINGS_END ]]\n')

            # Add a binding
            keybinds_manager.write_user_bindings(
                lines_to_add=[{"key": "SUPER + SHIFT + Z", "description": "Custom Tool", "command": "my-tool"}],
                unbinds_to_add=["SUPER + SHIFT + Z"]
            )

            parsed = keybinds_manager.parse_user_bindings()
            self.assertIn("SUPER + SHIFT + Z", parsed["unbinds"])
            self.assertTrue(any(b["key"] == "SUPER + SHIFT + Z" and b["description"] == "Custom Tool" for b in parsed["binds"]))

            # Verify mouse bindings preserved
            with open(tmp_path, "r") as f:
                content = f.read()
            self.assertIn("OMARCHY_MOUSE_BINDINGS_START", content)
            self.assertIn("mouse:275", content)

            # Remove the binding
            keybinds_manager.write_user_bindings(keys_to_remove=["SUPER + SHIFT + Z"])
            parsed_after = keybinds_manager.parse_user_bindings()
            self.assertNotIn("SUPER + SHIFT + Z", parsed_after["unbinds"])
            self.assertFalse(any(b["key"] == "SUPER + SHIFT + Z" for b in parsed_after["binds"]))

        finally:
            keybinds_manager.USER_BINDINGS_PATH = old_path
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(tmp_path + ".bak"):
                os.remove(tmp_path + ".bak")

    def test_parse_bind_line_roundtrip(self):
        """Verify actions with nested parens/braces survive parsing."""
        cases = [
            ('o.bind("SUPER + LEFT", "Focus Left", hl.dsp.focus.left())',
             "SUPER + LEFT", "Focus Left", "hl.dsp.focus.left()"),
            ('o.bind("SUPER + SHIFT + LEFT", "Move Left", hl.dsp.window.move({ direction = "left" }))',
             "SUPER + SHIFT + LEFT", "Move Left", 'hl.dsp.window.move({ direction = "left" })'),
            ('o.bind("SUPER + T", "Toggle Float", hl.dsp.window.float({ action = "toggle" }))',
             "SUPER + T", "Toggle Float", 'hl.dsp.window.float({ action = "toggle" })'),
            ('o.bind("SUPER + J", "Toggle Split", hl.dsp.layout("togglesplit"))',
             "SUPER + J", "Toggle Split", 'hl.dsp.layout("togglesplit")'),
            ('o.bind("SUPER + TAB", "Next WS", hl.dsp.focus({ workspace = "e+1" }))',
             "SUPER + TAB", "Next WS", 'hl.dsp.focus({ workspace = "e+1" })'),
            ('o.bind("SUPER + S", "Scratch", hl.dsp.special_workspace.focus({ name = "scratchpad" }))',
             "SUPER + S", "Scratch", 'hl.dsp.special_workspace.focus({ name = "scratchpad" })'),
            ('o.bind_toggle("SUPER + F5", "Toggle Bar", hl.dsp.bar.toggle())',
             "SUPER + F5", "Toggle Bar", "hl.dsp.bar.toggle()"),
            ('o.bind("SUPER + K", "Menu", "exec")',
             "SUPER + K", "Menu", '"exec"'),
        ]
        for line, exp_key, exp_desc, exp_action in cases:
            result = keybinds_manager._parse_bind_line(line)
            self.assertIsNotNone(result, f"Failed to parse: {line}")
            key, desc, action, opts = result
            self.assertEqual(key, exp_key, f"Key mismatch for: {line}")
            self.assertEqual(desc, exp_desc, f"Desc mismatch for: {line}")
            self.assertEqual(action, exp_action, f"Action mismatch for: {line}")

    def test_write_and_reparse_preserves_parens(self):
        """Write catalog dispatchers to file, reparse, verify actions intact."""
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".lua") as tmp:
            tmp_path = tmp.name

        try:
            old_path = keybinds_manager.USER_BINDINGS_PATH
            keybinds_manager.USER_BINDINGS_PATH = tmp_path

            with open(tmp_path, "w") as f:
                f.write('')

            dispatchers = [
                ("SUPER + W", "Close", "hl.dsp.window.close()"),
                ("SUPER + F", "Fullscreen", "hl.dsp.window.fullscreen()"),
                ("SUPER + ALT + F", "Full Width", "hl.dsp.window.fullwidth()"),
                ("SUPER + T", "Float", 'hl.dsp.window.float({ action = "toggle" })'),
                ("SUPER + J", "Split", 'hl.dsp.layout("togglesplit")'),
                ("SUPER + LEFT", "Focus L", "hl.dsp.focus.left()"),
                ("SUPER + SHIFT + LEFT", "Move L", 'hl.dsp.window.move({ direction = "left" })'),
                ("SUPER + TAB", "Next WS", 'hl.dsp.focus({ workspace = "e+1" })'),
                ("SUPER + S", "Scratch", 'hl.dsp.special_workspace.focus({ name = "scratchpad" })'),
                ("SUPER + ALT + S", "Move to Scratch", 'hl.dsp.special_workspace.move_window({ name = "scratchpad" })'),
            ]

            binds = [{"key": k, "description": d, "command": a, "action": ""} for k, d, a in dispatchers]
            keybinds_manager.write_user_bindings(
                lines_to_add=binds,
                unbinds_to_add=[k for k, _, _ in dispatchers]
            )

            parsed = keybinds_manager.parse_user_bindings()
            self.assertEqual(len(parsed["binds"]), len(dispatchers))

            for i, (k, d, a) in enumerate(dispatchers):
                self.assertEqual(parsed["binds"][i]["action"], a,
                    f"Action mismatch for {k}: got {parsed['binds'][i]['action']!r}")

        finally:
            keybinds_manager.USER_BINDINGS_PATH = old_path
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(tmp_path + ".bak"):
                os.remove(tmp_path + ".bak")

    def test_swap_two_keybindings_preserves_first(self):
        """Swapping two bindings' keys must keep the first binding's freshly written line.
        Regression for issue #11 -- the second edit's old_key equals the key the first
        edit just bound to, and key-only removal was dropping it as collateral."""
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".lua") as tmp:
            tmp_path = tmp.name

        orig_defaults = keybinds_manager.parse_default_bindings
        orig_reload = keybinds_manager.reload_hyprland
        orig_path = keybinds_manager.USER_BINDINGS_PATH
        try:
            keybinds_manager.USER_BINDINGS_PATH = tmp_path
            keybinds_manager.parse_default_bindings = lambda: [
                {"key": "SUPER + F", "description": "Full screen",
                 "action": 'hl.dsp.window.fullscreen({ mode = "fullscreen" })'},
                {"key": "SUPER + ALT + F", "description": "Full width",
                 "action": 'hl.dsp.window.fullscreen({ mode = "maximized" })'},
            ]
            keybinds_manager.reload_hyprland = lambda: {"success": True, "reload_output": "", "config_errors": ""}

            with open(tmp_path, "w") as f:
                f.write("")

            # Step 2: move "Full screen" SUPER + F -> SUPER + ALT + F
            keybinds_manager.set_keybinding(
                key="SUPER + ALT + F", description="Full screen", command="",
                action='hl.dsp.window.fullscreen({ mode = "fullscreen" })', old_key="SUPER + F")
            # Step 3: move "Full width" SUPER + ALT + F -> SUPER + F (the swap)
            keybinds_manager.set_keybinding(
                key="SUPER + F", description="Full width", command="",
                action='hl.dsp.window.fullscreen({ mode = "maximized" })', old_key="SUPER + ALT + F")

            with open(tmp_path) as f:
                content = f.read()

            self.assertIn('o.bind("SUPER + ALT + F", "Full screen"', content)
            self.assertIn('mode = "fullscreen"', content)
            self.assertIn('o.bind("SUPER + F", "Full width"', content)
            self.assertIn('mode = "maximized"', content)
        finally:
            keybinds_manager.USER_BINDINGS_PATH = orig_path
            keybinds_manager.parse_default_bindings = orig_defaults
            keybinds_manager.reload_hyprland = orig_reload
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(tmp_path + ".bak"):
                os.remove(tmp_path + ".bak")

    def test_in_place_action_edit_removes_old_line(self):
        """Editing a binding's action in place (same key, same description, new
        action) must replace the old o.bind line, not leave a stale one behind.
        Guards the preserve_entry identity match against matching on the NEW
        action, which would wrongly keep the old line."""
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".lua") as tmp:
            tmp_path = tmp.name

        orig_defaults = keybinds_manager.parse_default_bindings
        orig_reload = keybinds_manager.reload_hyprland
        orig_path = keybinds_manager.USER_BINDINGS_PATH
        try:
            keybinds_manager.USER_BINDINGS_PATH = tmp_path
            keybinds_manager.parse_default_bindings = lambda: [
                {"key": "SUPER + F", "description": "Full screen",
                 "action": 'hl.dsp.window.fullscreen({ mode = "fullscreen" })'},
            ]
            keybinds_manager.reload_hyprland = lambda: {"success": True, "reload_output": "", "config_errors": ""}

            with open(tmp_path, "w") as f:
                f.write("")

            # First write with action A
            keybinds_manager.set_keybinding(
                key="SUPER + F", description="Full screen", command="",
                action='hl.dsp.window.fullscreen({ mode = "fullscreen" })', old_key="SUPER + F")
            # In-place edit: same key + description, new action
            keybinds_manager.set_keybinding(
                key="SUPER + F", description="Full screen", command="",
                action='hl.dsp.window.fullscreen({ mode = "maximized" })', old_key="SUPER + F")

            with open(tmp_path) as f:
                content = f.read()

            # Old action must be gone; exactly one Full screen bind, with the new action
            self.assertNotIn('mode = "fullscreen"', content)
            self.assertIn('o.bind("SUPER + F", "Full screen"', content)
            self.assertIn('mode = "maximized"', content)
            fullscreen_binds = [l for l in content.splitlines()
                                if l.strip().startswith('o.bind("SUPER + F"')]
            self.assertEqual(len(fullscreen_binds), 1)
        finally:
            keybinds_manager.USER_BINDINGS_PATH = orig_path
            keybinds_manager.parse_default_bindings = orig_defaults
            keybinds_manager.reload_hyprland = orig_reload
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(tmp_path + ".bak"):
                os.remove(tmp_path + ".bak")

    def test_get_installed_apps(self):
        apps = keybinds_manager.get_installed_apps()
        self.assertIsInstance(apps, list)
        if apps:
            first = apps[0]
            self.assertIn("name", first)
            self.assertIn("exec", first)
            self.assertIn("icon", first)
            self.assertIn("terminal", first)
            # Verify alphabetical ordering
            names = [a["name"].lower() for a in apps]
            self.assertEqual(names, sorted(names))

    def test_expanded_catalog(self):
        catalog_ids = {p["id"] for p in keybinds_manager.PRESET_CATALOG}
        self.assertIn("ws.switch_1", catalog_ids)
        self.assertIn("ws.switch_10", catalog_ids)
        self.assertIn("ws.move_1", catalog_ids)
        self.assertIn("win.swap_left", catalog_ids)
        self.assertIn("media.play_pause", catalog_ids)
        self.assertIn("media.brightness_up", catalog_ids)

    def test_normalize_key_chord_punctuation(self):
        """Verify commas, pluses, and punctuation keys are preserved."""
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + ,"), "SUPER + comma")
        self.assertEqual(keybinds_manager.normalize_key_chord("CTRL + +"), "CTRL + plus")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + comma"), "SUPER + comma")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + COMMA"), "SUPER + comma")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + ."), "SUPER + period")

    def test_sanitize_lua_str_max_len(self):
        """Verify sanitize_lua_str respects max_len parameter."""
        long_cmd = "echo " + ("a" * 500)
        default_sanitized = keybinds_manager.sanitize_lua_str(long_cmd)
        self.assertEqual(len(default_sanitized), keybinds_manager.MAX_TEXT_LEN)
        cmd_sanitized = keybinds_manager.sanitize_lua_str(long_cmd, max_len=keybinds_manager.MAX_CMD_LEN)
        self.assertEqual(len(cmd_sanitized), len(long_cmd))

    def test_preset_catalog_valid_dispatchers(self):
        """Verify no non-existent hierarchical function paths exist in catalog."""
        invalid_patterns = ["hl.dsp.focus.left", "hl.dsp.focus.right", "hl.dsp.focus.up", "hl.dsp.focus.down",
                            "hl.dsp.window.fullwidth", "hl.dsp.special_workspace"]
        for preset in keybinds_manager.PRESET_CATALOG:
            dsp = preset.get("dispatcher", "")
            for pat in invalid_patterns:
                self.assertNotIn(pat, dsp, f"Invalid dispatcher pattern '{pat}' in preset {preset['id']}")

    def test_new_presets_included(self):
        catalog_ids = {p["id"] for p in keybinds_manager.PRESET_CATALOG}
        self.assertIn("mon.focus_next", catalog_ids)
        self.assertIn("mon.move_ws_left", catalog_ids)
        self.assertIn("win.pin", catalog_ids)
        self.assertIn("ws.previous_history", catalog_ids)

    def test_decode_code_key(self):
        """`code:N` is an XKB keycode (evdev + 8) and must render as the key."""
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + code:10"), "SUPER + 1")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + code:19"), "SUPER + 0")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + code:20"), "SUPER + minus")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + SHIFT + code:21"), "SUPER + SHIFT + equal")
        # F13-F24 = XKB 191-202. Omarchy binds its root menu to code:201, so this is
        # the difference between a chord you can press and a number you cannot.
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + SHIFT + code:201"), "SUPER + SHIFT + F23")
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + code:191"), "SUPER + F13")
        # unknown codes stay as written rather than inventing a key
        self.assertEqual(keybinds_manager.normalize_key_chord("SUPER + code:250"), "SUPER + code:250")

    def test_expand_loop_binds(self):
        """Workspace bindings a Lua for-loop writes must still be reported."""
        lua = '\n'.join([
            'for workspace = 1, 10 do',
            '  local key = "code:" .. tostring(workspace + 9)',
            '  o.bind("SUPER + " .. key, "Switch to workspace " .. workspace, hl.dsp.focus({ workspace = tostring(workspace) }))',
            'end',
        ])
        binds = keybinds_manager.expand_loop_binds(lua, "tiling.lua")
        self.assertEqual(len(binds), 10)
        self.assertEqual(binds[0]["raw_key"], "SUPER + code:10")
        self.assertEqual(binds[0]["description"], "Switch to workspace 1")
        self.assertEqual(binds[9]["description"], "Switch to workspace 10")
        self.assertEqual(keybinds_manager.normalize_key_chord(binds[9]["raw_key"]), "SUPER + 0")

    def test_parse_default_bindings_includes_loop_generated(self):
        keys = {b["key"] for b in keybinds_manager.parse_default_bindings()}
        self.assertIn("SUPER + 1", keys)
        self.assertIn("SUPER + 0", keys)

    def test_classify_binding(self):
        cases = [
            ({"source": "island", "description": "Anything"}, keybinds_manager.CATEGORY_ISLAND),
            ({"description": "Terminal", "action": '{ omarchy = "terminal" }'}, keybinds_manager.CATEGORY_APP),
            ({"description": "System monitor (btop)", "action": '{ tui = "btop" }'}, keybinds_manager.CATEGORY_APP),
            ({"description": "Switch to workspace 4", "action": "hl.dsp.focus({ workspace = 4 })"}, keybinds_manager.CATEGORY_WORKSPACE),
            ({"description": "Toggle scratchpad", "action": 'hl.dsp.workspace.toggle_special("scratchpad")'}, keybinds_manager.CATEGORY_WORKSPACE),
            ({"description": "Swap window up", "action": "hl.dsp.window.swap({ direction = \"u\" })"}, keybinds_manager.CATEGORY_WINDOW),
            ({"description": "Screenshot", "action": '"omarchy-capture-screenshot"'}, keybinds_manager.CATEGORY_CAPTURE),
            ({"description": "Universal paste", "action": 'universal_clipboard_shortcut("CTRL", "V")'}, keybinds_manager.CATEGORY_CLIPBOARD),
            ({"description": "Dismiss last notification", "action": '"omarchy-shell notifications dismissOne"'}, keybinds_manager.CATEGORY_NOTIFICATION),
            ({"description": "Brightness up", "action": '"omarchy-brightness-display +5%"'}, keybinds_manager.CATEGORY_DISPLAY),
            ({"description": "Volume up", "action": '"omarchy-audio-output-volume raise"'}, keybinds_manager.CATEGORY_MEDIA),
            ({"description": "My own thing", "action": '"some-command"', "source": "user"}, keybinds_manager.CATEGORY_CUSTOM),
        ]
        for item, expected in cases:
            self.assertEqual(keybinds_manager.classify_binding(item), expected, f"wrong category for {item['description']}")

    def test_localize_description(self):
        zh = keybinds_manager.localize_description
        self.assertEqual(zh("Switch to workspace 7"), "\u5207\u5230\u5de5\u4f5c\u533a 7")
        self.assertEqual(zh("Move Window to Workspace 3 (Silent)"), "\u79fb\u5230\u5de5\u4f5c\u533a 3\uff08\u4e0d\u8ddf\u968f\uff09")
        self.assertEqual(zh("Bar panel 4"), "\u6253\u5f00\u72b6\u6001\u680f\u7b2c 4 \u4e2a\u9762\u677f")
        # catalog preset names are Title Case versions of the same phrases
        self.assertEqual(zh("Close Window"), zh("Close window"))
        self.assertEqual(zh(""), "")
        self.assertEqual(zh("Something Nobody Defined"), "")

    def test_omarchy_defaults_are_all_translated(self):
        """Omarchy's own descriptions are a fixed set, so none may fall through."""
        for b in keybinds_manager.parse_default_bindings():
            self.assertTrue(
                keybinds_manager.localize_description(b["description"]),
                f"missing Chinese label for default binding {b['description']!r}",
            )

    def test_catalog_presets_are_all_translated(self):
        for preset in keybinds_manager.PRESET_CATALOG:
            self.assertTrue(
                keybinds_manager.localize_description(preset["name"]),
                f"missing Chinese label for preset {preset['id']} ({preset['name']!r})",
            )

    def test_model_exposes_chinese_labels(self):
        model = keybinds_manager.build_complete_model()
        self.assertTrue(all("description_zh" in b for b in model["active"]))
        self.assertTrue(all("name_zh" in p for p in model["catalog"]))

    def test_category_counts_cover_every_row(self):
        model = keybinds_manager.build_complete_model()
        self.assertEqual(sum(model["category_counts"].values()), len(model["active"]))
        self.assertTrue(all(b.get("category") for b in model["active"]))
        # rows are grouped: category order never goes backwards
        order = model["category_order"]
        ranks = [order.index(b["category"]) if b["category"] in order else len(order) for b in model["active"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_catalog_presets_use_current_categories(self):
        model = keybinds_manager.build_complete_model()
        valid = set(model["category_order"])
        for preset in model["catalog"]:
            self.assertIn(preset["category"], valid, f"preset {preset['id']} kept a stale category")

    def test_overlay_binding_replacing_a_moved_default_is_not_dropped(self):
        """Two overlay bindings may claim the same default (btop moved off its
        default key, then the island took that key); both must stay visible."""
        model = keybinds_manager.build_complete_model()
        rows = [b for b in model["active"] if b.get("status") in ("modified", "custom")]
        keys = {b["key"] for b in rows}
        user = keybinds_manager.parse_user_bindings()
        for ub in user["binds"]:
            self.assertIn(ub["key"], keys, f"{ub['description']} is missing from the panel")




class TestTerminalLayer(unittest.TestCase):
    """The read-only ghostty/herdr layers (see the terminal-layer section in the backend).

    These exist because a herdr binding can be perfectly valid and still do
    nothing: ghostty consumes the key first. That bug hid for months as "alt+3
    just doesn't work", so the parsers and the shadow pass are covered here.
    """

    def test_canonical_chord_folds_all_three_layers(self):
        fold = keybinds_manager.canonical_chord
        self.assertEqual(fold("SUPER + MINUS"), fold("super+-"))
        self.assertEqual(fold("CTRL + ALT + LEFT"), fold("ctrl+alt+arrow_left"))
        self.assertEqual(fold("ALT + 3"), fold("alt+digit_3"))
        self.assertEqual(fold("CTRL + SHIFT + COMMA"), fold("ctrl+shift+,"))
        self.assertEqual(fold("performable:alt+1"), fold("alt+1"))
        self.assertEqual(fold("alt+shift+enter"), "ALT + SHIFT + RETURN")
        self.assertEqual(fold("ctrl++"), "CTRL + PLUS")
        self.assertEqual(fold("ctrl+="), "CTRL + EQUAL")
        self.assertEqual(fold("prefix+z"), "Z")  # callers strip `prefix+` first
        self.assertEqual(fold("copy"), "")  # ghostty pseudo-trigger, not a chord
        self.assertEqual(fold(""), "")

    def test_ghostty_keybind_pairs(self):
        pairs = list(keybinds_manager._keybind_pairs(
            "# comment\n"
            "keybind = ctrl+,=open_config\n"
            "keybind = ctrl+==increase_font_size:1\n"
            "keybind = alt+3=unbind\n"
            "keybind = ctrl+a=new_window\n"
            "keybind = chain=goto_split:left\n"
            "keybind = clear\n"
            "font-size = 9\n"
        ))
        self.assertIn(("ctrl+,", "open_config"), pairs)
        self.assertIn(("ctrl+=", "increase_font_size:1"), pairs)
        self.assertIn(("alt+3", "unbind"), pairs)
        self.assertEqual([p for p in pairs if p[0] == "ctrl+a"], [("ctrl+a", "new_window")])
        self.assertFalse(any("chain" in t for t, _ in pairs))

    def test_ghostty_unbound_row_is_read_only_and_names_its_old_action(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".conf") as tmp:
            path = tmp.name
        try:
            with open(path, "w") as f:
                f.write("keybind = alt+3=unbind\n")
            old_cfg, old_cli = keybinds_manager.GHOSTTY_CONFIG_PATH, keybinds_manager._run_cli
            keybinds_manager.GHOSTTY_CONFIG_PATH = path
            keybinds_manager._run_cli = lambda argv, timeout=8: (
                "keybind = alt+3=goto_tab:3\n" if "--default" in argv else ""
            )
            rows = keybinds_manager.parse_ghostty_keybinds()
        finally:
            keybinds_manager.GHOSTTY_CONFIG_PATH = old_cfg
            keybinds_manager._run_cli = old_cli
            os.unlink(path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(row["key"], "ALT + 3")
        self.assertEqual(row["managed_by"], keybinds_manager.GHOSTTY_MANAGER_LABEL)
        self.assertEqual(row["default_action"], "goto_tab:3")
        self.assertIn("已解绑", row["description_zh"])
        self.assertIn("标签页", row["description_zh"])

    def test_herdr_rows_expand_ranges_and_tag_prefix_chords(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".toml") as tmp:
            path = tmp.name
        try:
            with open(path, "w") as f:
                f.write('[keys]\nprefix = "ctrl+space"\n'
                        'switch_tab = ["prefix+1..3", "alt+1..3"]\n'
                        'zoom = "prefix+z"\n')
            old_path = keybinds_manager.HERDR_CONFIG_PATH
            keybinds_manager.HERDR_CONFIG_PATH = path
            rows = keybinds_manager.parse_herdr_keybinds()
        finally:
            keybinds_manager.HERDR_CONFIG_PATH = old_path
            os.unlink(path)
        by_key = {r["key"]: r for r in rows}
        self.assertIn("PREFIX + 3", by_key)
        self.assertIn("ALT + 3", by_key)
        self.assertTrue(by_key["PREFIX + 3"]["herdr_prefix_chord"])
        self.assertFalse(by_key["ALT + 3"]["herdr_prefix_chord"])
        self.assertIn("第 3 个", by_key["ALT + 3"]["description_zh"])
        self.assertEqual(by_key["CTRL + SPACE"]["action"], "prefix")
        self.assertEqual(by_key["PREFIX + Z"]["managed_by"], keybinds_manager.HERDR_MANAGER_LABEL)
        self.assertEqual(by_key["PREFIX + Z"]["category"], keybinds_manager.CATEGORY_HERDR)

    def test_classify_binding_keeps_layer_rows_in_their_layer(self):
        self.assertEqual(
            keybinds_manager.classify_binding({"source": "ghostty", "category": keybinds_manager.CATEGORY_TERMINAL,
                                               "description": "ghostty: close_tab", "action": "close_tab"}),
            keybinds_manager.CATEGORY_TERMINAL)
        self.assertEqual(
            keybinds_manager.classify_binding({"source": "herdr", "category": keybinds_manager.CATEGORY_HERDR,
                                               "description": "herdr: close_tab", "action": "close_tab"}),
            keybinds_manager.CATEGORY_HERDR)

    def test_shadow_pass_names_the_layer_that_steals_the_key(self):
        hypr = {"key": "SUPER + CTRL + J", "description": "x", "action": "y", "status": "default"}
        ghost_stolen = {"key": "SUPER + CTRL + J", "raw_key": "super+ctrl+j", "source": "ghostty",
                        "layer": "terminal", "status": "default", "action": "new_split:right"}
        ghost_encoder = {"key": "ALT + SHIFT + RETURN", "raw_key": "alt+shift+enter", "source": "ghostty",
                         "layer": "terminal", "status": "default", "action": "csi:13;4u"}
        herdr_direct = {"key": "ALT + SHIFT + RETURN", "raw_key": "alt+shift+enter", "source": "herdr",
                        "layer": "herdr", "status": "default", "action": "split_vertical"}
        herdr_prefix = {"key": "PREFIX + Z", "raw_key": "prefix+z", "source": "herdr", "layer": "herdr",
                        "status": "default", "action": "zoom", "herdr_prefix_chord": True}
        rows = [hypr, ghost_stolen, ghost_encoder, herdr_direct, herdr_prefix]
        keybinds_manager.annotate_layer_shadows(rows)
        # ghostty's own row is dead because the WM owns SUPER+CTRL+J ...
        self.assertEqual(ghost_stolen["shadowed_by"], "Hyprland")
        # ... but a csi: re-encoder forwards the key, so herdr really does get it.
        self.assertNotIn("shadowed_by", ghost_encoder)
        self.assertNotIn("shadowed_by", herdr_direct)
        self.assertNotIn("shadowed_by", herdr_prefix)
        self.assertNotIn("shadowed_by", hypr)

    def test_range_expanded_chords_are_matched_against_outer_layers(self):
        """`switch_tab = ["alt+1..9"]` is nine chords, not one config line.

        Comparing chords on the config spelling ("alt+1..9") matched nothing, so
        every range binding stayed invisible to this pass - which is exactly how
        ghostty's goto_tab ate herdr's alt+1..9 unnoticed until 2026-10-13.
        """
        paths = []
        for suffix in (".toml", ".conf"):
            with tempfile.NamedTemporaryFile("w+", delete=False, suffix=suffix) as tmp:
                paths.append(tmp.name)
        herdr_cfg, ghostty_cfg = paths
        try:
            with open(herdr_cfg, "w") as f:
                f.write('[keys]\nswitch_tab = ["alt+1..3"]\n')
            old = (keybinds_manager.HERDR_CONFIG_PATH, keybinds_manager.GHOSTTY_CONFIG_PATH,
                   keybinds_manager._run_cli)
            keybinds_manager.HERDR_CONFIG_PATH = herdr_cfg
            keybinds_manager.GHOSTTY_CONFIG_PATH = ghostty_cfg
            keybinds_manager._run_cli = lambda argv, timeout=8: "keybind = alt+2=goto_tab:2\n"
            try:
                rows = keybinds_manager.parse_ghostty_keybinds()
                rows += keybinds_manager.parse_herdr_keybinds()
            finally:
                (keybinds_manager.HERDR_CONFIG_PATH, keybinds_manager.GHOSTTY_CONFIG_PATH,
                 keybinds_manager._run_cli) = old
        finally:
            for path in paths:
                os.unlink(path)
        keybinds_manager.annotate_layer_shadows(rows)
        by_key = {r["key"]: r for r in rows}
        self.assertEqual([r["source"] for r in rows if r["key"] == "ALT + 2"], ["ghostty", "herdr"])
        self.assertEqual(by_key["ALT + 2"]["shadowed_by"], "ghostty")
        self.assertNotIn("shadowed_by", by_key["ALT + 1"])
        self.assertNotIn("shadowed_by", by_key["ALT + 3"])
        # raw_key carries the chord this row means, herdr_chord the config line.
        self.assertEqual(by_key["ALT + 2"]["raw_key"], "alt+2")
        self.assertEqual(by_key["ALT + 2"]["herdr_chord"], "alt+1..3")

    def test_real_model_exposes_both_layers_without_touching_hyprland_totals(self):
        model = keybinds_manager.build_complete_model()
        for field in ("total_terminal", "total_terminal_unbound", "total_herdr", "total_shadows",
                      "ghostty_config_file", "herdr_config_file"):
            self.assertIn(field, model)
        counts = model["category_counts"]
        self.assertIn(keybinds_manager.CATEGORY_TERMINAL, counts)
        self.assertIn(keybinds_manager.CATEGORY_HERDR, counts)
        self.assertEqual(model["category_order"].index(keybinds_manager.CATEGORY_TERMINAL)
                         < model["category_order"].index(keybinds_manager.CATEGORY_HERDR), True)
        layer_rows = [b for b in model["active"] if b.get("layer")]
        self.assertGreater(len(layer_rows), 0)
        # Read-only: managed_by is the flag KeybindsPanel.qml uses to hide buttons,
        # and the panel's "Modified & Custom" section filters on these statuses.
        for row in layer_rows:
            self.assertTrue(row.get("managed_by"), row["key"])
            self.assertIn(row["status"], ("default", "disabled"), row["key"])
        # Hyprland-side conflict detection must stay blind to cross-layer shadows.
        self.assertEqual([b for b in layer_rows if b.get("is_conflict")], [])


class TestLegacyDispatchTranslation(unittest.TestCase):
    """`hyprctl dispatch <legacy name>` is dead under a Lua config, so the
    panel must never store one in bindings.lua."""

    def test_known_dispatchers_become_lua(self):
        cases = {
            "hyprctl dispatch killactive": "hl.dsp.window.close()",
            "hyprctl dispatch fullscreen": "hl.dsp.window.fullscreen()",
            "hyprctl dispatch fullscreen 2": 'hl.dsp.window.fullscreen({ mode = "maximized" })',
            "hyprctl dispatch togglefloating": 'hl.dsp.window.float({ action = "toggle" })',
            "hyprctl dispatch layoutmsg togglesplit": 'hl.dsp.layout("togglesplit")',
            "hyprctl dispatch movefocus l": 'hl.dsp.focus({ direction = "l" })',
            "hyprctl dispatch swapwindow d": 'hl.dsp.window.swap({ direction = "d" })',
            "hyprctl dispatch focusmonitor +1": 'hl.dsp.focus({ monitor = "+1" })',
            "hyprctl dispatch movewindow u": 'hl.dsp.window.move({ direction = "up" })',
            "hyprctl dispatch workspace e+1": 'hl.dsp.focus({ workspace = "e+1" })',
            "hyprctl dispatch movetoworkspace special:scratchpad": 'hl.dsp.window.move({ workspace = "special:scratchpad" })',
            "hyprctl dispatch movetoworkspacesilent 3": 'hl.dsp.window.move({ workspace = "3", follow = false })',
            "hyprctl dispatch togglespecialworkspace scratchpad": 'hl.dsp.workspace.toggle_special("scratchpad")',
            "hyprctl dispatch movecurrentworkspacetomonitor l": 'hl.dsp.workspace.move({ monitor = "l" })',
        }
        for legacy, lua in cases.items():
            self.assertEqual(keybinds_manager.legacy_dispatch_to_lua(legacy), lua, legacy)

    def test_unknown_or_unsafe_input_is_left_alone(self):
        for cmd in (
            "",
            "ghostty -e btop",
            "hyprctl dispatch",
            "hyprctl dispatch unknowndsp foo",
            'hyprctl dispatch workspace "evil"}',
            "hyprctl dispatch workspace a\nb",
        ):
            self.assertEqual(keybinds_manager.legacy_dispatch_to_lua(cmd), "", cmd)

    def test_normalize_prefers_existing_lua_action(self):
        entry = keybinds_manager.normalize_bind_entry({
            "command": "hyprctl dispatch killactive",
            "action": 'hl.dsp.window.kill()',
        })
        self.assertEqual(entry["action"], "hl.dsp.window.kill()")

    def test_written_identity_uses_lua_form(self):
        entry = {"description": "kill", "command": "hyprctl dispatch killactive", "action": ""}
        desc, action = keybinds_manager._written_bind_identity(entry)
        self.assertEqual(action, "hl.dsp.window.close()")

    def test_written_identity_leaves_plain_commands_quoted(self):
        entry = {"description": "btop", "command": "ghostty -e btop", "action": "exec"}
        _desc, action = keybinds_manager._written_bind_identity(entry)
        self.assertEqual(action, '"ghostty -e btop"')


if __name__ == "__main__":
    unittest.main()