"""Console entrypoint used in development and by the packaged executable.

Local API fork addition; modification notice dated 2026-10-20.
See LICENSE, NOTICE, and MODIFICATIONS.md for attribution and local changes.
"""

from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from local_api.app import app


def main() -> None:
    host = os.getenv("DOUYIN_LOCAL_API_HOST", "0.0.0.0")
    port = int(os.getenv("DOUYIN_LOCAL_API_PORT", "8787"))
    dashboard_url = f"http://127.0.0.1:{port}/dashboard"
    print(f"Douyin and Xiaohongshu Local API is listening on http://{host}:{port}")
    print(f"Management panel: {dashboard_url}")
    print(f"API reference: http://127.0.0.1:{port}/docs")
    print(f"Cookie data directory: {app.state.files.data_dir}")
    if os.getenv("DOUYIN_LOCAL_API_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no"}:
        # Delay until Uvicorn has bound the socket. This makes a double-clicked
        # Windows executable immediately useful while retaining an opt-out for
        # server deployments.
        threading.Timer(0.8, lambda: webbrowser.open(dashboard_url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
