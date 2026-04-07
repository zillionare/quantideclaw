#!/usr/bin/env python3
"""OpenClaw first-boot setup wizard."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import secrets
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - environment fallback only
    Image = None
    ImageTk = None

try:
    import qrcode
except ImportError:
    qrcode = None

INSTALLER_ENV = Path("/opt/openclaw-firstboot/installer.env")
APP_HOME = Path("/opt/openclaw-firstboot")
if (Path(__file__).resolve().parent / "openrouter.jpg").exists():
    ASSET_DIR = Path(__file__).resolve().parent
else:
    ASSET_DIR = APP_HOME / "assets"
MARKER_FILE = Path("/var/lib/openclaw-firstboot/completed")
LOG_FILE = Path("/var/lib/openclaw-firstboot/setup.log")
MODELS_URL = "https://openrouter.ai/api/v1/models"
MODEL_THRESHOLD = int(dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc).timestamp())
WELCOME_MESSAGE = "配置完成，欢迎使用。"
MODEL_PREFIXES = {
    "qwen/": "Qwen",
    "xiaomi/": "Xiaomi",
    "stepfun/": "StepFun",
    "moonshotai/": "Moonshot",
    "z-ai/": "Z.AI",
    "meta-llama/": "Meta Llama",
    "google/": "Google",
}
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
        self.root.title("OpenClaw 初始化向导")
        self.root.geometry("1120x920")
        self.root.minsize(980, 780)

        self.env = load_env(INSTALLER_ENV)
        self.openclaw_home = Path(self.env.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
        self.workspace_dir = Path(
            self.env.get("OPENCLAW_WORKSPACE", str(self.openclaw_home / "workspace"))
        )
        self.config_path = Path(
            self.env.get("OPENCLAW_CONFIG_PATH", str(self.openclaw_home / "openclaw.json"))
        )
        self.proxy_url = self.env.get("EDGE_TTS_PROXY_URL", "http://127.0.0.1:18792/v1")
        self.proxy_voice = self.env.get("EDGE_TTS_DEFAULT_VOICE", "zh-CN-XiaoxiaoNeural")
        self.browser_status_path = Path(
            self.env.get("CHROME_STATUS_FILE", "/var/lib/openclaw-build/browser-status.txt")
        )
        self.weixin_channel = self.env.get("WEIXIN_CHANNEL", "openclaw-weixin")
        self.qq_channel = self.env.get("QQBOT_CHANNEL", "qqbot")

        self.agent_name = tk.StringVar(value="Eve")
        self.user_name = tk.StringVar(value="Quantide")
        self.openrouter_key = tk.StringVar()
        self.model_query = tk.StringVar()
        self.model_id = tk.StringVar()
        self.install_weixin = tk.BooleanVar(value=True)
        self.install_qqbot = tk.BooleanVar(value=False)
        self.weixin_target = tk.StringVar()
        self.qq_target = tk.StringVar()
        self.qq_app_id = tk.StringVar()
        self.qq_app_secret = tk.StringVar()
        self.request_id = tk.StringVar()

        self.model_results: list[dict[str, object]] = []
        self.visible_model_results: list[dict[str, object]] = []
        self.pending_mode = "nodes"
        self.pending_requests: list[dict[str, str]] = []
        self.images: dict[str, object] = {}

        self.current_step = 0
        self.steps = [
            {"title": "欢迎", "build": self._build_welcome_step},
            {"title": "基础信息", "build": self._build_basic_step},
            {"title": "配置 OpenRouter", "build": self._build_openrouter_step},
            {"title": "渠道接入", "build": self._build_channel_step},
            {"title": "设备配对审批", "build": self._build_pairing_step},
            {"title": "执行", "build": self._build_execute_step},
        ]

        self._build_ui()
        # Moved fetch_models until the user clicks the query button to enforce they enter a key first
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
            text="OpenClaw 初始化向导",
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
            self.next_btn.configure(text="退出", command=self.root.destroy)
        else:
            self.prev_btn.state(["!disabled"])
            self.next_btn.state(["!disabled"])
            self.next_btn.configure(text="下一步", command=self._next_step)
        self.root.update_idletasks()

    def _next_step(self):
        if self.current_step < len(self.steps) - 1:
            # Add validation logic here before proceeding if needed
            self._show_step(self.current_step + 1)

    def _prev_step(self):
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def _build_welcome_step(self, parent):
        tk.Label(
            parent,
            text="欢迎使用 OpenClaw 初始化向导",
            font=("Noto Sans CJK SC", 18, "bold"),
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=(20, 10))

        tk.Label(
            parent,
            text=(
                "本向导会配置大模型、微信/QQ 机器人。配置好后，就可以通过微信/QQ 来管理 OpenClaw。\n"
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
            text="OpenClaw 每天请求超过 1 亿 token，费用非常惊人！不过，好在我们可以使用 OpenRouter 上的免费模型，从而实现免费养虾！现在我们就开始配置！",
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

        # Search bar with modern styling
        # search_card = tk.Frame(self.model_frame, bg="#f8f9fa", padx=15, pady=12)
        # search_card.pack(fill=tk.X, pady=(0, 10))

        # search_inner = tk.Frame(search_card, bg="#f8f9fa")
        # search_inner.pack(fill=tk.X)

        # Hint label
        hint_label = tk.Label(
            self.model_frame,
            text="💡 提示：单击表格中的模型进行选择",
            font=("Noto Sans CJK SC", 10),
            bg="#ffffff",
            fg="#888888",
        )
        hint_label.pack(anchor=tk.W, pady=(0, 8))

        # Model table using Treeview
        table_frame = tk.Frame(self.model_frame, bg="#ffffff")
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

        # Create Treeview with columns
        columns = ("date", "vendor", "model_id")
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

        # Define column widths
        self.model_tree.column("date", width=100, anchor=tk.CENTER)
        self.model_tree.column("vendor", width=120, anchor=tk.W)
        self.model_tree.column("model_id", width=400, anchor=tk.W)

        # Add scrollbar
        tree_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.model_tree.yview)
        self.model_tree.configure(yscrollcommand=tree_scrollbar.set)

        self.model_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind double-click event
        self.model_tree.bind("<Double-1>", self.on_model_double_click)

        # If they already fetched models (going back and forth), show the frame
        if self.model_results:
            self.model_frame.pack(fill=tk.BOTH, expand=True)
            self._apply_model_search()

    def _query_models_with_key(self):
        # We can implement key validation here if needed, but openrouter models list is public
        # Still, we want them to enter a key first per requirement
        if len(self.openrouter_key.get().strip()) < 10:
            messagebox.showerror("提示", "请先填入有效的 OpenRouter API Key")
            return

        self.model_frame.pack(fill=tk.BOTH, expand=True) # Show the frame
        self.fetch_models()

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
            text="OpenClaw 可以通过微信或 QQ 与你交互。至少启用一个渠道才能完成初始化。",
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

        # Load QR code immediately
        self._do_weixin_login()

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
            text="1. 点击左侧按钮获取二维码\n"
                 "2. 使用你的微信（手机）扫描二维码\n"
                 "3. 扫码后，机器人将只与你一人联系",
            font=("Noto Sans CJK SC", 11),
            bg="#f8f9fa",
            fg="#555555",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 10))

        tk.Label(
            wechat_info,
            text="提示：二维码有效期较短，请尽快扫描",
            font=("Noto Sans CJK SC", 10),
            bg="#f8f9fa",
            fg="#07c160",
        ).pack(anchor=tk.W)

        # Set initial state
        self._toggle_weixin_widgets()

        # QQ Section
        self._build_qq_section(container)

    def _toggle_weixin_widgets(self) -> None:
        """Enable/disable WeChat widgets based on checkbox state."""
        if hasattr(self, 'weixin_content'):
            state = tk.NORMAL if self.install_weixin.get() else tk.DISABLED
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

    def _show_weixin_login_button(self) -> None:
        """Show the initial login button for WeChat."""
        # Clear frame
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
        if self.preview:
            # Preview mode: show mock QR
            self._show_weixin_qr_in_frame("https://weixin.qq.com/mock-qr-for-preview")
            return

        # Run login command
        self.append_log(">>> 正在获取微信登录二维码...")
        login_result = self.run_command(
            "获取微信登录二维码",
            f"openclaw channels login --channel {shell_quote(self.weixin_channel)}",
            allow_failure=True,
        )

        # Try to extract QR code URL from output
        qr_url = None
        if login_result.returncode == 0:
            for line in login_result.stdout.splitlines():
                line = line.strip()
                if line.startswith("https://"):
                    qr_url = line
                    break
                # Check for QR data in output
                if "qr" in line.lower() or "二维码" in line:
                    import re as re_module
                    url_match = re_module.search(r'https?://[^\s<>"\']+', line)
                    if url_match:
                        qr_url = url_match.group(0)
                        break

        if qr_url:
            self.append_log(f">>> 获取到二维码 URL: {qr_url[:50]}...")
            self._show_weixin_qr_in_frame(qr_url)
        else:
            self.append_log("WARN: 未能获取二维码，请检查命令输出")
            messagebox.showerror(
                "获取二维码失败",
                "未能从命令输出中获取二维码。\n"
                "请确保 openclaw channels login 命令正常工作。",
            )

    def _show_weixin_qr_in_frame(self, qr_url: str) -> None:
        """Display QR code in the WeChat section frame."""
        # Clear frame
        for widget in self.weixin_qr_frame.winfo_children():
            widget.destroy()

        # Generate and display QR code
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
            # Fallback: show URL as text
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

        # Add refresh button
        refresh_btn = tk.Button(
            self.weixin_qr_frame,
            text="重新获取二维码",
            command=self._do_weixin_login,
            bg="#07c160",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 10),
            relief=tk.FLAT,
            padx=15,
            pady=5,
            cursor="hand2",
        )
        refresh_btn.pack(pady=(5, 0))

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
            text="需要先在 ",
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
            text=" 创建 Bot 获取 App ID 和 Secret。",
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
            text="请点击「刷新」以获取最新配对请求",
            wraplength=700,
            justify=tk.LEFT,
            bg="#ffffff",
            relief=tk.FLAT,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 15))

        # Top frame with refresh button, Request ID input, and Approve button
        top_frame = tk.Frame(frame, bg="#ffffff")
        top_frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        ttk.Button(top_frame, text="刷新设备列表", command=self.refresh_pairings).pack(side=tk.LEFT, padx=(0, 15))
        tk.Label(top_frame, text="Request ID:", bg="#ffffff", relief=tk.FLAT).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Entry(top_frame, textvariable=self.request_id, width=40).pack(side=tk.LEFT, padx=(0, 15))
        
        # Add Approve button
        approve_btn = tk.Button(
            top_frame,
            text="审批通过",
            command=self.approve_selected_request,
            bg="#07c160",
            fg="#ffffff",
            font=("Noto Sans CJK SC", 10, "bold"),
            relief=tk.FLAT,
            padx=15,
            pady=4,
            cursor="hand2",
        )
        approve_btn.pack(side=tk.LEFT)

        self.pairing_output = scrolledtext.ScrolledText(frame, height=12, font=("Monospace", 10))
        self.pairing_output.grid(row=2, column=0, columnspan=4, sticky="nsew")
        self.pairing_output.configure(state=tk.DISABLED)

        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

    def approve_selected_request(self) -> None:
        """Approve the selected pairing request."""
        request_id = self.request_id.get().strip()
        if not request_id:
            messagebox.showerror("错误", "请先输入或选择 Request ID")
            return

        if self.preview:
            messagebox.showinfo(
                "审批 (预览模式)",
                f"【预览模式】模拟审批请求: {request_id}\n\n"
                "在真实环境中，这会执行:\n"
                f"openclaw nodes approve {request_id}"
            )
            return

        # Determine mode based on pending_requests or try both
        mode = self.pending_mode if hasattr(self, 'pending_mode') else "nodes"
        
        # Try to approve
        result = self.run_command(
            f"审批配对请求 ({mode})",
            f"openclaw {mode} approve {shell_quote(request_id)}",
            allow_failure=True,
        )

        if result.returncode == 0:
            messagebox.showinfo("成功", f"Request ID {request_id} 已审批通过！")
            self.append_pairing_output(f"\n✓ 已审批: {request_id}")
        else:
            # Try alternative mode
            fallback_mode = "devices" if mode == "nodes" else "nodes"
            fallback_result = self.run_command(
                f"审批配对请求 ({fallback_mode})",
                f"openclaw {fallback_mode} approve {shell_quote(request_id)}",
                allow_failure=True,
            )
            if fallback_result.returncode == 0:
                messagebox.showinfo("成功", f"Request ID {request_id} 已审批通过！")
                self.append_pairing_output(f"\n✓ 已审批: {request_id}")
                self.pending_mode = fallback_mode
            else:
                messagebox.showerror(
                    "审批失败",
                    f"无法审批 Request ID: {request_id}\n\n"
                    "请确认:\n"
                    "1. Request ID 是否正确\n"
                    "2. 是否已在手机端发起绑定请求\n"
                    "3. openclaw 服务是否正常运行"
                )

    def _build_execute_step(self, parent):
        tk.Frame(parent, height=10).pack()

        frame = tk.Frame(parent, bg="#ffffff")
        frame.pack(fill=tk.BOTH, expand=True, padx=20)

        action_frame = tk.Frame(frame, bg="#ffffff")
        action_frame.pack(fill=tk.X, pady=(0, 15))

        self.start_button = tk.Button(
            action_frame,
            text="开始" + ("预览" if self.preview else "初始化"),
            command=self.run_setup,
            bg="#e41815",
            fg="white",
            font=("Noto Sans CJK SC", 12, "bold"),
            padx=20,
            pady=8
        )
        self.start_button.pack(side=tk.LEFT)

        tk.Label(
            action_frame,
            text="点击开始执行安装。请不要在此过程中关闭程序。",
            fg="#666",
            bg="#ffffff",
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(15, 0))

        log_frame = ttk.LabelFrame(frame, text="执行日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log = scrolledtext.ScrolledText(log_frame, font=("Monospace", 10), bg="#1e1e1e", fg="#e0e0e0")
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.configure(state=tk.DISABLED)

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
        import subprocess
        import shutil
        
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
        self.append_log(">>> 正在获取 OpenRouter 模型列表")
        try:
            request = urllib.request.Request(MODELS_URL, headers={"User-Agent": "openclaw-firstboot/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except Exception as exc:
            self.append_log(f"ERROR: 获取模型列表失败: {exc}")
            self.model_status.set("模型列表加载失败")
            return

        filtered: list[dict[str, object]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            candidate = self._extract_candidate(item)
            if candidate is not None:
                filtered.append(candidate)

        filtered.sort(key=lambda entry: int(entry["created_ts"]), reverse=True)
        self.model_results = filtered
        self._apply_model_search()

    def _extract_candidate(self, model: dict[str, object]) -> dict[str, object] | None:
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            return None

        family = None
        for prefix, label in MODEL_PREFIXES.items():
            if model_id.startswith(prefix):
                family = label
                break
        if family is None:
            return None

        # Filter: Only text models (exclude image/audio generation models)
        architecture = model.get("architecture") or {}
        if isinstance(architecture, dict):
            output_modalities = architecture.get("output_modalities") or []
            # If output_modalities exists and doesn't contain "text", skip
            if output_modalities and "text" not in output_modalities:
                return None
            # Also check modality field (older format)
            modality = architecture.get("modality") or ""
            if modality and "text" not in str(modality).lower():
                return None

        pricing = model.get("pricing") or {}
        if not isinstance(pricing, dict):
            return None
        prompt_price = parse_number(pricing.get("prompt"))
        completion_price = parse_number(pricing.get("completion"))
        if prompt_price != 0 or completion_price != 0:
            return None

        created_ts = parse_timestamp(model)
        if created_ts is None or created_ts < MODEL_THRESHOLD:
            return None

        created_label = dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        return {
            "id": model_id,
            "name": str(model.get("name") or model_id),
            "family": family,
            "created_ts": created_ts,
            "created_label": created_label,
        }

    def _apply_model_search(self) -> None:
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

        # Update Treeview
        if hasattr(self, "model_tree"):
            # Clear existing items
            for item in self.model_tree.get_children():
                self.model_tree.delete(item)
            # Insert new items
            for entry in visible[:200]:
                self.model_tree.insert(
                    "",
                    tk.END,
                    values=(
                        entry["created_label"],
                        entry["family"],
                        entry["id"],
                    ),
                )

        self.visible_model_results = visible
        self.append_log(f">>> 模型候选数: {len(visible)} / {len(self.model_results)}")
        if visible and not self.model_id.get().strip():
            self.model_id.set(str(visible[0]["id"]))

    def on_model_double_click(self, _event: object | None = None) -> None:
        """Handle double-click on model tree item."""
        selection = self.model_tree.selection()
        if selection:
            item = self.model_tree.item(selection[0])
            values = item.get("values", [])
            if values and len(values) >= 3:
                model_id = str(values[2])  # model_id is the 3rd column
                self.model_id.set(model_id)
                self.append_log(f">>> 已选择模型: {model_id}")
                messagebox.showinfo("模型已选择", f"已选择模型: {model_id}")

    def run_command(
        self,
        title: str,
        command: str,
        *,
        allow_failure: bool = False,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        self.append_log(f">>> {title}")
        self.append_log(f"$ {command}")

        if self.preview:
            self.append_log(f"[PREVIEW] 模拟执行 {title} 跳过实际调用")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        process = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

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
            self.append_log(f"[PREVIEW] 写入配置文件: user_name={user_name}, agent_name={agent_name}, model_id={model_id}, key={key[:4]}...{key[-4:] if len(key)>8 else ''}")
            return

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "env": {"OPENROUTER_API_KEY": key},
            "tools": {
                "profile": "coding",
                "web": {"search": {"enabled": True, "provider": "duckduckgo"}},
            },
            "session": {"dmScope": "per-channel-peer"},
            "messages": {
                "tts": {
                    "auto": "always",
                    "mode": "final",
                    "provider": "openai",
                    "openai": {
                        "baseUrl": self.proxy_url,
                        "apiKey": "edge-tts-local",
                        "model": "edge-tts",
                        "voice": self.proxy_voice,
                    },
                }
            },
            "agents": {
                "defaults": {
                    "workspace": str(self.workspace_dir),
                    "model": normalize_model_id(model_id),
                },
                "list": [
                    {
                        "name": "main",
                        "default": True,
                        "workspace": str(self.workspace_dir),
                        "model": normalize_model_id(model_id),
                        "identity": {"name": agent_name},
                    }
                ],
            },
            "profile": {"user": {"name": user_name}},
        }
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def refresh_pairings(self) -> None:
        if self.preview:
            self.append_log("[PREVIEW] 跳过刷新设备调用")
            self.request_id.set("preview-dummy-request-id-123456")
            self.append_pairing_output("模拟的待审批设备日志输出：\n\n- preview-dummy-request-id-123456")
            self.pending_requests = [{"id": "preview-dummy", "summary": "mock"}]
            return

        output = []
        pending_requests: list[dict[str, str]] = []
        mode = "nodes"

        for candidate_mode, command in (
            ("nodes", "openclaw nodes pending --json"),
            ("devices", "openclaw devices list --json"),
        ):
            result = self.run_command(
                f"查询待审批设备 ({candidate_mode})",
                command,
                allow_failure=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue

            pending_requests = self._normalize_request_payload(payload)
            if pending_requests:
                mode = candidate_mode
                break

        if not pending_requests:
            for candidate_mode, command in (
                ("nodes", "openclaw nodes pending"),
                ("devices", "openclaw devices list"),
            ):
                result = self.run_command(
                    f"查询待审批设备文本输出 ({candidate_mode})",
                    command,
                    allow_failure=True,
                )
                if result.returncode != 0:
                    continue
                raw = result.stdout.strip() or result.stderr.strip()
                if not raw:
                    continue
                output.append(raw)
                request_ids = REQUEST_ID_RE.findall(raw)
                pending_requests = [{"id": item, "summary": raw} for item in request_ids]
                if pending_requests:
                    mode = candidate_mode
                    break

        self.pending_mode = mode
        self.pending_requests = pending_requests

        if pending_requests:
            text = "\n\n".join(item["summary"] for item in pending_requests)
            self.append_pairing_output(text)
            if len(pending_requests) == 1:
                self.request_id.set(pending_requests[0]["id"])
            self.append_log(f">>> 找到 {len(pending_requests)} 个待审批请求，模式={mode}")
            return

        fallback = "\n\n".join(output) if output else "未查询到待审批设备。"
        self.append_pairing_output(fallback)
        self.append_log(">>> 当前未检测到可审批 request id，请先在手机端发起绑定，再点一次刷新")

    def _normalize_request_payload(self, payload: object) -> list[dict[str, str]]:
        items: list[object] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            for key in ("data", "items", "pending", "requests", "devices"):
                value = payload.get(key)
                if isinstance(value, list):
                    items = value
                    break
            if not items and any(key in payload for key in ("requestId", "id")):
                items = [payload]

        normalized: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("requestId") or item.get("id") or "").strip()
            if not request_id:
                continue
            normalized.append(
                {
                    "id": request_id,
                    "summary": json.dumps(item, ensure_ascii=False, indent=2),
                }
            )
        return normalized

    def approve_request(self, request_id: str) -> None:
        if self.preview:
            self.append_log(f"[PREVIEW] 审批请求 ID: {request_id}")
            return

        command = f"openclaw {self.pending_mode} approve {shell_quote(request_id)}"
        result = self.run_command("审批配对请求", command, allow_failure=True)
        if result.returncode == 0:
            return

        fallback_mode = "devices" if self.pending_mode == "nodes" else "nodes"
        fallback = self.run_command(
            f"使用 {fallback_mode} 再次审批配对请求",
            f"openclaw {fallback_mode} approve {shell_quote(request_id)}",
            allow_failure=True,
        )
        if fallback.returncode != 0:
            raise RuntimeError("审批配对请求失败，请确认 request id 是否正确。")
        self.pending_mode = fallback_mode

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
            text="添加完成后重新运行 /usr/local/bin/openclaw-firstboot，即可再次走完审批与欢迎消息流程。",
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

        for _ in range(10):
            result = self.run_command("检查 gateway 状态", "openclaw health", allow_failure=True, timeout=30)
            if result.returncode == 0:
                return
        raise RuntimeError("OpenClaw gateway 未能在预期时间内启动。")

    def run_setup(self) -> None:
        try:
            self.validate_inputs()
            confirmed = messagebox.askyesno(
                "确认初始化" + (" [预览模式]" if self.preview else ""),
                "确认开始初始化？\n\n"
                "1. 写入 OpenClaw 本地配置\n"
                "2. 配置微信或 QQ Bot 渠道\n"
                "3. 审批设备配对请求\n"
                "4. 发送欢迎消息\n\n"
                "只有欢迎消息发送成功后，才会写入完成标记。"
                + ("\n\n注意：当前处于预览模式，所有操作仅打印日志不实际执行！" if self.preview else ""),
            )
            if not confirmed:
                return

            self.start_button.configure(state=tk.DISABLED)
            self.execute_setup()
        except WelcomeMessagePending as exc:
            self.append_log(f"INFO: {exc}")
        except Exception as exc:
            self.append_log(f"ERROR: {exc}")
            messagebox.showerror("初始化失败", str(exc))
        else:
            try:
                if self.preview:
                    self.append_log("[PREVIEW] 写入 completed marker")
                else:
                    MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
                    MARKER_FILE.write_text("completed\n", encoding="utf-8")
            except PermissionError:
                pass
            messagebox.showinfo(
                "初始化完成" + (" (预览模式)" if self.preview else ""),
                "OpenClaw 初始化已完成。后续会自动拉起 gateway 与 Edge-TTS 代理。",
            )
            self.root.destroy()
        finally:
            if hasattr(self, "start_button") and self.start_button.winfo_exists():
                try:
                    self.start_button.configure(state=tk.NORMAL)
                except tk.TclError:
                    pass

    def execute_setup(self) -> None:
        agent_name = self.agent_name.get().strip()
        user_name = self.user_name.get().strip()
        model_id = self.model_id.get().strip()
        openrouter_key = self.openrouter_key.get().strip()
        request_id = self.request_id.get().strip()

        self.append_log("=" * 64)
        if self.preview:
            self.append_log("开始执行 OpenClaw 初始化流程 [预览模式]")
        else:
            self.append_log("开始执行 OpenClaw 初始化流程")
        self.append_log("=" * 64)

        self.write_workspace_files(user_name, agent_name)
        self.append_log(">>> 已写入 USER.md 和 IDENTITY.md")
        self.write_config(user_name, agent_name, model_id, openrouter_key)
        self.append_log(f">>> 已写入配置文件: {self.config_path}")

        self.run_command(
            "启动本地 gateway 和 Edge-TTS 代理",
            "/usr/local/bin/openclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()

        if self.install_qqbot.get():
            token = f"{self.qq_app_id.get().strip()}:{self.qq_app_secret.get().strip()}"
            self.run_command(
                "配置 QQ Bot 渠道",
                f"openclaw channels add --channel {shell_quote(self.qq_channel)} --token {shell_quote(token)}",
            )

        if self.install_weixin.get():
            self.append_log(">>> 开始微信登录流程")
            
            if self.preview:
                # Preview mode: show mock QR code dialog
                self.append_log("[PREVIEW] 模拟微信登录流程")
                mock_qr_url = "https://weixin.qq.com/mock-login-qr-for-preview-mode"
                self.root.after(100, lambda: self.show_weixin_qr_dialog(mock_qr_url))
                messagebox.showinfo(
                    "微信登录 (预览模式)",
                    "【预览模式】模拟微信登录流程\n\n"
                    "在真实环境中，这里会显示实际的微信登录二维码。\n"
                    "点击确定继续预览。",
                )
            else:
                # Run login command and capture QR code URL
                login_result = self.run_command(
                    "获取微信登录二维码",
                    f"openclaw channels login --channel {shell_quote(self.weixin_channel)}",
                    allow_failure=True,
                )
                
                # Try to extract QR code URL from output
                qr_url = None
                if login_result.returncode == 0:
                    # Parse output to find QR code URL
                    # Common patterns: URL starting with https://, or specific format
                    for line in login_result.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("https://") and ("qr" in line.lower() or "login" in line.lower() or "weixin" in line.lower()):
                            qr_url = line
                            break
                        # Also check for qr code data in brackets or quotes
                        if "qr" in line.lower() or "二维码" in line:
                            # Try to extract URL from the line
                            import re as re_module
                            url_match = re_module.search(r'https?://[^\s<>"\']+', line)
                            if url_match:
                                qr_url = url_match.group(0)
                                break
                
                if qr_url:
                    self.append_log(f">>> 检测到二维码 URL: {qr_url[:50]}...")
                    # Show QR code dialog
                    self.root.after(100, lambda: self.show_weixin_qr_dialog(qr_url))
                    # Wait for user to scan (dialog is modal)
                    messagebox.showinfo(
                        "微信登录",
                        "请在弹出的对话框中扫描二维码登录。\n"
                        "扫码完成后，点击「我已扫码」按钮继续。",
                    )
                else:
                    self.append_log("WARN: 未能从输出中检测到二维码 URL，请在终端查看")
                    messagebox.showinfo(
                        "微信登录",
                        "请在终端查看二维码并完成登录。\n"
                        "登录完成后点击确定继续。",
                    )

        self.run_command(
            "重启 gateway 以加载最新配置",
            "/usr/local/bin/openclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()

        if self.preview:
            messagebox.showinfo(
                "开始配对 (预览)",
                "在真实环境中此处将等待用户在手机端发起绑定。\n点击确定继续预览。",
            )
            self.request_id.set("preview-dummy-request-id-123456")
            self.append_pairing_output("模拟的设备请求: preview-dummy-request-id-123456")
            self.pending_requests = [
                {"id": "preview-dummy-request-id-123456", "summary": "模拟的配对请求"}
            ]
        else:
            messagebox.showinfo(
                "开始配对",
                "请先在手机端发起绑定请求，然后点击“确定”。向导会自动刷新待审批列表。",
            )
            self.refresh_pairings()

        request_id = self.request_id.get().strip() or request_id
        if not request_id:
            raise RuntimeError("未发现可审批的 request id，请先在手机端发起绑定，再点一次刷新。")

        self.approve_request(request_id)

        self.run_command(
            "配对后再次重启 gateway",
            "/usr/local/bin/openclaw-session-start --restart-gateway",
        )
        self.wait_for_gateway()

        if self.preview:
            # Only mock the check logic without actual logic for preview mode
            self.append_log("[PREVIEW] 跳过欢迎消息发送检查，认为成功。")
            delivered = True
        else:
            delivered = self.send_welcome()

        if not delivered:
            self.show_support_dialog()
            raise WelcomeMessagePending("欢迎消息发送失败，已展示微信兜底二维码。")

        self.append_log("=" * 64)
        self.append_log("初始化流程执行完成")
        self.append_log("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw First-Boot Wizard")
    parser.add_argument("--preview", action="store_true", help="Run without making actual system changes (Preview Mode)")
    args = parser.parse_args()

    try:
        if MARKER_FILE.exists() and not args.preview:
            print("First-boot wizard already completed. Exiting.")
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
