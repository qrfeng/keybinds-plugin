#!/usr/bin/env python3
"""
Keybindings Manager Backend for Omarchy Hyprland
Provides parsing, catalog discovery, conflict detection, mouse scroll integration, and safe Lua config manipulation.
"""

import os
import sys
import re
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
import tempfile
import stat
import glob
import configparser

DEFAULT_BINDINGS_DIR = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy") + "/default/hypr/bindings"
USER_BINDINGS_PATH = os.path.expanduser("~/.config/hypr/bindings.lua")
# Written by the Island plugin (guilhermerisu.island), loaded right after the
# user's own bindings.lua, so its keys win over both defaults and user edits.
ISLAND_BINDINGS_PATH = os.path.expanduser("~/.config/hypr/island-bindings.lua")
ISLAND_BINDINGS_SCRIPT = os.path.expanduser(
    "~/.config/omarchy/plugins/guilhermerisu.island/companion/bindings.sh"
)
ISLAND_MANAGER_LABEL = "Island"
FORCE_ISLAND_ENV = "OMARCHY_KEYBINDS_FORCE_ISLAND"
CACHE_DIR = os.path.expanduser("~/.cache/omarchy")

# Standard modifier weights for consistent ordering
MODIFIER_ORDER = {"SUPER": 1, "SHIFT": 2, "CTRL": 3, "CONTROL": 3, "ALT": 4}
# Reject any chord containing characters that could break out of a Lua literal or command context.
UNSAFE_KEY_RE = re.compile(r'["\'\\()\n\r\t]')
MAX_KEY_LEN = 64
MAX_TEXT_LEN = 200
MAX_CMD_LEN = 2000
MAX_FILE_BYTES = 65_536  # 64 KiB cap on any single config file read
MAX_DIR_FILES = 256  # ceiling on enumerated default-binding files
MAX_TOTAL_READ = 2_097_152  # 2 MiB cumulative cap across the default-bindings dir

MENU_CONFIG_PATH = os.path.expanduser("~/.config/omarchy/extensions/omarchy-menu.jsonc")
MENU_ENTRY_KEY = "setup.keybindings.gui"
MENU_ENTRY_VALUE = {
    "icon": "\uf464",
    "label": "Keybindings Manager",
    "action": "omarchy-shell shell summon meviusisback.keybinds",
}
_JSONC_LINE_COMMENT_RE = re.compile(r'//.*$')
_JSONC_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)

def sanitize_lua_str(s: str, max_len: int = MAX_TEXT_LEN) -> str:
    """Escape a string for safe embedding inside a Lua double-quoted literal."""
    s = (s or "").replace("\r", " ").replace("\n", " ")
    if len(s) > max_len:
        s = s[:max_len]
    return s.replace("\\", "\\\\").replace('"', '\\"')


# Pre-compiled regex for the o.bind() header: matches the function call
# up to and including the opening of the 3rd argument.
_BIND_HEADER_RE = re.compile(
    r"o\.bind(?:_toggle)?\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*"
    r"(?:[\"']([^\"']+)[\"']|nil)\s*,\s*"
)


def _parse_bind_line(line: str):
    """Extract (raw_key, desc, action, opts) from an o.bind() or o.bind_toggle() line.

    Uses parenthesis/brace depth tracking instead of a single regex to
    correctly handle actions containing nested parentheses and curly
    braces (e.g. ``hl.dsp.window.move({ direction = "left" })``).
    """
    m = _BIND_HEADER_RE.match(line)
    if not m:
        return None

    raw_key = m.group(1).strip()
    desc = (m.group(2) or "").strip()
    pos = m.end()

    depth_paren = 0
    depth_brace = 0
    in_string = None
    escape_next = False
    action_start = pos

    while pos < len(line):
        ch = line[pos]

        if escape_next:
            escape_next = False
            pos += 1
            continue

        if ch == '\\' and in_string:
            escape_next = True
            pos += 1
            continue

        if ch == '"' or ch == "'":
            if not in_string:
                in_string = ch
            elif in_string == ch:
                in_string = None
            pos += 1
            continue

        if in_string:
            pos += 1
            continue

        if ch == '(':
            depth_paren += 1
        elif ch == ')':
            if depth_paren > 0:
                depth_paren -= 1
            else:
                break  # closes o.bind()
        elif ch == '{':
            depth_brace += 1
        elif ch == '}':
            depth_brace -= 1
        elif ch == ',' and depth_paren == 0 and depth_brace == 0:
            break  # separator before opts

        pos += 1

    action = line[action_start:pos].strip()

    opts = None
    if pos < len(line) and line[pos] == ',':
        opts_start = pos + 1
        opts_text = line[opts_start:].rstrip()
        if opts_text.endswith(')'):
            opts_text = opts_text[:-1].strip()
        if opts_text:
            opts = opts_text

    return raw_key, desc, action, opts



# ---------------------------------------------------------------------------
# Categories
#
# Omarchy's own defaults only know which *file* a binding lives in (tiling.lua,
# utilities.lua, ...), which dumps dozens of unrelated things into one bucket and
# leaves every personal override in "Custom". This machine's bindings instead
# follow a modifier convention, so categories are derived from the description +
# command, first matching rule wins:
#
#   SUPER            move windows        SUPER+SHIFT+letter  launch an app
#   SUPER+number     workspaces          SUPER+CTRL+letter   drive the system
#   SUPER+ALT+letter second entry for the same thing
#
# CATEGORY_ORDER is the order the panel shows its filter chips in; KeybindsPanel.qml
# builds the chips from the model, so adding a bucket here is enough. Keep every
# label present here or a chip with nothing under it.
# ---------------------------------------------------------------------------
CATEGORY_WORKSPACE = "\U0001f5c2 Workspace"
CATEGORY_WINDOW = "\U0001fa9f Window"
CATEGORY_APP = "\U0001f680 App"
CATEGORY_SYSTEM = "\u2699\ufe0f System"
CATEGORY_DISPLAY = "\U0001f5a5 Display"
CATEGORY_CAPTURE = "\U0001f4f8 Capture"
CATEGORY_CLIPBOARD = "\u2702\ufe0f Clipboard"
CATEGORY_MEDIA = "\U0001f3b5 Media"
CATEGORY_NOTIFICATION = "\U0001f514 Notification"
CATEGORY_VOICE = "\U0001f3a4 Voice"
CATEGORY_ISLAND = "\U0001f9ca Island"
CATEGORY_CUSTOM = "\U0001f9e9 Custom"
# Read-only layers that live *inside* the terminal window rather than in Hyprland.
# Their names stay parenthesised so a chip never reads like a Hyprland bucket.
CATEGORY_TERMINAL = "\u2328\ufe0f Terminal (ghostty)"
CATEGORY_HERDR = "\U0001f916 Terminal (herdr)"
# The AI coding agent itself is a fullscreen TUI inside herdr, and its transcript
# scrolling keys are its own config file, not Hyprland's.
CATEGORY_PI = "\U0001f4ac Terminal (pi)"

# Short labels for the same buckets. The island's keybinding view uses these for
# its group tabs (`groups` below); the panel renders them on its filter chips.
#
# The nine Omarchy buckets say what a shortcut *does*, in Chinese. The four that
# belong to one program say *who owns the key*, using that program's own name:
# "终端" / "分屏" / "AI" were three angles on the same window (he calls the whole
# herdr window "the terminal", and ghostty is only the shell around it), so any
# functional name for one of them read wrong for the other two. A program name
# cannot be mistranslated, and it answers the question the tab is actually for.
CATEGORY_ZH = {
    CATEGORY_WORKSPACE: "\u5de5\u4f5c\u533a",
    CATEGORY_WINDOW: "\u7a97\u53e3",
    CATEGORY_APP: "\u5e94\u7528",
    CATEGORY_SYSTEM: "\u7cfb\u7edf",
    CATEGORY_DISPLAY: "\u663e\u793a",
    CATEGORY_CAPTURE: "\u622a\u56fe",
    CATEGORY_CLIPBOARD: "\u526a\u8d34\u677f",
    CATEGORY_MEDIA: "\u5a92\u4f53",
    CATEGORY_NOTIFICATION: "\u901a\u77e5",
    CATEGORY_VOICE: "\u8bed\u97f3",
    CATEGORY_ISLAND: "island",
    CATEGORY_TERMINAL: "ghostty",
    CATEGORY_HERDR: "herdr",
    CATEGORY_PI: "pi",
    CATEGORY_CUSTOM: "\u81ea\u5b9a\u4e49",
}

CATEGORY_ORDER = [
    CATEGORY_WORKSPACE,
    CATEGORY_WINDOW,
    CATEGORY_APP,
    CATEGORY_SYSTEM,
    CATEGORY_DISPLAY,
    CATEGORY_CAPTURE,
    CATEGORY_CLIPBOARD,
    CATEGORY_MEDIA,
    CATEGORY_NOTIFICATION,
    CATEGORY_VOICE,
    CATEGORY_ISLAND,
    CATEGORY_TERMINAL,
    CATEGORY_HERDR,
    CATEGORY_PI,
    CATEGORY_CUSTOM,
]

# Old file-derived labels (and anything a hand-written entry still carries) map
# onto the buckets above instead of becoming a 13th bucket.
LEGACY_CATEGORY_MAP = {
    "Window Management": CATEGORY_WINDOW,
    "Workspaces": CATEGORY_WORKSPACE,
    "Menus & System": CATEGORY_SYSTEM,
    "Applications": CATEGORY_APP,
    "Media & Audio": CATEGORY_MEDIA,
    "Clipboard": CATEGORY_CLIPBOARD,
    "AI & Voice": CATEGORY_VOICE,
    "General": CATEGORY_SYSTEM,
    "Custom": CATEGORY_CUSTOM,
}

# Matched against the description as a word prefix, in order, first hit wins.
# ("play" would otherwise match "display", hence the leading word boundary.)
_CATEGORY_DESC_RULES = [
    (CATEGORY_CAPTURE, ("screenshot", "screenrecording", "capture", "ocr", "color picker", "webcam")),
    (CATEGORY_NOTIFICATION, ("notification", "silencing")),
    (CATEGORY_CLIPBOARD, ("clipboard", "universal copy", "universal paste", "universal cut", "emoji")),
    (CATEGORY_VOICE, ("dictation", "voxtype", "voice")),
    # "keybinding" keeps the manager's own chord out of a 1-row 自定义 bucket: it
    # is a system entry like Lock, not a personal experiment.
    (CATEGORY_SYSTEM, ("touchpad", "lock", "reminder", "lid", "battery", "nightlight", "keybinding")),
    (CATEGORY_WORKSPACE, ("workspace", "scratchpad")),
    (CATEGORY_DISPLAY, ("brightness", "monitor", "laptop display", "scaling", "zoom", "clamshell", "resolution", "backlight")),
    (CATEGORY_MEDIA, ("volume", "track", "mute", "play", "pause", "media", "audio", "eject")),
    (CATEGORY_WINDOW, ("window", "full screen", "full width", "floating", "pseudo", "gaps", "transparency", "split", "tiling")),
]

# Anything launching a program, judged from the command so that two bindings
# sharing a description ("Calendar" the web app vs "Calendar" the clock panel)
# land in different buckets.
_APP_COMMAND_MARKERS = (
    "webapp =", "launch =", "tui =", "launch =",
    "omarchy = \"terminal", "omarchy = \"browser", "omarchy = \"editor",
    "omarchy = \"nautilus", "omarchy = \"signal", "omarchy = \"spotify",
    "omarchy = \"1password", "omarchy-agent", "omacalc", "omarchy-launch",
    "alacritty", "ghostty", "kitty", "obsidian", "omawrite",
)

# Descriptions that name an app but whose command is a plain shell command.
_APP_DESC_WORDS = (
    "terminal", "tmux", "herdr", "calculator", "browser", "editor", "file manager",
    "docker", "obsidian", "omawrite", "agent", "music", "spotify", "signal",
    "passwords", "chatgpt", "grok", "whatsapp", "youtube", "photos", "maps", "email",
)


def _desc_has(desc: str, words) -> bool:
    return any(re.search(r"\b" + re.escape(w), desc) for w in words)


def classify_binding(item: dict) -> str:
    """Return the display category for one binding model entry.

    Runs over every entry the model produces (defaults, modified, disabled,
    user-written, island) so the buckets describe what a shortcut *does* rather
    than which Omarchy file it came from.
    """
    # Terminal-layer rows are bucketed by layer, never by what they do: a
    # ghostty "close_tab" and a herdr "close_tab" must not land in App, and pi's
    # transcript scrolling has nothing to do with System.
    if item.get("source") in ("ghostty", "herdr", "pi"):
        return item.get("category") or CATEGORY_TERMINAL
    if item.get("source") == "island":
        return CATEGORY_ISLAND

    desc = str(item.get("description") or "").lower()
    cmd = str(item.get("command") or item.get("action") or "").lower()

    # A launcher command is unambiguous, and checking it first keeps a TUI whose
    # description happens to name a screen thing ("System monitor (btop)") out of
    # the Display bucket.
    if any(marker in cmd for marker in _APP_COMMAND_MARKERS):
        return CATEGORY_APP

    for category, words in _CATEGORY_DESC_RULES:
        if _desc_has(desc, words):
            return category

    if _desc_has(desc, _APP_DESC_WORDS):
        return CATEGORY_APP

    if item.get("source") == "user":
        return CATEGORY_CUSTOM

    legacy = str(item.get("category") or "")
    if legacy in LEGACY_CATEGORY_MAP and item.get("source") == "default":
        return LEGACY_CATEGORY_MAP[legacy]

    return CATEGORY_SYSTEM


