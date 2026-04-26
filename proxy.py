#!/usr/bin/env python3
"""
MemPalace Stdio-to-HTTP Proxy

VS Code 用 stdio 启动这个轻量代理（~10MB），
代理把 JSON-RPC 请求转发给单一的 HTTP 服务器进程（端口 47291）。
HNSW 索引只在 HTTP 服务器里加载一次，不会随 VS Code 窗口数量增长。

如果 HTTP 服务器未运行，代理会自动在后台启动它。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HTTP_URL = "http://127.0.0.1:47291/mcp"
HEALTH_URL = "http://127.0.0.1:47291/health"

# 服务器可执行文件路径（与本文件同目录）
_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON = os.path.join(_HERE, ".venv", "bin", "python")
_SERVER = os.path.join(_HERE, "http_server.py")
_START_SCRIPT = os.path.join(_HERE, "scripts", "start_memory_service.sh")
_LAUNCH_LABEL = os.environ.get("MEMPALACE_LAUNCH_LABEL", "com.mempalace.mcp-http")
_LAUNCH_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCH_LABEL}.plist")


def _log_handle():
    log_dir = os.path.expanduser("~/.mempalace/logs")
    os.makedirs(log_dir, exist_ok=True)
    return open(os.path.join(log_dir, "mcp-http-error.log"), "a")


def _server_alive() -> bool:
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=2)
        return True
    except Exception:
        return False


def _ensure_server():
    """如果 HTTP 服务器没在运行，后台启动它并等待就绪。"""
    if _server_alive():
        return

    log = _log_handle()
    if sys.platform == "darwin" and os.path.exists(_LAUNCH_PLIST):
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_LAUNCH_LABEL}"],
            stdout=log,
            stderr=log,
            check=False,
        )
        for _ in range(10):
            time.sleep(0.5)
            if _server_alive():
                return

    if os.path.exists(_START_SCRIPT):
        subprocess.Popen(
            [_START_SCRIPT],
            cwd=_HERE,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    else:
        subprocess.Popen(
            [_PYTHON, _SERVER, "--port", "47291", "--host", "127.0.0.1"],
            cwd=_HERE,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    # 最多等 15 秒
    for _ in range(30):
        time.sleep(0.5)
        if _server_alive():
            return
    raise RuntimeError("mempalace HTTP server failed to start in time")


def _post(payload: dict) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HTTP_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 202:
                return None  # notification, no response
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32000, "message": f"HTTP {e.code}: {e.reason}"},
        }


def main():
    _ensure_server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _post(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
