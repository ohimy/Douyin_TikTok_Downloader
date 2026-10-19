"""A compact local HTTP API for the two Douyin read operations we expose.

This module intentionally has no database, Redis, browser, or Docker dependency.
The cookie is kept in a local file and is read afresh for every upstream request,
so replacing it through the API takes effect immediately.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from local_api.xhs_runtime import XhsClient, XhsRuntimeUnavailable
from local_api.dashboard import DASHBOARD_HTML

# These two modules are from the checked-out upstream project.  They are pure
# Python and do not pull in the upstream database/queue application.

def _native_signing_dir() -> Path:
    """Locate the two pure-Python upstream signing modules.

    Importing ``dtk.signing`` normally also imports the full scheduler stack.
    Loading these files as their package modules keeps this compact executable
    independent from that stack. The build script bundles the same directory
    under ``dtk/signing/native`` in the PyInstaller extraction directory.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "dtk" / "signing" / "native"
    return Path(__file__).resolve().parent.parent / "src" / "dtk" / "signing" / "native"


def _load_upstream_signing_module(name: str) -> types.ModuleType:
    source_dir = _native_signing_dir()
    package_paths = {
        "dtk": source_dir.parent.parent,
        "dtk.signing": source_dir.parent,
        "dtk.signing.native": source_dir,
    }
    for package_name, package_path in package_paths.items():
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(package_path)]  # type: ignore[attr-defined]
            sys.modules[package_name] = package
    full_name = f"dtk.signing.native.{name}"
    existing = sys.modules.get(full_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(full_name, source_dir / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load bundled signing module {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


ABogus = _load_upstream_signing_module("abogus").ABogus
websign = _load_upstream_signing_module("websign")


DOUYIN_ORIGIN = "https://www.douyin.com"
USER_POST_URL = f"{DOUYIN_ORIGIN}/aweme/v1/web/aweme/post/"
VIDEO_DETAIL_URL = f"{DOUYIN_ORIGIN}/aweme/v1/web/aweme/detail/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class CookieUpdate(BaseModel):
    """A complete Cookie request header copied from a logged-in browser."""

    cookie: str = Field(min_length=12, max_length=30000)
    label: str = Field(default="手动导入", min_length=1, max_length=80)
    account_id: str | None = Field(default=None, max_length=64)
    platform: Literal["douyin", "xhs"] = "douyin"

    @field_validator("cookie")
    @classmethod
    def normalize_cookie(cls, value: str) -> str:
        value = value.strip()
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        if "=" not in value:
            raise ValueError("cookie must contain at least one name=value pair")
        if "\r" in value or "\n" in value:
            raise ValueError("cookie must be a single HTTP header value")
        return value


class LoginRequest(BaseModel):
    """A user-visible name for one browser login session."""

    label: str = Field(min_length=1, max_length=80)
    platform: Literal["douyin", "xhs"] = "douyin"

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("label cannot be blank")
        return value


class XhsLinkResolve(BaseModel):
    """A user-supplied XHS note/profile/share URL to open in the saved browser profile."""

    url: str = Field(min_length=12, max_length=8192)
    account_id: str | None = Field(default=None, max_length=64)

    @field_validator("url")
    @classmethod
    def validate_xhs_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        host = parsed.hostname or ""
        allowed = host == "xhslink.com" or host.endswith(".xhslink.com") or host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
        if parsed.scheme not in ("http", "https") or not allowed:
            raise ValueError("url must be a Xiaohongshu note, profile, or share link")
        return value


class RequestAuditLog:
    """Small, privacy-preserving request audit log for the local API.

    The JSONL file deliberately contains only request metadata. In particular,
    it never receives headers, request bodies, query strings, Cookies, or
    platform tokens. Keeping it file based makes it useful in the Windows EXE
    without adding a database dependency.
    """

    max_bytes = 5 * 1024 * 1024
    backup_count = 5
    _fields = (
        "timestamp",
        "request_id",
        "client_ip",
        "method",
        "path",
        "status_code",
        "duration_ms",
    )

    def __init__(self, data_dir: Path) -> None:
        self.log_dir = data_dir / "logs"
        self.path = self.log_dir / "requests.jsonl"
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _rotate_locked(self) -> None:
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def record(self, entry: Mapping[str, Any]) -> None:
        """Append one metadata-only entry and rotate bounded local files."""
        safe_entry = {field: entry.get(field) for field in self._fields}
        encoded = json.dumps(safe_entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size + len(encoded.encode("utf-8")) + 1 > self.max_bytes:
                    self._rotate_locked()
                with self.path.open("a", encoding="utf-8") as log_file:
                    log_file.write(encoded + "\n")
            except OSError:
                # Observability must never make the business API unavailable,
                # e.g. when a user temporarily has no free disk space.
                return

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return newest metadata-only entries, including rotated files if needed."""
        with self._lock:
            records: list[dict[str, Any]] = []
            paths = [self.path, *(self.path.with_name(f"{self.path.name}.{index}") for index in range(1, self.backup_count + 1))]
            for path in paths:
                if len(records) >= limit:
                    break
                try:
                    lines = deque(path.open("r", encoding="utf-8"), maxlen=limit - len(records))
                except OSError:
                    continue
                for line in reversed(lines):
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, Mapping):
                        records.append({field: parsed.get(field) for field in self._fields})
                        if len(records) >= limit:
                            break
            return records


class RuntimeFiles:
    """Persists browser accounts without exposing their cookies through the API."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.accounts_path = data_dir / "accounts.json"
        self.xhs_note_tokens_path = data_dir / "xhs-note-tokens.json"
        self.browser_profiles_dir = data_dir / "browser-profiles"
        self._write_lock = asyncio.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profiles_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temp_file:
            json.dump(value, temp_file, ensure_ascii=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_name = temp_file.name
        os.replace(temp_name, path)

    def _load_accounts(self) -> list[dict[str, Any]]:
        try:
            state = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            accounts = state.get("accounts", [])
            return accounts if isinstance(accounts, list) else []
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._atomic_json_write(self.accounts_path, {"accounts": accounts})

    def _load_xhs_note_tokens(self) -> dict[str, dict[str, str]]:
        try:
            state = json.loads(self.xhs_note_tokens_path.read_text(encoding="utf-8"))
            entries = state.get("tokens", {})
            return entries if isinstance(entries, dict) else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    async def cache_xhs_note_tokens(
        self, account_id: str, items: list[Any], *, source: str
    ) -> None:
        """Remember transient tokens returned by the user's own post list."""
        async with self._write_lock:
            entries = self._load_xhs_note_tokens()
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                note_id = item.get("note_id") or item.get("id")
                token = item.get("xsec_token")
                if isinstance(note_id, str) and isinstance(token, str) and token:
                    entries[f"{account_id}:{note_id}"] = {
                        "token": token, "source": source, "updated_at": now,
                    }
            self._atomic_json_write(self.xhs_note_tokens_path, {"tokens": entries})

    def xhs_note_token(self, account_id: str, note_id: str) -> tuple[str, str] | None:
        entry = self._load_xhs_note_tokens().get(f"{account_id}:{note_id}")
        if not isinstance(entry, Mapping):
            return None
        token = entry.get("token")
        source = entry.get("source")
        return (token, source) if isinstance(token, str) and token and isinstance(source, str) else None

    @staticmethod
    def _public_account(account: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "account_id": account["account_id"],
            "platform": account.get("platform", "douyin"),
            "label": account["label"],
            "nickname": account.get("nickname"),
            "avatar_url": account.get("avatar_url"),
            "status": account.get("status", "needs_login"),
            "updated_at": account.get("updated_at"),
            "last_checked_at": account.get("last_checked_at"),
            "last_error": account.get("last_error"),
        }

    def list_accounts(self, platform: str | None = None) -> list[dict[str, Any]]:
        accounts = self._load_accounts()
        if platform is not None:
            accounts = [item for item in accounts if item.get("platform", "douyin") == platform]
        return [self._public_account(account) for account in accounts]

    def account(self, account_id: str) -> dict[str, Any] | None:
        return next(
            (account for account in self._load_accounts() if account.get("account_id") == account_id),
            None,
        )

    def default_account_id(self, platform: str) -> str | None:
        candidates = [
            item for item in self._load_accounts()
            if item.get("platform", "douyin") == platform
            and item.get("status") == "active"
            and item.get("cookie")
        ]
        if not candidates:
            return None
        account_id = max(candidates, key=lambda item: str(item.get("updated_at", ""))).get("account_id")
        return account_id if isinstance(account_id, str) else None

    def read_cookie(self, account_id: str | None = None, *, platform: str = "douyin") -> str | None:
        accounts = self._load_accounts()
        if account_id:
            account = next((item for item in accounts if item.get("account_id") == account_id), None)
            if account and account.get("platform", "douyin") != platform:
                return None
            value = account.get("cookie") if account else None
            return value if isinstance(value, str) and value else None
        # Most recently updated usable account is the default for compatibility
        # with the original one-cookie API.
        usable = [
            item
            for item in accounts
            if item.get("platform", "douyin") == platform
            and item.get("status") == "active"
            and item.get("cookie")
        ]
        if not usable:
            return None
        value = max(usable, key=lambda item: str(item.get("updated_at", ""))).get("cookie")
        return value if isinstance(value, str) else None

    async def create_account(self, label: str, *, platform: str = "douyin") -> dict[str, Any]:
        async with self._write_lock:
            accounts = self._load_accounts()
            account = {
                "account_id": secrets.token_urlsafe(9),
                "platform": platform,
                "label": label,
                "nickname": None,
                "avatar_url": None,
                "status": "logging_in",
                "updated_at": None,
                "last_checked_at": None,
                "last_error": None,
                "cookie": None,
            }
            accounts.append(account)
            self._save_accounts(accounts)
            return self._public_account(account)

    async def set_status(
        self, account_id: str, status: str, *, error: str | None = None
    ) -> dict[str, Any] | None:
        async with self._write_lock:
            accounts = self._load_accounts()
            account = next((item for item in accounts if item.get("account_id") == account_id), None)
            if account is None:
                return None
            account["status"] = status
            account["last_error"] = error
            account["last_checked_at"] = datetime.now(timezone.utc).isoformat()
            self._save_accounts(accounts)
            return self._public_account(account)

    async def save_cookie(
        self, account_id: str, cookie: str, *, label: str | None = None, platform: str | None = None
    ) -> dict[str, Any] | None:
        async with self._write_lock:
            accounts = self._load_accounts()
            account = next((item for item in accounts if item.get("account_id") == account_id), None)
            if account is None:
                return None
            if platform is not None and account.get("platform", "douyin") != platform:
                return None
            if label is not None:
                account["label"] = label
            now = datetime.now(timezone.utc).isoformat()
            account.update(
                {
                    "cookie": cookie,
                    "status": "active",
                    "updated_at": now,
                    "last_checked_at": now,
                    "last_error": None,
                }
            )
            self._save_accounts(accounts)
            return self._public_account(account)

    async def set_profile(
        self, account_id: str, *, nickname: str | None, avatar_url: str | None
    ) -> dict[str, Any] | None:
        """Store display-only account metadata; Cookie values remain untouched."""
        async with self._write_lock:
            accounts = self._load_accounts()
            account = next((item for item in accounts if item.get("account_id") == account_id), None)
            if account is None:
                return None
            if nickname:
                account["nickname"] = nickname[:120]
            if avatar_url and avatar_url.startswith(("https://", "http://")):
                account["avatar_url"] = avatar_url[:4096]
            account["profile_updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_accounts(accounts)
            return self._public_account(account)

    async def replace_cookie(
        self, cookie: str, label: str, account_id: str | None, *, platform: str = "douyin"
    ) -> dict[str, Any]:
        if account_id:
            account = await self.save_cookie(account_id, cookie, label=label, platform=platform)
            if account is None:
                raise HTTPException(status_code=404, detail="Account not found")
            return account
        account = await self.create_account(label, platform=platform)
        return (await self.save_cookie(account["account_id"], cookie, platform=platform)) or account

    def _profile_dir_for_account(self, account_id: str) -> Path | None:
        """Resolve a profile path without allowing an account ID to escape its root."""
        root = self.browser_profiles_dir.resolve()
        candidate = (root / account_id).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    async def delete_account(self, account_id: str) -> dict[str, Any] | None:
        """Permanently remove one account, its Cookie profile, and XHS token cache."""
        async with self._write_lock:
            accounts = self._load_accounts()
            account = next((item for item in accounts if item.get("account_id") == account_id), None)
            if account is None:
                return None

            profile_dir = self._profile_dir_for_account(account_id)
            if profile_dir is not None and profile_dir.exists():
                # A caller cancels any active login task before this operation,
                # so the Chromium profile should no longer be locked on Windows.
                await asyncio.to_thread(shutil.rmtree, profile_dir)

            entries = self._load_xhs_note_tokens()
            retained_entries = {
                key: value for key, value in entries.items()
                if not key.startswith(f"{account_id}:")
            }
            self._atomic_json_write(self.xhs_note_tokens_path, {"tokens": retained_entries})
            self._save_accounts([item for item in accounts if item.get("account_id") != account_id])
            return self._public_account(account)


def default_data_dir() -> Path:
    configured = os.getenv("DOUYIN_LOCAL_API_DATA")
    if configured:
        return Path(configured).expanduser()
    # Keep a frozen executable portable: its data directory sits beside the exe.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path.cwd() / "data"


def _intranet_addresses(port: int, listen_host: str) -> list[str]:
    """Return display-safe private IPv4 URLs reachable by other LAN devices.

    Binding to ``0.0.0.0`` makes Uvicorn listen on every IPv4 interface, but it
    is not an address a browser on another machine can open. Discover the
    private addresses for the management panels instead.
    """
    normalized_host = listen_host.strip().strip("[]")
    if normalized_host in {"127.0.0.1", "localhost", "::1"}:
        return []

    candidates: set[str] = set()
    if normalized_host not in {"0.0.0.0", "::", ""}:
        candidates.add(normalized_host)
    else:
        with suppress(OSError):
            candidates.update(socket.gethostbyname_ex(socket.gethostname())[2])
        with suppress(OSError):
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                candidates.add(info[4][0])
        # This UDP connect does not send a packet. It asks the OS which LAN
        # interface would be used, covering Windows hosts with incomplete DNS.
        with suppress(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 80))
                candidates.add(probe.getsockname()[0])

    private_addresses: list[str] = []
    for candidate in candidates:
        with suppress(ValueError):
            address = ipaddress.ip_address(candidate)
            if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
                private_addresses.append(str(address))
    return [f"http://{address}:{port}" for address in sorted(set(private_addresses))]


def _cookie_map(cookie_header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in cookie_header.split(";"):
        key, separator, value = part.strip().partition("=")
        if key and separator:
            result[key] = value
    return result


def _encode_pairs(pairs: list[tuple[str, str]]) -> str:
    # The local signature must hash exactly the same URLSearchParams-like query
    # bytes that are ultimately transmitted.
    return "&".join(
        f"{quote(key, safe='*-._')}={quote(value, safe='*-._')}" for key, value in pairs
    )


def _base_params() -> list[tuple[str, str]]:
    return [
        ("device_platform", "webapp"), ("aid", "6383"), ("channel", "channel_pc_web"),
        ("pc_client_type", "1"), ("version_code", "290100"), ("version_name", "29.1.0"),
        ("cookie_enabled", "true"), ("screen_width", "1920"), ("screen_height", "1080"),
        ("browser_language", "zh-CN"), ("browser_platform", "Win32"),
        ("browser_name", "Chrome"), ("browser_version", "130.0.0.0"),
        ("browser_online", "true"), ("engine_name", "Blink"),
        ("engine_version", "130.0.0.0"), ("os_name", "Windows"), ("os_version", "10"),
        ("cpu_core_num", "12"), ("device_memory", "8"), ("platform", "PC"),
        ("downlink", "10"), ("effective_type", "4g"), ("round_trip_time", "0"),
        ("update_version_code", "170400"),
    ]


class DouyinClient:
    """Issues a signed upstream request using the most recently saved cookie."""

    def __init__(self, files: RuntimeFiles) -> None:
        self.files = files

    def _signed_query(self, params: list[tuple[str, str]], cookie: str) -> tuple[str, dict[str, str]]:
        cookie_map = _cookie_map(cookie)
        ms_token = cookie_map.get("msToken") or secrets.token_urlsafe(80)
        base_pairs = [*params, ("msToken", ms_token)]
        base_query = _encode_pairs(base_pairs)
        bogus = ABogus(USER_AGENT).get_value(base_query)
        signed_pairs = [*base_pairs, ("a_bogus", bogus)]

        uifid = websign.pick_uifid(cookie_map)
        if uifid:
            verify_fp = cookie_map.get(websign.VERIFY_FP_COOKIE)
            if verify_fp:
                signed_pairs.extend((("verifyFp", verify_fp), ("fp", verify_fp)))
            query, _, signature_headers = websign.sign(signed_pairs, uifid)
            return query, signature_headers
        return _encode_pairs(signed_pairs), {}

    async def get(
        self, url: str, params: list[tuple[str, str]], *, account_id: str | None = None
    ) -> dict[str, Any]:
        cookie = self.files.read_cookie(account_id)
        if not cookie:
            raise HTTPException(status_code=409, detail="No active Douyin account is available")
        query, signature_headers = self._signed_query(params, cookie)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{DOUYIN_ORIGIN}/",
            "Origin": DOUYIN_ORIGIN,
            "Cookie": cookie,
            **signature_headers,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=True) as client:
                response = await client.get(f"{url}?{query}", headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Could not reach Douyin") from exc
        if response.status_code >= 400:
            if account_id and response.status_code in (401, 403):
                await self.files.set_status(
                    account_id, "needs_login", error=f"Douyin returned HTTP {response.status_code}"
                )
            raise HTTPException(
                status_code=502,
                detail={"message": "Douyin rejected the request", "upstream_status": response.status_code},
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="Douyin returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="Douyin returned an unexpected response")
        return payload


class BrowserLoginManager:
    """Opens one isolated local browser profile per account for manual login."""

    def __init__(self, files: RuntimeFiles) -> None:
        self.files = files
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._xhs_resolution_locks: dict[str, asyncio.Lock] = {}
        self.on_login: Callable[[str], Awaitable[dict[str, Any]]] | None = None

    _LOGIN_TARGETS: Mapping[str, tuple[str, tuple[str, ...]]] = {
        "douyin": (DOUYIN_ORIGIN, ("sessionid", "sessionid_ss")),
        # XHS also issues these cookies for anonymous visitors.  They are only
        # candidates; the self-info probe below decides whether login succeeded.
        "xhs": ("https://www.xiaohongshu.com/", ("a1", "web_session")),
    }

    async def cancel(self, account_id: str) -> None:
        """Close a pending login browser before removing its account files."""
        task = self._tasks.get(account_id)
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _browser_launch_options() -> dict[str, str]:
        """Find a locally installed Chromium browser without Playwright downloads.

        Chrome is preferred, Edge is the Windows fallback, and an administrator
        may pin a non-standard executable with ``DOUYIN_LOCAL_API_BROWSER_PATH``.
        This avoids the brittle Playwright ``channel='chrome'`` lookup that
        assumes one particular Chrome installation directory.
        """
        configured = os.getenv("DOUYIN_LOCAL_API_BROWSER_PATH", "").strip().strip('"')
        candidates = [Path(configured)] if configured else []
        if os.name == "nt":
            local_app_data = os.getenv("LOCALAPPDATA", "")
            program_files = os.getenv("PROGRAMFILES", "")
            program_files_x86 = os.getenv("PROGRAMFILES(X86)", "")
            for root in (local_app_data, program_files, program_files_x86):
                if not root:
                    continue
                candidates.extend((
                    Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                    Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                ))
        elif sys.platform == "darwin":
            candidates.extend((
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ))

        for candidate in candidates:
            if candidate.is_file():
                return {"executable_path": str(candidate)}
        raise RuntimeError(
            "No supported browser was found. Install Google Chrome or Microsoft Edge, "
            "or set DOUYIN_LOCAL_API_BROWSER_PATH to its executable path."
        )

    async def start(
        self, label: str, *, platform: Literal["douyin", "xhs"] = "douyin", account_id: str | None = None
    ) -> dict[str, Any]:
        if account_id is None:
            account = await self.files.create_account(label, platform=platform)
            account_id = account["account_id"]
        else:
            account = self.files.account(account_id)
            if account is None:
                raise HTTPException(status_code=404, detail="Account not found")
            if account.get("platform", "douyin") != platform:
                raise HTTPException(status_code=400, detail="Account belongs to a different platform")
            if account_id in self._tasks and not self._tasks[account_id].done():
                raise HTTPException(status_code=409, detail="This account already has a login window open")
            await self.files.set_status(account_id, "logging_in")
        if account_id in self._tasks and not self._tasks[account_id].done():
            raise HTTPException(status_code=409, detail="This account already has a login window open")
        self._tasks[account_id] = asyncio.create_task(self._run(account_id, platform))
        return {
            "account_id": account_id,
            "status": "logging_in",
            "platform": platform,
            "message": "A dedicated browser window has been opened; finish login there and poll /api/v1/accounts.",
        }

    async def _run(self, account_id: str, platform: str) -> None:
        context = None
        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                await self.files.set_status(
                    account_id,
                    "needs_login",
                    error="Browser login is unavailable: install the Playwright dependency.",
                )
                return
            async with async_playwright() as playwright:
                # Each account keeps a separate browser profile. The executable
                # is discovered locally so the packaged app needs no Playwright
                # browser download and can fall back to Windows Edge.
                context = await playwright.chromium.launch_persistent_context(
                    str(self.files.browser_profiles_dir / account_id),
                    headless=False,
                    **self._browser_launch_options(),
                )
                # A re-login must not immediately succeed by reading a stale session.
                await context.clear_cookies()
                page = context.pages[0] if context.pages else await context.new_page()
                origin, session_names = self._LOGIN_TARGETS[platform]
                await page.goto(origin, wait_until="domcontentloaded")
                xhs_session_checker = XhsClient() if platform == "xhs" else None
                for _ in range(300):  # five minutes, then report a clear retry state
                    cookies = await context.cookies(origin)
                    cookie_map = {item["name"]: item["value"] for item in cookies}
                    if platform == "xhs":
                        # Guest pages create a1/web_session before the user has
                        # scanned a QR code.  Never promote that to active: only
                        # the signed current-user endpoint proves authentication.
                        candidate = all(cookie_map.get(name) for name in session_names)
                        if candidate and _ % 5 == 0:
                            cookie = "; ".join(f"{name}={value}" for name, value in cookie_map.items())
                            try:
                                await asyncio.to_thread(xhs_session_checker.check_session, cookie)
                            except Exception:
                                # A visitor session or a not-yet-complete login
                                # simply keeps the browser open for the user.
                                pass
                            else:
                                await self.files.save_cookie(account_id, cookie, platform=platform)
                                if self.on_login is not None:
                                    try:
                                        await self.on_login(account_id)
                                    except Exception:
                                        pass
                                return
                    elif any(cookie_map.get(name) for name in session_names):
                        cookie = "; ".join(f"{name}={value}" for name, value in cookie_map.items())
                        await self.files.save_cookie(account_id, cookie, platform=platform)
                        if self.on_login is not None:
                            try:
                                await self.on_login(account_id)
                            except Exception:
                                pass
                        return
                    await asyncio.sleep(1)
                await self.files.set_status(account_id, "needs_login", error="Login timed out; please try again.")
        except Exception as exc:  # Browser executable/errors should be visible in the account list.
            await self.files.set_status(account_id, "needs_login", error=f"Browser login failed: {exc}")
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            self._tasks.pop(account_id, None)

    async def resolve_xhs_link(self, url: str, account_id: str) -> str:
        """Open an XHS link with the existing isolated profile and return its final URL."""
        account = self.files.account(account_id)
        if account is None or account.get("platform", "douyin") != "xhs":
            raise HTTPException(status_code=404, detail="Xiaohongshu account not found")
        if account_id in self._tasks and not self._tasks[account_id].done():
            raise HTTPException(status_code=409, detail="This account already has a login window open")
        profile_dir = self.files.browser_profiles_dir / account_id
        if not profile_dir.is_dir():
            raise HTTPException(
                status_code=409,
                detail="This account has no local browser profile. Please log in through /accounts first.",
            )
        context = None
        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise HTTPException(status_code=503, detail="Browser link resolution is unavailable.") from exc
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile_dir), headless=False, **self._browser_launch_options(),
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1200)
                return page.url
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not resolve Xiaohongshu link: {exc}") from exc
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

    async def resolve_xhs_identifier_silently(
        self, kind: Literal["user", "note"], identifier: str, account_id: str
    ) -> dict[str, str]:
        """Try to obtain an XHS token in a hidden browser, never a popup window.

        XHS tokens are emitted by the site at request time and cannot be derived
        from an ID alone. A serialized, headless visit is the best automatic
        fallback after the normal post-list token cache. The site may still
        refuse to issue one for an arbitrary direct note ID; that is returned as
        a clear 422 rather than opening a visible error page.
        """
        account = self.files.account(account_id)
        if account is None or account.get("platform", "douyin") != "xhs":
            raise HTTPException(status_code=404, detail="Xiaohongshu account not found")
        if account_id in self._tasks and not self._tasks[account_id].done():
            raise HTTPException(status_code=409, detail="This account already has a login window open")
        profile_dir = self.files.browser_profiles_dir / account_id
        if not profile_dir.is_dir():
            raise HTTPException(
                status_code=409,
                detail="This account has no local browser profile. Please log in through /accounts first.",
            )

        lock = self._xhs_resolution_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            target_url = (
                f"https://www.xiaohongshu.com/user/profile/{identifier}"
                if kind == "user" else f"https://www.xiaohongshu.com/explore/{identifier}"
            )
            captured: list[dict[str, str]] = []
            context = None
            try:
                from playwright.async_api import async_playwright
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile_dir), headless=True, **self._browser_launch_options(),
                    )
                    page = context.pages[0] if context.pages else await context.new_page()

                    def capture(request: Any) -> None:
                        parsed = urlparse(request.url)
                        if kind == "note" and parsed.path.endswith("/feed"):
                            try:
                                payload = json.loads(request.post_data or "{}")
                            except json.JSONDecodeError:
                                return
                            token = payload.get("xsec_token")
                            source = payload.get("xsec_source")
                            if isinstance(token, str) and token:
                                captured.append({
                                    "xsec_token": token,
                                    "xsec_source": source if isinstance(source, str) else "pc_user",
                                })
                        elif kind == "user" and parsed.path.endswith("/user_posted"):
                            params = parse_qs(parsed.query)
                            token = (params.get("xsec_token") or [""])[0]
                            if token:
                                captured.append({
                                    "xsec_token": token,
                                    "xsec_source": (params.get("xsec_source") or ["pc_user"])[0],
                                })

                    page.on("request", capture)
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    for _ in range(12):
                        if captured:
                            return captured[-1]
                        await page.wait_for_timeout(500)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Could not silently resolve Xiaohongshu token: {exc}",
                ) from exc
            finally:
                if context is not None:
                    with suppress(Exception):
                        await context.close()
        raise HTTPException(
            status_code=422,
            detail=(
                "Xiaohongshu did not issue a token for this note ID. Provide its current "
                "share URL to POST /api/v1/xhs/resolve-link or pass xsec_token explicitly. "
                "No browser window was opened."
            ),
        )

def _upstream_error(payload: Mapping[str, Any]) -> HTTPException | None:
    code = payload.get("status_code")
    if code not in (None, 0, "0"):
        return HTTPException(
            status_code=502,
            detail={"message": "Douyin did not return data", "upstream_code": code},
        )
    return None


def _xhs_link_fields(url: str, account_id: str) -> dict[str, str | None]:
    """Extract the transient request fields from an XHS URL after browser redirects."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
        raise HTTPException(status_code=422, detail="The share link did not resolve to Xiaohongshu.")
    query = parse_qs(parsed.query)
    token = (query.get("xsec_token") or [None])[0]
    if not token:
        raise HTTPException(
            status_code=422,
            detail="The final page URL has no xsec_token. Open the target page in Chrome and try its current URL again.",
        )
    source = (query.get("xsec_source") or ["pc_user"])[0]
    parts = [part for part in parsed.path.split("/") if part]
    user_id = None
    note_id = None
    if len(parts) >= 3 and parts[0] == "user" and parts[1] == "profile":
        user_id = parts[2]
        note_id = parts[3] if len(parts) >= 4 else None
    elif len(parts) >= 2 and parts[0] in {"explore", "discovery"}:
        note_id = parts[-1]
    return {
        "account_id": account_id,
        "url": url,
        "xsec_token": token,
        "xsec_source": source,
        "user_id": user_id,
        "note_id": note_id,
    }


def _first_string(value: Any, keys: set[str]) -> str | None:
    """Find a short text field in a small API payload tree without assuming one schema."""
    pending = [value]
    visited = 0
    while pending and visited < 80:
        current = pending.pop(0)
        visited += 1
        if not isinstance(current, Mapping):
            continue
        for key in keys:
            candidate = current.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        pending.extend(item for item in current.values() if isinstance(item, Mapping))
    return None


def _first_avatar_url(value: Any) -> str | None:
    pending = [value]
    visited = 0
    avatar_keys = {
        "avatar_larger", "avatar_medium", "avatar_thumb", "avatar", "avatar_url",
        "avatarUrl", "image", "imageb", "images",
    }
    url_keys = ("url_list", "url", "url_default", "url_pre", "url_1080", "url_720")
    while pending and visited < 100:
        current = pending.pop(0)
        visited += 1
        if not isinstance(current, Mapping):
            continue
        for key in avatar_keys:
            avatar = current.get(key)
            if isinstance(avatar, str) and avatar.startswith(("https://", "http://")):
                return avatar
            if isinstance(avatar, Mapping):
                for url_key in url_keys:
                    candidate = avatar.get(url_key)
                    if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                        return candidate
                    if isinstance(candidate, list):
                        url = next((item for item in candidate if isinstance(item, str) and item.startswith(("https://", "http://"))), None)
                        if url:
                            return url
            if isinstance(avatar, list):
                for candidate in avatar:
                    if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                        return candidate
                    if isinstance(candidate, Mapping):
                        for url_key in url_keys:
                            url = candidate.get(url_key)
                            if isinstance(url, str) and url.startswith(("https://", "http://")):
                                return url
        pending.extend(item for item in current.values() if isinstance(item, Mapping))
    return None


def _account_display_fields(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return (
        _first_string(payload, {"nickname", "nick_name", "nickName", "name"}),
        _first_avatar_url(payload),
    )


def create_app(data_dir: Path | None = None) -> FastAPI:
    files = RuntimeFiles(data_dir or default_data_dir())
    request_audit = RequestAuditLog(files.data_dir)
    client = DouyinClient(files)
    xhs_client = XhsClient()
    logins = BrowserLoginManager(files)
    app = FastAPI(
        title="Douyin Local API",
        version="1.1.0",
        description="Intranet API for author posts, video details, and hot Cookie updates.",
    )
    app.state.files = files
    app.state.logins = logins
    app.state.xhs_client = xhs_client
    app.state.request_audit = request_audit
    app.state.started_at = datetime.now(timezone.utc)
    app.state.started_monotonic = time.monotonic()
    app.state.account_check_interval = max(int(os.getenv("DOUYIN_ACCOUNT_CHECK_SECONDS", "900")), 60)
    app.state.listen_host = os.getenv("DOUYIN_LOCAL_API_HOST", "0.0.0.0")
    app.state.listen_port = int(os.getenv("DOUYIN_LOCAL_API_PORT", "8787"))
    origins = [item.strip() for item in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    # The management page polls its own state every 15 seconds. Excluding that
    # UI noise keeps the audit log focused on API calls made by users/clients.
    audit_excluded_paths = {
        "/",
        "/dashboard",
        "/accounts",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/health",
        "/api/v1/dashboard",
        "/api/v1/logs/requests",
    }

    def audit_path(request: Request) -> str:
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        # Route templates preserve useful IDs structurally without persisting
        # the caller-provided path values. Unknown paths stay anonymous too.
        return template if isinstance(template, str) else "<unmatched>"

    @app.middleware("http")
    async def audit_request(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        started = time.perf_counter()
        request_id = secrets.token_hex(8)
        response = None
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_path = request.url.path
            if request_path not in audit_excluded_paths:
                client_host = request.client.host if request.client else "unknown"
                request_audit.record(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "request_id": request_id,
                        "client_ip": str(client_host)[:64],
                        "method": request.method,
                        "path": audit_path(request),
                        "status_code": response.status_code if response is not None else 500,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )

    async def check_account(account_id: str) -> dict[str, Any]:
        account = files.account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        platform = str(account.get("platform", "douyin"))
        if not files.read_cookie(account_id, platform=platform):
            return (await files.set_status(account_id, "needs_login", error="No saved session.")) or {}
        if platform == "xhs":
            await files.set_status(account_id, "checking")
            try:
                cookie = files.read_cookie(account_id, platform="xhs")
                if not cookie:
                    raise RuntimeError("No saved session.")
                user = await asyncio.to_thread(xhs_client.current_user, cookie)
            except Exception as exc:
                message = str(exc)
                auth_markers = ("-101", "-104", "登录", "unauthorized", "HTTP 401", "HTTP 403")
                status = "needs_login" if any(marker.lower() in message.lower() for marker in auth_markers) else "unknown"
                return (await files.set_status(account_id, status, error=message[:240])) or {}
            nickname, avatar_url = _account_display_fields(user)
            await files.set_profile(account_id, nickname=nickname, avatar_url=avatar_url)
            return (await files.set_status(account_id, "active")) or {}
        await files.set_status(account_id, "checking")
        try:
            payload = await client.get(
                f"{DOUYIN_ORIGIN}/aweme/v1/web/user/profile/self/",
                _base_params(),
                account_id=account_id,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            upstream_status = detail.get("upstream_status")
            status = "needs_login" if upstream_status in (401, 403) else "unknown"
            return (
                await files.set_status(account_id, status, error="Could not verify the Douyin session.")
            ) or {}
        if payload.get("status_code") in (None, 0, "0"):
            nickname, avatar_url = _account_display_fields(payload)
            await files.set_profile(account_id, nickname=nickname, avatar_url=avatar_url)
            return (await files.set_status(account_id, "active")) or {}
        return (
            await files.set_status(account_id, "needs_login", error="Douyin did not accept this session.")
        ) or {}

    logins.on_login = check_account

    async def monitor_accounts() -> None:
        while True:
            await asyncio.sleep(app.state.account_check_interval)
            for account in files.list_accounts():
                if account["status"] == "active":
                    try:
                        await check_account(account["account_id"])
                    except Exception:
                        # The account's last state remains visible; an individual
                        # check must never stop monitoring all other accounts.
                        continue

    @app.on_event("startup")
    async def start_monitor() -> None:
        app.state.account_monitor = asyncio.create_task(monitor_accounts())

    @app.on_event("shutdown")
    async def stop_monitor() -> None:
        app.state.account_monitor.cancel()

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        accounts = files.list_accounts()
        return {
            "status": "ok",
            "accounts": {
                "total": len(accounts),
                "active": sum(account["status"] == "active" for account in accounts),
                "needs_login": sum(account["status"] == "needs_login" for account in accounts),
                "douyin": len([account for account in accounts if account["platform"] == "douyin"]),
                "xhs": len([account for account in accounts if account["platform"] == "xhs"]),
            },
        }

    @app.get("/api/v1/logs/requests", tags=["system"])
    async def request_logs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        """Return recent local request metadata without credentials or payloads."""
        return {
            "items": request_audit.recent(limit),
            "retention": {
                "current_file_max_bytes": request_audit.max_bytes,
                "backup_files": request_audit.backup_count,
            },
        }

    @app.get("/api/v1/dashboard", tags=["system"])
    async def dashboard_state() -> dict[str, Any]:
        """Return display-safe service and account state for the local dashboard."""
        accounts = files.list_accounts()
        return {
            "service": {
                "status": "ok",
                "version": app.version,
                "started_at": app.state.started_at.isoformat(),
                "uptime_seconds": round(time.monotonic() - app.state.started_monotonic),
                "check_interval_seconds": app.state.account_check_interval,
                "listen_address": f"http://{app.state.listen_host}:{app.state.listen_port}",
                "intranet_addresses": _intranet_addresses(app.state.listen_port, app.state.listen_host),
                "data_dir": str(files.data_dir),
            },
            "accounts": {
                "items": accounts,
                "total": len(accounts),
                "active": sum(account["status"] == "active" for account in accounts),
                "needs_login": sum(account["status"] == "needs_login" for account in accounts),
                "douyin": sum(account["platform"] == "douyin" for account in accounts),
                "xhs": sum(account["platform"] == "xhs" for account in accounts),
            },
        }

    @app.get("/api/v1/accounts", tags=["accounts"])
    async def accounts() -> dict[str, Any]:
        """List every saved login without returning any Cookie value."""
        return {"accounts": files.list_accounts()}

    @app.post("/api/v1/accounts/login", status_code=202, tags=["accounts"])
    async def login(login_request: LoginRequest) -> dict[str, Any]:
        """Open local Chrome for a new account; scan the QR code there."""
        return await logins.start(login_request.label, platform=login_request.platform)

    @app.post("/api/v1/accounts/{account_id}/relogin", status_code=202, tags=["accounts"])
    async def relogin(account_id: str) -> dict[str, Any]:
        account = files.account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        return await logins.start(
            str(account["label"]),
            platform=str(account.get("platform", "douyin")),
            account_id=account_id,
        )

    @app.post("/api/v1/accounts/{account_id}/check", tags=["accounts"])
    async def account_check(account_id: str) -> dict[str, Any]:
        """Immediately verify an account and update its visible login state."""
        return {"account": await check_account(account_id)}

    @app.delete("/api/v1/accounts/{account_id}", tags=["accounts"])
    async def delete_account(account_id: str) -> dict[str, Any]:
        """Permanently remove a saved account and its local login artefacts."""
        await logins.cancel(account_id)
        account = await files.delete_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        return {"message": "Account and its local login data were deleted", "account": account}

    @app.post("/api/v1/accounts/check-all", tags=["accounts"])
    async def check_all_accounts() -> dict[str, Any]:
        """Verify active accounts serially to avoid triggering platform rate limits."""
        checked: list[dict[str, Any]] = []
        for account in files.list_accounts():
            if account["status"] != "active":
                continue
            result = await check_account(account["account_id"])
            if result:
                checked.append(result)
        return {"checked": len(checked), "accounts": checked}

    @app.post("/api/v1/cookies", tags=["configuration"])
    async def update_cookie(update: CookieUpdate) -> dict[str, Any]:
        """Atomically replace the cookie; its plaintext is never returned."""
        return {
            "message": "Cookie updated",
            "account": await files.replace_cookie(
                update.cookie, update.label, update.account_id, platform=update.platform
            ),
        }

    def xhs_cookie(account_id: str | None) -> str:
        cookie = files.read_cookie(account_id, platform="xhs")
        if not cookie:
            raise HTTPException(status_code=409, detail="No active Xiaohongshu account is available")
        return cookie

    async def xhs_call(account_id: str | None, operation: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(operation, *args, xhs_cookie(account_id), **kwargs)
        except XhsRuntimeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            if account_id and any(marker in message.lower() for marker in ("-101", "-104", "登录", "unauthorized", "http 401", "http 403")):
                await files.set_status(account_id, "needs_login", error=message[:240])
            raise HTTPException(status_code=502, detail=message) from exc
        return result

    def xhs_account_id(account_id: str | None) -> str:
        selected_account_id = account_id or files.default_account_id("xhs")
        if not selected_account_id:
            raise HTTPException(status_code=409, detail="No active Xiaohongshu account is available")
        return selected_account_id

    @app.post("/api/v1/xhs/resolve-link", tags=["Xiaohongshu"])
    async def xhs_resolve_link(request: XhsLinkResolve) -> dict[str, str | None]:
        """Use the logged-in isolated Chrome profile to resolve an XHS link and extract xsec fields."""
        account_id = request.account_id or files.default_account_id("xhs")
        if not account_id:
            raise HTTPException(status_code=409, detail="No active Xiaohongshu account is available")
        final_url = await logins.resolve_xhs_link(request.url, account_id)
        return _xhs_link_fields(final_url, account_id)

    @app.get("/api/v1/xhs/users/{user_id}/posts", tags=["Xiaohongshu"])
    async def xhs_user_posts(
        user_id: str,
        xsec_token: str | None = Query(default=None, min_length=1, max_length=4096),
        xsec_source: Literal["pc_search", "pc_user", "pc_feed"] | None = None,
        cursor: str = Query(default="", max_length=512),
        count: int = Query(default=20, ge=1, le=30),
        account_id: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        """Return one page of a creator's published Xiaohongshu notes."""
        selected_account_id = xhs_account_id(account_id)
        if xsec_token:
            token, source = xsec_token, xsec_source or "pc_user"
        else:
            # Logged-in user_posted accepts an empty token. Prefer it because it
            # avoids a browser window and supports the requested ID-only API.
            token, source = "", "pc_user"
        try:
            result = await xhs_call(
                selected_account_id, xhs_client.user_posts, user_id, cursor=cursor, count=count,
                xsec_token=token, xsec_source=source,
            )
        except HTTPException as exc:
            # Most creator pages accept an empty token. If this specific page
            # does not, retry once after silent, headless token resolution. An
            # explicit caller token is never replaced or retried automatically.
            if xsec_token or exc.status_code != 502:
                raise
            fields = await logins.resolve_xhs_identifier_silently(
                "user", user_id, selected_account_id
            )
            token, source = fields["xsec_token"], fields["xsec_source"]
            result = await xhs_call(
                selected_account_id, xhs_client.user_posts, user_id, cursor=cursor, count=count,
                xsec_token=token, xsec_source=source,
            )
        items = result.get("items")
        if isinstance(items, list):
            await files.cache_xhs_note_tokens(selected_account_id, items, source=source)
            # The service consumes the platform token internally; callers only
            # need the note ID from the list to request a detail record later.
            result["items"] = [
                {key: value for key, value in item.items() if key != "xsec_token"}
                if isinstance(item, Mapping) else item
                for item in items
            ]
        return result

    @app.get("/api/v1/xhs/notes/{note_id}", tags=["Xiaohongshu"])
    async def xhs_note_detail(
        note_id: str,
        xsec_token: str | None = Query(default=None, min_length=1, max_length=4096),
        xsec_source: Literal["pc_search", "pc_user", "pc_feed"] | None = None,
        account_id: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        """Return detail without ever opening a browser for an API request."""
        selected_account_id = xhs_account_id(account_id)
        if xsec_token:
            token, source = xsec_token, xsec_source or "pc_user"
        else:
            cached = files.xhs_note_token(selected_account_id, note_id)
            if cached is None:
                fields = await logins.resolve_xhs_identifier_silently(
                    "note", note_id, selected_account_id
                )
                token, source = fields["xsec_token"], fields["xsec_source"]
            else:
                token, source = cached
        return await xhs_call(
            selected_account_id, xhs_client.note_detail, note_id,
            xsec_token=token, xsec_source=source,
        )

    @app.get(
        "/api/v1/douyin/users/{sec_user_id}/posts",
        tags=["Douyin"],
    )
    async def author_posts(
        sec_user_id: str,
        cursor: str = Query(default="0", max_length=64),
        count: int = Query(default=20, ge=1, le=20),
        account_id: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        """Return one page of a creator's public works, newest first.

        `sec_user_id` is the stable ID contained in a Douyin profile URL; the
        ordinary numeric account UID is not accepted by Douyin's post endpoint.
        """
        payload = await client.get(
            USER_POST_URL,
            [
                *_base_params(), ("sec_user_id", sec_user_id), ("max_cursor", cursor),
                ("count", str(count)), ("publish_video_strategy_type", "2"),
                ("from_user_page", "1"), ("locate_query", "false"),
                ("need_time_list", "1"), ("show_live_replay_strategy", "1"),
                ("time_list_query", "0"), ("pc_libra_divert", "Windows"), ("whale_cut_token", ""),
            ], account_id=account_id,
        )
        if error := _upstream_error(payload):
            raise error
        return {
            "items": payload.get("aweme_list", []),
            "cursor": str(payload["max_cursor"]) if payload.get("has_more") and payload.get("max_cursor") is not None else None,
            "has_more": bool(payload.get("has_more")),
        }

    @app.get(
        "/api/v1/douyin/videos/{aweme_id}",
        tags=["Douyin"],
    )
    async def video_detail(
        aweme_id: str, account_id: str | None = Query(default=None, max_length=64)
    ) -> dict[str, Any]:
        """Return the raw public detail record for one work ID."""
        payload = await client.get(
            VIDEO_DETAIL_URL, [*_base_params(), ("aweme_id", aweme_id)], account_id=account_id
        )
        if error := _upstream_error(payload):
            raise error
        detail = payload.get("aweme_detail")
        if not isinstance(detail, dict):
            raise HTTPException(status_code=502, detail="Douyin response has no aweme_detail")
        return {"item": detail}

    dashboard_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; img-src https: data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

    @app.get("/", include_in_schema=False)
    async def root_page() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=307)

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/accounts", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        """Serve the local, dependency-free administration panel."""
        return HTMLResponse(DASHBOARD_HTML, headers=dashboard_headers)

    return app


app = create_app()