# ---------------------------------------------------------------------------
# Chinese display labels
#
# The bindings themselves keep their English descriptions: they live in
# /usr/share/omarchy (overwritten by package updates) and in
# ~/.config/hypr/island-bindings.lua (regenerated by the island's own settings),
# and the plugin matches defaults against overrides by description. So the
# Chinese text is a display-only overlay: every model row also carries
# `description_zh` and the panel renders that, falling back to the original
# whenever a binding is not in the table below.
#
# Keys are lowercase; catalog preset names are Title Case versions of the same
# phrases, so one table serves both. Unknown text is left alone on purpose -
# a missing translation must never hide what a shortcut actually is.
# ---------------------------------------------------------------------------
DESCRIPTION_ZH = {
    # --- workspaces -------------------------------------------------------
    "next workspace": "下一个工作区",
    "previous workspace": "上一个工作区",
    "former workspace": "回到上一个待过的工作区",
    "scroll active workspace forward": "滚轮切到下一个工作区",
    "scroll active workspace backward": "滚轮切到上一个工作区",
    "toggle workspace layout": "切换工作区布局",
    "toggle scratchpad": "打开/收起暂存盘",
    "move window to scratchpad": "把窗口放进暂存盘",
    "move to scratchpad": "把窗口放进暂存盘",
    "toggle special scratchpad": "打开/收起暂存盘",
    "move workspace to left monitor": "把整个工作区搬到左屏",
    "move workspace to right monitor": "把整个工作区搬到右屏",
    "move workspace to up monitor": "把整个工作区搬到上屏",
    "move workspace to down monitor": "把整个工作区搬到下屏",
    # --- windows ----------------------------------------------------------
    "focus on left window": "聚焦左侧窗口",
    "focus on right window": "聚焦右侧窗口",
    "focus on above window": "聚焦上方窗口",
    "focus on below window": "聚焦下方窗口",
    "focus on next window": "聚焦下一个窗口",
    "focus window left": "聚焦左侧窗口",
    "focus window right": "聚焦右侧窗口",
    "focus window up": "聚焦上方窗口",
    "focus window down": "聚焦下方窗口",
    "previous visited workspace": "回到上一个待过的工作区",
    "focus on previous window": "聚焦上一个窗口",
    "reveal active window on top": "把当前窗口顶到最前",
    "swap window to the left": "与左侧窗口交换",
    "swap window to the right": "与右侧窗口交换",
    "swap window up": "与上方窗口交换",
    "swap window down": "与下方窗口交换",
    "swap window left": "与左侧窗口交换",
    "swap window right": "与右侧窗口交换",
    "close window": "关闭当前窗口",
    "kill window": "关闭当前窗口",
    "close all windows": "关闭所有窗口",
    "full screen": "全屏",
    "toggle fullscreen": "全屏",
    "tiled full screen": "平铺全屏",
    "full width": "满宽（最大化）",
    "toggle full width": "满宽（最大化）",
    "toggle window floating/tiling": "浮动/平铺切换",
    "toggle window floating": "浮动/平铺切换",
    "pseudo window": "伪窗口（按内容尺寸）",
    "pop window out (float & pin)": "弹出窗口（浮动+置顶）",
    "pin active window": "弹出窗口（浮动+置顶）",
    "toggle window split": "切换分屏方向",
    "toggle split direction": "切换分屏方向",
    "toggle window transparency": "切换窗口透明度",
    "toggle window gaps": "切换窗口间隙",
    "toggle single-window square aspect": "单窗口切成正方形",
    "save window width": "记住窗口宽度",
    "restore window width": "恢复窗口宽度",
    "expand window left": "窗口变宽（+100px）",
    "shrink window left": "窗口变窄（-100px）",
    "expand window down": "窗口变高（+100px）",
    "shrink window up": "窗口变矮（-100px）",
    "expand window left a little": "窗口变宽一点（25px）",
    "shrink window left a little": "窗口变窄一点（25px）",
    "expand window down a little": "窗口变高一点（25px）",
    "shrink window up a little": "窗口变矮一点（25px）",
    "expand window left a lot": "窗口变宽很多（300px）",
    "shrink window left a lot": "窗口变窄很多（300px）",
    "expand window down a lot": "窗口变高很多（300px）",
    "shrink window up a lot": "窗口变矮很多（300px）",
    "move window": "拖动窗口（鼠标）",
    "resize window": "缩放窗口（鼠标）",
    "move window left": "移动窗口：左",
    "move window right": "移动窗口：右",
    "move window up": "移动窗口：上",
    "move window down": "移动窗口：下",
    "toggle window grouping": "开/关窗口分组",
    "move active window out of group": "把当前窗口移出分组",
    "move window to group on left": "把窗口并入左侧窗口的分组",
    "move window to group on right": "把窗口并入右侧窗口的分组",
    "move window to group on top": "把窗口并入上方窗口的分组",
    "move window to group on bottom": "把窗口并入下方窗口的分组",
    "next window in group": "分组内下一个窗口",
    "previous window in group": "分组内上一个窗口",
    "move grouped window focus left": "分组内焦点左移",
    "move grouped window focus right": "分组内焦点右移",
    # --- apps -------------------------------------------------------------
    "terminal": "终端",
    "tmux": "终端（tmux）",
    "herdr": "终端（herdr）",
    "browser": "浏览器",
    "browser (private)": "浏览器（无痕）",
    "editor": "编辑器",
    "code editor": "编辑器",
    "file manager": "文件管理器",
    "file manager (cwd)": "文件管理器（当前目录）",
    "file manager (active cwd)": "文件管理器（当前目录）",
    "obsidian": "Obsidian 笔记",
    "omawrite": "Omawrite 写作",
    "docker": "Docker 监控界面",
    "music": "音乐（Spotify）",
    "music tui": "音乐播放器",
    "music tui (cliamp)": "音乐播放器",
    "passwords": "密码库（1Password）",
    "signal": "Signal",
    "calculator": "计算器",
    "system monitor (btop)": "系统监控（btop）",
    "agent": "AI 代理入口",
    "agent usage": "Agent 用量看板",
    "apps menu": "应用启动器",
    "app launcher": "应用启动器",
    "task list": "任务清单",
    "chatgpt": "网页版 ChatGPT",
    "grok": "网页版 Grok",
    "x": "网页版 X",
    "x post": "网页版 X 发帖",
    "youtube": "网页版 YouTube",
    "google photos": "网页版 Google 相册",
    "google maps": "网页版 Google 地图",
    "google messages": "网页版 Google 信息",
    "email": "网页版 hey 邮箱",
    "new email": "网页版 hey 写新邮件",
    "calendar": "网页版 hey 日历",
    "whatsapp": "网页版 WhatsApp",
    # --- system -----------------------------------------------------------
    "lock system": "锁屏",
    "power menu": "电源菜单",
    "system / power menu": "电源菜单",
    "power": "电源面板",
    "toggle locking on idle": "开/关空闲自动锁屏",
    "toggle nightlight": "开/关夜览",
    "hardware menu": "硬件菜单",
    "toggle menu": "切换悬浮菜单",
    "omarchy menu": "Omarchy 菜单",
    "theme menu": "主题菜单",
    "themes": "主题",
    "wallpapers": "壁纸",
    "background switcher": "背景切换器",
    "toggle menu bar": "显示/隐藏顶栏",
    "toggle top bar": "显示/隐藏顶栏",
    "audio": "音频面板",
    "bluetooth": "蓝牙面板",
    "display": "显示器面板",
    "share": "分享（localsend）",
    "transcode": "媒体转码",
    "set reminder": "设置提醒",
    "clear reminders": "清空提醒",
    "show reminders": "显示提醒",
    "show time": "显示时间",
    "show battery remaining": "显示剩余电量",
    "toggle weather": "显示天气",
    "disable touchpad": "禁用触控板",
    "enable touchpad": "启用触控板",
    "toggle touchpad": "开/关触控板",
    "keybindings": "快捷键一览",
    "keybindings manager": "快捷键管理器",
    "keybindings helper": "快捷键一览",
    "tmux keybindings": "tmux 快捷键一览",
    "herdr keybindings": "herdr 快捷键一览",
    "emoji": "表情面板",
    "emojis": "表情面板",
    "activity": "系统监控（btop）",
    "network": "网络面板",
    "system menu": "系统菜单",
    "emoji picker": "表情面板",
    "clipboard": "剪贴板历史",
    "clipboard manager": "剪贴板历史",
    "screensaver": "屏保",
    # --- display ----------------------------------------------------------
    "brightness up": "亮度 +",
    "brightness down": "亮度 −",
    "brightness up precise": "亮度 +（微调 1%）",
    "brightness down precise": "亮度 −（微调 1%）",
    "brightness maximum": "亮度最亮",
    "brightness minimum": "亮度最暗",
    "raise brightness": "亮度 +",
    "lower brightness": "亮度 −",
    "keyboard brightness up": "键盘背光 +",
    "keyboard brightness down": "键盘背光 −",
    "keyboard backlight cycle": "键盘背光循环切换",
    "monitor scaling up": "屏幕缩放 +",
    "monitor scaling down": "屏幕缩放 −",
    "toggle laptop display": "开/关笔记本内屏",
    "toggle laptop display mirroring": "开/关内屏镜像",
    "omarchy-hyprland-monitor-clamshell": "合盖显示模式切换",
    "omarchy-system-lid-close": "合盖动作",
    "zoom in": "放大镜：放大",
    "reset zoom": "放大镜：复位",
    "focus on next monitor": "切到下一个显示器",
    "focus on previous monitor": "切到上一个显示器",
    "focus next monitor": "切到下一个显示器",
    # --- capture ----------------------------------------------------------
    "screenshot": "截图",
    "capture screenshot": "截图",
    "screenrecording": "录屏",
    "toggle screenrecording": "录屏",
    "capture menu": "截图/录制菜单",
    "extract text (ocr) from screenshot": "截图 OCR 取字",
    "ocr text from screen": "截图 OCR 取字",
    "color picker": "屏幕取色器",
    "make webcam overlay larger": "摄像头浮窗放大",
    "make webcam overlay smaller": "摄像头浮窗缩小",
    # --- clipboard --------------------------------------------------------
    "universal copy": "万能复制",
    "universal paste": "万能粘贴",
    "universal cut": "万能剪切",
    # --- media ------------------------------------------------------------
    "play": "播放",
    "pause": "暂停",
    "play / pause media": "播放/暂停",
    "next track": "下一曲",
    "previous track": "上一曲",
    "mute": "静音",
    "mute audio": "静音",
    "mute microphone": "麦克风静音",
    "volume up": "音量 +",
    "volume down": "音量 −",
    "volume up precise": "音量 +（微调）",
    "volume down precise": "音量 −（微调）",
    "switch audio output": "切换音频输出设备",
    "switch media source": "切换播放源",
    "eject media": "弹出光碟/介质",
    # --- notifications ----------------------------------------------------
    "dismiss last notification": "关掉最后一条通知",
    "dismiss all notifications": "关掉所有通知",
    "invoke last notification": "打开最后一条通知的内容",
    "open notification history": "打开通知历史",
    "toggle silencing notifications": "开/关通知免打扰",
    # --- voice ------------------------------------------------------------
    "start dictation (push-to-talk)": "开始听写（按住说话）",
    "stop dictation (push-to-talk)": "结束听写（按住说话）",
    "toggle dictation": "开/关听写",
}

# Numbered families generated by Lua loops (and their catalog twins) - one rule
# each instead of dozens of near-duplicate dictionary entries.
_DESCRIPTION_ZH_PATTERNS = [
    (re.compile(r"^switch to workspace (\d+)$"), "切到工作区 {}"),
    (re.compile(r"^move window to workspace (\d+) \(silent\)$"), "移到工作区 {}（不跟随）"),
    (re.compile(r"^move window to workspace (\d+)$"), "移到工作区 {}（跟随）"),
    (re.compile(r"^move window silently to workspace (\d+)$"), "移到工作区 {}（不跟随）"),
    (re.compile(r"^switch to group window (\d+)$"), "切到分组内第 {} 个窗口"),
    (re.compile(r"^bar panel (\d+)$"), "打开状态栏第 {} 个面板"),
]


def localize_description(text: str) -> str:
    """Chinese label for a binding description / preset name ('' if unknown).

    Matching is case-insensitive so the Title Case catalog preset names share
    the table with the lowercase descriptions coming from the Lua configs.
    """
    key = str(text or "").strip().lower()
    if not key:
        return ""
    hit = DESCRIPTION_ZH.get(key)
    if hit:
        return hit
    for pattern, template in _DESCRIPTION_ZH_PATTERNS:
        m = pattern.match(key)
        if m:
            return template.format(*m.groups())
    return ""


def category_sort_key(binding: dict):
    """Sort key: CATEGORY_ORDER first, then description, then key."""
    cat = binding.get("category") or ""
    try:
        idx = CATEGORY_ORDER.index(cat)
    except ValueError:
        idx = len(CATEGORY_ORDER)
    return (idx, binding.get("description", "").lower(), binding.get("key", ""))


