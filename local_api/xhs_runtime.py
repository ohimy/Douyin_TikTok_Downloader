"""Minimal Xiaohongshu web client adapted from Nirvana919/XHS_Crawler.

Only the signed public-read operations needed by this application are kept:
one page of a creator's published notes, one note detail record, and a session
health probe.  Cookies continue to be owned by ``RuntimeFiles`` in app.py.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import execjs
import requests

XHS_ORIGIN = "https://edith.xiaohongshu.com"
_RUNTIME_DIRNAME = "xhs_assets"


def _runtime_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "local_api" / _RUNTIME_DIRNAME
    return Path(__file__).resolve().parent / _RUNTIME_DIRNAME


class XhsRuntimeUnavailable(RuntimeError):
    """Raised when the signed-request runtime was not prepared for this build."""


class XhsRiskControlError(RuntimeError):
    """The platform asked this session to slow down or complete verification."""

    def __init__(self, status_code: int, platform_code: int | str | None = None) -> None:
        self.status_code = status_code
        self.platform_code = platform_code
        detail = f"HTTP {status_code}"
        if platform_code is not None:
            detail += f", code {platform_code}"
        super().__init__(f"Xiaohongshu risk control response: {detail}")


class XhsClient:
    """Synchronous signed XHS client; API handlers run it in a worker thread."""

    def __init__(self) -> None:
        self.runtime_dir = _runtime_dir()
        self._signer: Any | None = None

    def _ensure_signer(self) -> Any:
        if self._signer is not None:
            return self._signer
        script = self.runtime_dir / "xhs_main_260411.js"
        if not script.is_file():
            raise XhsRuntimeUnavailable(
                "Xiaohongshu runtime assets are missing. Run tools/fetch_xhs_assets.py before development or use build_windows.bat."
            )
        node_dir = self.runtime_dir / "node"
        node_path = node_dir / ("node.exe" if os.name == "nt" else "node")
        if node_path.is_file():
            os.environ["PATH"] = f"{node_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        node_modules = self.runtime_dir / "node_modules"
        if not node_modules.is_dir():
            raise XhsRuntimeUnavailable("Xiaohongshu runtime dependencies are missing: crypto-js.")
        old_node_path = os.environ.get("NODE_PATH", "")
        os.environ["NODE_PATH"] = f"{node_modules}{os.pathsep}{old_node_path}".rstrip(os.pathsep)
        try:
            self._signer = execjs.compile(script.read_text(encoding="utf-8"))
        except Exception as exc:
            raise XhsRuntimeUnavailable(f"Could not start Xiaohongshu signing runtime: {exc}") from exc
        return self._signer

    @staticmethod
    def _cookie_map(cookie: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if name and separator:
                result[name] = value
        return result

    @staticmethod
    def _headers(cookie: dict[str, str], api: str, data: dict[str, Any] | str, method: str, signer: Any) -> tuple[dict[str, str], str]:
        a1 = cookie.get("a1")
        if not a1:
            raise ValueError("The Xiaohongshu Cookie is missing a1; please log in again.")
        try:
            signed = signer.call("get_request_headers_params", api, data, a1, method)
        except Exception as exc:
            raise XhsRuntimeUnavailable(f"Xiaohongshu signing failed: {exc}") from exc
        return (
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json;charset=UTF-8",
                "origin": "https://www.xiaohongshu.com",
                "referer": "https://www.xiaohongshu.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "x-s": signed["xs"],
                "x-t": str(signed["xt"]),
                "x-s-common": signed["xs_common"],
                "x-b3-traceid": "".join(random.choice("abcdef0123456789") for _ in range(16)),
            },
            json.dumps(data, separators=(",", ":"), ensure_ascii=False) if data else "",
        )

    def _request(self, method: str, api: str, cookie_header: str, data: dict[str, Any] | str = "") -> dict[str, Any]:
        cookies = self._cookie_map(cookie_header)
        headers, body = self._headers(cookies, api, data, method, self._ensure_signer())
        try:
            response = requests.request(
                method, f"{XHS_ORIGIN}{api}", headers=headers, cookies=cookies,
                data=body or None, timeout=20,
            )
            # 461/471 are verification/risk-control responses rather than
            # transient transport failures. Do not parse/retry them here: the
            # API layer applies a shared cooldown before accepting more work.
            if response.status_code in (429, 461, 471):
                raise XhsRiskControlError(response.status_code)
            payload = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Xiaohongshu: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Xiaohongshu returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Xiaohongshu returned an unexpected response")
        if response.status_code >= 400:
            raise RuntimeError(f"Xiaohongshu returned HTTP {response.status_code}: {payload.get('msg', '')}")
        return payload

    @staticmethod
    def _reject_if_failed(payload: dict[str, Any]) -> None:
        if payload.get("success") is not True:
            code = payload.get("code")
            # These documented-in-the-wild payload codes accompany risk and
            # verification pages even when the HTTP layer itself is 200.
            if str(code) in {"300012", "300031"}:
                raise XhsRiskControlError(200, code)
            message = payload.get("msg") or "Xiaohongshu rejected the request"
            raise RuntimeError(f"Xiaohongshu error {code}: {message}")

    def current_user(self, cookie_header: str) -> dict[str, Any]:
        payload = self._request("GET", "/api/sns/web/v1/user/selfinfo", cookie_header)
        self._reject_if_failed(payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Xiaohongshu response has no current-user data")
        return data

    def check_session(self, cookie_header: str) -> dict[str, Any]:
        return self.current_user(cookie_header)

    def user_posts(
        self, user_id: str, cookie_header: str, *, cursor: str, count: int, xsec_token: str, xsec_source: str
    ) -> dict[str, Any]:
        query = urlencode({
            "num": str(count), "cursor": cursor, "user_id": user_id,
            "image_formats": "jpg,webp,avif", "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        })
        payload = self._request("GET", f"/api/sns/web/v1/user_posted?{query}", cookie_header)
        self._reject_if_failed(payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Xiaohongshu response has no post list")
        return {
            "items": data.get("notes", []),
            "cursor": str(data["cursor"]) if data.get("has_more") and data.get("cursor") is not None else None,
            "has_more": bool(data.get("has_more")),
        }

    def note_detail(self, note_id: str, cookie_header: str, *, xsec_token: str, xsec_source: str) -> dict[str, Any]:
        payload = self._request(
            "POST", "/api/sns/web/v1/feed", cookie_header,
            {
                "source_note_id": note_id,
                "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": "1"},
                "xsec_source": xsec_source,
                "xsec_token": xsec_token,
            },
        )
        self._reject_if_failed(payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Xiaohongshu response has no note detail")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise RuntimeError("Xiaohongshu response has no note detail")
        item = items[0]
        if not isinstance(item, dict):
            raise RuntimeError("Xiaohongshu response has an invalid note detail")
        return {"item": item}
