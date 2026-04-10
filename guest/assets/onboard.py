#!/usr/bin/env python3
"""QuantideClaw first-login onboarding wizard."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import queue
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
import os

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - environment fallback only
    Image = None
    ImageTk = None

try:
    import qrcode
except ImportError:
    qrcode = None

INSTALLER_ENV = Path("/opt/quantideclaw-onboard/installer.env")
APP_HOME = Path("/opt/quantideclaw-onboard")
if (Path(__file__).resolve().parent / "openrouter.jpg").exists():
    ASSET_DIR = Path(__file__).resolve().parent
else:
    ASSET_DIR = APP_HOME / "assets"
MARKER_FILE = Path("/var/lib/quantideclaw-onboard/completed")
LOG_FILE = Path("/var/lib/quantideclaw-onboard/setup.log")
MODELS_URL = "https://openrouter.ai/api/v1/models"
CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_THRESHOLD = int(dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc).timestamp())
WELCOME_MESSAGE = "你好， Quantide Claw 欢迎你！"
OPENCLAW_WRAPPER = Path("/usr/local/bin/quantideclaw-openclaw")
DEFAULT_CONTROL_UI_URL = "http://127.0.0.1:18789/"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SHELL_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
REQUEST_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


class WelcomeMessagePending(RuntimeError):
    """Raised when setup is otherwise complete but welcome delivery still needs manual help."""


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned = value.strip()
        if cleaned[:1] in {"'", '"'} and cleaned[-1:] == cleaned[:1]:
            cleaned = cleaned[1:-1]
        env[key.strip()] = cleaned
    return env


def expand_path(value: str) -> Path:
    return Path(value).expanduser()


def deep_merge_dict(base: dict[str, object], updates: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge_dict(current, value)
        else:
            merged[key] = value
    return merged


def migrate_openclaw_config_schema(config: dict[str, object]) -> dict[str, object]:
    migrated = dict(config)
    migrated.pop("profile", None)

    messages = migrated.get("messages")
    if isinstance(messages, dict):
        tts = messages.get("tts")
        if isinstance(tts, dict):
            providers = tts.get("providers")
            if not isinstance(providers, dict):
                providers = {}
            for provider_name in ("openai", "elevenlabs", "microsoft", "edge"):
                provider_config = tts.pop(provider_name, None)
                if isinstance(provider_config, dict) and provider_name not in providers:
                    providers[provider_name] = provider_config
            if providers:
                tts["providers"] = providers

    agents = migrated.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        if isinstance(defaults, dict):
            defaults.pop("tools", None)

        list_entries = agents.get("list")
        if isinstance(list_entries, list):
            normalized_list: list[dict[str, object]] = []
            for index, entry in enumerate(list_entries):
                if not isinstance(entry, dict):
                    continue
                normalized_entry = dict(entry)
                entry_tools = normalized_entry.get("tools")
                if isinstance(entry_tools, dict):
                    entry_tools.pop("browser", None)
                    if entry_tools:
                        normalized_entry["tools"] = entry_tools
                    else:
                        normalized_entry.pop("tools", None)
                fallback_id = "main" if index == 0 else f"agent-{index + 1}"
                entry_id = str(normalized_entry.get("id") or normalized_entry.get("name") or fallback_id).strip()
                normalized_entry["id"] = entry_id or fallback_id
                normalized_list.append(normalized_entry)
            agents["list"] = normalized_list

    return migrated


def parse_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def parse_timestamp(model: dict[str, object]) -> int | None:
    created = model.get("created")
    if isinstance(created, (int, float)):
        return int(created)
    if isinstance(created, str):
        cleaned = created.strip()
        if cleaned.isdigit():
            return int(cleaned)

    slug = str(model.get("canonical_slug") or "")
    match = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", slug)
    if not match:
        return None

    year, month, day = (int(part) for part in match.groups())
    try:
        return int(dt.datetime(year, month, day, tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        return None


def extract_modalities(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {part.lower() for part in re.findall(r"[a-zA-Z]+", value)}
    if isinstance(value, (list, tuple, set)):
        parts: set[str] = set()
        for item in value:
            parts |= extract_modalities(item)
        return parts
    return set()


def is_text_only_model(model: dict[str, object]) -> bool:
    architecture = model.get("architecture") or {}
    if not isinstance(architecture, dict):
        return True

    for field_name in ("modality", "input_modalities", "output_modalities"):
        modalities = extract_modalities(architecture.get(field_name))
        if not modalities:
            continue
        if "text" not in modalities:
            return False
        if modalities - {"text"}:
            return False

    return True


def infer_provider_label(model_id: str) -> str:
    provider = model_id.split("/", 1)[0].strip()
    if not provider:
        return "unknown"
    words = [part for part in re.split(r"[-_]+", provider) if part]
    return " ".join(word.upper() if len(word) <= 2 else word.capitalize() for word in words) or provider


def summarize_http_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        detail = re.sub(r"\s+", " ", detail).strip()
        if detail:
            return f"HTTP {exc.code}: {detail[:160]}"
        return f"HTTP {exc.code}"
    return str(exc)

def normalize_model_id(model_id: str) -> str:
    cleaned = model_id.strip()
    if cleaned.startswith("openrouter/"):
        return cleaned
    return f"openrouter/{cleaned}"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


class FirstBootApp:
    def __init__(self, root: tk.Tk, preview: bool = False) -> None:
        self.preview = preview
        self.root = root
        self.root.title("QuantideClaw 初始化向导")
        self.root.geometry("1120x920")
        self.root.minsize(980, 780)

        self.env = load_env(INSTALLER_ENV)
        self.openclaw_home = expand_path(
            self.env.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))
        )
        self.workspace_dir = expand_path(
            self.env.get("OPENCLAW_WORKSPACE", str(self.openclaw_home / "workspace"))
        )
        self.config_path = expand_path(
            self.env.get("OPENCLAW_CONFIG_PATH", str(self.openclaw_home / "openclaw.json"))
        )
        self.proxy_url = self.env.get("EDGE_TTS_PROXY_URL", "http://127.0.0.1:18792/v1")
        self.proxy_voice = self.env.get("EDGE_TTS_DEFAULT_VOICE", "zh-CN-XiaoxiaoNeural")
        self.browser_status_path = Path(
            self.env.get("CHROME_STATUS_FILE", "/var/lib/quantideclaw-build/browser-status.txt")
        )
        self.weixin_plugin_id = "openclaw-weixin"
        self.weixin_channel = self.env.get("WEIXIN_CHANNEL", "openclaw-weixin")
        self.qq_channel = self.env.get("QQBOT_CHANNEL", "qqbot")
        self.qq_plugin_id = "openclaw-qqbot"

        self.agent_name = tk.StringVar(value="Eve")
        self.user_name = tk.StringVar(value="Quantide")
        self.openrouter_key = tk.StringVar()
        self.model_query = tk.StringVar()
        self.model_id = tk.StringVar()
        self.model_status = tk.StringVar(value="请输入 API Key 后查询免费文本模型。")
        self.model_apply_status = tk.StringVar(value="尚未通过 openclaw 应用模型。")
        self.install_weixin = tk.BooleanVar(value=True)
        self.install_qqbot = tk.BooleanVar(value=False)
        self.weixin_target = tk.StringVar()
        self.qq_target = tk.StringVar()
        self.qq_app_id = tk.StringVar()
        self.qq_app_secret = tk.StringVar()
        self.request_id = tk.StringVar()

        self.model_results: list[dict[str, object]] = []
        self.visible_model_results: list[dict[str, object]] = []
        self.model_query_generation = 0
        self.model_query_in_progress = False
        self.cancelled_model_query_tokens: set[int] = set()
        self.syncing_model_tree_selection = False
        self.openrouter_auto_query_started = False
        self.openrouter_prefilled_from_config = False
        self.openrouter_auto_advance_scheduled = False
        self.model_worker_queue: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.pending_mode = "nodes"
        self.pending_requests: list[dict[str, str]] = []
        self.paired_devices: list[dict[str, str]] = []
        self.pairing_status = tk.StringVar(value="请点击下方按钮刷新配对状态。")
        self.pairing_runtime_signature: str | None = None
        self.setup_completed_signature: str | None = None
        self.images: dict[str, object] = {}
        self.openrouter_step_index = 2
        self.channel_step_index = 3
        self.verification_step_index = 4
        self.weixin_qr_url: str | None = None
        self.weixin_login_requested = False
        self.weixin_login_in_progress = False
        self.weixin_login_generation = 0
        self.weixin_login_process: subprocess.Popen[str] | None = None
        initial_key, initial_model = self._load_existing_openrouter_settings()
        if initial_key:
            self.openrouter_key.set(initial_key)
            self.model_status.set("已从现有配置读取 OpenRouter API Key。")
            self.openrouter_prefilled_from_config = True
        if initial_model:
            self.model_apply_status.set(f"现有默认模型: {initial_model}")

        self.current_step = 0
        self.steps = [
            {"title": "欢迎", "build": self._build_welcome_step},
            {"title": "基础信息", "build": self._build_basic_step},
            {"title": "配置 OpenRouter", "build": self._build_openrouter_step},
            {"title": "渠道接入", "build": self._build_channel_step},
            {"title": "验证", "build": self._build_verification_step},
        ]

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close_app)
        if self.openrouter_prefilled_from_config:
            self.root.after(150, lambda: self._show_step(self.openrouter_step_index))
        self.root.deiconify()

    def _build_ui(self) -> None:
        # Enable DPI awareness for Retina displays
        try:
            # Try to detect and set appropriate scaling
            self.root.tk.call('tk', 'scaling', 1.5)
        except Exception:
            pass
        
        self.root.configure(bg="#f0f0f0")

        # Header
        self.header = tk.Frame(self.root, bg="#e41815", height=60)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)

        self.header_title = tk.Label(
            self.header,
            text="QuantideClaw 初始化向导",
            fg="white",
            bg="#e41815",
            font=("Noto Sans CJK SC", 20, "bold")
        )
        self.header_title.pack(side=tk.LEFT, padx=30, pady=10)

        # Container
        self.container = tk.Frame(self.root, bg="#ffffff")
        self.container.pack(fill=tk.BOTH, expand=True, padx=20, pady=(40, 20))

        # Sidebar (Progress)
        self.sidebar = tk.Frame(self.container, bg="#f8f9fa", width=200)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))
        self.sidebar.pack_propagate(False)

        self.step_labels = []
        for i, step in enumerate(self.steps):
            lbl = tk.Label(
                self.sidebar,
                text=f"{step['title']}",
                bg="#f8f9fa",
                fg="#333333",
                font=("Noto Sans CJK SC", 13),
                anchor="w"
            )
            lbl.pack(fill=tk.X, padx=15, pady=10)
            self.step_labels.append(lbl)

        # Main Content Area with Scrollbar
        self.main_area_outer = tk.Frame(self.container, bg="#ffffff")
        self.main_area_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Create canvas and scrollbar for scrolling support
        self.main_canvas = tk.Canvas(self.main_area_outer, bg="#ffffff", highlightthickness=0)
        self.main_scrollbar = ttk.Scrollbar(self.main_area_outer, orient="vertical", command=self.main_canvas.yview)
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)

        self.main_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.main_area = tk.Frame(self.main_canvas, bg="#ffffff")
        self.main_canvas_window = self.main_canvas.create_window((0, 0), window=self.main_area, anchor="nw")

        # Configure canvas scrolling
        def configure_canvas(event):
            self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))
            self.main_canvas.itemconfig(self.main_canvas_window, width=event.width)

        self.main_area.bind("<Configure>", configure_canvas)
        self.main_canvas.bind("<Configure>", lambda e: self.main_canvas.itemconfig(self.main_canvas_window, width=e.width))

        # Mouse wheel scrolling
        def on_mousewheel(event):
            self.main_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.main_canvas.bind_all("<MouseWheel>", on_mousewheel)

        # Footer
        self.footer = tk.Frame(self.root, bg="#f0f0f0", height=60)
        self.footer.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_frame = tk.Frame(self.footer, bg="#f0f0f0")
        self.btn_frame.pack(side=tk.RIGHT, padx=30, pady=15)

        self.prev_btn = ttk.Button(self.btn_frame, text="上一步", command=self._prev_step)
        self.prev_btn.pack(side=tk.LEFT, padx=10)

        self.next_btn = ttk.Button(self.btn_frame, text="下一步", command=self._next_step)
        self.next_btn.pack(side=tk.LEFT)

        self._show_step(0)

    def _update_sidebar(self):
        for i, lbl in enumerate(self.step_labels):
            if i == self.current_step:
                lbl.configure(fg="#e41815", font=("Noto Sans CJK SC", 12, "bold"))
            elif i < self.current_step:
                lbl.configure(fg="#4caf50", font=("Noto Sans CJK SC", 12))
            else:
                lbl.configure(fg="#333333", font=("Noto Sans CJK SC", 12))

    def _show_step(self, step_idx):
        if self.current_step == self.openrouter_step_index and step_idx != self.openrouter_step_index:
            self._cancel_model_query("已离开 OpenRouter 页面，停止当前模型验证。")
        # Clear main area and reset scroll
        for widget in self.main_area.winfo_children():
            widget.destroy()
        self.main_canvas.yview_moveto(0)

        self.current_step = step_idx
        self._update_sidebar()

        # Build current step content
        self.steps[step_idx]["build"](self.main_area)

        # Update buttons
        if step_idx == 0:
            self.prev_btn.state(["disabled"])
            self.next_btn.state(["!disabled"])
            self.next_btn.configure(text="下一步", command=self._next_step)
        elif step_idx == len(self.steps) - 1:
            self.prev_btn.state(["!disabled"])
            self.next_btn.state(["!disabled"])
            self.next_btn.configure(text="完成", command=self.complete_setup)
        else:
            self.prev_btn.state(["!disabled"])
            self.next_btn.state(["!disabled"])
            self.next_btn.configure(text="下一步", command=self._next_step)
        self._refresh_next_button_state()
        self.root.update_idletasks()
        if step_idx == self.channel_step_index:
            self.root.after(150, self._maybe_start_weixin_login)

    def _maybe_start_weixin_login(self) -> None:
        if self.current_step != self.channel_step_index:
            return
        if not self.install_weixin.get():
            return
        if self.weixin_login_requested or self.weixin_login_in_progress:
            return
        self._do_weixin_login()

    def _next_step(self):
        if self.current_step >= len(self.steps) - 1:
            return
        if self.current_step == self.openrouter_step_index and self.model_id.get().strip():
            self._cancel_model_query("已选择可用模型，停止其余模型验证并进入下一步。")
        if self.current_step == self.channel_step_index and self.install_weixin.get() and not self.weixin_login_requested:
            messagebox.showerror("请先获取二维码", "请先在当前页面获取微信登录二维码，并完成扫码。")
            return
        if self.current_step == self.channel_step_index:
            self.run_setup()
            return
        self._show_step(self.current_step + 1)

    def _refresh_next_button_state(self) -> None:
        if not hasattr(self, "next_btn"):
            return
        if self.current_step != self.openrouter_step_index:
            self.next_btn.state(["!disabled"])
            return
        model_selected = bool(self.model_id.get().strip())
        if not model_selected:
            self.next_btn.state(["disabled"])
        else:
            self.next_btn.state(["!disabled"])

    def _prev_step(self):
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def _build_welcome_step(self, parent):
        tk.Label(
            parent,
            text="欢迎使用 QuantideClaw 初始化向导",
            font=("Noto Sans CJK SC", 18, "bold"),
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=(20, 10))

        tk.Label(
            parent,
            text=(
                "本向导会配置大模型、微信/QQ 机器人。配置好后，就可以通过微信/QQ 来管理 QuantideClaw。\n"
                "在配置完成后，系统会发送一条欢迎消息。\n\n"
                "请点击右下角的「下一步」开始操作。"
            ),
            wraplength=700,
            justify=tk.LEFT,
            font=("Noto Sans CJK SC", 13),
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=10)

        # Browser note removed - not needed for end users

    def _build_basic_step(self, parent):
        tk.Label(
            parent,
            text="基础信息",
            font=("Noto Sans CJK SC", 16, "bold"),
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=(20, 20))

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, padx=20)
        self._labeled_entry(frame, 0, "第一个机器人名字", self.agent_name, 50)
        self._labeled_entry(frame, 1, "使用者（你本人）姓名", self.user_name, 50)

        tk.Label(
            parent,
            text="这些信息将用于初始化配置文件。",
            fg="#888",
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, padx=20, pady=20)

    def _build_openrouter_step(self, parent):
        # Main container with consistent white background
        container = tk.Frame(parent, bg="#ffffff")
        container.pack(fill=tk.BOTH, expand=True, padx=30, pady=(40, 20))

        # Title section with larger top margin
        title_frame = tk.Frame(container, bg="#ffffff")
        title_frame.pack(fill=tk.X, pady=(0, 30))

        tk.Label(
            title_frame,
            text="🔑 配置 OpenRouter",
            font=("Noto Sans CJK SC", 20, "bold"),
            bg="#ffffff",
            fg="#1a1a1a",
        ).pack(anchor=tk.W)

        # Description with better spacing
        desc_frame = tk.Frame(container, bg="#ffffff")
        desc_frame.pack(fill=tk.X, pady=(0, 25))

        tk.Label(
            desc_frame,
            text="QuantideClaw 每天请求超过 1 亿 token，费用非常惊人！不过，好在我们可以使用 OpenRouter 上的免费模型，从而实现免费养虾！现在我们就开始配置！",
            wraplength=700,
            justify=tk.LEFT,
            font=("Noto Sans CJK SC", 13),
            bg="#ffffff",
            fg="#333333",
        ).pack(anchor=tk.W, pady=(0, 8))


        tk.Label(
            desc_frame,
            text="请先填写你的 OpenRouter API Key，然后点击「查询免费模型」。",
            font=("Noto Sans CJK SC", 13),
            bg="#ffffff",
            fg="#666666",
        ).pack(anchor=tk.W)

        # API Key input section with card-like styling
        input_card = tk.Frame(container, bg="#f8f9fa", padx=20, pady=20)
        input_card.pack(fill=tk.X, pady=(0, 20))

        key_row = tk.Frame(input_card, bg="#f8f9fa")
        key_row.pack(fill=tk.X)

        tk.Label(
            key_row,
            text="API Key:",
            font=("Noto Sans CJK SC", 12),
            bg="#f8f9fa",
            fg="#333333",
        ).pack(side=tk.LEFT, padx=(0, 12))

        key_entry = tk.Entry(
            key_row,
            textvariable=self.openrouter_key,
            width=45,
            font=("Monospace", 11),
            bg="#ffffff",
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightcolor="#e41815",
            highlightbackground="#dddddd",
        )
        key_entry.pack(side=tk.LEFT, padx=(0, 12))

        query_btn = tk.Button(
            key_row,
            text="查询免费模型",
            command=self._query_models_with_key,
            bg="#ffffff",
            fg="#e41815",
            font=("Noto Sans CJK SC", 11, "bold"),
            relief=tk.SOLID,
            bd=2,
            highlightthickness=0,
            padx=16,
            pady=6,
            cursor="hand2",
        )
        query_btn.pack(side=tk.LEFT)
        self.model_query_button = query_btn
        if (
            self.openrouter_key.get().strip()
            and not self.model_results
            and not self.model_query_in_progress
            and not self.openrouter_auto_query_started
        ):
            self.openrouter_auto_query_started = True
            self.root.after(200, self._query_models_with_key)

        # Help buttons row
        help_row = tk.Frame(input_card, bg="#f8f9fa")
        help_row.pack(fill=tk.X, pady=(15, 0))

        help_btn = tk.Label(
            help_row,
            text="如何获取 API Key？",
            font=("Noto Sans CJK SC", 11, "underline"),
            bg="#f8f9fa",
            fg="#1a73e8",
            cursor="hand2",
        )
        help_btn.pack(side=tk.LEFT, padx=(0, 20))
        help_btn.bind("<Button-1>", lambda e: self._show_help_dialog())

        open_link = tk.Label(
            help_row,
            text="🌐 去 OpenRouter 官网申请",
            font=("Noto Sans CJK SC", 11, "underline"),
            bg="#f8f9fa",
            fg="#1a73e8",
            cursor="hand2",
        )
        open_link.pack(side=tk.LEFT)
        open_link.bind("<Button-1>", lambda e: self._open_keys_page())

        # Model selection section (hidden initially)
        self.model_frame = tk.Frame(container, bg="#ffffff")
        # Don't pack yet, will be shown when models are loaded

        # Model section header
        model_header = tk.Frame(self.model_frame, bg="#ffffff")
        model_header.pack(fill=tk.X, pady=(20, 15))

        tk.Label(
            model_header,
            text="选择免费模型",
            font=("Noto Sans CJK SC", 16, "bold"),
            bg="#ffffff",
            fg="#1a1a1a",
        ).pack(side=tk.LEFT)

        status_label = tk.Label(
            self.model_frame,
            textvariable=self.model_status,
            font=("Noto Sans CJK SC", 11),
            bg="#ffffff",
            fg="#666666",
            justify=tk.LEFT,
            wraplength=760,
        )
        status_label.pack(anchor=tk.W, pady=(0, 8))

        apply_status_label = tk.Label(
            self.model_frame,
            textvariable=self.model_apply_status,
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#4a4a4a",
            justify=tk.LEFT,
            wraplength=760,
        )
        apply_status_label.pack(anchor=tk.W, pady=(0, 8))

        search_row = tk.Frame(self.model_frame, bg="#ffffff")
        search_row.pack(fill=tk.X, pady=(0, 12))

        tk.Label(
            search_row,
            text="搜索:",
            font=("Noto Sans CJK SC", 11),
            bg="#ffffff",
            fg="#333333",
        ).pack(side=tk.LEFT, padx=(0, 8))

        search_entry = tk.Entry(
            search_row,
            textvariable=self.model_query,
            width=28,
            font=("Noto Sans CJK SC", 11),
        )
        search_entry.pack(side=tk.LEFT, padx=(0, 12))
        search_entry.bind("<KeyRelease>", lambda _event: self._apply_model_search())

        # Hint label
        hint_label = tk.Label(
            self.model_frame,
            text="💡 提示：查询后会按新到旧测试；找到首个通过模型后会自动选中，你也可以单击切换",
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#888888",
        )
        hint_label.pack(anchor=tk.W, pady=(0, 8))

        # Model table using Treeview
        table_frame = tk.Frame(self.model_frame, bg="#ffffff")
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

        # Create Treeview with columns
        columns = ("date", "vendor", "model_id", "test")
        self.model_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=10,
        )

        # Define column headings
        self.model_tree.heading("date", text="发布日期")
        self.model_tree.heading("vendor", text="厂商")
        self.model_tree.heading("model_id", text="模型 ID")
        self.model_tree.heading("test", text="测试结果")

        # Define column widths
        self.model_tree.column("date", width=100, anchor=tk.CENTER)
        self.model_tree.column("vendor", width=120, anchor=tk.W)
        self.model_tree.column("model_id", width=360, anchor=tk.W)
        self.model_tree.column("test", width=220, anchor=tk.W)
        self.model_tree.tag_configure("passed", foreground="#2e7d32")
        self.model_tree.tag_configure("failed", foreground="#b71c1c")
        self.model_tree.tag_configure("pending", foreground="#9e9e9e")

        # Add scrollbar
        tree_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.model_tree.yview)
        self.model_tree.configure(yscrollcommand=tree_scrollbar.set)

        self.model_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Single selection applies the model immediately.
        self.model_tree.bind("<<TreeviewSelect>>", self.on_model_select)

        # If they already fetched models (going back and forth), show the frame
        if self.model_results:
            self.model_frame.pack(fill=tk.BOTH, expand=True)
            self._apply_model_search()

    def _query_models_with_key(self):
        if len(self.openrouter_key.get().strip()) < 10:
            messagebox.showerror("提示", "请先填入有效的 OpenRouter API Key")
            return
        if self.model_query_in_progress:
            messagebox.showinfo("提示", "正在查询并测试模型，请稍候。")
            return

        self.model_frame.pack(fill=tk.BOTH, expand=True)
        self.model_query_generation += 1
        query_token = self.model_query_generation
        self.cancelled_model_query_tokens.discard(query_token)
        self.model_results = []
        self.visible_model_results = []
        self.model_id.set("")
        self.model_status.set("正在查询模型列表...")
        self.model_apply_status.set("尚未通过 openclaw 应用模型。")
        self._apply_model_search(log_summary=False)
        self._set_model_query_busy(True)
        self._refresh_next_button_state()
        self.append_log(">>> 已启动后台模型查询与测试任务")
        threading.Thread(
            target=self._fetch_models_worker,
            args=(query_token, self.openrouter_key.get().strip()),
            daemon=True,
        ).start()
        self.root.after(50, lambda: self._drain_model_worker_queue(query_token))

    def _set_model_query_busy(self, busy: bool) -> None:
        self.model_query_in_progress = busy
        if hasattr(self, "model_query_button"):
            try:
                self.model_query_button.configure(
                    state=tk.DISABLED if busy else tk.NORMAL,
                    text="查询中..." if busy else "查询免费模型",
                )
            except tk.TclError:
                pass
        self._refresh_next_button_state()

    def _is_model_query_cancelled(self, query_token: int) -> bool:
        return query_token in self.cancelled_model_query_tokens

    def _cancel_model_query(self, reason: str) -> None:
        if not self.model_query_in_progress:
            return
        query_token = self.model_query_generation
        self.cancelled_model_query_tokens.add(query_token)
        self._set_model_query_busy(False)
        self.append_log(f">>> {reason}")
        selected_model = self.model_id.get().strip()
        if selected_model:
            self.model_status.set(f"已选中可用模型 {selected_model}，已停止剩余测试。")
        else:
            self.model_status.set("已停止模型测试。")

    def _widget_exists(self, widget: object | None) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _fetch_models_worker(self, query_token: int, api_key: str) -> None:
        self.model_worker_queue.put(("log", query_token, ">>> 正在获取 OpenRouter 免费文本模型列表"))
        try:
            request = urllib.request.Request(
                MODELS_URL,
                headers={"User-Agent": "quantideclaw-onboard/1.0"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            self.model_worker_queue.put(("error", query_token, summarize_http_error(exc)))
            return

        filtered: list[dict[str, object]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            candidate = self._extract_candidate(item)
            if candidate is not None:
                filtered.append(candidate)

        filtered.sort(key=lambda entry: int(entry["created_ts"]), reverse=True)
        self.model_worker_queue.put(("candidates", query_token, filtered))
        if not filtered:
            self.model_worker_queue.put(("done", query_token, 0, 0))
            return

        passed_count = 0
        total = len(filtered)
        tested_count = 0
        cancelled = False
        for index, entry in enumerate(filtered, start=1):
            if self._is_model_query_cancelled(query_token):
                cancelled = True
                break
            model_id = str(entry["id"])
            self.model_worker_queue.put(("progress", query_token, index, total, model_id))
            ok, message = self._probe_model_request(model_id, api_key)
            tested_count = index
            if self._is_model_query_cancelled(query_token):
                cancelled = True
                break
            if ok:
                passed_count += 1
            self.model_worker_queue.put(("result", query_token, index - 1, model_id, ok, message))
            lowered = message.lower()
            if (
                "401" in lowered
                or "unauthorized" in lowered
                or "invalid api key" in lowered
                or "authentication" in lowered
            ):
                break

        event_type = "cancelled" if cancelled else "done"
        self.model_worker_queue.put((event_type, query_token, passed_count, tested_count or total))

    def _drain_model_worker_queue(self, query_token: int) -> None:
        while True:
            try:
                event = self.model_worker_queue.get_nowait()
            except queue.Empty:
                break
            event_type = str(event[0])
            event_token = int(event[1])
            if event_token != self.model_query_generation or event_token != query_token:
                continue
            if self._is_model_query_cancelled(event_token) and event_type not in {"cancelled", "done"}:
                continue
            if event_type == "log":
                self.append_log(str(event[2]))
            elif event_type == "error":
                self._handle_model_query_error(event_token, str(event[2]))
            elif event_type == "candidates":
                self._install_model_candidates(event_token, list(event[2]))
            elif event_type == "progress":
                self._update_model_test_progress(
                    event_token,
                    int(event[2]),
                    int(event[3]),
                    str(event[4]),
                )
            elif event_type == "result":
                self._record_model_test_result(
                    event_token,
                    int(event[2]),
                    str(event[3]),
                    bool(event[4]),
                    str(event[5]),
                )
            elif event_type == "done":
                self._finish_model_query(event_token, int(event[2]), int(event[3]))
            elif event_type == "cancelled":
                self._finish_model_query(event_token, int(event[2]), int(event[3]), cancelled=True)

        if query_token == self.model_query_generation and self.model_query_in_progress:
            self.root.after(50, lambda: self._drain_model_worker_queue(query_token))

    def _handle_model_query_error(self, query_token: int, error: str) -> None:
        if query_token != self.model_query_generation or self._is_model_query_cancelled(query_token):
            return
        self._set_model_query_busy(False)
        self.append_log(f"ERROR: 获取模型列表失败: {error}")
        self.model_status.set(f"模型列表加载失败: {error}")

    def _install_model_candidates(self, query_token: int, filtered: list[dict[str, object]]) -> None:
        if query_token != self.model_query_generation or self._is_model_query_cancelled(query_token):
            return
        self.model_results = filtered
        self.append_log(f">>> 免费文本候选数: {len(filtered)}")
        if not filtered:
            self.model_status.set("当前没有符合条件的免费文本模型。")
        else:
            self.model_status.set(f"已查询到 {len(filtered)} 个免费文本模型，开始逐个测试...")
        self._apply_model_search(log_summary=False)

    def _update_model_test_progress(
        self,
        query_token: int,
        current: int,
        total: int,
        model_id: str,
    ) -> None:
        if query_token != self.model_query_generation or self._is_model_query_cancelled(query_token):
            return
        self.model_status.set(f"正在测试模型 {current}/{total}: {model_id}")
        self.append_log(f">>> 测试模型 {current}/{total}: {model_id}")

    def _record_model_test_result(
        self,
        query_token: int,
        row_index: int,
        model_id: str,
        passed: bool,
        detail: str,
    ) -> None:
        if query_token != self.model_query_generation or self._is_model_query_cancelled(query_token):
            return
        if row_index >= len(self.model_results):
            return
        entry = self.model_results[row_index]
        if str(entry.get("id")) != model_id:
            return
        entry["test_ok"] = passed
        entry["test_status"] = "通过" if passed else "失败"
        entry["test_message"] = detail
        if passed:
            self.append_log(f">>> 模型测试通过: {model_id}")
        else:
            self.append_log(f"WARN: 模型测试失败: {model_id} -> {detail}")
        self._apply_model_search(log_summary=False)
        if passed and self.current_step == self.openrouter_step_index:
            self.model_status.set(
                f"已找到可用模型 {model_id}；你现在可以点击“下一步”，也可以继续等待更多模型验证结果。"
            )

    def _finish_model_query(self, query_token: int, passed_count: int, total: int, *, cancelled: bool = False) -> None:
        if query_token != self.model_query_generation:
            return
        self._set_model_query_busy(False)
        if cancelled:
            self.cancelled_model_query_tokens.discard(query_token)
            return
        if total == 0:
            self.model_status.set("当前没有符合条件的免费文本模型。")
            return
        self.model_status.set(f"测试完成：{passed_count}/{total} 个模型可用。")
        self.append_log(f">>> 测试完成，可用模型数: {passed_count} / {total}")

    def _sync_model_tree_selection(self) -> None:
        model_tree = getattr(self, "model_tree", None)
        if not self._widget_exists(model_tree):
            return
        selected = self.model_id.get().strip()
        try:
            current = model_tree.selection()
        except tk.TclError:
            return
        if not selected:
            if current:
                self.syncing_model_tree_selection = True
                try:
                    model_tree.selection_remove(*current)
                finally:
                    self.syncing_model_tree_selection = False
            return
        try:
            children = model_tree.get_children()
        except tk.TclError:
            return
        for item in children:
            try:
                values = model_tree.item(item).get("values", [])
            except tk.TclError:
                return
            if len(values) >= 3 and str(values[2]) == selected:
                self.syncing_model_tree_selection = True
                try:
                    if tuple(current) != (item,):
                        model_tree.selection_set(item)
                    model_tree.focus(item)
                    model_tree.see(item)
                except tk.TclError:
                    return
                finally:
                    self.syncing_model_tree_selection = False
                return

    def _select_model(self, model_id: str, *, auto_selected: bool = False) -> bool:
        matched_entry = next(
            (entry for entry in self.model_results if str(entry.get("id")) == model_id),
            None,
        )
        if matched_entry is None:
            return False
        if not matched_entry.get("test_ok"):
            self.append_log(f"WARN: 阻止选择测试失败模型: {model_id}")
            return False
        if self.model_id.get().strip() == model_id:
            self._refresh_next_button_state()
            return True
        self.model_id.set(model_id)
        normalized_model_id = normalize_model_id(model_id)
        self.model_apply_status.set(
            f"已选择模型: {normalized_model_id}；将在完成初始化时通过 openclaw 应用"
        )
        self._sync_model_tree_selection()
        self._refresh_next_button_state()
        self.root.update_idletasks()
        if auto_selected:
            self.append_log(f">>> 已自动选择首个可用模型: {model_id}")
        else:
            self.append_log(f">>> 已选择模型: {model_id}")
        if (
            auto_selected
            and self.openrouter_prefilled_from_config
            and self.current_step == self.openrouter_step_index
            and not self.openrouter_auto_advance_scheduled
        ):
            self.openrouter_auto_advance_scheduled = True
            self.append_log(">>> 已自动选择模型，准备进入渠道接入页。")
            self.root.after(300, lambda: self._show_step(self.channel_step_index))
        return True

    def _build_channel_step(self, parent):
        # Main container
        container = tk.Frame(parent, bg="#ffffff")
        container.pack(fill=tk.BOTH, expand=True, padx=30, pady=(40, 20))

        # Title
        tk.Label(
            container,
            text="配置消息渠道",
            font=("Noto Sans CJK SC", 20, "bold"),
            bg="#ffffff",
            fg="#1a1a1a",
        ).pack(anchor=tk.W, pady=(0, 20))

        # Description
        tk.Label(
            container,
            text="QuantideClaw 可以通过微信或 QQ 与你交互。至少启用一个渠道才能完成初始化。",
            font=("Noto Sans CJK SC", 13),
            bg="#ffffff",
            fg="#333333",
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 25))

        # WeChat Section
        wechat_card = tk.Frame(container, bg="#f8f9fa", padx=20, pady=20)
        wechat_card.pack(fill=tk.X, pady=(0, 15))

        # WeChat header with checkbox
        wechat_header = tk.Frame(wechat_card, bg="#f8f9fa")
        wechat_header.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            wechat_header,
            text="微信渠道",
            font=("Noto Sans CJK SC", 15, "bold"),
            bg="#f8f9fa",
            fg="#07c160",  # WeChat green
        ).pack(side=tk.LEFT)

        wechat_check = tk.Checkbutton(
            wechat_header,
            text="启用",
            variable=self.install_weixin,
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            activebackground="#f8f9fa",
            command=self._toggle_weixin_widgets,
        )
        wechat_check.pack(side=tk.LEFT, padx=(15, 0))

        # WeChat content area - QR on left, info on right
        self.weixin_content = tk.Frame(wechat_card, bg="#f8f9fa")
        self.weixin_content.pack(fill=tk.X)

        # Left: QR code display area
        self.weixin_qr_frame = tk.Frame(self.weixin_content, bg="#f8f9fa", width=250, height=280)
        self.weixin_qr_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))
        self.weixin_qr_frame.pack_propagate(False)

        # Right: Info text
        wechat_info = tk.Frame(self.weixin_content, bg="#f8f9fa")
        wechat_info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(
            wechat_info,
            text="使用说明:",
            font=("Noto Sans CJK SC", 12, "bold"),
            bg="#f8f9fa",
            fg="#333333",
        ).pack(anchor=tk.W, pady=(0, 10))

        tk.Label(
            wechat_info,
            text="1. 请使用你的微信（手机扫描左侧的二维码）\n"
                 "2. 后续你还可以添加更多的微信号，跟机器人绑定\n"
                 "3. 二维码有效期较短，请尽快扫描。如过期请点下方的按钮刷新",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#555555",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 14))

        tk.Button(
            wechat_info,
            text="刷新二维码",
            command=self._do_weixin_login,
            bg="#07c160",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 10, "bold"),
            relief=tk.FLAT,
            padx=16,
            pady=6,
            cursor="hand2",
        ).pack(anchor=tk.W)

        # Set initial state
        self._toggle_weixin_widgets()

        # QQ Section
        self._build_qq_section(container)

    def _toggle_weixin_widgets(self) -> None:
        """Enable/disable WeChat widgets based on checkbox state."""
        if not hasattr(self, "weixin_content"):
            return

        enabled = self.install_weixin.get()
        if not enabled:
            self.weixin_login_generation += 1
            self.weixin_login_in_progress = False
            self.weixin_login_requested = False
            self.weixin_qr_url = None
            self._stop_weixin_login_process("已停用微信渠道，停止二维码获取。")

        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in self.weixin_content.winfo_children():
            try:
                widget.configure(state=state)
            except tk.TclError:
                # Some widgets don't support state
                pass
            # Recursively set state for child widgets
            for child in widget.winfo_children():
                try:
                    child.configure(state=state)
                except tk.TclError:
                    pass

        if enabled and self.current_step == self.channel_step_index:
            self.root.after(0, self._maybe_start_weixin_login)

    def _close_app(self) -> None:
        self._stop_weixin_login_process()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _write_completion_marker(self) -> None:
        try:
            if self.preview:
                self.append_log("[PREVIEW] 跳过写入 completed marker")
                return
            MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            MARKER_FILE.write_text("completed\n", encoding="utf-8")
        except PermissionError:
            pass

    def _resolve_control_console_url(self) -> str:
        url = DEFAULT_CONTROL_UI_URL
        host = "127.0.0.1"
        port = 18789
        base_path = "/"
        try:
            if self.config_path.exists():
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    gateway = loaded.get("gateway")
                    if isinstance(gateway, dict):
                        raw_port = gateway.get("port")
                        if isinstance(raw_port, int):
                            port = raw_port
                        elif isinstance(raw_port, str) and raw_port.strip().isdigit():
                            port = int(raw_port.strip())
                        control_ui = gateway.get("controlUi")
                        if isinstance(control_ui, dict):
                            raw_base_path = str(control_ui.get("basePath") or "").strip()
                            if raw_base_path:
                                cleaned = "/" + raw_base_path.strip("/")
                                base_path = f"{cleaned}/"
            url = f"http://{host}:{port}{base_path}"
        except Exception:
            pass
        return url

    def _open_control_console(self) -> None:
        import shutil
        import subprocess

        url = self._resolve_control_console_url()
        browsers = ["firefox", "chromium", "chromium-browser", "google-chrome", "xdg-open"]
        for browser in browsers:
            if shutil.which(browser):
                try:
                    subprocess.Popen([browser, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue
        try:
            webbrowser.open(url)
        except Exception:
            print(f"[LOG] 无法自动打开控制台，请手动访问: {url}")

    def complete_setup(self) -> None:
        self._write_completion_marker()
        self._close_app()
        if not self.preview:
            self._open_control_console()

    def _build_command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({key: value for key, value in self.env.items() if value})
        env.setdefault("HOME", str(Path.home()))
        env["OPENCLAW_CONFIG_PATH"] = str(self.config_path)
        env["OPENCLAW_WORKSPACE"] = str(self.workspace_dir)
        env["TERM"] = env.get("TERM") or "xterm-256color"
        return env

    def _resolve_openclaw_binary(self) -> str:
        if OPENCLAW_WRAPPER.exists():
            return str(OPENCLAW_WRAPPER)
        return "openclaw"

    def _render_shell_token(self, token: str) -> str:
        if SHELL_ENV_ASSIGNMENT_RE.match(token):
            key, value = token.split("=", 1)
            return f"{key}={shell_quote(value)}"
        return shell_quote(token)

    def _rewrite_openclaw_command(self, command: str) -> str:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command

        for index, token in enumerate(tokens):
            if SHELL_ENV_ASSIGNMENT_RE.match(token):
                continue
            if token == "openclaw":
                tokens[index] = self._resolve_openclaw_binary()
                return " ".join(self._render_shell_token(part) for part in tokens)
            break
        return command

    def _build_weixin_login_command(self) -> str:
        login_command = " ".join(
            shell_quote(part)
            for part in [
                self._resolve_openclaw_binary(),
                "channels",
                "login",
                "--channel",
                self.weixin_channel,
            ]
        )
        return f"script -qefc {shell_quote(login_command)} /dev/null"

    def _looks_like_qr_ascii_line(self, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and len(stripped) >= 20 and all(ch in " ▄▀█" for ch in stripped)

    def _strip_ansi(self, text: str) -> str:
        return ANSI_ESCAPE_RE.sub("", text).replace("\r", "")

    def _stop_weixin_login_process(self, reason: str | None = None) -> None:
        process = self.weixin_login_process
        self.weixin_login_process = None
        if process is None or process.poll() is not None:
            return
        if reason:
            self.append_log(f">>> {reason}")
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _show_weixin_login_button(self) -> None:
        """Show the initial login button for WeChat."""
        if not self._widget_exists(getattr(self, "weixin_qr_frame", None)):
            return
        for widget in self.weixin_qr_frame.winfo_children():
            widget.destroy()

        tk.Label(
            self.weixin_qr_frame,
            text="点击按钮获取微信登录二维码",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#333333",
        ).pack(pady=(0, 10))

        login_btn = tk.Button(
            self.weixin_qr_frame,
            text="获取登录二维码",
            command=self._do_weixin_login,
            bg="#07c160",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 11, "bold"),
            relief=tk.FLAT,
            padx=20,
            pady=8,
            cursor="hand2",
        )
        login_btn.pack()

    def _do_weixin_login(self) -> None:
        """Execute WeChat login command and display QR code."""
        if self.weixin_login_in_progress:
            return
        if self.preview:
            self.weixin_login_requested = True
            self.weixin_qr_url = "https://weixin.qq.com/mock-qr-for-preview"
            self._show_weixin_qr_in_frame("https://weixin.qq.com/mock-qr-for-preview")
            return
        self._stop_weixin_login_process("正在重新获取微信二维码，已停止上一轮登录会话。")
        self.weixin_login_in_progress = True
        self.weixin_login_requested = False
        self.weixin_qr_url = None
        self.weixin_login_generation += 1
        login_token = self.weixin_login_generation
        self._show_weixin_login_loading()
        threading.Thread(target=self._fetch_weixin_qr_worker, args=(login_token,), daemon=True).start()

    def _fetch_weixin_qr_worker(self, login_token: int) -> None:
        command = self._build_weixin_login_command()
        env = self._build_command_env()
        self.root.after(0, lambda: self.append_log(">>> 正在获取微信登录二维码..."))
        self.root.after(0, lambda cmd=command: self.append_log(f"$ {cmd}"))

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            self.root.after(0, lambda error=str(exc): self._handle_weixin_qr_failure(login_token, error))
            return

        self.weixin_login_process = process
        deadline = time.monotonic() + 20
        qr_url: str | None = None
        collected_lines: list[str] = []

        try:
            stream = process.stdout
            if stream is None:
                raise RuntimeError("无法读取 openclaw 登录命令输出。")
            while True:
                if login_token != self.weixin_login_generation:
                    self._stop_weixin_login_process()
                    return
                line = stream.readline()
                if line:
                    clean_line = self._strip_ansi(line).strip()
                    if not clean_line:
                        continue
                    collected_lines.append(clean_line)
                    if not self._looks_like_qr_ascii_line(clean_line):
                        self.root.after(0, lambda line=clean_line: self.append_log(f"    {line}"))
                    if qr_url is None:
                        qr_url = self._extract_qr_url("\n".join(collected_lines[-20:]))
                        if qr_url:
                            self.root.after(
                                0,
                                lambda url=qr_url, token=login_token: self._handle_weixin_qr_result(token, url),
                            )
                    continue
                if process.poll() is not None:
                    break
                if qr_url is None and time.monotonic() >= deadline:
                    self._stop_weixin_login_process()
                    self.root.after(
                        0,
                        lambda: self._handle_weixin_qr_failure(
                            login_token,
                            "等待 20 秒仍未检测到二维码链接，请检查 openclaw 输出。",
                        ),
                    )
                    return
                time.sleep(0.05)
        except Exception as exc:
            error_message = f"读取微信登录输出失败: {exc}"
            self.root.after(0, lambda msg=error_message: self._handle_weixin_qr_failure(login_token, msg))
            return
        finally:
            if self.weixin_login_process is process and process.poll() is not None:
                self.weixin_login_process = None

        if login_token != self.weixin_login_generation:
            return
        if qr_url is None:
            summary = "\n".join(collected_lines[-12:])
            error_message = "未能从命令输出中提取二维码链接。"
            if summary:
                error_message = f"{error_message}\n最近输出:\n{summary}"
            self.root.after(0, lambda msg=error_message: self._handle_weixin_qr_failure(login_token, msg))
            return
        return_code = process.returncode or 0
        if return_code not in (0, 124):
            self.root.after(0, lambda code=return_code: self.append_log(f"WARN: 微信登录命令已退出，退出码 {code}"))

    def _handle_weixin_qr_result(self, login_token: int, qr_url: str) -> None:
        if login_token != self.weixin_login_generation:
            return
        self.weixin_login_in_progress = False
        self.weixin_login_requested = True
        self.weixin_qr_url = qr_url
        self.append_log(f">>> 获取到二维码 URL: {qr_url[:80]}")
        self._show_weixin_qr_in_frame(qr_url)

    def _handle_weixin_qr_failure(self, login_token: int, error_message: str) -> None:
        if login_token != self.weixin_login_generation:
            return
        self.weixin_login_in_progress = False
        self.append_log(f"WARN: {error_message}")
        self._show_weixin_login_button()
        if self.current_step == self.channel_step_index:
            messagebox.showerror("获取二维码失败", error_message)

    def _show_weixin_login_loading(self) -> None:
        if not self._widget_exists(getattr(self, "weixin_qr_frame", None)):
            return
        for widget in self.weixin_qr_frame.winfo_children():
            widget.destroy()
        tk.Label(
            self.weixin_qr_frame,
            text="正在获取微信登录二维码...",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#333333",
            wraplength=220,
            justify=tk.CENTER,
        ).pack(pady=(20, 10))
        tk.Label(
            self.weixin_qr_frame,
            text="检测到二维码后会立刻显示，不会等待扫码完成",
            font=("Noto Sans CJK SC", 10),
            bg="#f8f9fa",
            fg="#888888",
            wraplength=220,
            justify=tk.CENTER,
        ).pack()

    def _show_weixin_qr_in_frame(self, qr_url: str) -> None:
        """Display QR code in the WeChat section frame."""
        if not self._widget_exists(getattr(self, "weixin_qr_frame", None)):
            return
        for widget in self.weixin_qr_frame.winfo_children():
            widget.destroy()

        if qrcode is not None and Image is not None and ImageTk is not None:
            try:
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_H,
                    box_size=8,
                    border=4,
                )
                qr.add_data(qr_url)
                qr.make(fit=True)

                qr_img = qr.make_image(fill_color="black", back_color="white")
                qr_img = qr_img.resize((200, 200))
                photo = ImageTk.PhotoImage(qr_img)
                self.images["weixin_qr_current"] = photo

                qr_label = tk.Label(self.weixin_qr_frame, image=photo, bg="#f8f9fa")
                qr_label.pack(pady=(15, 10))
            except Exception as e:
                tk.Label(
                    self.weixin_qr_frame,
                    text=f"二维码生成失败: {e}",
                    fg="red",
                    bg="#f8f9fa",
                ).pack(pady=(20, 0))
        else:
            text_widget = tk.Text(
                self.weixin_qr_frame,
                height=4,
                width=35,
                font=("Monospace", 9),
                wrap=tk.WORD,
            )
            text_widget.insert("1.0", qr_url)
            text_widget.configure(state=tk.DISABLED)
            text_widget.pack(pady=(20, 5))


    def _build_qq_section(self, container):
        """Build QQ section - separated to avoid scope issues."""
        # QQ Section
        qq_card = tk.Frame(container, bg="#f8f9fa", padx=20, pady=20)
        qq_card.pack(fill=tk.X)

        # QQ header with checkbox
        qq_header = tk.Frame(qq_card, bg="#f8f9fa")
        qq_header.pack(fill=tk.X, pady=(0, 15))

        tk.Label(
            qq_header,
            text="QQ Bot 渠道",
            font=("Noto Sans CJK SC", 15, "bold"),
            bg="#f8f9fa",
            fg="#12b7f5",  # QQ blue
        ).pack(side=tk.LEFT)

        qq_check = tk.Checkbutton(
            qq_header,
            text="启用",
            variable=self.install_qqbot,
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            activebackground="#f8f9fa",
            command=self._toggle_qq_widgets,
        )
        qq_check.pack(side=tk.LEFT, padx=(15, 0))

        # QQ content
        self.qq_content = tk.Frame(qq_card, bg="#f8f9fa")
        self.qq_content.pack(fill=tk.X)

        # App ID
        row1 = tk.Frame(self.qq_content, bg="#f8f9fa")
        row1.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            row1,
            text="App ID:",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#333333",
            width=12,
            anchor=tk.W,
        ).pack(side=tk.LEFT)

        self.qq_app_id_entry = tk.Entry(
            row1,
            textvariable=self.qq_app_id,
            width=30,
            font=("Monospace", 11),
            bg="#ffffff",
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightcolor="#12b7f5",
            highlightbackground="#dddddd",
        )
        self.qq_app_id_entry.pack(side=tk.LEFT)

        # App Secret
        row2 = tk.Frame(self.qq_content, bg="#f8f9fa")
        row2.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            row2,
            text="App Secret:",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#333333",
            width=12,
            anchor=tk.W,
        ).pack(side=tk.LEFT)

        self.qq_app_secret_entry = tk.Entry(
            row2,
            textvariable=self.qq_app_secret,
            width=30,
            font=("Monospace", 11),
            show="•",
            bg="#ffffff",
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightcolor="#12b7f5",
            highlightbackground="#dddddd",
        )
        self.qq_app_secret_entry.pack(side=tk.LEFT)

        # QQ help text with link
        help_frame = tk.Frame(self.qq_content, bg="#f8f9fa")
        help_frame.pack(anchor=tk.W, pady=(5, 0))

        tk.Label(
            help_frame,
            text="需要在 ",
            font=("Noto Sans CJK SC", 10),
            bg="#f8f9fa",
            fg="#888888",
        ).pack(side=tk.LEFT)

        qq_link = tk.Label(
            help_frame,
            text="QQ 开放平台",
            font=("Noto Sans CJK SC", 10, "underline"),
            bg="#f8f9fa",
            fg="#12b7f5",
            cursor="hand2",
        )
        qq_link.pack(side=tk.LEFT)
        qq_link.bind("<Button-1>", lambda e: webbrowser.open("https://q.qq.com"))

        tk.Label(
            help_frame,
            text=" 创建 Bot，获取 AppID 和 Secret，并扫描该机器人的二维码添加为好友",
            font=("Noto Sans CJK SC", 10),
            bg="#f8f9fa",
            fg="#888888",
        ).pack(side=tk.LEFT)

        # Set initial state
        self._toggle_qq_widgets()

    def _toggle_qq_widgets(self) -> None:
        """Enable/disable QQ widgets based on checkbox state."""
        if hasattr(self, 'qq_app_id_entry') and hasattr(self, 'qq_app_secret_entry'):
            state = tk.NORMAL if self.install_qqbot.get() else tk.DISABLED
            self.qq_app_id_entry.configure(state=state)
            self.qq_app_secret_entry.configure(state=state)

    def _build_pairing_step(self, parent):
        tk.Frame(parent, height=10).pack()

        frame = tk.Frame(parent, bg="#ffffff")
        frame.pack(fill=tk.BOTH, expand=True, padx=20)

        tk.Label(
            frame,
            text="点击「刷新配对请求」后，向导会先同步当前渠道配置并启动 gateway，再查询最新的待审批请求。待审批设备会以表格方式显示，可逐条审批。",
            wraplength=760,
            justify=tk.LEFT,
            bg="#ffffff",
            relief=tk.FLAT,
        ).grid(row=0, column=0, sticky="w", pady=(0, 15))

        top_frame = tk.Frame(frame, bg="#ffffff")
        top_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        ttk.Button(top_frame, text="刷新配对请求", command=self.refresh_pairings).pack(side=tk.LEFT)
        tk.Label(
            top_frame,
            textvariable=self.pairing_status,
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#666666",
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(12, 0))

        pending_card = ttk.LabelFrame(frame, text="待审批请求", padding=12)
        pending_card.grid(row=2, column=0, sticky="nsew")
        self.pending_table_container = tk.Frame(pending_card, bg="#ffffff")
        self.pending_table_container.pack(fill=tk.BOTH, expand=True)

        paired_card = ttk.LabelFrame(frame, text="已配对设备（参考）", padding=12)
        paired_card.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        tk.Label(
            paired_card,
            text="下方仅展示当前已配对设备供参考；其中 `cli / linux` 通常是这台机器自身的 operator 设备，不代表新的待审批请求。",
            wraplength=760,
            justify=tk.LEFT,
            bg="#ffffff",
            fg="#666666",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=(0, 8))
        self.paired_table_container = tk.Frame(paired_card, bg="#ffffff")
        self.paired_table_container.pack(fill=tk.BOTH, expand=True)

        diagnostics_card = ttk.LabelFrame(frame, text="命令输出与诊断", padding=10)
        diagnostics_card.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        self.pairing_output = scrolledtext.ScrolledText(diagnostics_card, height=8, font=("Monospace", 10))
        self.pairing_output.pack(fill=tk.BOTH, expand=True)
        self.pairing_output.configure(state=tk.DISABLED)

        frame.rowconfigure(2, weight=3)
        frame.rowconfigure(3, weight=2)
        frame.rowconfigure(4, weight=1)
        frame.columnconfigure(0, weight=1)

        self._render_pairing_tables()

    def _clear_pairing_table(self, container: tk.Misc) -> None:
        for widget in container.winfo_children():
            widget.destroy()

    def _render_pairing_tables(self) -> None:
        self._render_pending_request_table()
        self._render_paired_device_table()

    def _render_pending_request_table(self) -> None:
        container = getattr(self, "pending_table_container", None)
        if not self._widget_exists(container):
            return
        self._clear_pairing_table(container)

        if not self.pending_requests:
            tk.Label(
                container,
                text="当前没有待审批请求。若你刚完成扫码或手机端刚发起绑定，请点击上方“刷新配对请求”。",
                font=("Noto Sans CJK SC", 10),
                bg="#ffffff",
                fg="#666666",
                justify=tk.LEFT,
                wraplength=720,
            ).pack(anchor=tk.W)
            return

        headers = ["请求 ID", "来源", "平台 / 客户端", "角色 / 权限", "创建时间", "操作"]
        for column, header in enumerate(headers):
            tk.Label(
                container,
                text=header,
                font=("Noto Sans CJK SC", 10, "bold"),
                bg="#e9f5ee",
                fg="#1f1f1f",
                padx=8,
                pady=8,
                anchor=tk.W,
                relief=tk.GROOVE,
            ).grid(row=0, column=column, sticky="nsew")

        column_weights = [3, 1, 2, 4, 2, 1]
        for column, weight in enumerate(column_weights):
            container.grid_columnconfigure(column, weight=weight)

        for row_index, item in enumerate(self.pending_requests, start=1):
            row_bg = "#ffffff" if row_index % 2 else "#f7fbff"
            values = [
                item.get("id", "-"),
                item.get("source", "待审批"),
                item.get("platform", "-"),
                item.get("roles", "-"),
                item.get("created_at", "-"),
            ]
            wrap_lengths = [220, 120, 160, 320, 140]
            for column, value in enumerate(values):
                tk.Label(
                    container,
                    text=value or "-",
                    font=("Noto Sans CJK SC", 10),
                    bg=row_bg,
                    fg="#333333",
                    justify=tk.LEFT,
                    anchor=tk.W,
                    wraplength=wrap_lengths[column],
                    padx=8,
                    pady=8,
                    relief=tk.GROOVE,
                ).grid(row=row_index, column=column, sticky="nsew")

            tk.Button(
                container,
                text="审批",
                command=lambda request_id=item.get("id", ""): self._approve_request_from_table(request_id),
                bg="#07c160",
                fg="#ffffff",
                font=("Noto Sans CJK SC", 10, "bold"),
                relief=tk.FLAT,
                padx=12,
                pady=5,
                cursor="hand2",
            ).grid(row=row_index, column=5, padx=8, pady=8, sticky="nsew")

    def _render_paired_device_table(self) -> None:
        container = getattr(self, "paired_table_container", None)
        if not self._widget_exists(container):
            return
        self._clear_pairing_table(container)

        if not self.paired_devices:
            tk.Label(
                container,
                text="当前没有已配对设备。",
                font=("Noto Sans CJK SC", 10),
                bg="#ffffff",
                fg="#666666",
            ).pack(anchor=tk.W)
            return

        headers = ["设备 ID", "平台 / 客户端", "角色 / 权限", "配对时间"]
        for column, header in enumerate(headers):
            tk.Label(
                container,
                text=header,
                font=("Noto Sans CJK SC", 10, "bold"),
                bg="#eef3f8",
                fg="#1f1f1f",
                padx=8,
                pady=8,
                anchor=tk.W,
                relief=tk.GROOVE,
            ).grid(row=0, column=column, sticky="nsew")

        for column, weight in enumerate((3, 2, 4, 2)):
            container.grid_columnconfigure(column, weight=weight)

        for row_index, item in enumerate(self.paired_devices, start=1):
            row_bg = "#ffffff" if row_index % 2 else "#f7fbff"
            values = [
                item.get("id", "-"),
                item.get("platform", "-"),
                item.get("roles", "-"),
                item.get("created_at", "-"),
            ]
            wrap_lengths = [220, 180, 320, 140]
            for column, value in enumerate(values):
                tk.Label(
                    container,
                    text=value or "-",
                    font=("Noto Sans CJK SC", 10),
                    bg=row_bg,
                    fg="#333333",
                    justify=tk.LEFT,
                    anchor=tk.W,
                    wraplength=wrap_lengths[column],
                    padx=8,
                    pady=8,
                    relief=tk.GROOVE,
                ).grid(row=row_index, column=column, sticky="nsew")

    def _approve_request_with_fallback(self, request_id: str) -> bool:
        mode = self.pending_mode if hasattr(self, "pending_mode") else "nodes"
        result = self.run_command(
            f"审批配对请求 ({mode})",
            f"openclaw {mode} approve {shell_quote(request_id)}",
            allow_failure=True,
        )
        if result.returncode == 0:
            self.pending_mode = mode
            return True

        fallback_mode = "devices" if mode == "nodes" else "nodes"
        fallback_result = self.run_command(
            f"审批配对请求 ({fallback_mode})",
            f"openclaw {fallback_mode} approve {shell_quote(request_id)}",
            allow_failure=True,
        )
        if fallback_result.returncode == 0:
            self.pending_mode = fallback_mode
            return True
        return False

    def _approve_request_from_table(self, request_id: str) -> None:
        if not request_id:
            messagebox.showerror("错误", "无效的请求 ID，无法审批。")
            return

        if self.preview:
            messagebox.showinfo(
                "审批 (预览模式)",
                f"【预览模式】模拟审批请求: {request_id}\n\n"
                "在真实环境中，这会执行:\n"
                f"openclaw nodes approve {request_id}"
            )
            return

        if self._approve_request_with_fallback(request_id):
            self.append_log(f">>> 已审批请求: {request_id}")
            messagebox.showinfo("成功", f"请求 {request_id} 已审批通过。")
            self.refresh_pairings(prepare_runtime=False)
            return

        messagebox.showerror(
            "审批失败",
            f"无法审批请求: {request_id}\n\n"
            "请确认:\n"
            "1. 该请求仍处于待审批状态\n"
            "2. 绑定请求已在手机端发起\n"
            "3. openclaw 服务是否正常运行"
        )

    def _build_verification_step(self, parent):
        tk.Frame(parent, height=10, bg="#ffffff").pack()

        frame = tk.Frame(parent, bg="#ffffff")
        frame.pack(fill=tk.BOTH, expand=True, padx=20)

        tk.Label(
            frame,
            text="验证",
            font=("Noto Sans CJK SC", 16, "bold"),
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=(0, 18))

        card = tk.Frame(frame, bg="#f8f9fa", padx=24, pady=24)
        card.pack(fill=tk.X, anchor=tk.NW)

        tk.Label(
            card,
            text="这一屏都是提示信息。",
            font=("Noto Sans CJK SC", 13, "bold"),
            bg="#f8f9fa",
            fg="#1f1f1f",
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        tk.Label(
            card,
            text=(
                "我刚刚往你配置的渠道发送了一条『你好， Quantide Claw 欢迎你！』的消息，"
                "请在微信或 QQ 上面接收。\n\n"
                "如果收到，说明配置全部成功完成，你可以正常使用啦！\n\n"
                "如果没有收到，请重试。"
            ),
            wraplength=760,
            justify=tk.LEFT,
            font=("Noto Sans CJK SC", 12),
            bg="#f8f9fa",
            fg="#333333",
        ).pack(anchor=tk.W, pady=(16, 0))

        tk.Label(
            frame,
            text="点击右下角“完成”后，将关闭当前对话框并打开 OpenClaw 控制台。",
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#666666",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(18, 0))

    def _sync_scroll_region(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_canvas_width(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        if self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _labeled_entry(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row, column=1, sticky="ew", pady=6
        )
        parent.columnconfigure(1, weight=1)

    def _add_image_preview(self, parent: tk.Frame, image_path: Path, row: int, width: int) -> None:
        """Add an image preview (legacy method, kept for compatibility)."""
        # This method is kept for compatibility but images are now shown in dialog
        pass

    def _open_url(self, url: str) -> None:
        """Open URL in browser, trying multiple methods."""
        import shutil
        import subprocess

        # Try different browser commands
        browsers = ["firefox", "chromium", "chromium-browser", "google-chrome", "xdg-open"]
        
        for browser in browsers:
            if shutil.which(browser):
                try:
                    subprocess.Popen([browser, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue
        
        # Fallback to webbrowser module
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("打开浏览器失败", f"无法打开浏览器: {e}\n请手动访问: {url}")

    def _open_keys_page(self) -> None:
        self._open_url("https://openrouter.ai/keys")

    def _open_models_page(self) -> None:
        self._open_url("https://openrouter.ai/models")

    def _show_help_dialog(self) -> None:
        """Show help dialog with OpenRouter image."""
        dialog = tk.Toplevel(self.root)
        dialog.title("如何获取 OpenRouter API Key")
        dialog.geometry("700x600")
        dialog.configure(bg="#ffffff")
        dialog.transient(self.root)
        dialog.grab_set()

        # Center the dialog
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (700 // 2)
        y = (dialog.winfo_screenheight() // 2) - (600 // 2)
        dialog.geometry(f"700x600+{x}+{y}")

        # Header
        header = tk.Frame(dialog, bg="#e41815", height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(
            header,
            text="📖 获取 API Key 指南",
            font=("Noto Sans CJK SC", 14, "bold"),
            bg="#e41815",
            fg="#ffffff",
        ).pack(side=tk.LEFT, padx=20, pady=10)

        # Content frame with scrollbar
        content_frame = tk.Frame(dialog, bg="#ffffff")
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Instructions
        tk.Label(
            content_frame,
            text="请按照以下步骤获取你的 OpenRouter API Key:",
            font=("Noto Sans CJK SC", 12),
            bg="#ffffff",
            fg="#333333",
            wraplength=640,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 15))

        steps = [
            "1. 访问 openrouter.ai 并注册账号",
            '2. 登录后点击左侧菜单的 "API Keys"',
            '3. 点击蓝色的 "Create" 按钮',
            "4. 复制生成的 Key 并粘贴到向导中",
        ]
        for step in steps:
            tk.Label(
                content_frame,
                text=step,
                font=("Noto Sans CJK SC", 11),
                bg="#ffffff",
                fg="#555555",
                wraplength=640,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(0, 8))

        # Image
        image_path = ASSET_DIR / "openrouter.jpg"
        if image_path.exists() and Image is not None and ImageTk is not None:
            try:
                img = Image.open(image_path)
                # Resize to fit dialog width (640px)
                ratio = 640 / img.width
                preview = img.resize((640, max(1, int(img.height * ratio))))
                photo = ImageTk.PhotoImage(preview)
                self.images[f"help_{image_path}"] = photo  # Keep reference

                img_label = tk.Label(content_frame, image=photo, bg="#ffffff")
                img_label.pack(pady=(20, 0))
            except Exception as e:
                tk.Label(
                    content_frame,
                    text=f"图片加载失败: {e}",
                    fg="red",
                    bg="#ffffff",
                ).pack(pady=(20, 0))
        else:
            tk.Label(
                content_frame,
                text="（参考图片未找到）",
                fg="#888888",
                bg="#ffffff",
            ).pack(pady=(20, 0))

        # Close button
        btn_frame = tk.Frame(dialog, bg="#f8f9fa", height=60)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        btn_frame.pack_propagate(False)

        close_btn = tk.Button(
            btn_frame,
            text="知道了",
            command=dialog.destroy,
            bg="#e41815",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 11),
            relief=tk.FLAT,
            padx=30,
            pady=6,
            cursor="hand2",
        )
        close_btn.pack(expand=True)

    def show_weixin_qr_dialog(self, qr_data: str) -> None:
        """Show WeChat login QR code in a dialog.
        
        Args:
            qr_data: The QR code content (URL or text)
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("微信登录")
        dialog.geometry("400x500")
        dialog.configure(bg="#ffffff")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Center the dialog
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (400 // 2)
        y = (dialog.winfo_screenheight() // 2) - (500 // 2)
        dialog.geometry(f"400x500+{x}+{y}")

        # Header
        header = tk.Frame(dialog, bg="#07c160", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(
            header,
            text="📱 微信扫码登录",
            font=("Noto Sans CJK SC", 14, "bold"),
            bg="#07c160",
            fg="#ffffff",
        ).pack(expand=True)

        # Content
        content_frame = tk.Frame(dialog, bg="#ffffff")
        content_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=30)

        # Instructions
        tk.Label(
            content_frame,
            text="请使用微信扫描下方二维码登录",
            font=("Noto Sans CJK SC", 12),
            bg="#ffffff",
            fg="#333333",
        ).pack(pady=(0, 20))

        # Generate QR code
        if qrcode is not None and Image is not None and ImageTk is not None:
            try:
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_H,
                    box_size=10,
                    border=4,
                )
                qr.add_data(qr_data)
                qr.make(fit=True)

                # Create PIL image
                qr_img = qr.make_image(fill_color="black", back_color="white")
                
                # Resize to fit dialog
                qr_img = qr_img.resize((300, 300))
                
                # Convert to PhotoImage
                photo = ImageTk.PhotoImage(qr_img)
                self.images["weixin_qr"] = photo  # Keep reference

                qr_label = tk.Label(content_frame, image=photo, bg="#ffffff")
                qr_label.pack(pady=(0, 20))
            except Exception as e:
                tk.Label(
                    content_frame,
                    text=f"二维码生成失败: {e}",
                    fg="red",
                    bg="#ffffff",
                ).pack(pady=(20, 0))
        else:
            # Fallback: show QR data as text
            tk.Label(
                content_frame,
                text="二维码内容：",
                font=("Noto Sans CJK SC", 11),
                bg="#ffffff",
                fg="#333333",
            ).pack(pady=(0, 10))
            
            text_widget = tk.Text(
                content_frame,
                height=8,
                width=40,
                font=("Monospace", 10),
                wrap=tk.WORD,
            )
            text_widget.insert("1.0", qr_data)
            text_widget.configure(state=tk.DISABLED)
            text_widget.pack(pady=(0, 20))
            
            tk.Label(
                content_frame,
                text="（请安装 qrcode 和 pillow 库以显示图形二维码）",
                font=("Noto Sans CJK SC", 9),
                bg="#ffffff",
                fg="#888888",
            ).pack()

        # Hint
        tk.Label(
            content_frame,
            text="登录后，机器人将只与你一人联系",
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#888888",
        ).pack(pady=(10, 0))

        # Close button
        btn_frame = tk.Frame(dialog, bg="#f8f9fa", height=60)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        btn_frame.pack_propagate(False)

        close_btn = tk.Button(
            btn_frame,
            text="我已扫码",
            command=dialog.destroy,
            bg="#07c160",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 11),
            relief=tk.FLAT,
            padx=30,
            pady=6,
            cursor="hand2",
        )
        close_btn.pack(expand=True)

    def _load_browser_note(self) -> str:
        if not self.browser_status_path.exists():
            return ""
        items: list[str] = []
        for raw_line in self.browser_status_path.read_text(encoding="utf-8").splitlines():
            key, _, value = raw_line.partition("=")
            if key and value:
                items.append(f"{key}: {value}")
        if not items:
            return ""
        return "浏览器预装状态：" + "；".join(items)

    def append_log(self, text: str) -> None:
        if hasattr(self, "log"):
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
            self.root.update_idletasks()
        else:
            print(f"[LOG] {text}")

        if self.preview:
            # Skip actual file write entirely under preview to avoid flooding stdout with error
            return

        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except PermissionError:
            # Under preview mode we often don't have access. Do not flood terminal if it fails
            pass

    def append_pairing_output(self, text: str) -> None:
        self.pairing_output.configure(state=tk.NORMAL)
        self.pairing_output.delete("1.0", tk.END)
        self.pairing_output.insert(tk.END, text)
        self.pairing_output.configure(state=tk.DISABLED)

    def fetch_models(self) -> None:
        self._query_models_with_key()

    def _load_existing_openrouter_settings(self) -> tuple[str, str]:
        key = self.env.get("OPENROUTER_API_KEY", "").strip()
        model_id = ""
        if not self.config_path.exists():
            return key, model_id
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return key, model_id
        if not isinstance(loaded, dict):
            return key, model_id
        env_config = loaded.get("env")
        if isinstance(env_config, dict):
            config_key = str(env_config.get("OPENROUTER_API_KEY") or "").strip()
            if config_key:
                key = config_key
        agents = loaded.get("agents")
        if isinstance(agents, dict):
            defaults = agents.get("defaults")
            if isinstance(defaults, dict):
                model_id = str(defaults.get("model") or "").strip()
        return key, model_id

    def _extract_candidate(self, model: dict[str, object]) -> dict[str, object] | None:
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            return None

        if not is_text_only_model(model):
            return None

        pricing = model.get("pricing") or {}
        if not isinstance(pricing, dict):
            return None
        prompt_price = parse_number(pricing.get("prompt"))
        completion_price = parse_number(pricing.get("completion"))
        if completion_price is None:
            completion_price = parse_number(pricing.get("output"))
        if prompt_price is None or completion_price is None:
            return None
        if prompt_price != 0 or completion_price != 0:
            return None

        created_ts = parse_timestamp(model)
        if created_ts is None or created_ts < MODEL_THRESHOLD:
            return None

        created_label = dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        return {
            "id": model_id,
            "name": str(model.get("name") or model_id),
            "family": infer_provider_label(model_id),
            "created_ts": created_ts,
            "created_label": created_label,
            "test_ok": False,
            "test_status": "待测试",
            "test_message": "等待连通性测试",
        }

    def _probe_model_request(self, model_id: str, api_key: str) -> tuple[bool, str]:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        request = urllib.request.Request(
            CHAT_COMPLETIONS_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "quantideclaw-onboard/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                json.load(response)
            return True, "通过"
        except Exception as exc:
            return False, summarize_http_error(exc)

    def _apply_model_with_openclaw(self, model_id: str) -> None:
        normalized_model_id = normalize_model_id(model_id)
        command = (
            f"OPENCLAW_CONFIG_PATH={shell_quote(str(self.config_path))} "
            f"openclaw config set agents.defaults.model {shell_quote(normalized_model_id)}"
        )
        if self.preview:
            self.model_apply_status.set(f"[预览] 将通过 openclaw 应用模型: {normalized_model_id}")
        else:
            self.model_apply_status.set(f"正在通过 openclaw 应用模型: {normalized_model_id}")
        self.run_command("设置默认模型", command)
        if self.preview:
            self.model_apply_status.set(f"[预览] 已模拟应用模型: {normalized_model_id}")
        else:
            self.model_apply_status.set(f"已通过 openclaw 应用模型: {normalized_model_id}")
        self.append_log(f">>> 当前模型已应用: {normalized_model_id}")

    def _apply_model_search(self, log_summary: bool = True) -> None:
        query = self.model_query.get().strip().lower()
        visible = self.model_results
        if query:
            visible = [
                entry
                for entry in self.model_results
                if query in str(entry["id"]).lower()
                or query in str(entry["name"]).lower()
                or query in str(entry["family"]).lower()
            ]

        model_tree = getattr(self, "model_tree", None)
        if self._widget_exists(model_tree):
            try:
                for item in model_tree.get_children():
                    model_tree.delete(item)
                for entry in visible[:200]:
                    test_status = str(entry.get("test_status") or "待测试")
                    row_tag = "passed" if entry.get("test_ok") else "failed"
                    if test_status == "待测试":
                        row_tag = "pending"
                    model_tree.insert(
                        "",
                        tk.END,
                        values=(
                            entry["created_label"],
                            entry["family"],
                            entry["id"],
                            test_status if test_status == "通过" else f"{test_status}: {entry.get('test_message', '')}",
                        ),
                        tags=(row_tag,),
                    )
            except tk.TclError:
                pass

        self.visible_model_results = visible
        if log_summary:
            self.append_log(f">>> 模型候选数: {len(visible)} / {len(self.model_results)}")
        selected = self.model_id.get().strip()
        if selected:
            for entry in self.model_results:
                if str(entry["id"]) == selected and not entry.get("test_ok"):
                    self.model_id.set("")
                    self._refresh_next_button_state()
                    break
        if not self.model_id.get().strip():
            for entry in visible:
                if entry.get("test_ok") and self._select_model(str(entry["id"]), auto_selected=True):
                    break
        self._sync_model_tree_selection()

    def on_model_select(self, _event: object | None = None) -> None:
        """Handle single selection on model tree item."""
        model_tree = getattr(self, "model_tree", None)
        if self.syncing_model_tree_selection or not self._widget_exists(model_tree):
            return
        try:
            selection = model_tree.selection()
        except tk.TclError:
            return
        if selection:
            try:
                item = model_tree.item(selection[0])
            except tk.TclError:
                return
            values = item.get("values", [])
            if values and len(values) >= 3:
                self._select_model(str(values[2]))

    def _extract_qr_url(self, output: str) -> str | None:
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("https://") and (
                "qr" in line.lower() or "login" in line.lower() or "weixin" in line.lower()
            ):
                return line
            if "qr" in line.lower() or "二维码" in line:
                url_match = re.search(r'https?://[^\s<>"\']+', line)
                if url_match:
                    return url_match.group(0)
        return None

    def _fetch_weixin_qr_url(self) -> str | None:
        self.append_log(">>> 正在获取微信登录二维码...")
        login_result = self.run_command(
            "获取微信登录二维码",
            f"openclaw channels login --channel {shell_quote(self.weixin_channel)}",
            allow_failure=True,
            timeout=8,
        )
        combined_output = "\n".join(
            part for part in [login_result.stdout, login_result.stderr] if part and part.strip()
        )
        return self._extract_qr_url(combined_output)

    def run_command(
        self,
        title: str,
        command: str,
        *,
        allow_failure: bool = False,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_command = self._rewrite_openclaw_command(command)
        effective_env = env or self._build_command_env()
        self.append_log(f">>> {title}")
        self.append_log(f"$ {effective_command}")

        if self.preview:
            self.append_log(f"[PREVIEW] 模拟执行 {title} 跳过实际调用")
            return subprocess.CompletedProcess(effective_command, 0, stdout="", stderr="")

        try:
            process = subprocess.run(
                effective_command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=effective_env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            self.append_log(f"WARN: {title} 超时（{timeout}s），将使用已输出内容继续处理")
            process = subprocess.CompletedProcess(effective_command, 124, stdout=stdout, stderr=stderr)

        if process.stdout.strip():
            for line in process.stdout.strip().splitlines()[:40]:
                self.append_log(f"    {line}")
        if process.stderr.strip():
            for line in process.stderr.strip().splitlines()[:40]:
                self.append_log(f"    [stderr] {line}")

        if process.returncode != 0 and not allow_failure:
            raise RuntimeError(f"{title} 失败，退出码 {process.returncode}")
        return process

    def validate_inputs(self) -> None:
        if not self.agent_name.get().strip():
            raise ValueError("请填写第一个 Agent 名称。")
        if not self.user_name.get().strip():
            raise ValueError("请填写使用者姓名。")
        if len(self.openrouter_key.get().strip()) < 12:
            raise ValueError("请填写有效的 OpenRouter API Key。")
        if not self.model_id.get().strip():
            raise ValueError("请先选择或手工填写模型 ID。")
        if not self.install_weixin.get() and not self.install_qqbot.get():
            raise ValueError("至少启用一个渠道。")
        if self.install_qqbot.get() and (
            not self.qq_app_id.get().strip() or not self.qq_app_secret.get().strip()
        ):
            raise ValueError("启用 QQ Bot 时必须填写 App ID 和 App Secret。")

    def write_workspace_files(self, user_name: str, agent_name: str) -> None:
        if self.preview:
            self.append_log(f"[PREVIEW] 写入 USER.md: user_name={user_name}")
            self.append_log(f"[PREVIEW] 写入 IDENTITY.md: agent_name={agent_name}")
            return

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "USER.md").write_text(
            f"# User\n\nName: {user_name}\n",
            encoding="utf-8",
        )
        (self.workspace_dir / "IDENTITY.md").write_text(
            f"# Agent\n\nName: {agent_name}\n",
            encoding="utf-8",
        )

    def write_config(self, user_name: str, agent_name: str, model_id: str, key: str) -> None:
        if self.preview:
            self.append_log(
                f"[PREVIEW] 写入配置文件: user_name={user_name}, agent_name={agent_name}, "
                f"model_id={model_id}, agent_id=main, "
                f"key={key[:4]}...{key[-4:] if len(key)>8 else ''}"
            )
            return

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        existing_config: dict[str, object] = {}
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing_config = migrate_openclaw_config_schema(loaded)
            except (OSError, json.JSONDecodeError):
                self.append_log("WARN: 现有 openclaw.json 无法解析，将覆盖为新的向导配置。")

        gateway_token = ""
        existing_gateway = existing_config.get("gateway")
        if isinstance(existing_gateway, dict):
            existing_auth = existing_gateway.get("auth")
            if isinstance(existing_auth, dict):
                gateway_token = str(existing_auth.get("token") or "").strip()
        if not gateway_token:
            user_config_path = Path.home() / ".openclaw" / "openclaw.json"
            if user_config_path != self.config_path and user_config_path.exists():
                try:
                    loaded = json.loads(user_config_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        gateway = loaded.get("gateway")
                        if isinstance(gateway, dict):
                            auth = gateway.get("auth")
                            if isinstance(auth, dict):
                                gateway_token = str(auth.get("token") or "").strip()
                except (OSError, json.JSONDecodeError):
                    gateway_token = ""
        if not gateway_token:
            gateway_token = secrets.token_hex(24)

        normalized_model_id = normalize_model_id(model_id)
        managed_config: dict[str, object] = {
            "env": {"OPENROUTER_API_KEY": key},
            "tools": {
                "profile": "coding",
                "web": {"search": {"enabled": True, "provider": "duckduckgo"}},
            },
            "gateway": {
                "mode": "local",
                "auth": {
                    "mode": "token",
                    "token": gateway_token,
                },
            },
            "session": {"dmScope": "per-channel-peer"},
            "messages": {
                "tts": {
                    "auto": "always",
                    "mode": "final",
                    "provider": "openai",
                    "providers": {
                        "openai": {
                            "baseUrl": self.proxy_url,
                            "apiKey": "edge-tts-local",
                            "model": "edge-tts",
                            "voice": self.proxy_voice,
                        }
                    },
                }
            },
            "agents": {
                "defaults": {
                    "workspace": str(self.workspace_dir),
                    "model": normalized_model_id,
                },
                "list": [
                    {
                        "id": "main",
                        "default": True,
                        "workspace": str(self.workspace_dir),
                        "model": normalized_model_id,
                        "identity": {"name": agent_name},
                    }
                ],
            },
        }
        config = deep_merge_dict(existing_config, managed_config)
        config = migrate_openclaw_config_schema(config)
        plugins_config = config.get("plugins")
        if not isinstance(plugins_config, dict):
            plugins_config = {}

        allow_list = plugins_config.get("allow")
        if not isinstance(allow_list, list):
            allow_list = []
        allow_list = [
            item for item in allow_list
            if item not in {self.weixin_plugin_id, self.qq_channel, self.qq_plugin_id}
        ]
        if self.install_weixin.get():
            allow_list.append(self.weixin_plugin_id)
        if self.install_qqbot.get():
            allow_list.append(self.qq_plugin_id)
        plugins_config["allow"] = allow_list
        config["plugins"] = plugins_config
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _pairing_runtime_state_signature(self) -> str:
        return json.dumps(
            {
                "agent_name": self.agent_name.get().strip(),
                "user_name": self.user_name.get().strip(),
                "model_id": self.model_id.get().strip(),
                "install_weixin": self.install_weixin.get(),
                "install_qqbot": self.install_qqbot.get(),
                "qq_app_id": self.qq_app_id.get().strip(),
                "qq_app_secret": self.qq_app_secret.get().strip(),
                "openrouter_key": self.openrouter_key.get().strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _ensure_pairing_runtime_ready(self) -> None:
        if self.preview:
            self.pairing_runtime_signature = "preview"
            return

        signature = self._pairing_runtime_state_signature()
        if self.pairing_runtime_signature == signature:
            return

        agent_name = self.agent_name.get().strip()
        user_name = self.user_name.get().strip()
        model_id = self.model_id.get().strip()
        openrouter_key = self.openrouter_key.get().strip()

        if not self.install_weixin.get():
            self.weixin_login_generation += 1
            self.weixin_login_in_progress = False
            self.weixin_login_requested = False
            self.weixin_qr_url = None
            self._stop_weixin_login_process("检测到微信渠道未启用，停止二维码获取。")

        self.append_log(">>> 先同步当前配置并准备配对运行环境")
        self.write_workspace_files(user_name, agent_name)
        self.append_log(">>> 已同步 USER.md 和 IDENTITY.md")
        self.write_config(user_name, agent_name, model_id, openrouter_key)
        self.append_log(f">>> 已同步配置文件: {self.config_path}")
        self._apply_model_with_openclaw(model_id)

        self.run_command(
            "启动本地 gateway 和 Edge-TTS 代理",
            "/usr/local/bin/quantideclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()

        if self.install_qqbot.get():
            token = f"{self.qq_app_id.get().strip()}:{self.qq_app_secret.get().strip()}"
            qq_result = self.run_command(
                "配置 QQ Bot 渠道",
                f"openclaw channels add --channel {shell_quote(self.qq_channel)} --token {shell_quote(token)}",
                allow_failure=True,
            )
            if qq_result.returncode != 0:
                qq_error = (qq_result.stderr or qq_result.stdout or "").strip()
                qq_error_lower = qq_error.lower()
                if qq_error and any(word in qq_error_lower for word in ("already", "exists", "duplicate")):
                    self.append_log(">>> QQ Bot 渠道已存在，继续沿用现有配置")
                elif "已存在" in qq_error:
                    self.append_log(">>> QQ Bot 渠道已存在，继续沿用现有配置")
                else:
                    raise RuntimeError(qq_error or "配置 QQ Bot 渠道失败。")

        if self.install_weixin.get():
            self.append_log(">>> 微信扫码状态沿用渠道接入页当前结果")

        self.run_command(
            "重启 gateway 以加载最新配置",
            "/usr/local/bin/quantideclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()
        self.pairing_runtime_signature = signature

    def _format_pairing_time(self, value: object) -> str:
        timestamp = parse_number(value)
        if timestamp is None:
            return "-"
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return str(value)

    def _stringify_pairing_value(self, value: object) -> str:
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(parts)
        if value is None:
            return ""
        return str(value).strip()

    def _build_pairing_platform_text(self, item: dict[str, object]) -> str:
        parts = [
            self._stringify_pairing_value(item.get("platform")),
            self._stringify_pairing_value(item.get("clientMode") or item.get("clientId")),
        ]
        return " / ".join(part for part in parts if part) or "-"

    def refresh_pairings(self, prepare_runtime: bool = True) -> None:
        if self.preview:
            self.append_log("[PREVIEW] 跳过刷新设备调用")
            self.pending_mode = "nodes"
            self.pending_requests = [
                {
                    "id": "preview-dummy-request-id-123456",
                    "source": "nodes pending",
                    "platform": "android / weixin",
                    "roles": "operator；operator.admin, operator.pairing",
                    "created_at": "预览模式",
                    "summary": "模拟的配对请求",
                }
            ]
            self.paired_devices = [
                {
                    "id": "preview-local-operator-device",
                    "platform": "linux / cli",
                    "roles": "operator；operator.admin, operator.pairing",
                    "created_at": "预览模式",
                    "summary": "模拟的已配对设备",
                }
            ]
            self.pairing_status.set("预览模式：显示 1 个待审批请求。")
            self.append_pairing_output("模拟诊断输出：\n- nodes pending --json -> 1 item\n- devices list --json -> pending=1, paired=1")
            self._render_pairing_tables()
            return

        self.pairing_status.set("正在刷新配对状态...")
        self.root.update_idletasks()
        if prepare_runtime:
            self._ensure_pairing_runtime_ready()

        diagnostics: list[str] = []
        pending_requests: list[dict[str, str]] = []
        paired_devices: list[dict[str, str]] = []
        mode = "nodes"

        devices_json = self.run_command(
            "查询设备列表 JSON",
            "openclaw devices list --json",
            allow_failure=True,
        )
        devices_payload: object | None = None
        devices_stdout = devices_json.stdout.strip()
        if devices_json.returncode == 0 and devices_stdout:
            diagnostics.append("devices list --json:\n" + devices_stdout)
            try:
                devices_payload = json.loads(devices_stdout)
            except json.JSONDecodeError:
                self.append_log("WARN: devices list --json 输出不是合法 JSON，已退回诊断文本展示")
        elif devices_json.stderr.strip():
            diagnostics.append("devices list --json stderr:\n" + devices_json.stderr.strip())

        if isinstance(devices_payload, dict):
            pending_requests = self._normalize_request_payload(
                devices_payload.get("pending", []),
                source_label="devices pending",
            )
            paired_devices = self._normalize_paired_payload(devices_payload.get("paired", []))
            if pending_requests:
                mode = "devices"

        nodes_json = self.run_command(
            "查询待审批请求 JSON",
            "openclaw nodes pending --json",
            allow_failure=True,
        )
        nodes_stdout = nodes_json.stdout.strip()
        if nodes_json.returncode == 0 and nodes_stdout:
            diagnostics.append("nodes pending --json:\n" + nodes_stdout)
            try:
                nodes_payload = json.loads(nodes_stdout)
            except json.JSONDecodeError:
                self.append_log("WARN: nodes pending --json 输出不是合法 JSON，已退回诊断文本展示")
            else:
                node_requests = self._normalize_request_payload(nodes_payload, source_label="nodes pending")
                if node_requests:
                    pending_requests = node_requests
                    mode = "nodes"
        elif nodes_json.stderr.strip():
            diagnostics.append("nodes pending --json stderr:\n" + nodes_json.stderr.strip())

        if not pending_requests:
            nodes_text = self.run_command(
                "查询待审批请求文本输出",
                "openclaw nodes pending",
                allow_failure=True,
            )
            nodes_raw = nodes_text.stdout.strip() or nodes_text.stderr.strip()
            if nodes_raw:
                diagnostics.append("nodes pending:\n" + nodes_raw)
                request_ids = REQUEST_ID_RE.findall(nodes_raw)
                if request_ids:
                    pending_requests = [
                        {
                            "id": item,
                            "source": "nodes pending",
                            "platform": "-",
                            "roles": "待解析",
                            "created_at": "-",
                            "summary": nodes_raw,
                        }
                        for item in request_ids
                    ]
                    mode = "nodes"

        self.pending_mode = mode
        self.pending_requests = pending_requests
        self.paired_devices = paired_devices
        self.request_id.set(pending_requests[0]["id"] if len(pending_requests) == 1 else "")

        if pending_requests:
            self.pairing_status.set(
                f"找到 {len(pending_requests)} 个待审批请求；已配对设备 {len(paired_devices)} 台。"
            )
            self.append_log(f">>> 找到 {len(pending_requests)} 个待审批请求，模式={mode}")
        elif paired_devices:
            self.pairing_status.set(
                f"当前没有待审批请求；已配对设备 {len(paired_devices)} 台。"
            )
            self.append_log(
                ">>> 当前没有待审批请求；下方已配对设备通常包含本机自身的 cli/operator 设备。"
            )
        else:
            self.pairing_status.set("当前没有待审批请求，也没有已配对设备。")
            self.append_log(">>> 当前未检测到可审批 request id，请先在手机端发起绑定，再点一次刷新")

        self._render_pairing_tables()
        self.append_pairing_output("\n\n".join(diagnostics) if diagnostics else "暂无额外诊断输出。")

    def _normalize_request_payload(self, payload: object, *, source_label: str) -> list[dict[str, str]]:
        items: list[object] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            for key in ("data", "items", "pending", "requests", "devices"):
                value = payload.get(key)
                if isinstance(value, list):
                    items = value
                    break
            if not items and any(
                key in payload
                for key in (
                    "requestId",
                    "request_id",
                    "id",
                    "deviceId",
                    "device_id",
                )
            ):
                items = [payload]

        normalized: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            request_id = self._stringify_pairing_value(
                item.get("requestId")
                or item.get("request_id")
                or item.get("id")
                or item.get("deviceId")
                or item.get("device_id")
            )
            if not request_id:
                continue
            role_text = self._stringify_pairing_value(item.get("roles") or item.get("role"))
            scopes_text = self._stringify_pairing_value(item.get("scopes"))
            normalized.append(
                {
                    "id": request_id,
                    "source": self._stringify_pairing_value(item.get("channel") or item.get("source")) or source_label,
                    "platform": self._build_pairing_platform_text(item),
                    "roles": "；".join(part for part in (role_text, scopes_text) if part) or "-",
                    "created_at": self._format_pairing_time(
                        item.get("createdAtMs") or item.get("createdAt") or item.get("requestedAtMs") or item.get("requestedAt")
                    ),
                    "summary": json.dumps(item, ensure_ascii=False, indent=2),
                }
            )
        return normalized

    def _normalize_paired_payload(self, payload: object) -> list[dict[str, str]]:
        items = payload if isinstance(payload, list) else []
        normalized: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            device_id = self._stringify_pairing_value(item.get("deviceId") or item.get("id"))
            if not device_id:
                continue
            role_text = self._stringify_pairing_value(item.get("roles") or item.get("role"))
            scopes_text = self._stringify_pairing_value(item.get("scopes"))
            normalized.append(
                {
                    "id": device_id,
                    "platform": self._build_pairing_platform_text(item),
                    "roles": "；".join(part for part in (role_text, scopes_text) if part) or "-",
                    "created_at": self._format_pairing_time(item.get("approvedAtMs") or item.get("createdAtMs") or item.get("createdAt")),
                    "summary": json.dumps(item, ensure_ascii=False, indent=2),
                }
            )
        return normalized

    def approve_request(self, request_id: str) -> None:
        if self.preview:
            self.append_log(f"[PREVIEW] 审批请求 ID: {request_id}")
            return

        if not self._approve_request_with_fallback(request_id):
            raise RuntimeError("审批配对请求失败，请确认 request id 是否正确。")

    def send_welcome(self) -> bool:
        """Send welcome message to connected channels.
        
        WeChat: After QR code login, the bot is bound to the user who scanned it (1-on-1)
        QQ: The bot automatically becomes friends with the QQ user who applied for the AppID (1-on-1)
        
        Both channels don't require specifying a target - just send to the connected peer.
        """
        delivered = False
        channels = []
        
        if self.install_weixin.get():
            channels.append(self.weixin_channel)
        if self.install_qqbot.get():
            channels.append(self.qq_channel)

        for channel in channels:
            # Try to send welcome message to the connected peer
            # For WeChat/QQ with 1-on-1 binding, we don't need to specify target
            command = (
                f"openclaw message send --channel {shell_quote(channel)} "
                f"--message {shell_quote(WELCOME_MESSAGE)}"
            )
            result = self.run_command(
                f"向 {channel} 发送欢迎消息",
                command,
                allow_failure=True,
            )
            if result.returncode == 0:
                delivered = True
                self.append_log(f">>> 欢迎消息已通过 {channel} 发送成功")
            else:
                self.append_log(f"WARN: 通过 {channel} 发送欢迎消息失败")

        return delivered

    def show_support_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("欢迎消息未发送成功")
        dialog.geometry("520x640")
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text="欢迎消息未能成功发送。请先添加微信后重试。",
            wraplength=460,
            justify=tk.LEFT,
            font=("Noto Sans CJK SC", 12, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="添加完成后重新运行 /usr/local/bin/quantideclaw-onboard，即可再次尝试发送欢迎消息。",
            wraplength=460,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 12))

        if (ASSET_DIR / "quantfans.png").exists():
            image_frame = ttk.Frame(frame)
            image_frame.pack(anchor=tk.W)
            self._add_image_preview(image_frame, ASSET_DIR / "quantfans.png", row=0, width=280)

        ttk.Button(frame, text="关闭", command=dialog.destroy).pack(anchor=tk.E, pady=(12, 0))

    def wait_for_gateway(self) -> None:
        if self.preview:
            self.append_log("[PREVIEW] 跳过等待 gateway")
            return

        last_failure: str | None = None
        for attempt in range(1, 7):
            self.append_log(f">>> 检查 gateway 状态 (第 {attempt}/6 次)")
            result = self.run_command(
                "检查 gateway 状态",
                "openclaw health",
                allow_failure=True,
                timeout=5,
            )
            if result.returncode == 0:
                return
            last_output = (result.stderr or result.stdout or "").strip()
            if last_output:
                last_failure = last_output.splitlines()[-1]
            time.sleep(1)
        if last_failure:
            raise RuntimeError(f"QuantideClaw gateway 未能启动: {last_failure}")
        raise RuntimeError("QuantideClaw gateway 未能在预期时间内启动。")

    def run_setup(self) -> None:
        signature = self._pairing_runtime_state_signature()

        self.prev_btn.state(["disabled"])
        self.next_btn.state(["disabled"])
        self.root.update_idletasks()

        try:
            self.validate_inputs()
            self.execute_setup()
        except WelcomeMessagePending as exc:
            self.setup_completed_signature = None
            self.append_log(f"INFO: {exc}")
            messagebox.showwarning("欢迎消息未送达", str(exc))
        except Exception as exc:
            self.setup_completed_signature = None
            self.append_log(f"ERROR: {exc}")
            messagebox.showerror("初始化失败", str(exc))
        else:
            self.setup_completed_signature = signature
            self._show_step(self.verification_step_index)
        finally:
            if self.current_step != self.verification_step_index:
                if self.current_step == 0:
                    self.prev_btn.state(["disabled"])
                else:
                    self.prev_btn.state(["!disabled"])
                self._refresh_next_button_state()

    def execute_setup(self) -> None:
        self.append_log("=" * 64)
        if self.preview:
            self.append_log("开始执行 QuantideClaw 初始化流程 [预览模式]")
        else:
            self.append_log("开始执行 QuantideClaw 初始化流程")
        self.append_log("=" * 64)

        self._ensure_pairing_runtime_ready()
        self.run_command(
            "最终重启 gateway",
            "/usr/local/bin/quantideclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()

        if self.preview:
            self.append_log("[PREVIEW] 跳过欢迎消息发送检查，认为成功。")
            delivered = True
        else:
            delivered = self.send_welcome()

        if not delivered:
            self.show_support_dialog()
            raise WelcomeMessagePending("欢迎消息发送失败，请回到上一页确认渠道配置后重试。")

        self.append_log("=" * 64)
        self.append_log("初始化流程执行完成")
        self.append_log("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantideClaw Onboard Wizard")
    parser.add_argument("--preview", action="store_true", help="Run without making actual system changes (Preview Mode)")
    args = parser.parse_args()

    try:
        if MARKER_FILE.exists() and not args.preview:
            print("QuantideClaw onboard already completed. Exiting.")
            return 0
    except PermissionError:
        pass

    root = tk.Tk()
    try:
        style = ttk.Style()
        style.configure(".", font=("Noto Sans CJK SC", 10))
    except Exception:
        pass

    FirstBootApp(root, preview=args.preview)
    root.lift()
    root.attributes('-topmost', True)
    root.after_idle(root.attributes, '-topmost', False)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