PRESET_CATALOG = [
    # Window Management
    {
        "id": "win.close",
        "category": "Window Management",
        "name": "Close Window",
        "description": "Close currently focused window",
        "dispatcher": "hl.dsp.window.close()",
        "command": "hyprctl dispatch killactive",
        "default_key": "SUPER + W",
    },
    {
        "id": "win.close_all",
        "category": "Window Management",
        "name": "Close All Windows",
        "description": "Close all open windows on current workspace",
        "dispatcher": "exec",
        "command": "omarchy-hyprland-window-close-all",
        "default_key": "CTRL + ALT + DELETE",
    },
    {
        "id": "win.fullscreen",
        "category": "Window Management",
        "name": "Toggle Fullscreen",
        "description": "Toggle fullscreen state for focused window",
        "dispatcher": "hl.dsp.window.fullscreen()",
        "command": "hyprctl dispatch fullscreen",
        "default_key": "SUPER + F",
    },
    {
        "id": "win.fullwidth",
        "category": "Window Management",
        "name": "Toggle Full Width",
        "description": "Expand window across the full screen width",
        "dispatcher": 'hl.dsp.window.fullscreen({ mode = "maximized" })',
        "command": "hyprctl dispatch fullscreen 2",
        "default_key": "SUPER + ALT + F",
    },
    {
        "id": "win.floating",
        "category": "Window Management",
        "name": "Toggle Window Floating",
        "description": "Toggle between tiling and floating mode",
        "dispatcher": "hl.dsp.window.float({ action = \"toggle\" })",
        "command": "hyprctl dispatch togglefloating",
        "default_key": "SUPER + T",
    },
    {
        "id": "win.split",
        "category": "Window Management",
        "name": "Toggle Split Direction",
        "description": "Toggle window tiling split between horizontal/vertical",
        "dispatcher": "hl.dsp.layout(\"togglesplit\")",
        "command": "hyprctl dispatch layoutmsg togglesplit",
        "default_key": "SUPER + J",
    },
    {
        "id": "win.popout",
        "category": "Window Management",
        "name": "Pop Window Out (Float & Pin)",
        "description": "Float focused window and pin it across workspaces",
        "dispatcher": "exec",
        "command": "omarchy-hyprland-window-pop",
        "default_key": "SUPER + O",
    },
    {
        "id": "win.pin",
        "category": "Window Management",
        "name": "Pin Active Window",
        "description": "Pin focused window across all workspaces",
        "dispatcher": "hl.dsp.window.pin()",
        "command": "hyprctl dispatch pin",
        "default_key": "SUPER + ALT + P",
    },
    {
        "id": "win.transparency",
        "category": "Window Management",
        "name": "Toggle Window Transparency",
        "description": "Toggle transparency for active window",
        "dispatcher": "exec",
        "command": "omarchy-hyprland-window-transparency-toggle",
        "default_key": "SUPER + BACKSPACE",
    },
    {
        "id": "win.gaps",
        "category": "Window Management",
        "name": "Toggle Window Gaps",
        "description": "Toggle inner and outer workspace window gaps",
        "dispatcher": "exec",
        "command": "omarchy-hyprland-window-gaps-toggle",
        "default_key": "SUPER + SHIFT + BACKSPACE",
    },
    {
        "id": "win.focus_left",
        "category": "Window Management",
        "name": "Focus Window Left",
        "description": "Move focus to the window on the left",
        "dispatcher": 'hl.dsp.focus({ direction = "l" })',
        "command": "hyprctl dispatch movefocus l",
        "default_key": "SUPER + LEFT",
    },
    {
        "id": "win.focus_right",
        "category": "Window Management",
        "name": "Focus Window Right",
        "description": "Move focus to the window on the right",
        "dispatcher": 'hl.dsp.focus({ direction = "r" })',
        "command": "hyprctl dispatch movefocus r",
        "default_key": "SUPER + RIGHT",
    },
    {
        "id": "win.focus_up",
        "category": "Window Management",
        "name": "Focus Window Up",
        "description": "Move focus to the window above",
        "dispatcher": 'hl.dsp.focus({ direction = "u" })',
        "command": "hyprctl dispatch movefocus u",
        "default_key": "SUPER + UP",
    },
    {
        "id": "win.focus_down",
        "category": "Window Management",
        "name": "Focus Window Down",
        "description": "Move focus to the window below",
        "dispatcher": 'hl.dsp.focus({ direction = "d" })',
        "command": "hyprctl dispatch movefocus d",
        "default_key": "SUPER + DOWN",
    },
    {
        "id": "mon.focus_next",
        "category": "Window Management",
        "name": "Focus Next Monitor",
        "description": "Switch active focus to the next physical monitor",
        "dispatcher": 'hl.dsp.focus({ monitor = "+1" })',
        "command": "hyprctl dispatch focusmonitor +1",
        "default_key": "CTRL + ALT + TAB",
    },
    {
        "id": "win.move_left",
        "category": "Window Management",
        "name": "Move Window Left",
        "description": "Move active window to the left in tiling layout",
        "dispatcher": "hl.dsp.window.move({ direction = \"left\" })",
        "command": "hyprctl dispatch movewindow l",
        "default_key": "SUPER + ALT + LEFT",
    },
    {
        "id": "win.move_right",
        "category": "Window Management",
        "name": "Move Window Right",
        "description": "Move active window to the right in tiling layout",
        "dispatcher": "hl.dsp.window.move({ direction = \"right\" })",
        "command": "hyprctl dispatch movewindow r",
        "default_key": "SUPER + ALT + RIGHT",
    },
    {
        "id": "win.move_up",
        "category": "Window Management",
        "name": "Move Window Up",
        "description": "Move active window up in tiling layout",
        "dispatcher": "hl.dsp.window.move({ direction = \"up\" })",
        "command": "hyprctl dispatch movewindow u",
        "default_key": "SUPER + ALT + UP",
    },
    {
        "id": "win.move_down",
        "category": "Window Management",
        "name": "Move Window Down",
        "description": "Move active window down in tiling layout",
        "dispatcher": "hl.dsp.window.move({ direction = \"down\" })",
        "command": "hyprctl dispatch movewindow d",
        "default_key": "SUPER + ALT + DOWN",
    },

    # Workspaces
    {
        "id": "ws.next",
        "category": "Workspaces",
        "name": "Next Workspace",
        "description": "Switch to next workspace",
        "dispatcher": "hl.dsp.focus({ workspace = \"e+1\" })",
        "command": "hyprctl dispatch workspace e+1",
        "default_key": "SUPER + TAB",
    },
    {
        "id": "ws.prev",
        "category": "Workspaces",
        "name": "Previous Workspace",
        "description": "Switch to previous workspace",
        "dispatcher": "hl.dsp.focus({ workspace = \"e-1\" })",
        "command": "hyprctl dispatch workspace e-1",
        "default_key": "SUPER + SHIFT + TAB",
    },
    {
        "id": "ws.previous_history",
        "category": "Workspaces",
        "name": "Previous Visited Workspace",
        "description": "Toggle focus back to the previously active workspace",
        "dispatcher": 'hl.dsp.focus({ workspace = "previous" })',
        "command": "hyprctl dispatch workspace previous",
        "default_key": "SUPER + GRAVE",
    },
    {
        "id": "mon.move_ws_left",
        "category": "Workspaces",
        "name": "Move Workspace to Left Monitor",
        "description": "Shift current workspace onto the monitor to the left",
        "dispatcher": 'hl.dsp.workspace.move({ monitor = "l" })',
        "command": "hyprctl dispatch movecurrentworkspacetomonitor l",
        "default_key": "SUPER + SHIFT + ALT + LEFT",
    },
    {
        "id": "ws.scratchpad_toggle",
        "category": "Workspaces",
        "name": "Toggle Special Scratchpad",
        "description": "Toggle overlay special workspace (scratchpad)",
        "dispatcher": 'hl.dsp.workspace.toggle_special("scratchpad")',
        "command": "hyprctl dispatch togglespecialworkspace scratchpad",
        "default_key": "SUPER + S",
    },
    {
        "id": "ws.scratchpad_moveto",
        "category": "Workspaces",
        "name": "Move to Scratchpad",
        "description": "Move focused window into special scratchpad",
        "dispatcher": 'hl.dsp.window.move({ workspace = "special:scratchpad", follow = false })',
        "command": "hyprctl dispatch movetoworkspace special:scratchpad",
        "default_key": "SUPER + ALT + S",
    },

    # Menus & Omarchy Shell
    {
        "id": "menu.root",
        "category": "Menus & System",
        "name": "Omarchy Menu",
        "description": "Summon the primary Omarchy launcher menu",
        "dispatcher": "exec",
        "command": "omarchy-menu toggle",
        "default_key": "SUPER + SPACE",
    },
    {
        "id": "menu.apps",
        "category": "Menus & System",
        "name": "Apps Menu",
        "description": "Summon the applications list menu",
        "dispatcher": "exec",
        "command": "omarchy-menu toggle apps",
        "default_key": "SUPER + ALT + SPACE",
    },
    {
        "id": "menu.system",
        "category": "Menus & System",
        "name": "System / Power Menu",
        "description": "Summon the system power/logout menu",
        "dispatcher": "exec",
        "command": "omarchy-menu toggle system",
        "default_key": "SUPER + ESCAPE",
    },
    {
        "id": "menu.keybindings",
        "category": "Menus & System",
        "name": "Keybindings Helper",
        "description": "Open keybindings quick selector",
        "dispatcher": "exec",
        "command": "omarchy-menu-keybindings",
        "default_key": "SUPER + K",
    },
    {
        "id": "shell.emojis",
        "category": "Menus & System",
        "name": "Emoji Picker",
        "description": "Open full-screen emoji selector overlay",
        "dispatcher": "exec",
        "command": "omarchy-shell shell toggle omarchy.emojis",
        "default_key": "SUPER + CTRL + E",
    },
    {
        "id": "shell.clipboard",
        "category": "Menus & System",
        "name": "Clipboard Manager",
        "description": "Open clipboard history overlay",
        "dispatcher": "exec",
        "command": "omarchy-shell shell toggle omarchy.clipboard",
        "default_key": "SUPER + CTRL + V",
    },
    {
        "id": "shell.theme",
        "category": "Menus & System",
        "name": "Theme Menu",
        "description": "Summon the Omarchy theme switcher",
        "dispatcher": "exec",
        "command": "omarchy-menu toggle theme",
        "default_key": "SUPER + SHIFT + CTRL + SPACE",
    },
    {
        "id": "shell.background",
        "category": "Menus & System",
        "name": "Background Switcher",
        "description": "Summon wallpaper selector overlay",
        "dispatcher": "exec",
        "command": "omarchy-menu toggle background",
        "default_key": "SUPER + CTRL + SPACE",
    },
    {
        "id": "shell.toggle_bar",
        "category": "Menus & System",
        "name": "Toggle Menu Bar",
        "description": "Show or hide the top status bar",
        "dispatcher": "exec",
        "command": "omarchy-toggle-bar",
        "default_key": "SUPER + SHIFT + SPACE",
    },
    {
        "id": "sys.lock",
        "category": "Menus & System",
        "name": "Lock System",
        "description": "Trigger session screen lock",
        "dispatcher": "exec",
        "command": "omarchy-system-lock",
        "default_key": "SUPER + CTRL + L",
    },
    {
        "id": "sys.screensaver",
        "category": "Menus & System",
        "name": "Screensaver",
        "description": "Start the Omarchy screensaver",
        "dispatcher": "exec",
        "command": "omarchy-launch-screensaver force",
        "default_key": "SUPER + ALT + L",
    },

    # Applications
    {
        "id": "app.terminal",
        "category": "Applications",
        "name": "Terminal",
        "description": "Terminal",
        "dispatcher": "{ omarchy = \"terminal\" }",
        "command": "omarchy-launch-terminal",
        "default_key": "SUPER + RETURN",
    },
    {
        "id": "app.browser",
        "category": "Applications",
        "name": "Browser",
        "description": "Browser",
        "dispatcher": "{ omarchy = \"browser\" }",
        "command": "omarchy-launch-browser",
        "default_key": "SUPER + SHIFT + RETURN",
    },
    {
        "id": "app.file_manager",
        "category": "Applications",
        "name": "File Manager",
        "description": "File manager",
        "dispatcher": "{ omarchy = \"nautilus\" }",
        "command": "omarchy-launch-nautilus",
        "default_key": "SUPER + SHIFT + F",
    },
    {
        "id": "app.file_manager_cwd",
        "category": "Applications",
        "name": "File Manager (Active CWD)",
        "description": "File manager (cwd)",
        "dispatcher": "{ omarchy = \"nautilus-cwd\" }",
        "command": "omarchy-launch-nautilus-cwd",
        "default_key": "SUPER + ALT + SHIFT + F",
    },
    {
        "id": "app.editor",
        "category": "Applications",
        "name": "Code Editor",
        "description": "Editor",
        "dispatcher": "{ omarchy = \"editor\" }",
        "command": "omarchy-launch-editor",
        "default_key": "SUPER + SHIFT + N",
    },
    {
        "id": "app.docker",
        "category": "Applications",
        "name": "Docker",
        "description": "Docker",
        "dispatcher": "{ tui = \"lazydocker\" }",
        "command": "lazydocker",
        "default_key": "SUPER + SHIFT + D",
    },
    {
        "id": "app.calculator",
        "category": "Applications",
        "name": "Calculator",
        "description": "Calculator",
        "dispatcher": "exec",
        "command": "omacalc",
        "default_key": "SUPER + CTRL + Q",
    },

    # Media & Screen
    {
        "id": "media.volume_up",
        "category": "Media & Audio",
        "name": "Volume Up",
        "description": "Raise audio output volume",
        "dispatcher": "exec",
        "command": "omarchy-audio-output-volume raise",
        "default_key": "XF86AudioRaiseVolume",
    },
    {
        "id": "media.volume_down",
        "category": "Media & Audio",
        "name": "Volume Down",
        "description": "Lower audio output volume",
        "dispatcher": "exec",
        "command": "omarchy-audio-output-volume lower",
        "default_key": "XF86AudioLowerVolume",
    },
    {
        "id": "media.mute",
        "category": "Media & Audio",
        "name": "Mute Audio",
        "description": "Toggle audio output mute",
        "dispatcher": "exec",
        "command": "omarchy-audio-output-volume mute-toggle",
        "default_key": "XF86AudioMute",
    },
    {
        "id": "media.mic_mute",
        "category": "Media & Audio",
        "name": "Mute Microphone",
        "description": "Toggle audio microphone mute",
        "dispatcher": "exec",
        "command": "omarchy-audio-input-mute",
        "default_key": "XF86AudioMicMute",
    },
    {
        "id": "media.screenshot",
        "category": "Media & Audio",
        "name": "Capture Screenshot",
        "description": "Capture screen or interactive slurp region",
        "dispatcher": "exec",
        "command": "omarchy-capture-screenshot",
        "default_key": "PRINT",
    },
    {
        "id": "media.screenrecord",
        "category": "Media & Audio",
        "name": "Toggle Screenrecording",
        "description": "Start or stop screenrecording",
        "dispatcher": "exec",
        "command": "omarchy-capture-screenrecording --stop-recording || omarchy-menu toggle trigger.capture.screenrecord",
        "default_key": "ALT + PRINT",
    },
    {
        "id": "media.color_picker",
        "category": "Media & Audio",
        "name": "Color Picker",
        "description": "Pick color under cursor to clipboard",
        "dispatcher": "exec",
        "command": "pkill hyprpicker || hyprpicker -a",
        "default_key": "SUPER + PRINT",
    },
    {
        "id": "media.ocr_text",
        "category": "Media & Audio",
        "name": "OCR Text from Screen",
        "description": "Extract text from selected screen area",
        "dispatcher": "exec",
        "command": "omarchy-capture-text",
        "default_key": "SUPER + CTRL + PRINT",
    },
    # Workspaces 1-10 Switching & Window Movement
    *[
        {
            "id": f"ws.switch_{i}",
            "category": "Workspaces",
            "name": f"Switch to Workspace {i}",
            "description": f"Switch active focus to workspace {i}",
            "dispatcher": f'hl.dsp.focus({{ workspace = "{i}" }})',
            "command": f"hyprctl dispatch workspace {i}",
            "default_key": f"SUPER + {i % 10}",
        }
        for i in range(1, 11)
    ],
    *[
        {
            "id": f"ws.move_{i}",
            "category": "Workspaces",
            "name": f"Move Window to Workspace {i}",
            "description": f"Move active window to workspace {i}",
            "dispatcher": f'hl.dsp.window.move({{ workspace = "{i}" }})',
            "command": f"hyprctl dispatch movetoworkspace {i}",
            "default_key": f"SUPER + SHIFT + {i % 10}",
        }
        for i in range(1, 11)
    ],
    *[
        {
            "id": f"ws.movesilent_{i}",
            "category": "Workspaces",
            "name": f"Move Window to Workspace {i} (Silent)",
            "description": f"Move active window to workspace {i} without switching focus",
            "dispatcher": f'hl.dsp.window.move({{ workspace = "{i}", follow = false }})',
            "command": f"hyprctl dispatch movetoworkspacesilent {i}",
            "default_key": f"SUPER + SHIFT + ALT + {i % 10}",
        }
        for i in range(1, 11)
    ],
    # Window Swapping
    {
        "id": "win.swap_left",
        "category": "Window Management",
        "name": "Swap Window Left",
        "description": "Swap active window with the window to the left",
        "dispatcher": 'hl.dsp.window.swap({ direction = "l" })',
        "command": "hyprctl dispatch swapwindow l",
        "default_key": "SUPER + SHIFT + LEFT",
    },
    {
        "id": "win.swap_right",
        "category": "Window Management",
        "name": "Swap Window Right",
        "description": "Swap active window with the window to the right",
        "dispatcher": 'hl.dsp.window.swap({ direction = "r" })',
        "command": "hyprctl dispatch swapwindow r",
        "default_key": "SUPER + SHIFT + RIGHT",
    },
    {
        "id": "win.swap_up",
        "category": "Window Management",
        "name": "Swap Window Up",
        "description": "Swap active window with the window above",
        "dispatcher": 'hl.dsp.window.swap({ direction = "u" })',
        "command": "hyprctl dispatch swapwindow u",
        "default_key": "SUPER + SHIFT + UP",
    },
    {
        "id": "win.swap_down",
        "category": "Window Management",
        "name": "Swap Window Down",
        "description": "Swap active window with the window below",
        "dispatcher": 'hl.dsp.window.swap({ direction = "d" })',
        "command": "hyprctl dispatch swapwindow d",
        "default_key": "SUPER + SHIFT + DOWN",
    },
    # Media Playback Controls
    {
        "id": "media.play_pause",
        "category": "Media & Audio",
        "name": "Play / Pause Media",
        "description": "Toggle media playback",
        "dispatcher": "exec",
        "command": "omarchy-shell media playPause",
        "default_key": "XF86AudioPlay",
    },
    {
        "id": "media.next_track",
        "category": "Media & Audio",
        "name": "Next Track",
        "description": "Skip to next media track",
        "dispatcher": "exec",
        "command": "omarchy-shell media next",
        "default_key": "XF86AudioNext",
    },
    {
        "id": "media.prev_track",
        "category": "Media & Audio",
        "name": "Previous Track",
        "description": "Return to previous media track",
        "dispatcher": "exec",
        "command": "omarchy-shell media previous",
        "default_key": "XF86AudioPrev",
    },
    # Display Brightness
    {
        "id": "media.brightness_up",
        "category": "Media & Audio",
        "name": "Raise Brightness",
        "description": "Increase display brightness by 5%",
        "dispatcher": "exec",
        "command": "omarchy-brightness-display +5%",
        "default_key": "XF86MonBrightnessUp",
    },
    {
        "id": "media.brightness_down",
        "category": "Media & Audio",
        "name": "Lower Brightness",
        "description": "Decrease display brightness by 5%",
        "dispatcher": "exec",
        "command": "omarchy-brightness-display 5%-",
        "default_key": "XF86MonBrightnessDown",
    },
]

_INSTALLED_APPS_CACHE = None

def get_installed_apps():
    """Scan standard XDG application directories for launchable applications.
    Returns a sorted list of unique applications: [{'name': ..., 'exec': ..., 'icon': ..., 'comment': ..., 'terminal': bool}]
    """
    global _INSTALLED_APPS_CACHE
    if _INSTALLED_APPS_CACHE is not None:
        return _INSTALLED_APPS_CACHE

    apps = []
    seen = set()
    dirs = [
        os.path.expanduser("~/.local/share/applications"),
        "/usr/share/applications",
        "/usr/local/share/applications",
    ]

    for app_dir in dirs:
        if not os.path.isdir(app_dir):
            continue
        for desktop_file in sorted(glob.glob(os.path.join(app_dir, "*.desktop"))):
            cp = configparser.RawConfigParser()
            try:
                cp.read(desktop_file, encoding="utf-8")
                if not cp.has_section("Desktop Entry"):
                    continue
                entry = cp["Desktop Entry"]
                if entry.get("Type", "Application") != "Application":
                    continue
                if entry.get("NoDisplay", "false").lower() == "true":
                    continue
                if entry.get("Hidden", "false").lower() == "true":
                    continue

                name = entry.get("Name", "").strip()
                exec_cmd = entry.get("Exec", "").strip()
                if not name or not exec_cmd:
                    continue

                # Strip field codes like %u, %f, %F, %U, --uri=%u, and trailing --
                clean_exec = re.sub(r'--\w+=%[a-zA-Z%]', '', exec_cmd)
                clean_exec = re.sub(r'["\']?%[a-zA-Z%]["\']?', '', clean_exec)
                clean_exec = re.sub(r'\s+--\s*$', '', clean_exec.strip())
                clean_exec = " ".join(clean_exec.split())
                if not clean_exec:
                    clean_exec = exec_cmd

                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)

                apps.append({
                    "name": name,
                    "exec": clean_exec,
                    "icon": entry.get("Icon", "").strip(),
                    "comment": entry.get("Comment", "").strip(),
                    "terminal": entry.get("Terminal", "false").lower() == "true",
                })
            except Exception:
                continue

    apps.sort(key=lambda x: x["name"].lower())
    _INSTALLED_APPS_CACHE = apps
    return apps



