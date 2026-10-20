"""Windows desktop entrypoint: API server and native management window.

Local API fork addition; modification notice dated 2026-10-20.
See LICENSE, NOTICE, and MODIFICATIONS.md for attribution and local changes.
"""

from __future__ import annotations

import os
import threading

import uvicorn

from local_api.app import app
from local_api.desktop import launch_native_dashboard


def main() -> None:
    host = os.getenv("DOUYIN_LOCAL_API_HOST", "0.0.0.0")
    port = int(os.getenv("DOUYIN_LOCAL_API_PORT", "8787"))
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", use_colors=False)
    )
    thread = threading.Thread(target=server.run, name="local-api-server", daemon=True)
    thread.start()

    def stop_server() -> None:
        server.should_exit = True

    launch_native_dashboard(f"http://127.0.0.1:{port}", stop_server)


if __name__ == "__main__":
    main()
