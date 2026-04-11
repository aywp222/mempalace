#!/usr/bin/env python3
"""
MemPalace HTTP MCP Server — 单实例多窗口共享

问题：VS Code 每个窗口 stdio 模式各自加载一份完整的 HNSW 索引 (~2.5GB)。
方案：保持一个常驻 HTTP MCP 服务器，所有窗口通过 HTTP 共享，HNSW 只加载一次。

用法:
  python http_server.py [--port 47291] [--host 127.0.0.1] [--palace PATH]

VS Code mcp.json 配置 (替换原来的 stdio 配置):
  {
    "servers": {
      "mempalace": {
        "type": "http",
        "url": "http://127.0.0.1:47291/mcp"
      }
    }
  }
"""

import argparse

# 先解析 HTTP 服务器参数（--port / --host），剩余参数（含 --palace）
# 会被 mcp_server._parse_args() 通过 sys.argv 自动读取。
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--port", type=int, default=47291)
_parser.add_argument("--host", default="127.0.0.1")
_srv_args, _ = _parser.parse_known_args()

import asyncio
import logging
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn

logger = logging.getLogger("mempalace_http")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# 在外层 arg 解析完成后再导入，确保 mcp_server._parse_args() 能正确读到 --palace
from mempalace.mcp_server import handle_request

app = FastAPI(title="MemPalace MCP HTTP", docs_url=None, redoc_url=None)


def _sse_event(data: str, event: str = "message") -> str:
    return f"event: {event}\ndata: {data}\n\n"


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP transport 端点 (JSON-RPC 2.0 over POST)"""
    accept = request.headers.get("accept", "")
    logger.info(f"POST /mcp accept={accept!r} headers={dict(request.headers)}")

    body = await request.json()
    logger.info(f"POST /mcp body={body}")

    if isinstance(body, list):
        results = await asyncio.gather(
            *[asyncio.to_thread(handle_request, req) for req in body]
        )
        out = [r for r in results if r is not None]
        resp_data = out if out else []
    else:
        result = await asyncio.to_thread(handle_request, body)
        resp_data = result

    if resp_data is None:
        return Response(status_code=202)

    import json
    # 如果客户端接受 SSE，以 SSE 格式返回（Streamable HTTP spec 要求）
    if "text/event-stream" in accept:
        async def sse_gen():
            yield _sse_event(json.dumps(resp_data))
        return StreamingResponse(sse_gen(), media_type="text/event-stream")

    return JSONResponse(resp_data)


@app.get("/mcp")
async def mcp_sse_endpoint(request: Request):
    """GET /mcp — MCP Streamable HTTP 服务器推送端点（保持连接，等待服务器发起的事件）"""
    logger.info(f"GET /mcp (server-sent events stream opened)")
    # 基础实现：保持 SSE 连接，定期发送心跳。mempalace 无须服务器主动推送，
    # 但 VS Code 仍需要此端点存在以完成协议握手。
    async def keep_alive():
        while True:
            if await request.is_disconnected():
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(keep_alive(), media_type="text/event-stream")


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "mempalace-mcp-http", "port": _srv_args.port}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=_srv_args.host,
        port=_srv_args.port,
        log_level="info",
        access_log=True,
    )