# ``code:N`` chords. Omarchy generates the workspace 1-10 and resize families by
# looping over raw keycodes, and a panel that renders "SUPER + code:10" is
# unreadable. N here is an XKB keycode, i.e. the kernel evdev code + 8 (see
# /usr/share/xkeyboard-config-2/keycodes/evdev). Translating both sides also makes
# a literal ``hl.unbind("SUPER + code:10")`` in the user config match the
# ``SUPER + 1`` it disables.
CODE_KEY_NAMES = {}
for _i in range(9):  # 1..9
    CODE_KEY_NAMES[str(10 + _i)] = str(_i + 1)
CODE_KEY_NAMES["19"] = "0"
for _i, _ch in enumerate("QWERTYUIOP"):
    CODE_KEY_NAMES[str(24 + _i)] = _ch
for _i, _ch in enumerate("ASDFGHJKL"):
    CODE_KEY_NAMES[str(38 + _i)] = _ch
for _i, _ch in enumerate("ZXCVBNM"):
    CODE_KEY_NAMES[str(52 + _i)] = _ch
CODE_KEY_NAMES.update({
    "20": "minus", "21": "equal", "22": "BACKSPACE", "23": "TAB", "34": "bracketleft",
    "35": "bracketright", "36": "RETURN", "48": "semicolon", "49": "grave",
    "51": "backslash", "65": "SPACE", "9": "ESCAPE", "66": "CAPS_LOCK",
})
# F13-F24 are XKB 191-202 (evdev 183-194). Laptop special keys land here: Omarchy
# binds its root menu to `SUPER + SHIFT + code:201` (bindings/utilities.lua), which
# is F23 -- a chord nobody presses on purpose while it is printed as a number.
for _i in range(12):
    CODE_KEY_NAMES[str(191 + _i)] = "F%d" % (13 + _i)


def decode_code_key(main_key: str) -> str:
    """Turn ``code:10`` into the physical key it names, else return it unchanged."""
    m = re.fullmatch(r"code:(\d+)", main_key.strip(), re.IGNORECASE)
    if not m:
        return main_key
    return CODE_KEY_NAMES.get(m.group(1), main_key.strip().lower())


def normalize_key_chord(key_chord: str) -> str:
    """Normalize a key combination string to standard 'MOD1 + MOD2 + KEY' format."""
    if not key_chord:
        return ""

    chord = key_chord.strip()
    trailing_key = None
    if chord.endswith("++"):
        chord = chord[:-1]
        trailing_key = "+"
    elif chord.endswith("+") and chord != "+":
        trailing_key = "+"
        chord = chord[:-1]

    raw_parts = [p.strip() for p in chord.split("+") if p.strip()]
    if trailing_key:
        raw_parts.append(trailing_key)
    if not raw_parts:
        return ""

    mods = []
    main_key = ""

    for part in raw_parts:
        upper = part.upper()
        if upper in ("SUPER", "WIN", "LOGO", "SUPER_L", "SUPER_R", "META", "MOD4"):
            if "SUPER" not in mods:
                mods.append("SUPER")
        elif upper in ("SHIFT", "SHIFT_L", "SHIFT_R"):
            if "SHIFT" not in mods:
                mods.append("SHIFT")
        elif upper in ("CTRL", "CONTROL", "CONTROL_L", "CONTROL_R"):
            if "CTRL" not in mods:
                mods.append("CTRL")
        elif upper in ("ALT", "ALT_L", "ALT_R", "MOD1"):
            if "ALT" not in mods:
                mods.append("ALT")
        else:
            main_key = part

    mods.sort(key=lambda m: MODIFIER_ORDER.get(m, 99))

    SYMBOL_MAP = {
        ",": "comma",
        ".": "period",
        "/": "slash",
        "-": "minus",
        "=": "equal",
        "[": "bracketleft",
        "]": "bracketright",
        ";": "semicolon",
        "'": "apostrophe",
        "\\": "backslash",
        "`": "grave",
        "+": "plus",
    }

    if main_key:
        main_key = decode_code_key(main_key)
        upper_k = main_key.upper()
        lower_k = main_key.lower()
        if upper_k in ("RETURN", "ENTER", "SPACE", "ESCAPE", "TAB", "BACKSPACE", "DELETE", "PRINT"):
            main_key = upper_k
        elif upper_k in ("LEFT", "RIGHT", "UP", "DOWN"):
            main_key = upper_k
        elif upper_k.startswith("F") and upper_k[1:].isdigit():
            main_key = upper_k
        elif main_key in SYMBOL_MAP:
            main_key = SYMBOL_MAP[main_key]
        elif lower_k in SYMBOL_MAP.values():
            main_key = lower_k
        elif len(main_key) == 1:
            main_key = main_key.upper()

    if mods and main_key:
        out = f"{' + '.join(mods)} + {main_key}"
    elif mods and not main_key:
        out = " + ".join(mods)
    else:
        out = main_key
    if len(out) > MAX_KEY_LEN or UNSAFE_KEY_RE.search(out or ""):
        return ""
    return out



