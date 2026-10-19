"""Native Windows management window for the local API service.

This intentionally uses Tk/ttk from the Python standard library.  The packaged
application therefore has a native desktop control surface without embedding a
web browser or adding an Electron-style runtime.  The HTTP dashboard remains
available for intranet operators and programmatic monitoring.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import ttk


class NativeDashboard(ttk.Frame):
    """A responsive-to-state Tk interface that talks only to its local API."""

    REFRESH_MS = 15_000

    def __init__(self, root: tk.Tk, api_base_url: str, stop_server: Callable[[], None]) -> None:
        super().__init__(root, padding=18)
        self.root = root
        self.api_base_url = api_base_url.rstrip("/")
        self.stop_server = stop_server
        self.accounts_by_row: dict[str, dict[str, Any]] = {}
        self.events: Queue[tuple[str, Any, Any]] = Queue()
        self.refresh_in_progress = False
        self.status_text = tk.StringVar(value="正在连接本地服务…")
        self.service_text = tk.StringVar(value="服务状态：启动中")
        self.metrics_text = tk.StringVar(value="账号：—")
        self.listen_text = tk.StringVar(value=f"本机地址：{self.api_base_url}")
        self.platform_var = tk.StringVar(value="douyin")
        self.label_var = tk.StringVar()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.after(150, self.refresh)
        self.after(100, self._drain_events)
        self.after(self.REFRESH_MS, self._periodic_refresh)

    def _build(self) -> None:
        self.pack(fill="both", expand=True)
        self.root.title("社媒数据服务 · 本地管理器")
        self.root.minsize(900, 590)
        self.root.geometry("1080x680")

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Sub.TLabel", foreground="#596579")
        style.configure("Status.TLabel", foreground="#2463a8")
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 12, "bold"))

        header = ttk.Frame(self)
        header.pack(fill="x")
        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="社媒数据服务", style="Header.TLabel").pack(anchor="w")
        ttk.Label(left, text="抖音 / 小红书 · Windows 原生管理器", style="Sub.TLabel").pack(anchor="w", pady=(2, 0))
        actions = ttk.Frame(header)
        actions.pack(side="right")
        ttk.Button(actions, text="打开 Web 面板", command=lambda: self.open_url("/dashboard")).pack(side="left", padx=(0, 7))
        ttk.Button(actions, text="API 文档", command=lambda: self.open_url("/docs")).pack(side="left")

        service = ttk.LabelFrame(self, text="服务概况", padding=12)
        service.pack(fill="x", pady=(16, 10))
        ttk.Label(service, textvariable=self.service_text, style="Metric.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(service, textvariable=self.metrics_text, style="Metric.TLabel").grid(row=0, column=1, sticky="w", padx=34)
        ttk.Label(service, textvariable=self.listen_text, style="Sub.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        accounts_frame = ttk.LabelFrame(self, text="账号状态", padding=10)
        accounts_frame.pack(fill="both", expand=True, pady=(0, 10))
        columns = ("platform", "account", "status", "checked", "updated", "message")
        self.tree = ttk.Treeview(accounts_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "platform": "平台", "account": "昵称 / 备注", "status": "状态",
            "checked": "最近校验", "updated": "会话更新", "message": "提示",
        }
        widths = {"platform": 78, "account": 185, "status": 90, "checked": 145, "updated": 145, "message": 340}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70, stretch=column == "message")
        self.tree.tag_configure("active", foreground="#147a45")
        self.tree.tag_configure("needs_login", foreground="#b33a3a")
        self.tree.tag_configure("unknown", foreground="#a36b00")
        self.tree.tag_configure("checking", foreground="#2463a8")
        scroll = ttk.Scrollbar(accounts_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        account_actions = ttk.Frame(self)
        account_actions.pack(fill="x", pady=(0, 10))
        ttk.Button(account_actions, text="刷新状态", command=self.refresh).pack(side="left")
        ttk.Button(account_actions, text="检查选中账号", command=self.check_selected).pack(side="left", padx=7)
        ttk.Button(account_actions, text="检查全部有效账号", command=self.check_all).pack(side="left")
        ttk.Button(account_actions, text="重新登录选中账号", command=self.relogin_selected).pack(side="left", padx=7)

        login = ttk.LabelFrame(self, text="新增登录账号", padding=10)
        login.pack(fill="x")
        ttk.Label(login, text="平台").pack(side="left")
        platform_box = ttk.Combobox(login, textvariable=self.platform_var, values=("douyin", "xhs"), state="readonly", width=12)
        platform_box.pack(side="left", padx=(7, 12))
        ttk.Label(login, text="账号备注").pack(side="left")
        label_entry = ttk.Entry(login, textvariable=self.label_var, width=34)
        label_entry.pack(side="left", padx=7, fill="x", expand=True)
        label_entry.bind("<Return>", lambda _event: self.login())
        ttk.Button(login, text="打开隔离登录窗口", command=self.login).pack(side="left")

        ttk.Label(self, textvariable=self.status_text, style="Status.TLabel", anchor="w").pack(fill="x", pady=(11, 0))
        ttk.Label(self, text="登录窗口由本机 Chrome 打开；Cookie 与小红书临时令牌不会显示在此处。", style="Sub.TLabel", anchor="w").pack(fill="x", pady=(2, 0))

    @staticmethod
    def _format_time(value: Any) -> str:
        if not isinstance(value, str) or not value:
            return "—"
        return value.replace("T", " ").replace("+00:00", " UTC")

    @staticmethod
    def _platform_name(platform: str) -> str:
        return "小红书" if platform == "xhs" else "抖音"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            self.api_base_url + path, data=body, method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("detail", "请求失败")
            except (json.JSONDecodeError, OSError):
                message = "请求失败"
            raise RuntimeError(str(message)) from exc
        except URLError as exc:
            raise RuntimeError("本地服务尚未就绪或端口已被占用。") from exc
        if not isinstance(data, dict):
            raise RuntimeError("本地服务返回了无效响应。")
        return data

    def _run_async(
        self, work: Callable[[], dict[str, Any]], on_success: Callable[[dict[str, Any]], None] | None = None
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self.events.put(("error", str(exc), None))
                return
            self.events.put(("success", on_success, result))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self) -> None:
        """Run network callbacks on Tk's owning thread, never worker threads."""
        while True:
            try:
                kind, first, second = self.events.get_nowait()
            except Empty:
                break
            if kind == "error":
                self._show_error(str(first))
            elif callable(first):
                first(second)
        if self.winfo_exists():
            self.after(100, self._drain_events)

    def _show_error(self, message: str) -> None:
        self.status_text.set(f"操作失败：{message}")
        self.refresh_in_progress = False

    def _render(self, payload: dict[str, Any]) -> None:
        service = payload.get("service", {})
        accounts = payload.get("accounts", {})
        if not isinstance(service, dict) or not isinstance(accounts, dict):
            self._show_error("状态响应格式无效。")
            return
        self.service_text.set(f"服务状态：{'运行中' if service.get('status') == 'ok' else '异常'} · v{service.get('version', '—')}")
        self.metrics_text.set(
            f"账号：{accounts.get('active', 0)} 已登录 / {accounts.get('total', 0)} 总计 · {accounts.get('needs_login', 0)} 需重登"
        )
        self.listen_text.set(f"本机地址：{service.get('listen_address', self.api_base_url)} · 自动检查 {service.get('check_interval_seconds', '—')} 秒")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.accounts_by_row.clear()
        items = accounts.get("items", [])
        if isinstance(items, list):
            for account in items:
                if not isinstance(account, dict):
                    continue
                row = self.tree.insert(
                    "", "end", tags=(str(account.get("status", "unknown")),),
                    values=(
                        self._platform_name(str(account.get("platform", "douyin"))),
                        account.get("nickname") or account.get("label") or "—",
                        account.get("status", "unknown"),
                        self._format_time(account.get("last_checked_at")),
                        self._format_time(account.get("updated_at")),
                        account.get("last_error") or "—",
                    ),
                )
                self.accounts_by_row[row] = account
        self.status_text.set(f"状态已刷新。服务启动：{self._format_time(service.get('started_at'))}")
        self.refresh_in_progress = False

    def refresh(self) -> None:
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True
        self._run_async(lambda: self._request("GET", "/api/v1/dashboard"), self._render)

    def _periodic_refresh(self) -> None:
        self.refresh()
        if self.winfo_exists():
            self.after(self.REFRESH_MS, self._periodic_refresh)

    def _selected_account(self) -> dict[str, Any] | None:
        selected = self.tree.selection()
        if not selected:
            self.status_text.set("请先在账号列表中选中一个账号。")
            return None
        return self.accounts_by_row.get(selected[0])

    def check_selected(self) -> None:
        account = self._selected_account()
        if not account:
            return
        account_id = str(account["account_id"])
        self.status_text.set("正在检查账号…")
        self._run_async(
            lambda: self._request("POST", f"/api/v1/accounts/{account_id}/check"),
            lambda _result: self.refresh(),
        )

    def relogin_selected(self) -> None:
        account = self._selected_account()
        if not account:
            return
        account_id = str(account["account_id"])
        self.status_text.set("正在打开隔离登录窗口…")
        self._run_async(
            lambda: self._request("POST", f"/api/v1/accounts/{account_id}/relogin"),
            lambda result: (self.status_text.set(str(result.get("message", "已打开登录窗口。"))), self.refresh()),
        )

    def check_all(self) -> None:
        self.status_text.set("正在依次检查有效账号…")
        self._run_async(
            lambda: self._request("POST", "/api/v1/accounts/check-all"),
            lambda result: (self.status_text.set(f"已检查 {result.get('checked', 0)} 个有效账号。"), self.refresh()),
        )

    def login(self) -> None:
        label = self.label_var.get().strip()
        if not label:
            self.status_text.set("请填写账号备注。")
            return
        self.status_text.set("正在打开隔离登录窗口…")
        self._run_async(
            lambda: self._request("POST", "/api/v1/accounts/login", {"platform": self.platform_var.get(), "label": label}),
            lambda result: (self.label_var.set(""), self.status_text.set(str(result.get("message", "已打开登录窗口。"))), self.refresh()),
        )

    def open_url(self, path: str) -> None:
        webbrowser.open(self.api_base_url + path)

    def close(self) -> None:
        self.stop_server()
        self.root.destroy()


def launch_native_dashboard(api_base_url: str, stop_server: Callable[[], None]) -> None:
    """Start the Tk event loop. Kept separate to make the entrypoint tiny."""
    root = tk.Tk()
    NativeDashboard(root, api_base_url, stop_server)
    root.mainloop()
