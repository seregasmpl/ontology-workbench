from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn

from workbench.api import create_app


def main():
    parser = argparse.ArgumentParser(description="Local ontology workbench")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    data_dir = args.data_dir or root / "data"
    app = create_app(data_dir)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    if not args.no_browser:
        def open_browser():
            for _ in range(100):
                try:
                    if httpx.get(url + "/api/session", timeout=1, trust_env=False).status_code == 200:
                        webbrowser.open(url)
                        return
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
        threading.Thread(target=open_browser, daemon=True).start()
    print(f"Ontology Workbench: {url}\nData: {data_dir}\nClose with Ctrl+C or the application button.", flush=True)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