def get_mouse_scroll_settings():
    """Retrieve active Hyprland mouse scroll configuration (scroll_factor & natural_scroll)."""
    settings = {"natural_scroll": False, "scroll_factor": 1.0}
    try:
        res = subprocess.run(["hyprctl", "getoption", "input:scroll_factor", "-j"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            if "float" in data:
                settings["scroll_factor"] = float(data["float"])
    except Exception:
        pass

    try:
        res = subprocess.run(["hyprctl", "getoption", "input:natural_scroll", "-j"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            if "bool" in data:
                settings["natural_scroll"] = bool(data["bool"])
    except Exception:
        pass

    return settings


def category_for_file(fname: str) -> str:
    """Legacy bucket name for a default-bindings file (see classify_binding)."""
    return {
        "applications": "Applications",
        "tiling": "Window Management",
        "media": "Media & Audio",
        "utilities": "Menus & System",
        "clipboard": "Clipboard",
        "voxtype": "AI & Voice",
    }.get(fname[:-4] if fname.endswith(".lua") else fname, "General")


def parse_default_bindings():
    """Parse all default keybindings shipped with Omarchy."""
    bindings = []
    if not os.path.isdir(DEFAULT_BINDINGS_DIR):
        return bindings

    file_count = 0
    total_read = 0
    for fname in sorted(os.listdir(DEFAULT_BINDINGS_DIR)):
        if file_count >= MAX_DIR_FILES or total_read > MAX_TOTAL_READ:
            break
        file_count += 1
        if not fname.endswith(".lua"):
            continue
        filepath = os.path.join(DEFAULT_BINDINGS_DIR, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read(MAX_FILE_BYTES)
            total_read += len(content.encode("utf-8"))
        except Exception:
            continue

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith('--'):
                continue

            parsed = _parse_bind_line(stripped)
            if not parsed:
                continue

            raw_key, desc, action_raw, opts_raw = parsed

            norm_key = normalize_key_chord(raw_key)
            if stripped.startswith("o.bind_toggle"):
                clean_target = action_raw.strip('"\'')
                action_raw = f'"omarchy-toggle-{clean_target}"'
            if not desc:
                desc = action_raw.strip('"\'')

            cat = category_for_file(fname)

            is_release = opts_raw and ("on_release = true" in opts_raw or "on_release=true" in opts_raw)

            bindings.append({
                "source": "default",
                "file": fname,
                "key": norm_key,
                "raw_key": raw_key,
                "description": desc,
                "action": action_raw,
                "category": cat,
                "is_mouse": "mouse" in raw_key.lower() or "mouse" in (opts_raw or ""),
                "is_release": is_release,
            })

        # Bindings a Lua for-loop writes (workspace 1-10, group windows, bar
        # panels) are invisible to the line parser above, so materialise them
        # here; otherwise the panel is silently missing ~44 shortcuts, including
        # every SUPER + <number> workspace switch.
        try:
            looped = expand_loop_binds(content, fname)
        except Exception as e:  # never lose the whole file over one loop
            print(f"keybinds_manager: loop expansion failed for {fname}: {e}", file=sys.stderr)
            looped = []
        for entry in looped:
            norm_key = normalize_key_chord(entry["raw_key"])
            if not norm_key:
                continue
            bindings.append({
                "source": "default",
                "file": fname,
                "key": norm_key,
                "raw_key": entry["raw_key"],
                "description": entry["description"],
                "action": entry["action"],
                "category": category_for_file(fname),
                "is_mouse": "mouse" in entry["raw_key"].lower(),
                "is_release": False,
                "loop_generated": True,
            })

    return bindings


_LOOP_FOR_RE = re.compile(r"^\s*for\s+(\w+)\s*=\s*(-?\d+)\s*,\s*(-?\d+)\s+do\s*$")


def _split_lua_args(text):
    """Split a Lua argument list at top-level commas (string/brace aware)."""
    args, depth, cur, in_str, esc = [], 0, "", None, False
    for ch in text:
        if in_str:
            cur += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
            cur += ch
            continue
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _split_lua_concat(expr: str):
    """Split a Lua concatenation chain at top-level '..' (string/paren aware)."""
    parts, cur, depth, in_str, esc = [], "", 0, None, False
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_str:
            cur += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
            cur += ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0 and expr.startswith("..", i):
            parts.append(cur.strip())
            cur = ""
            i += 2
            continue
        cur += ch
        i += 1
    parts.append(cur.strip())
    return parts


def _strip_outer_parens(expr: str) -> str:
    """Drop redundant wrapping parens, e.g. ("code:" .. n) -> "code:" .. n."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth, wrapped = 0, True
        for pos, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and pos != len(expr) - 1:
                    wrapped = False
                    break
        if not wrapped:
            break
        expr = expr[1:-1].strip()
    return expr


def _substitute_outside_strings(expr: str, name: str, replacement: str) -> str:
    """Replace whole-word ``name`` outside string literals with ``replacement``.

    Local-key substitution must not touch text inside quotes, or a description
    like "Switch to key window " would get the loop value pasted into it.
    """
    word = re.compile(r"\b" + re.escape(name) + r"\b")
    out, i, in_str = [], 0, None
    while i < len(expr):
        ch = expr[i]
        if in_str:
            if ch == "\\":
                out.append(expr[i:i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
            out.append(ch)
            i += 1
            continue
        m = word.match(expr, i)
        if m:
            out.append(replacement)
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _inline_loop_var(expr: str, var: str, index: int) -> str:
    """Best-effort inlining of the loop number into a dispatch expression.

    Only used for display (the third o.bind argument), so a miss is cosmetic.
    """
    def repl(match):
        step = int(match.group(2)) if match.group(2) else 0
        if match.group(1) == "-":
            step = -step
        return str(index + step)

    expr = re.sub(
        r"tostring\(\s*" + re.escape(var) + r"\s*(?:([+-])\s*(\d+))?\s*\)",
        repl,
        expr,
    )
    return re.sub(r"\b" + re.escape(var) + r"\b", str(index), expr)


def _lua_concat_value(expr: str, var: str, index: int, _depth: int = 0):
    """Evaluate a Lua expression made of literals, concat chains and one loop var.

    Covers what Omarchy's binding loops actually write, including the nested form
    the workspace loop uses ("SUPER + " .. key with key = "code:" ..
    tostring(workspace + 9)). Anything requiring real Lua evaluation returns
    None and the caller skips that binding.
    """
    expr = _strip_outer_parens(expr)
    if not expr or _depth > 8:
        return None

    parts = _split_lua_concat(expr)
    if len(parts) > 1:
        joined = []
        for part in parts:
            value = _lua_concat_value(part, var, index, _depth + 1)
            if value is None:
                return None
            joined.append(value)
        return "".join(joined)

    if len(expr) >= 2 and expr[0] in "\"'" and expr[-1] == expr[0]:
        return expr[1:-1]

    m = re.fullmatch(r"tostring\(\s*" + re.escape(var) + r"\s*(?:([+-])\s*(\d+))?\s*\)", expr)
    if m:
        step = int(m.group(2)) if m.group(2) else 0
        if m.group(1) == "-":
            step = -step
        return str(index + step)

    m = re.fullmatch(re.escape(var) + r"\s*(?:([+-])\s*(\d+))?", expr)
    if m:
        step = int(m.group(2)) if m.group(2) else 0
        if m.group(1) == "-":
            step = -step
        return str(index + step)

    if re.fullmatch(r"-?\d+", expr):
        return expr

    if expr.startswith(("hl.", "o.", "{")):
        return _inline_loop_var(expr, var, index)
    return None


def _lua_statements(body_lines):
    """Join physical lines into logical statements (a bind may span 6 lines)."""
    statements, buf, depth, in_str, esc = [], "", 0, None, False
    for raw in body_lines:
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        buf = (buf + " " + line) if buf else line
        for ch in line:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == in_str:
                    in_str = None
                continue
            if ch in "\"'":
                in_str = ch
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
        if depth <= 0 and in_str is None:
            statements.append(buf.strip())
            buf, depth = "", 0
    if buf.strip():
        statements.append(buf.strip())
    return statements


_LOOP_LOCAL_RE = re.compile(r"^local\s+(\w+)\s*=\s*(.+?)\s*$")
_LOOP_BIND_RE = re.compile(r"^o\.bind(?:_toggle)?\((.*)\)\s*$", re.DOTALL)


def expand_loop_binds(content, fname):
    """Materialise bindings written by a Lua for-loop.

    Omarchy generates workspace 1-10, "Switch to group window" and "Bar panel"
    bindings inside loops, and a line-based parser cannot see any of them, which
    made the panel silently under-report ~44 bindings. Only loops whose arguments
    reduce to literals plus the loop variable are expanded; anything cleverer is
    skipped. Returns [{"file", "raw_key", "description", "action"}].
    """
    lines = content.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = _LOOP_FOR_RE.match(lines[i])
        if not m:
            i += 1
            continue
        var, start, stop = m.group(1), int(m.group(2)), int(m.group(3))
        indent = len(lines[i]) - len(lines[i].lstrip())
        body = []
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            cur_indent = len(lines[j]) - len(lines[j].lstrip())
            if stripped == "end" and cur_indent == indent:
                break
            body.append(lines[j])
            j += 1

        local_exprs, call_args = [], []
        for stmt in _lua_statements(body):
            lm = _LOOP_LOCAL_RE.match(stmt)
            if lm:
                local_exprs.append((lm.group(1), lm.group(2)))
                continue
            bm = _LOOP_BIND_RE.match(stmt)
            if bm:
                try:
                    call_args.append(_split_lua_args(bm.group(1)))
                except Exception:
                    continue

        for index in range(start, stop + 1):
            locals_values = {}
            for name, expr in local_exprs:
                merged = expr
                for known, value in locals_values.items():
                    if value is None:
                        continue
                    merged = _substitute_outside_strings(merged, known, '"' + value + '"')
                locals_values[name] = _lua_concat_value(merged, var, index)

            for args in call_args:
                if len(args) < 3:
                    continue
                exprs, ok = [], True
                for arg in args[:3]:
                    merged = arg
                    for name, value in locals_values.items():
                        if re.search(r"\b" + re.escape(name) + r"\b", merged):
                            if value is None:
                                ok = False
                                break
                            merged = _substitute_outside_strings(merged, name, '"' + value + '"')
                    if not ok:
                        break
                    value = _lua_concat_value(merged, var, index)
                    if value is None:
                        ok = False
                        break
                    exprs.append(value)
                if not ok or len(exprs) < 3:
                    continue
                out.append({
                    "file": fname,
                    "raw_key": exprs[0],
                    "description": exprs[1],
                    "action": exprs[2],
                })
        i = j + 1
    return out


def parse_user_bindings():
    """
    Parse user overrides from ~/.config/hypr/bindings.lua.
    CRITICAL: Skips all commented-out lines (starting with --) to avoid false unbind detection.
    """
    result = {
        "raw_content": "",
        "unbinds": set(),
        "binds": [],
        "mouse_binds": [],
    }

    if not os.path.isfile(USER_BINDINGS_PATH):
        return result

    try:
        with open(USER_BINDINGS_PATH, "r", encoding="utf-8") as f:
            content = f.read(MAX_FILE_BYTES)
            result["raw_content"] = content
    except Exception:
        return result

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue

        m_unbind = re.match(r'hl\.unbind\s*\(\s*["\']([^"\']+)["\']\s*\)', stripped)
        if m_unbind:
            raw_key = m_unbind.group(1).strip()
            result["unbinds"].add(normalize_key_chord(raw_key))
            continue

        parsed = _parse_bind_line(stripped)
        if parsed:
            raw_key, desc, action_raw, opts_raw = parsed
            norm_key = normalize_key_chord(raw_key)

            entry = {
                "source": "user",
                "key": norm_key,
                "raw_key": raw_key,
                "description": desc or action_raw.strip('"\''),
                "action": action_raw,
                "is_mouse": "mouse" in raw_key.lower() or "mouse" in (opts_raw or ""),
                "is_release": opts_raw and ("on_release = true" in opts_raw or "on_release=true" in opts_raw),
            }

            if entry["is_mouse"]:
                result["mouse_binds"].append(entry)
            else:
                result["binds"].append(entry)

    return result


_ISLAND_LINE_RE = re.compile(
    r"^(?P<off>--\s*off:\s*)?island\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]*)\"\s*,\s*\"([^\"]*)\"\s*\)\s*$"
)
# The file's helper prepends a fixed IPC prefix to every action argument:
#   o.bind(keys, description, "omarchy-shell guilhermerisu.island " .. command)
# Read it back instead of hardcoding it, so the model shows the real command.
_ISLAND_PREFIX_RE = re.compile(r'"([^"]*island\s+)"\s*\.\.\s*command')
_ISLAND_PREFIX_DEFAULT = "omarchy-shell guilhermerisu.island "


def parse_island_bindings():
    """
    Parse ~/.config/hypr/island-bindings.lua, the file the Island plugin writes.

    Every live line is ``island("KEY", "Description", "show keybinds")``, which the
    file's own helper expands to ``hl.unbind(KEY)`` + ``o.bind(...)``. Lines the
    island prefixes with ``-- off:`` belong to disabled addons: their keys are
    reported, but they are NOT active bindings.

    Read-only by design. The island regenerates this whole file from its own
    Settings -> Keybinds store, so editing it behind the island's back gets
    overwritten; mutate it through companion/bindings.sh instead (see
    ``island_set_keybinding``).
    """
    result = {"binds": [], "unbinds": set(), "off_keys": set()}
    if not os.path.isfile(ISLAND_BINDINGS_PATH):
        return result

    try:
        with open(ISLAND_BINDINGS_PATH, "r", encoding="utf-8") as f:
            content = f.read(MAX_FILE_BYTES)
    except Exception:
        return result

    prefix_found = _ISLAND_PREFIX_RE.search(content)
    prefix = prefix_found.group(1) if prefix_found else _ISLAND_PREFIX_DEFAULT

    for line in content.splitlines():
        m = _ISLAND_LINE_RE.match(line.strip())
        if not m:
            continue
        raw_key = m.group(2).strip()
        desc = m.group(3).strip()
        command = m.group(4).strip()
        norm_key = normalize_key_chord(raw_key)
        if not norm_key:
            continue
        if m.group("off"):
            result["off_keys"].add(norm_key)
            continue
        full_command = prefix + command
        result["unbinds"].add(norm_key)
        result["binds"].append({
            "source": "island",
            "file": os.path.basename(ISLAND_BINDINGS_PATH),
            "key": norm_key,
            "raw_key": raw_key,
            "description": desc or command,
            "action": '"' + full_command + '"',
            "command": full_command,
            "category": "Island",
            "is_mouse": False,
            "is_release": False,
            "managed_by": ISLAND_MANAGER_LABEL,
            "managed_file": ISLAND_BINDINGS_PATH,
            "read_only": True,
        })

    return result


def island_keybinding_actions():
    """The island's own action list (id, label, keys, command, addon), straight
    from its companion helper so addon-disabled actions and ids stay accurate."""
    actions = []
    if os.path.isfile(ISLAND_BINDINGS_SCRIPT):
        try:
            res = subprocess.run(
                ["bash", ISLAND_BINDINGS_SCRIPT, "list"],
                capture_output=True, text=True, timeout=15,
            )
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                actions.append({
                    "id": parts[0],
                    "label": parts[1],
                    "keys": parts[2],
                    "command": parts[3],
                    "addon": parts[4],
                })
        except Exception:
            actions = []
    return actions


def island_set_keybinding(action_id: str, keys: str):
    """Rebind (or clear, with an empty ``keys``) an island action through the
    island's own helper, which owns that file and rewrites it atomically."""
    if not os.path.isfile(ISLAND_BINDINGS_SCRIPT):
        return {
            "success": False,
            "error": f"Island helper not found: {ISLAND_BINDINGS_SCRIPT}",
        }
    try:
        res = subprocess.run(
            ["bash", ISLAND_BINDINGS_SCRIPT, "set", action_id, keys],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    try:
        errs = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=10)
        config_errors = errs.stdout.strip() if errs.returncode == 0 else ""
    except Exception:
        config_errors = ""

    return {
        "success": res.returncode == 0,
        "id": action_id,
        "keys": keys,
        "output": (res.stdout + res.stderr).strip(),
        "config_errors": config_errors,
    }


def _island_clash(norm_key: str, island_state=None):
    """The island binding that owns ``norm_key``, if any."""
    state = island_state if island_state is not None else parse_island_bindings()
    return next((b for b in state["binds"] if b["key"] == norm_key), None)


def _island_block(norm_key: str, clash) -> dict:
    return {
        "success": False,
        "key": norm_key,
        "managed_by": ISLAND_MANAGER_LABEL,
        "error": (
            f"'{norm_key}' is held by the island action '{clash['description']}' "
            f"({ISLAND_BINDINGS_PATH}), which Hyprland loads after "
            f"{USER_BINDINGS_PATH}, so a binding written here would never fire. "
            f"Change it in the island's Settings -> Keybinds, or run: "
            f"omarchy-keybinds island-set <id> '{norm_key}' "
            f"(ids: omarchy-keybinds island). "
            f"Set {FORCE_ISLAND_ENV}=1 to write it anyway."
        ),
    }


# ---------------------------------------------------------------------------
# Read-only view of the two layers that live *inside* the terminal window.
#
# A keystroke on this machine is offered to three owners, outermost first:
#
#   1. Hyprland  - owns the SUPER chords; anything it grabs never reaches a window.
#   2. ghostty   - its own config; a bound key is consumed here unless the entry
#                  says `=unbind`, which forwards the key inward.
#   3. herdr     - the workspace manager inside ghostty; it only ever sees what
#                  the two layers above left behind.
#
# Rows below are informational. They carry `managed_by`, which KeybindsPanel.qml
# already treats as "read-only" (it hides the edit/reset/delete buttons for such
# rows), and `shadowed_by`, which the panel renders as "DEAD - <layer> rebinds
# this key". Two rows in *different* layers sharing a chord is a shadow, not a
# Hyprland conflict, so layer rows are skipped by the conflict pass and annotated
# here instead - that tripwire is how the 2026-10-13 finding (herdr's alt+1..9 and
# ctrl+alt+arrow "second entry" bindings were being eaten by ghostty's defaults)
# becomes visible instead of silently doing nothing.
# ---------------------------------------------------------------------------
GHOSTTY_CONFIG_PATH = os.path.expanduser("~/.config/ghostty/config")
HERDR_CONFIG_PATH = os.path.expanduser("~/.config/herdr/config.toml")
GHOSTTY_MANAGER_LABEL = "ghostty config"
HERDR_MANAGER_LABEL = "herdr config"
PI_MANAGER_LABEL = "pi keybindings"
PI_KEYBINDINGS_PATH = os.path.expanduser("~/.pi/agent/keybindings.json")
_GHOSTTY_TRIGGER_PREFIXES = ("global:", "all:", "performable:", "unconsumed:", "physical:")

_CHORD_MODS = {
    "SUPER": "SUPER", "WIN": "SUPER", "LOGO": "SUPER", "META": "SUPER", "MOD4": "SUPER",
    "CMD": "SUPER", "COMMAND": "SUPER",
    "CTRL": "CTRL", "CONTROL": "CTRL", "CONTROL_L": "CTRL", "CONTROL_R": "CTRL",
    "ALT": "ALT", "ALT_L": "ALT", "ALT_R": "ALT", "MOD1": "ALT", "OPTION": "ALT",
    "SHIFT": "SHIFT", "SHIFT_L": "SHIFT", "SHIFT_R": "SHIFT",
}
_CHORD_MOD_ORDER = {"SUPER": 1, "CTRL": 2, "ALT": 3, "SHIFT": 4}
_CHORD_KEYS = {
    "ENTER": "RETURN", "ESC": "ESCAPE", "ARROW_LEFT": "LEFT", "ARROW_RIGHT": "RIGHT",
    "ARROW_UP": "UP", "ARROW_DOWN": "DOWN", "PG_UP": "PAGE_UP", "PG_DOWN": "PAGE_DOWN",
    "-": "MINUS", "=": "EQUAL", ",": "COMMA", ".": "PERIOD", "/": "SLASH", ";": "SEMICOLON",
    "'": "APOSTROPHE", "\\": "BACKSLASH", "`": "GRAVE", "+": "PLUS",
    "[": "LEFTBRACKET", "]": "RIGHTBRACKET", "BRACKETLEFT": "LEFTBRACKET",
    "BRACKETRIGHT": "RIGHTBRACKET", "COMMA": "COMMA", "PERIOD": "PERIOD",
}


def canonical_chord(chord) -> str:
    """Reduce any layer's chord spelling to one comparable form ('CTRL + ALT + LEFT').

    Hyprland writes `SUPER + MINUS`, ghostty writes `ctrl+alt+arrow_left` and
    `alt+digit_3`, herdr writes `alt+shift+enter`. Comparison across layers has
    to ignore all of that, so everything folds through here. Returns "" for
    pseudo-triggers (`copy`, `paste`) and unparseable input.
    """
    if not chord:
        return ""
    text = str(chord).strip()
    if "/" in text:  # ghostty key table: `foo/ctrl+a=...`
        text = text.rsplit("/", 1)[-1]
    changed = True
    while changed:
        changed = False
        for prefix in _GHOSTTY_TRIGGER_PREFIXES:
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
                changed = True
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return ""
    # ghostty writes an explicit plus key as `ctrl++`; the empty token survives
    # only as a doubled separator, so recognise the trailing plus as the key.
    if text.endswith("++"):
        parts = parts + ["+"]
    mods, key = [], ""
    for part in parts:
        alias = _CHORD_MODS.get(part.upper())
        if alias and part != "+":
            if alias not in mods:
                mods.append(alias)
        else:
            key = part
    if not key:
        return ""
    upper = key.upper().replace(" ", "_")
    if upper.startswith("DIGIT_") and upper[6:].isdigit():
        upper = upper[6:]
    upper = _CHORD_KEYS.get(upper, _CHORD_KEYS.get(key, upper))
    if upper in ("COPY", "PASTE") and not mods:
        return ""  # ghostty pseudo-triggers are not chords; no comparable form
    mods.sort(key=lambda m: _CHORD_MOD_ORDER.get(m, 9))
    out = " + ".join(mods + [upper])
    return "" if (len(out) > MAX_KEY_LEN or UNSAFE_KEY_RE.search(out)) else out


GHOSTTY_ACTION_ZH = {
    "copy_to_clipboard": "复制选区到剪贴板",
    "paste_from_clipboard": "从剪贴板粘贴",
    "paste_from_selection": "粘贴 X 选区",
    "increase_font_size": "放大字号",
    "decrease_font_size": "缩小字号",
    "reset_font_size": "恢复默认字号",
    "reload_config": "重载配置",
    "open_config": "打开配置文件",
    "new_window": "新建窗口",
    "new_tab": "新建标签页",
    "close_tab": "关闭标签页",
    "goto_tab": "跳到第 N 个标签页",
    "next_tab": "下一个标签页",
    "previous_tab": "上一个标签页",
    "last_tab": "跳到最后一个标签页",
    "new_split": "ghostty 自带分屏",
    "goto_split": "切换 ghostty 自带分屏",
    "resize_split": "调整 ghostty 自带分屏",
    "toggle_split_zoom": "ghostty 分屏缩放",
    "toggle_fullscreen": "切换全屏",
    "close_window": "关闭整个窗口",
    "quit": "退出终端",
    "adjust_selection": "扩展选区",
    "scroll_page_up": "向上翻页",
    "scroll_page_down": "向下翻页",
    "scroll_to_top": "滚到最顶部",
    "scroll_to_bottom": "滚到最底部",
    "jump_to_prompt": "跳到上/下一个命令提示符",
    "start_search": "终端内搜索",
    "end_search": "结束搜索",
    "select_all": "全选",
    "inspector": "打开 Inspector 调试面板",
    "toggle_command_palette": "命令面板",
    "write_screen_file": "导出屏幕内容到文件",
    "clear_screen": "清屏",
    "csi": "发送 CSI 序列",
    "esc": "发送 ESC 序列",
    "text": "发送文本序列",
    "ignore": "吞掉这个键（什么都不做）",
    "unbind": "解绑：把键让给内层程序",
    "activate_key_table": "进入 ghostty 按键表",
    "deactivate_key_table": "退出 ghostty 按键表",
}

GHOSTTY_PARAM_ZH = {
    "write_screen_file": {"copy": "（进剪贴板）", "paste": "（粘贴出来）", "open": "（开文件）"},
    # Chords this machine keeps as a raw CSI sequence on purpose. Without
    # `shift+enter=csi:13;2u` a TUI (pi, herdr, ...) cannot tell Shift+Enter from
    # Enter, so the input box cannot insert a newline - worth naming in the panel
    # instead of showing the opaque "发送 CSI 序列".
    "csi": {"13;2u": "在 TUI 输入框里换行（不提交）"},
}

HERDR_ACTION_ZH = {
    "prefix": "前缀键（进入前缀模式）",
    "help": "快捷键帮助",
    "settings": "设置面板",
    "reload_config": "重载配置",
    "detach": "脱离会话（后台继续运行）",
    "open_notification_target": "跳到通知指向的分屏格",
    "copy_mode": "复制/回滚模式",
    "edit_scrollback": "用编辑器打开回滚内容",
    "clear_pane": "清空当前分屏格",
    "split_horizontal": "分屏：上下两栏",
    "split_vertical": "分屏：左右两栏",
    "close_pane": "关闭分屏格",
    "zoom": "分屏格最大化/还原",
    "rename_pane": "重命名分屏格",
    "last_pane": "回到上一个分屏格",
    "cycle_pane_next": "下一个分屏格",
    "cycle_pane_previous": "上一个分屏格",
    "focus_pane_left": "聚焦左侧分屏格",
    "focus_pane_down": "聚焦下方分屏格",
    "focus_pane_up": "聚焦上方分屏格",
    "focus_pane_right": "聚焦右侧分屏格",
    "swap_pane_left": "与左侧分屏格交换",
    "swap_pane_down": "与下方分屏格交换",
    "swap_pane_up": "与上方分屏格交换",
    "swap_pane_right": "与右侧分屏格交换",
    "resize_mode": "进入调整大小模式",
    "resize_pane_left": "向左调整分屏格宽度",
    "resize_pane_down": "向下调整分屏格高度",
    "resize_pane_up": "向上调整分屏格高度",
    "resize_pane_right": "向右调整分屏格宽度",
    "new_tab": "新建标签页",
    "rename_tab": "重命名标签页",
    "close_tab": "关闭标签页",
    "switch_tab": "跳到第 N 个标签页",
    "previous_tab": "上一个标签页",
    "next_tab": "下一个标签页",
    "move_tab_previous": "把标签页往前挪",
    "move_tab_next": "把标签页往后挪",
    "new_workspace": "新建工作区",
    "rename_workspace": "重命名工作区",
    "close_workspace": "关闭工作区",
    "switch_workspace": "跳到第 N 个工作区",
    "previous_workspace": "上一个工作区",
    "next_workspace": "下一个工作区",
    "workspace_picker": "工作区选择器",
    "goto": "会话导航（可搜索）",
    "new_worktree": "新建 git worktree",
    "open_worktree": "打开 git worktree",
    "remove_worktree": "删除 git worktree",
    "previous_agent": "上一个 AI 对话",
    "next_agent": "下一个 AI 对话",
    "focus_agent": "直达第 N 个 AI 对话",
    "toggle_sidebar": "显示/隐藏侧栏",
    "navigate_workspace_up": "导航模式：上移选择工作区",
    "navigate_workspace_down": "导航模式：下移选择工作区",
    "navigate_pane_left": "导航模式：选左侧分屏格",
    "navigate_pane_down": "导航模式：选下方分屏格",
    "navigate_pane_up": "导航模式：选上方分屏格",
    "navigate_pane_right": "导航模式：选右侧分屏格",
    "remote_image_paste": "远程会话里粘贴图片",
}


def _run_cli(argv, timeout=8):
    """Run a helper command and return stdout, or "" for any failure."""
    exe = shutil.which(argv[0])
    if not exe:
        return ""
    try:
        res = subprocess.run([exe] + list(argv[1:]), capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    if res.returncode != 0:
        return ""
    return (res.stdout or "")[:MAX_FILE_BYTES]


def _keybind_pairs(text):
    """Yield (trigger, action) from `keybind = trigger=action` lines of ghostty config.

    ghostty has no inline comments, so a value may carry a trailing comment only
    by mistake; keep the first whitespace-free token as the action. `chain=` lines
    continue the previous binding and are skipped.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("keybind"):
            continue
        body = line[len("keybind"):].lstrip()
        if not body.startswith("="):
            continue
        rest = body[1:].strip()
        if rest.split()[0:1] == ["clear"]:
            continue
        if rest.startswith("chain="):
            continue
        trigger, sep, action = rest.partition("=")
        if not sep:
            continue
        # `keybind = ctrl+==increase_font_size:1` - the key itself is "=", so the
        # first split ate one character too many.
        if action.startswith("=") and trigger.endswith("+"):
            trigger, action = trigger + "=", action[1:]
        action = action.strip().split()[0] if action.strip() else ""
        yield trigger.strip(), action


def _ghostty_row(trigger, action, status, defaults, tuned):
    """Build one read-only panel row for a ghostty binding."""
    display = canonical_chord(trigger)
    if not display:
        return None
    base = (action or "").split(":")[0].split(",")[0]
    params = action[len(base) + 1:] if ":" in (action or "") else ""
    zh = GHOSTTY_ACTION_ZH.get(base) or ("ghostty 动作：" + (base or action or "?"))
    # A parenthesised note is appended to the action; a bare phrase replaces it
    # (used by `csi`, where the action name is meaningless and the parameter is
    # the whole story). No note at all = keep the plain action label.
    note = GHOSTTY_PARAM_ZH.get(base, {}).get(params.split(",")[0], "")
    if note.startswith("\uff08"):
        zh += note
    elif note:
        zh = note
    row = {
        "source": "ghostty",
        "layer": "terminal",
        "file": "ghostty/config",
        "key": display,
        "raw_key": trigger,
        "description": "ghostty: " + (action or "unbind"),
        "description_zh": zh,
        "action": action or "unbind",
        "category": CATEGORY_TERMINAL,
        "status": status,
        "default_key": display if status != "disabled" else "",
        "is_mouse": False,
        "is_release": None,
        # managed_by is what makes KeybindsPanel.qml hide every mutating button.
        "managed_by": GHOSTTY_MANAGER_LABEL,
        "ghostty_trigger": trigger,
    }
    if tuned:
        row["user_tuned"] = True
        # `user_tuned` stays as data for the panel; the label itself must not
        # carry a source breadcrumb - every ghostty row lives in that one file.
    if status == "disabled":
        was = defaults.get(trigger, "")
        row["default_action"] = was
        row["default_key"] = display
    return row


def parse_ghostty_keybinds():
    """Effective ghostty keybinds + the ones the user unbound on purpose.

    Prefers `ghostty +list-keybinds` (the resolved set, ~90 ms) so the panel shows
    what the terminal really does; falls back to reading the config file when the
    CLI is unavailable. `+show-config --default` supplies the built-in map, which
    is what lets an unbound row say what it used to be.
    """
    defaults = {}
    for trigger, action in _keybind_pairs(_run_cli(["ghostty", "+show-config", "--default"])):
        if trigger and action:
            defaults.setdefault(trigger, action)

    user_text = ""
    try:
        user_text = _safe_read_regular_file(GHOSTTY_CONFIG_PATH)
    except Exception:
        user_text = ""
    user_pairs = list(_keybind_pairs(user_text))
    user_triggers = {t for t, _ in user_pairs}
    unbound = [t for t, a in user_pairs if a == "unbind"]

    effective = {}
    for trigger, action in _keybind_pairs(_run_cli(["ghostty", "+list-keybinds"])):
        if not trigger:
            continue
        effective.setdefault(trigger, []).append(action)
    if not effective:  # no CLI: show whatever the user file itself declares
        for trigger, action in user_pairs:
            if action and action != "unbind":
                effective.setdefault(trigger, []).append(action)

    rows = []
    seen = set()
    for trigger, actions in effective.items():
        merged = ";".join([a for a in actions if a])
        is_default = defaults.get(trigger) in actions and trigger not in user_triggers
        row = _ghostty_row(trigger, merged, "default", defaults, tuned=trigger in user_triggers)
        if row and row["key"] not in seen:
            if not is_default:
                row["status"] = "default"  # never "custom": that would offer delete on a foreign layer
            rows.append(row)
            seen.add(row["key"])
    for trigger in unbound:
        row = _ghostty_row(trigger, "unbind", "disabled", defaults, tuned=True)
        if row and row["key"] not in seen:
            was = defaults.get(trigger, "")
            base = was.split(":")[0]
            if was:
                row["description"] = "ghostty: unbound (was %s)" % was
                row["description_zh"] = "已解绑：原本是 %s" % (GHOSTTY_ACTION_ZH.get(base) or base)
            else:
                # Not a built-in default. `alt+shift+enter` for instance never was
                # one - it only existed here as our own csi: re-encode - so
                # "原本是 ghostty 内置动作" would state something false.
                row["description"] = "ghostty: not bound by default, key forwarded inward"
                row["description_zh"] = "已解绑：ghostty 本来不绑这个键，直接透传给内层"
            # annotate_layer_shadows() appends which inner layer now answers it.
            rows.append(row)
            seen.add(row["key"])
    return rows


def _herdr_key_entries():
    """Return [(action, chord)] from the [keys] table of herdr's config.toml."""
    try:
        text = _safe_read_regular_file(HERDR_CONFIG_PATH)
    except Exception:
        return []
    try:
        import tomllib

        keys = tomllib.loads(text).get("keys", {})
        out = []
        for action, value in keys.items():
            values = value if isinstance(value, list) else [value]
            for chord in values:
                if isinstance(chord, str) and chord:
                    out.append((action, chord))
        return out
    except Exception:
        pass
    # Fallback for python < 3.11: enough for herdr's flat scalar/list values.
    out, in_keys = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_keys = stripped == "[keys]"
            continue
        if not in_keys or stripped.startswith("#") or "=" not in stripped:
            continue
        action, _, value = stripped.partition("=")
        for chord in re.findall(r'"([^"]+)"', value) or ([value.strip()] if value.strip() else []):
            if chord:
                out.append((action.strip(), chord))
    return out


def _indexed_label(zh, index):
    """Label for one member of a `1..9` range binding."""
    # "跳到第 N 个标签页" already carries the placeholder, so fill it in instead of
    # stacking "跳到第 N 个标签页（第 3 个）" on top of itself.
    if "第 N 个" in zh:
        return zh.replace("第 N 个", "第 %d 个" % index)
    return "%s（第 %d 个）" % (zh, index)


def _herdr_disabled_actions():
    """Return actions herdr *does* ship a default key for, switched off with `x = ""`.

    An empty chord is herdr's disable spelling (verified with `herdr config check`:
    `""` validates clean, while `"none"`/`"unbind"` parse as invalid bindings). Such
    an action has no chord to show, so without a row of its own the panel would
    silently hide a deliberate decision and the key would look "dead for no
    reason" once the built-in default it used to have is gone.
    """
    try:
        import tomllib

        keys = tomllib.loads(_safe_read_regular_file(HERDR_CONFIG_PATH)).get("keys", {})
    except Exception:
        return []
    out = []
    for action, value in keys.items():
        values = value if isinstance(value, list) else [value]
        if values and all(isinstance(v, str) and not v.strip() for v in values):
            out.append(action)
    return out


def parse_herdr_keybinds():
    """herdr's own bindings, split into prefix-mode chords and direct chords.

    A `prefix+...` chord only exists inside herdr's prefix mode, so it can never
    collide with the outer layers; a direct chord (`alt+enter`, `ctrl+alt+left`)
    competes with ghostty and Hyprland, which is exactly what the shadow pass
    below checks for.
    """
    rows, seen = [], set()
    for action, chord in _herdr_key_entries():
        is_prefix = chord.startswith("prefix+")
        rest = chord[len("prefix+"):] if is_prefix else chord
        # A range like `alt+1..9` / `prefix+shift+1..9` expands on the *last* token.
        head, sep, tail = rest.rpartition("+")
        if ".." not in tail:
            keys = [rest]
        else:
            lo, _, hi = tail.partition("..")
            try:
                keys = [(head + "+" if sep else "") + str(i) for i in range(int(lo), int(hi) + 1)]
            except ValueError:
                keys = [rest]
        zh = HERDR_ACTION_ZH.get(action) or ("herdr 动作：" + action)
        for index, one in enumerate(keys, start=1):
            chord_key = canonical_chord(one)
            if not chord_key:
                continue
            if is_prefix:
                chord_key = "PREFIX + " + chord_key
            if chord_key in seen:
                continue
            seen.add(chord_key)
            rows.append({
                "source": "herdr",
                "layer": "herdr",
                "file": "herdr/config.toml",
                "key": chord_key,
                # `raw_key` is the one chord this row actually means; a range in
                # the config (`alt+1..9`) is one line for herdr but nine chords
                # on the keyboard, and matching chords across layers only works
                # on the expanded form. `herdr_chord` keeps the config spelling.
                "raw_key": one,
                "herdr_chord": chord,
                "description": "herdr: " + action,
                "description_zh": zh if len(keys) == 1 else _indexed_label(zh, index),
                "action": action,
                "category": CATEGORY_HERDR,
                "status": "default",
                "default_key": chord_key,
                "is_mouse": False,
                "is_release": None,
                "managed_by": HERDR_MANAGER_LABEL,
                "herdr_action": action,
                "herdr_prefix_chord": is_prefix,
            })
    for action in _herdr_disabled_actions():
        zh = HERDR_ACTION_ZH.get(action) or action
        rows.append({
            "source": "herdr",
            "layer": "herdr",
            "file": "herdr/config.toml",
            "key": "无按键",
            "raw_key": "",
            "herdr_chord": "",
            "description": "herdr: %s (disabled: bound to an empty key)" % action,
            "description_zh": "已关闭：%s —— config.toml 里绑了空键，要用的话自己给它指定一个键" % zh,
            "action": action,
            "category": CATEGORY_HERDR,
            "status": "disabled",
            "default_key": "",
            "is_mouse": False,
            "is_release": None,
            "managed_by": HERDR_MANAGER_LABEL,
            "herdr_action": action,
            "herdr_prefix_chord": False,
        })
    return rows


def _pi_docs_path():
    """Locate pi's keybindings.md from the `pi` executable (mise keeps docs beside it)."""
    exe = shutil.which("pi")
    if not exe:
        return ""
    root = os.path.dirname(os.path.realpath(exe))
    for cand in (os.path.join(root, "docs", "keybindings.md"),
                 os.path.join(os.path.dirname(root), "docs", "keybindings.md")):
        if os.path.isfile(cand):
            return cand
    return ""


PI_DOCS_PATH = _pi_docs_path()
# pi's default chord table lives in its docs as markdown tables. Only the
# transcript-scrolling section is mirrored: that is what gets pressed while reading
# a long AI answer, and mirroring all ~300 editor actions would bury the panel.
# The chords are parsed from the doc rather than hard-coded, so an upgrade that
# moves a default shows up here instead of silently lying.
PI_DOCS_SECTION = "#### Fullscreen"

PI_ACTION_ZH = {
    "tui.altScreen.pageUp": "对话上翻一页",
    "tui.altScreen.pageDown": "对话下翻一页",
    "tui.altScreen.halfPageUp": "对话上翻半页",
    "tui.altScreen.halfPageDown": "对话下翻半页",
    "tui.altScreen.lineUp": "对话上滑一行",
    "tui.altScreen.lineDown": "对话下滑一行",
    "tui.altScreen.previousPrompt": "跳到上一条消息",
    "tui.altScreen.nextPrompt": "跳到下一条消息",
    "tui.altScreen.search": "在对话里搜索",
    "tui.altScreen.searchNext": "搜索：下一个匹配",
    "tui.altScreen.searchPrevious": "搜索：上一个匹配",
    "tui.altScreen.searchClose": "关闭对话搜索",
    "tui.altScreen.top": "跳到对话开头",
    "tui.altScreen.bottom": "跳到对话末尾（继续跟随）",
}


def _pi_default_actions():
    """[(action_id, [default chords], english description)] from pi's docs table."""
    if not PI_DOCS_PATH:
        return []
    try:
        text = _safe_read_regular_file(PI_DOCS_PATH)
    except Exception:
        return []
    out, in_section = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            if in_section:
                break
            in_section = line.strip() == PI_DOCS_SECTION
            continue
        if not in_section or not line.startswith("|"):
            continue
        cols = [col.strip() for col in line.strip().strip("|").split("|")]
        if len(cols) < 3 or not cols[0].startswith("`"):
            continue
        # Drop the chords the doc mentions only inside a parenthetical: those are
        # platform exceptions ("`ctrl+up`, `ctrl+up` (`ctrl+up` only on Windows and
        # WSL)"), and listing them would claim a Windows-only default is live here.
        cell = cols[1]
        notes = "".join(re.findall(r"\(([^)]*)\)", cell) + re.findall(r"\uff08([^\uff09]*)\uff09", cell))
        skipped = set(re.findall(r"`([^`]+)`", notes))
        chords = [c for c in re.findall(r"`([^`]+)`", cell) if " " not in c and c not in skipped]
        out.append((cols[0].strip("`"), chords, cols[2]))
    return out


def _pi_overrides():
    """The user's own pi keybindings: value replaces the default, [] disables it."""
    try:
        data = json.loads(_safe_read_regular_file(PI_KEYBINDINGS_PATH))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    data.pop("$schema", None)
    return data


def _pi_row(action, chords, zh, source_file, status):
    row = {
        "source": "pi",
        "layer": "pi",
        "file": source_file,
        "key": "无按键",
        "raw_key": "",
        "description": "pi: " + action,
        "description_zh": ("已关闭：%s —— pi 里这个动作被绑了空列表" % zh) if status == "disabled"
        else zh,
        "action": action,
        "category": CATEGORY_PI,
        "status": status,
        "default_key": "",
        "is_mouse": False,
        "is_release": None,
        "managed_by": PI_MANAGER_LABEL,
        "pi_action": action,
    }
    if not chords:
        return [row]
    return [dict(row, key=canonical_chord(str(chord)), raw_key=str(chord),
                 default_key=canonical_chord(str(chord))) for chord in chords]


def parse_pi_keybinds():
    """pi's own TUI keys: documented defaults with ~/.pi/agent/keybindings.json on top.

    pi is the innermost layer - it paints a fullscreen TUI inside a herdr pane -
    so an outer layer that owns a chord wins and the pi row becomes decoration.
    That is why the transcript scroll keys had to avoid the ones herdr already
    took, and why these rows go through the same shadow pass as herdr's.
    """
    overrides = _pi_overrides()
    actions = list(_pi_default_actions())
    known = {action for action, _, _ in actions}
    actions.extend((action, [], "") for action in sorted(overrides) if action not in known)
    rows, seen = [], set()
    for action, defaults, desc in actions:
        chords = overrides.get(action, defaults)
        if isinstance(chords, str):
            chords = [chords]
        zh = PI_ACTION_ZH.get(action) or desc or action
        for row in _pi_row(action, list(chords or []), zh,
                          "pi/keybindings.json" if action in overrides else "pi 内置默认",
                          "default" if chords else "disabled"):
            marker = (row["key"], action)
            if marker in seen:
                continue
            seen.add(marker)
            rows.append(row)
    return rows


def terminal_layer_rows():
    """All read-only rows for the layers inside the terminal window."""
    rows = parse_ghostty_keybinds()
    rows.extend(parse_herdr_keybinds())
    rows.extend(parse_pi_keybinds())
    return rows


def _ghostty_forwards_key(row) -> bool:
    """True when a ghostty binding hands the keystroke inward instead of owning it.

    `csi:`/`esc:`/`text:` actions re-encode the key for the program inside the
    terminal - e.g. `alt+shift+enter=csi:13;4u` exists precisely so herdr and tmux
    can tell Alt+Shift+Enter apart - and `unconsumed:` explicitly does not consume
    the input. Counting those as "ghostty owns this chord" would flag herdr's real
    bindings as dead.
    """
    action = str(row.get("action") or "")
    if action.split(":")[0] in ("csi", "esc", "text"):
        return True
    return "unconsumed:" in str(row.get("raw_key") or "").lower()


def _row_canon(b):
    """The comparable chord of one binding row, however its layer spells chords.

    Every parser stores its already-folded chord in `key` (herdr's expanded
    range members included), while `raw_key` keeps the source spelling: herdr's
    `alt+1..9`, ghostty's `alt+digit_3` or `foo/ctrl+a`. Folding `key` first is
    what lets a range member match the outer layer's single binding.
    """
    return canonical_chord(b.get("key")) or canonical_chord(b.get("raw_key"))


def annotate_layer_shadows(all_active):
    """Mark rows whose chord never reaches their layer because an outer layer owns it.

    Precedence is Hyprland > ghostty > herdr, so:
      * a ghostty bind on a chord Hyprland grabbed is dead code in ghostty;
      * a herdr *direct* bind on a chord Hyprland or ghostty grabbed is dead code
        in herdr - that is how herdr's alt+1..9 / ctrl+alt+arrow "second entry"
        bindings silently did nothing until 2026-10-13.
    Prefix-mode herdr chords are skipped: they are only ever offered to herdr.
    """
    hypr, ghostty = set(), set()
    for b in all_active:
        if b.get("status") == "disabled" or b.get("is_mouse") or b.get("is_release"):
            continue
        canon = _row_canon(b)
        if not canon:
            continue
        if not b.get("layer"):
            hypr.add(canon)
        elif b.get("source") == "ghostty" and not _ghostty_forwards_key(b):
            ghostty.add(canon)
    for b in all_active:
        layer = b.get("layer")
        if not layer or b.get("status") == "disabled":
            continue
        if b.get("herdr_prefix_chord"):
            continue
        canon = _row_canon(b)
        if not canon:
            continue
        if canon in hypr:
            b["shadowed_by"] = "Hyprland"
        elif layer == "herdr" and canon in ghostty:
            b["shadowed_by"] = "ghostty"

    # The mirror image of the above: a chord ghostty forwarded (`=unbind`) is not
    # wasted, it is handed to whoever lives inside the terminal. Naming the new
    # owner turns ~50 bare "已解绑" rows into a map of who actually answers the
    # key - which is the question this panel exists to answer.
    claims = {}
    for b in all_active:
        if b.get("status") == "disabled" or b.get("shadowed_by"):
            continue
        if b.get("is_mouse") or b.get("is_release"):
            continue
        canon = _row_canon(b)
        if not canon:
            continue
        if not b.get("layer"):
            claims.setdefault(canon, ("Hyprland", b.get("description_zh") or b.get("description") or ""))
        elif b.get("source") == "herdr" and not b.get("herdr_prefix_chord"):
            claims.setdefault(canon, ("herdr", b.get("description_zh") or b.get("action") or ""))
    for b in all_active:
        if b.get("source") != "ghostty" or b.get("status") != "disabled":
            continue
        claim = claims.get(_row_canon(b))
        if not claim:
            continue
        owner, what = claim
        b["handed_to"] = owner
        b["description_zh"] = str(b.get("description_zh", "")) + (
            " ⇒ 现在由 %s 接手：%s" % (owner, what) if what else " ⇒ 现在由 %s 接手" % owner)
        b["description"] = str(b.get("description", "")) + " -> forwarded to " + owner


def build_complete_model():
    """
    Construct the unified model of active bindings, catalog, conflicts, and mouse scroll settings.
    """
    default_binds = parse_default_bindings()
    user_state = parse_user_bindings()
    island_state = parse_island_bindings()

    # hyprland.lua requires hypr.bindings *before* hypr.island-bindings, so on a
    # shared key the island wins and the user's line is dead code. Pull such
    # user entries out of the overlay and report them alongside the island entry
    # so the conflict view names the dead binding instead of hiding it.
    island_by_key = {b["key"]: b for b in island_state["binds"]}
    shadowed_binds = []
    overlay_binds = []
    for ub in user_state["binds"]:
        clash = island_by_key.get(ub["key"])
        if clash:
            dead = dict(ub)
            dead["shadowed_by"] = ISLAND_MANAGER_LABEL
            dead["shadowed_action"] = clash["action"]
            shadowed_binds.append(dead)
        else:
            overlay_binds.append(ub)
    overlay_binds.extend(island_state["binds"])
    active_unbinds = user_state["unbinds"] | island_state["unbinds"]

    # An overlay binding "claims" the default it replaced, and its row is rendered
    # in that default's place. Two overlay binds can legitimately claim the same
    # default: here the user moved btop off SUPER + CTRL + T and the island then
    # took that key for Themes. Keeping a single claim per default silently
    # dropped the user's btop row, so claims are collected per default key and
    # every claimant gets its own row.
    rebound_defaults = {}
    for ub in overlay_binds:
        if ub.get("source") == "island":
            # The island helper always unbinds and rebinds the *same* chord, so
            # match its takeover against that key only. Matching by description
            # across keys would, for example, hand island's SUPER+ESCAPE
            # 'Power menu' the untouched XF86PowerOff default and drop it from
            # the list entirely.
            matched = next((d for d in default_binds if d["key"] == ub["key"]), None)
        else:
            matched = next(
                (d for d in default_binds if d["description"].lower() == ub["description"].lower() or d["action"] == ub["action"]),
                None
            )
        if matched:
            rebound_defaults.setdefault(matched["key"], []).append(ub)

    matched_user_bind_keys = {claim["key"] for claims in rebound_defaults.values() for claim in claims}

    all_active = []

    # 1. Iterate defaults
    for d in default_binds:
        k = d["key"]

        if k in rebound_defaults:
            for ub in rebound_defaults[k]:
                item = dict(ub)
                item["status"] = "modified"
                item["category"] = d.get("category", "Custom")
                item["default_key"] = k
                item["default_action"] = d.get("action", "")
                all_active.append(item)
            continue

        same_key_user_bind = next((ub for ub in overlay_binds if ub["key"] == k and ub["key"] not in matched_user_bind_keys), None)
        if same_key_user_bind:
            item = dict(same_key_user_bind)
            item["status"] = "modified" if same_key_user_bind["description"].lower() == d["description"].lower() else "custom"
            item["category"] = d.get("category", "Custom")
            item["default_key"] = k
            item["default_action"] = d["action"]
            all_active.append(item)
            matched_user_bind_keys.add(k)
            continue

        if k in active_unbinds:
            item = dict(d)
            item["status"] = "disabled"
            item["default_key"] = k
            if k in island_state["unbinds"]:
                item["disabled_by"] = ISLAND_MANAGER_LABEL
            all_active.append(item)
            continue

        item = dict(d)
        item["status"] = "default"
        item["default_key"] = k
        all_active.append(item)

    # 2. Append custom bindings from the overlay layer (user file, then island)
    for ub in overlay_binds:
        if ub["key"] in matched_user_bind_keys:
            continue
        item = dict(ub)
        item["status"] = "custom"
        item["category"] = ub.get("category") or "Custom"
        item["default_key"] = ""
        all_active.append(item)

    # 3. Island overrides: live in the user file but never fire, since the island
    #    rebinds the same key afterwards. Keep them visible and conflicting.
    for ub in shadowed_binds:
        item = dict(ub)
        item["status"] = "modified"
        item["category"] = "Custom"
        item["default_key"] = ""
        all_active.append(item)

    # 3.5 Read-only rows for the layers inside the terminal window (ghostty owns
    #     the key first, herdr sees what is left). They carry managed_by, so the
    #     panel renders them without edit/reset/delete affordances.
    all_active.extend(terminal_layer_rows())

    # 4. Categorize, then group by category with alphabetical order inside each
    #    group, so the "All" view reads as one section per category.
    for b in all_active:
        b["category"] = classify_binding(b)
        if not b.get("description_zh"):
            b["description_zh"] = localize_description(b.get("description", ""))

    all_active.sort(key=category_sort_key)

    # 5. Detect Real Key Conflicts. Terminal-layer rows are excluded: a ghostty
    #    or herdr row sharing a chord with a Hyprland binding is a cross-layer
    #    shadow (step 6.5), not two Hyprland bindings fighting over one key.
    conflicts_dict = {}
    key_groups = {}
    for b in all_active:
        if b.get("status") == "disabled" or b.get("is_mouse") or b.get("is_release") or b.get("layer"):
            continue
        k = b["key"]
        if k not in key_groups:
            key_groups[k] = []
        key_groups[k].append(b)

    for k, group in key_groups.items():
        sources = {x.get("source") for x in group}
        files = {x.get("file") for x in group}
        is_intentional_composite = (sources == {"default"} and len(files) == 1 and files != {None})

        if len(group) > 1 and not is_intentional_composite:
            for b in group:
                b["is_conflict"] = True
                b["conflict_with"] = [
                    {"description": other["description"], "description_zh": localize_description(other["description"])}
                    for other in group if other != b
                ]
            conflicts_dict[k] = {
                "key": k,
                "bindings": [
                    {
                        "description": x["description"],
                        "description_zh": localize_description(x["description"]),
                        "action": x["action"],
                        "status": x.get("status", "unknown"),
                    }
                    for x in group
                ],
            }
        else:
            for b in group:
                b["is_conflict"] = False

    conflicts = list(conflicts_dict.values())

    # 6.5 Cross-layer shadows: which layer actually gets the key.
    annotate_layer_shadows(all_active)

    # 6. Available Catalog (sorted alphabetically by name)
    bound_descriptions = {b["description"].lower() for b in all_active if b.get("status") != "disabled"}
    catalog = []

    for preset in PRESET_CATALOG:
        is_bound = preset["name"].lower() in bound_descriptions or preset["description"].lower() in bound_descriptions
        curr_key = ""
        for b in all_active:
            if b.get("status") != "disabled" and (b["description"].lower() == preset["name"].lower() or b["description"].lower() == preset["description"].lower()):
                curr_key = b["key"]
                break

        cat_item = dict(preset)
        cat_item["name_zh"] = localize_description(preset.get("name", ""))
        # Re-bucket presets with the same rules as live bindings, otherwise the
        # catalog keeps the legacy file names and no chip ever matches them.
        cat_item["category"] = classify_binding(
            {
                "description": preset.get("name") or preset.get("description", ""),
                "action": preset.get("command", ""),
                "category": preset.get("category", ""),
                "source": "default",
            }
        )
        cat_item["is_bound"] = is_bound
        cat_item["current_key"] = curr_key
        catalog.append(cat_item)

    catalog.sort(key=lambda x: x.get("name", "").lower())

    total_modified = len([b for b in all_active if b.get("status") in ("modified", "custom") and not b.get("layer")])
    layer_rows = [b for b in all_active if b.get("layer")]

    # Chip counts: every row the "All" list actually shows, including the
    # disabled (unbound) ones, so a chip's number matches what clicking it gives.
    category_counts = {}
    for b in all_active:
        cat = b.get("category") or CATEGORY_CUSTOM
        b["category"] = cat
        b["category_zh"] = CATEGORY_ZH.get(cat, cat)
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # Group tabs for the island's keybinding view. Counts here exclude the
    # unbound (struck-out) rows on purpose: that view lists keys you can
    # actually press, so a tab's number must be the number of rows under it.
    # The panel keeps `category_counts` (which includes unbound) untouched.
    live_counts: dict = {}
    for b in all_active:
        if b.get("status") == "disabled":
            continue
        live_counts[b["category"]] = live_counts.get(b["category"], 0) + 1
    groups = [
        {"id": cat, "label": CATEGORY_ZH.get(cat, cat), "count": live_counts[cat]}
        for cat in CATEGORY_ORDER
        if live_counts.get(cat)
    ]

    return {
        "active": all_active,
        "category_order": CATEGORY_ORDER,
        "category_counts": category_counts,
        "category_zh": CATEGORY_ZH,
        "groups": groups,
        "catalog": catalog,
        "conflicts": conflicts,
        "apps": get_installed_apps(),
        "total_active": len([b for b in all_active if b.get("status") != "disabled" and not b.get("layer")]),
        "total_modified": total_modified,
        "total_conflicts": len(conflicts),
        # Terminal-layer counters stay separate so the header still describes the
        # Hyprland bindings this panel can actually edit.
        "total_terminal": len([b for b in layer_rows if b.get("source") == "ghostty" and b.get("status") != "disabled"]),
        "total_terminal_unbound": len([b for b in layer_rows if b.get("source") == "ghostty" and b.get("status") == "disabled"]),
        "total_herdr": len([b for b in layer_rows if b.get("source") == "herdr"]),
        "total_shadows": len([b for b in layer_rows if b.get("shadowed_by")]),
        "ghostty_config_file": GHOSTTY_CONFIG_PATH if os.path.isfile(GHOSTTY_CONFIG_PATH) else "",
        "herdr_config_file": HERDR_CONFIG_PATH if os.path.isfile(HERDR_CONFIG_PATH) else "",
        "mouse_settings": get_mouse_scroll_settings(),
        "user_bindings_file": USER_BINDINGS_PATH,
        "island_bindings_file": ISLAND_BINDINGS_PATH if os.path.isfile(ISLAND_BINDINGS_PATH) else "",
        "total_island": len(island_state["binds"]),
        "total_shadowed": len(shadowed_binds),
    }


def _written_bind_identity(entry):
    """Return the (description, action) pair a single o.bind line for ``entry``
    will store on disk. Scoping key removal to this exact pair lets a rebind
    drop only its own previous line, never a *different* binding that shares
    the old key (e.g. the pair it is being swapped with)."""
    desc = sanitize_lua_str(entry.get("description", ""))
    cmd = entry.get("command", "")
    action = entry.get("action", "")
    if action and (action.strip().startswith("{") or action.strip().startswith("hl.")):
        return desc, action.strip()[:MAX_CMD_LEN]
    if cmd and (cmd.strip().startswith("{") or cmd.strip().startswith("hl.")):
        return desc, cmd.strip()[:MAX_CMD_LEN]
    raw_cmd = (cmd or action).strip()
    if (raw_cmd.startswith('"') and raw_cmd.endswith('"')) or (raw_cmd.startswith("'") and raw_cmd.endswith("'")):
        raw_cmd = raw_cmd[1:-1]
    return desc, f'"{sanitize_lua_str(raw_cmd)}"'


def write_user_bindings(lines_to_add=None, unbinds_to_add=None, keys_to_remove=None, preserve_entry=None):
    """
    Safely modify ~/.config/hypr/bindings.lua.
    Preserves comments and mouse bindings, ensures clean formatted sections.

    ``preserve_entry``: when set, an existing o.bind/o.bind_toggle whose key is
    in ``keys_to_remove`` is only removed if it is *the same binding* (matching
    description OR action, mirroring build_complete_model's identity convention)
    as this entry. Any other binding sharing the key is left intact (needed for
    swaps, where one binding's old key is another binding's freshly written key,
    and for in-place action edits, where the description is unchanged).
    """
    cfg_dir = os.path.dirname(USER_BINDINGS_PATH)
    preserve_identity = _written_bind_identity(preserve_entry) if preserve_entry else None
    os.makedirs(cfg_dir, exist_ok=True)

    existing_content = ""
    # Open the source directly (no isfile() pre-check -> no TOCTOU window in
    # which a FIFO could be swapped in to block the open indefinitely).
    try:
        src_fd = os.open(USER_BINDINGS_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        src_fd = None
    except OSError:
        raise ValueError(f"refusing to operate on non-regular/symlink path: {USER_BINDINGS_PATH}")
    if src_fd is not None:
        try:
            if not stat.S_ISREG(os.fstat(src_fd).st_mode):
                os.close(src_fd)
                raise ValueError(f"refusing non-regular file: {USER_BINDINGS_PATH}")
            with os.fdopen(src_fd, "r", encoding="utf-8") as f:
                existing_content = f.read(MAX_FILE_BYTES)
        except BaseException:
            raise
        backup_path = USER_BINDINGS_PATH + ".bak"
        # Write the backup through a unique temp file and atomically replace,
        # instead of O_TRUNC on the predictable .bak path. This avoids
        # truncating an existing inode, so a planted hard link / symlink at
        # that path cannot destroy another file's contents.
        bak_fd, bak_tmp = tempfile.mkstemp(dir=cfg_dir, prefix=".bindings_", suffix=".bak.tmp")
        try:
            with os.fdopen(bak_fd, "w", encoding="utf-8") as bak_f:
                bak_f.write(existing_content)
                bak_f.flush()
                os.fsync(bak_f.fileno())
            os.replace(bak_tmp, backup_path)
        except BaseException:
            if os.path.exists(bak_tmp):
                try:
                    os.unlink(bak_tmp)
                except OSError:
                    pass
            raise

    current_lines = existing_content.splitlines() if existing_content else []

    if not current_lines:
        current_lines = [
            "-- Personal keybinding overrides for Omarchy Hyprland",
            "-- Managed graphically by Omarchy Keybindings Plugin",
            "",
        ]

    clean_keys = set(normalize_key_chord(k) for k in (keys_to_remove or []))

    new_lines = []

    for line in current_lines:
        stripped = line.strip()

        if stripped.startswith("--"):
            new_lines.append(line)
            continue

        is_targeted = False
        if clean_keys:
            m_unbind = re.match(r'hl\.unbind\s*\(\s*["\']([^"\']+)["\']\s*\)', stripped)
            if m_unbind and normalize_key_chord(m_unbind.group(1)) in clean_keys:
                is_targeted = True

            m_bind = re.match(r'o\.bind(?:_toggle)?\s*\(\s*["\']([^"\']+)["\']', stripped)
            if m_bind and normalize_key_chord(m_bind.group(1)) in clean_keys:
                if preserve_identity is not None:
                    _parsed = _parse_bind_line(stripped)
                    if _parsed:
                        norm_parsed_action = (_parsed[2] or "").strip().strip('"\'')
                        norm_pres_action = preserve_identity[1].strip().strip('"\'')
                        if ((_parsed[1] or "").strip().lower() == preserve_identity[0].lower()
                                or norm_parsed_action == norm_pres_action
                                or normalize_key_chord(m_bind.group(1)) in [normalize_key_chord(e.get("key", "")) for e in (lines_to_add or [])]):
                            is_targeted = True
                    # Else: a different binding sharing this key (swap partner) -> keep.
                else:
                    is_targeted = True

        if not is_targeted:
            new_lines.append(line)

    if unbinds_to_add:
        for k in unbinds_to_add:
            norm_k = normalize_key_chord(k)
            line_str = f'hl.unbind("{norm_k}")'
            if line_str not in new_lines:
                new_lines.append(line_str)

    if lines_to_add:
        for entry in lines_to_add:
            key = normalize_key_chord(entry.get("key", ""))
            desc = sanitize_lua_str(entry.get("description", ""))
            cmd = entry.get("command", "")
            action = entry.get("action", "")

            if action and (action.strip().startswith("{") or action.strip().startswith("hl.")):
                new_lines.append(f'o.bind("{key}", "{desc}", {action.strip()[:MAX_CMD_LEN]})')
            elif cmd and (cmd.strip().startswith("{") or cmd.strip().startswith("hl.")):
                new_lines.append(f'o.bind("{key}", "{desc}", {cmd.strip()[:MAX_CMD_LEN]})')
            else:
                raw_cmd = (cmd or action).strip()
                if (raw_cmd.startswith('"') and raw_cmd.endswith('"')) or (raw_cmd.startswith("'") and raw_cmd.endswith("'")):
                    raw_cmd = raw_cmd[1:-1]
                escaped_cmd = sanitize_lua_str(raw_cmd, max_len=MAX_CMD_LEN)
                new_lines.append(f'o.bind("{key}", "{desc}", "{escaped_cmd}")')


    final_output = []
    prev_blank = False
    for line in new_lines:
        if not line.strip():
            if not prev_blank:
                final_output.append("")
                prev_blank = True
        else:
            final_output.append(line)
            prev_blank = False

    fd, temp_file = tempfile.mkstemp(dir=cfg_dir, prefix=".bindings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(final_output) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.islink(temp_file):
            os.unlink(temp_file)
            raise ValueError("temp write path is a symlink")
        os.replace(temp_file, USER_BINDINGS_PATH)
    except BaseException:
        if os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except BaseException:
                pass
        raise


def reload_hyprland():
    """Reload Hyprland configuration and check for errors."""
    try:
        res = subprocess.run(["hyprctl", "reload"], capture_output=True, text=True, timeout=10)
        err_res = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=10)
        return {
            "success": res.returncode == 0,
            "reload_output": res.stdout.strip(),
            "config_errors": err_res.stdout.strip() if err_res.returncode == 0 else "",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def set_keybinding(key: str, description: str, command: str, action: str = "", old_key: str = "", override_conflicts: bool = True):
    """Set, modify, or replace a keybinding."""
    norm_key = normalize_key_chord(key)
    norm_old_key = normalize_key_chord(old_key) if old_key else ""

    if not norm_key:
        return {"success": False, "error": "Invalid key combination"}

    island_state = parse_island_bindings()
    clash = _island_clash(norm_key, island_state)
    if clash and not os.environ.get(FORCE_ISLAND_ENV):
        return _island_block(norm_key, clash)

    default_binds = parse_default_bindings()
    keys_to_clean = [norm_key]
    unbinds_to_add = []

    if norm_old_key and norm_old_key != norm_key:
        keys_to_clean.append(norm_old_key)
        if any(d["key"] == norm_old_key for d in default_binds):
            unbinds_to_add.append(norm_old_key)

    if any(d["key"] == norm_key for d in default_binds) or override_conflicts:
        unbinds_to_add.append(norm_key)

    binds = [{
        "key": norm_key,
        "description": description or command,
        "command": command,
        "action": action,
    }]

    write_user_bindings(lines_to_add=binds, unbinds_to_add=unbinds_to_add, keys_to_remove=keys_to_clean,
                        preserve_entry=binds[0])
    reload_status = reload_hyprland()

    return {
        "success": reload_status.get("success", False),
        "key": norm_key,
        "old_key": norm_old_key,
        "description": description,
        "reload": reload_status,
    }


def reset_keybinding(key: str, default_key: str = ""):
    """Reset a keybinding back to default Omarchy configuration."""
    norm_key = normalize_key_chord(key)
    norm_def = normalize_key_chord(default_key) if default_key else ""

    keys_to_remove = [norm_key]
    if norm_def and norm_def != norm_key:
        keys_to_remove.append(norm_def)

    default_binds = parse_default_bindings()
    user_state = parse_user_bindings()

    for ub in user_state["binds"]:
        if ub["key"] == norm_key:
            matched_default = next(
                (d for d in default_binds if d["description"].lower() == ub["description"].lower() or d["action"] == ub["action"]),
                None
            )
            if matched_default and matched_default["key"] not in keys_to_remove:
                keys_to_remove.append(matched_default["key"])

    write_user_bindings(keys_to_remove=keys_to_remove)
    reload_status = reload_hyprland()
    return {"success": reload_status.get("success", False), "key": norm_key, "reload": reload_status}


def disable_keybinding(key: str):
    """Disable/unbind a keybinding."""
    norm_key = normalize_key_chord(key)
    clash = _island_clash(norm_key)
    if clash and not os.environ.get(FORCE_ISLAND_ENV):
        return _island_block(norm_key, clash)
    write_user_bindings(unbinds_to_add=[norm_key], keys_to_remove=[norm_key])
    reload_status = reload_hyprland()
    return {"success": reload_status.get("success", False), "key": norm_key, "reload": reload_status}


def enable_keybinding(key: str):
    """Re-enable a previously disabled default keybinding by removing its unbind."""
    norm_key = normalize_key_chord(key)
    write_user_bindings(keys_to_remove=[norm_key])
    reload_status = reload_hyprland()
    return {"success": reload_status.get("success", False), "key": norm_key, "reload": reload_status}


def _strip_jsonc_comments(text: str) -> str:
    """Remove JSONC (JSON with Comments) style comments."""
    text = _JSONC_BLOCK_COMMENT_RE.sub('', text)
    lines = []
    for line in text.splitlines():
        lines.append(_JSONC_LINE_COMMENT_RE.sub('', line))
    return '\n'.join(lines)


def _drop_trailing_commas(text: str) -> str:
    """Remove commas that sit directly before a closing } or ].

    The launcher config is shared with other plugins (the island appends its own
    rows and leaves a trailing comma), and strict json.loads rejects that while
    the menu's own reader accepts it. String-aware, so a ", }" inside a label is
    left alone.
    """
    out = []
    in_string = False
    escape_next = False
    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escape_next:
                escape_next = False
            elif ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ',':
            j = i + 1
            while j < len(text) and text[j] in ' \t\r\n':
                j += 1
            if j < len(text) and text[j] in '}]':
                continue
        out.append(ch)
    return ''.join(out)


def _jsonc_loads(text: str):
    """Parse the launcher JSONC, tolerating comments and trailing commas.

    Raises ValueError when the file is genuinely unreadable, so callers can
    refuse to write instead of replacing a config they cannot understand.
    """
    stripped = _strip_jsonc_comments(text)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return json.loads(_drop_trailing_commas(stripped))
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"cannot parse {MENU_CONFIG_PATH}: {e}")


def _safe_read_regular_file(path: str):
    """Open path with O_NOFOLLOW | O_NONBLOCK, verify S_ISREG, return contents or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f"refusing to operate on non-regular/symlink path: {path}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"refusing non-regular file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            return f.read(MAX_FILE_BYTES)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _safe_atomic_write_file(path: str, content: str):
    """Write content to path atomically via a temp file in the same directory."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".menu_", suffix=".jsonc.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def check_menu_entry():
    """Return whether the launcher menu entry already exists."""
    try:
        contents = _safe_read_regular_file(MENU_CONFIG_PATH)
    except (ValueError, OSError) as e:
        # Reported instead of silently meaning "not installed": a false negative
        # here makes the panel nag for a launcher entry that is already there.
        return {"exists": False, "error": str(e), "path": MENU_CONFIG_PATH}
    if contents is None:
        return {"exists": False, "path": MENU_CONFIG_PATH, "reason": "file missing"}
    try:
        data = _jsonc_loads(contents)
    except ValueError as e:
        return {"exists": False, "error": str(e), "path": MENU_CONFIG_PATH, "reason": "unparseable"}
    return {"exists": MENU_ENTRY_KEY in data, "path": MENU_CONFIG_PATH}


def install_menu_entry():
    """Safely add the launcher menu entry without destroying existing entries."""
    contents = _safe_read_regular_file(MENU_CONFIG_PATH)
    data = {}
    if contents is not None:
        try:
            data = _jsonc_loads(contents)
        except ValueError as e:
            # Never rewrite a config we cannot read: this file is shared, and an
            # empty dict here used to silently delete every other plugin's rows.
            return {"success": False, "message": str(e)}
    if MENU_ENTRY_KEY in data:
        return {"success": True, "message": "Menu entry already exists."}
    data[MENU_ENTRY_KEY] = dict(MENU_ENTRY_VALUE)
    out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _safe_atomic_write_file(MENU_CONFIG_PATH, out)
    return {"success": True, "message": "Added Keybindings Manager to launcher."}


def remove_menu_entry():
    """Safely remove the launcher menu entry if present."""
    contents = _safe_read_regular_file(MENU_CONFIG_PATH)
    if contents is None:
        return {"success": True, "message": "Config file not found — nothing to remove."}
    try:
        data = _jsonc_loads(contents)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if MENU_ENTRY_KEY not in data:
        return {"success": True, "message": "Menu entry not present — nothing to remove."}
    del data[MENU_ENTRY_KEY]
    out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _safe_atomic_write_file(MENU_CONFIG_PATH, out)
    return {"success": True, "message": "Removed Keybindings Manager from launcher."}


def main():
    if len(sys.argv) < 2:
        print(json.dumps(build_complete_model(), indent=2))
        return

    cmd = sys.argv[1]

    if cmd == "list":
        print(json.dumps(build_complete_model()))

    elif cmd == "set":
        if len(sys.argv) < 5:
            print(json.dumps({"success": False, "error": "Usage: set <key> <desc> <command> [action] [old_key]"}))
            sys.exit(1)
        key = sys.argv[2]
        desc = sys.argv[3]
        command = sys.argv[4]
        action = sys.argv[5] if len(sys.argv) > 5 else ""
        old_key = sys.argv[6] if len(sys.argv) > 6 else ""
        res = set_keybinding(key, desc, command, action, old_key)
        print(json.dumps(res))

    elif cmd == "reset":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: reset <key> [default_key]"}))
            sys.exit(1)
        key = sys.argv[2]
        def_key = sys.argv[3] if len(sys.argv) > 3 else ""
        res = reset_keybinding(key, def_key)
        print(json.dumps(res))

    elif cmd == "disable":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: disable <key>"}))
            sys.exit(1)
        key = sys.argv[2]
        res = disable_keybinding(key)
        print(json.dumps(res))

    elif cmd == "enable":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: enable <key>"}))
            sys.exit(1)
        key = sys.argv[2]
        res = enable_keybinding(key)
        print(json.dumps(res))

    elif cmd == "reload":
        res = reload_hyprland()
        print(json.dumps(res))

    elif cmd == "island":
        state = parse_island_bindings()
        print(json.dumps({
            "file": ISLAND_BINDINGS_PATH if os.path.isfile(ISLAND_BINDINGS_PATH) else "",
            "helper": ISLAND_BINDINGS_SCRIPT if os.path.isfile(ISLAND_BINDINGS_SCRIPT) else "",
            "bindings": state["binds"],
            "addon_disabled_keys": sorted(state["off_keys"]),
            "actions": island_keybinding_actions(),
        }, indent=2))

    elif cmd == "island-set":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: island-set <action-id> [keys]"}))
            sys.exit(1)
        res = island_set_keybinding(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(json.dumps(res))

    elif cmd == "menu-check":
        res = check_menu_entry()
        print(json.dumps(res))

    elif cmd == "menu-install":
        res = install_menu_entry()
        print(json.dumps(res))

    elif cmd == "menu-remove":
        res = remove_menu_entry()
        print(json.dumps(res))

    elif cmd == "apps":
        print(json.dumps(get_installed_apps()))

    elif cmd == "terminal":
        # Debug view of the read-only layers inside the terminal window. Reuse the
        # full model so the cross-layer shadow annotations come from one code path.
        model = build_complete_model()
        rows = [b for b in model["active"] if b.get("layer")]
        print(json.dumps({
            "ghostty_config_file": model["ghostty_config_file"],
            "herdr_config_file": model["herdr_config_file"],
            "total_terminal": model["total_terminal"],
            "total_terminal_unbound": model["total_terminal_unbound"],
            "total_herdr": model["total_herdr"],
            "rows": rows,
            "shadows": [{"key": r["key"], "by": r["shadowed_by"], "action": r["action"]}
                        for r in rows if r.get("shadowed_by")],
        }, indent=2))

    else:
        print(json.dumps({"success": False, "error": f"Unknown command {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
