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
import sys

# 先解析 HTTP 服务器参数（--port / --host），剩余参数（含 --palace）
# 会被 mcp_server._parse_args() 通过 sys.argv 自动读取。
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--port", type=int, default=47291)
_parser.add_argument("--host", default="127.0.0.1")
_srv_args, _ = _parser.parse_known_args()

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn

logger = logging.getLogger("mempalace_http")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# 在外层 arg 解析完成后再导入，确保 mcp_server._parse_args() 能正确读到 --palace
from mempalace.mcp_server import handle_request
from mempalace import mcp_server as _mcp

_SINGLETON_LOCK_HANDLE = None


def _singleton_lock_path() -> Path:
    run_dir = Path(os.path.expanduser(os.environ.get("MEMPALACE_RUN_DIR", "~/.mempalace/run")))
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / f"memory-service-{_srv_args.host}-{_srv_args.port}.lock"


def _acquire_singleton_lock() -> None:
    """Prevent multiple HTTP backends from loading the same palace index."""
    global _SINGLETON_LOCK_HANDLE
    if os.environ.get("MEMPALACE_DISABLE_SINGLETON") == "1":
        return
    try:
        import fcntl
    except ImportError:
        return

    lock_path = _singleton_lock_path()
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        print(f"MemPalace HTTP service is already running: {owner}", file=sys.stderr)
        raise SystemExit(0)

    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": _srv_args.host,
                "port": _srv_args.port,
                "python": sys.executable,
                "cwd": os.getcwd(),
                "started_at": datetime.now().isoformat(),
            },
            ensure_ascii=False,
        )
    )
    handle.flush()
    _SINGLETON_LOCK_HANDLE = handle

app = FastAPI(title="MemPalace MCP HTTP", docs_url=None, redoc_url=None)

# CORS: 允许任意本地源（nginx 静态页面在不同端口）调用 REST API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    return {
        "status": "ok",
        "service": "mempalace-mcp-http",
        "host": _srv_args.host,
        "port": _srv_args.port,
        "pid": os.getpid(),
        "python": sys.executable,
        "palace_path": _mcp._config.palace_path,
    }


# =====================================================================
# REST API — 给本地静态前端使用（CORS 已开启）
# =====================================================================

def _run(fn, **kwargs):
    """在线程池中调用同步的 mcp_server tool 函数。"""
    return asyncio.to_thread(fn, **kwargs)


def _count_drawers_sqlite(wing: str = None, room: str = None) -> int:
    """Count filtered drawers via SQLite metadata.

    Chroma's Python count() in the currently installed version does not accept
    a `where` parameter, so filtered counts must be resolved separately.
    """
    import os
    import sqlite3

    db_path = os.path.join(_mcp._config.palace_path, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        sql = ["SELECT COUNT(DISTINCT e.embedding_id) FROM embeddings e"]
        params = []
        if wing:
            sql.append(
                "JOIN embedding_metadata emw ON e.id = emw.id AND emw.key = 'wing' AND emw.string_value = ?"
            )
            params.append(wing)
        if room:
            sql.append(
                "JOIN embedding_metadata emr ON e.id = emr.id AND emr.key = 'room' AND emr.string_value = ?"
            )
            params.append(room)
        return conn.execute(" ".join(sql), params).fetchone()[0] or 0
    finally:
        conn.close()


# =====================================================================
# Dual-layer memory bridge (L1 file memory + L2 palace memory)
# =====================================================================

_TYPE_TO_ROOM = {
    "user": "user",
    "feedback": "feedback",
    "project": "project",
    "reference": "reference",
}


def _slug(text: str, max_len: int = 48) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-")
    if not token:
        token = "memory"
    return token[:max_len]


def _guess_cc_project_memory_dir(project_path: str) -> str:
    normalized = os.path.abspath(os.path.expanduser(project_path))
    slug = "-" + normalized.replace(":", "").replace("\\", "-").replace("/", "-")
    return os.path.join(os.path.expanduser("~/.claude/projects"), slug, "memory")


def _default_l1_dir() -> str:
    configured = os.environ.get("MEMPALACE_L1_DIR", "~/.claude/memory-bridge")
    return os.path.abspath(os.path.expanduser(configured))


def _resolve_l1_dir(
    l1_dir: Optional[str] = None,
    project_path: Optional[str] = None,
    create: bool = False,
) -> str:
    candidates = []
    if l1_dir:
        candidates.append(os.path.abspath(os.path.expanduser(l1_dir)))
    if project_path:
        candidates.append(_guess_cc_project_memory_dir(project_path))
    candidates.append(_default_l1_dir())

    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate

    target = candidates[0]
    if create:
        os.makedirs(target, exist_ok=True)
        return target
    raise ValueError(
        f"L1 memory dir not found: {target}. Provide l1_dir or project_path, "
        "or set MEMPALACE_L1_DIR."
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text

    raw = text[4:end].strip()
    body = text[end + 5 :].lstrip()
    meta = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    return meta, body


def _memory_md_files(l1_dir: str, limit: int = 1500) -> list[Path]:
    root = Path(l1_dir)
    out = []
    for p in root.rglob("*.md"):
        if p.name == "MEMORY.md":
            continue
        if p.name.startswith("."):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _pick_preview_line(text: str, query_lower: str, tokens: list[str]) -> str:
    for line in text.splitlines():
        lower = line.lower()
        if query_lower in lower:
            return line.strip()
        if any(t in lower for t in tokens):
            return line.strip()
    return text.strip().splitlines()[0] if text.strip() else ""


def _cjk_ngrams(text: str, n: int = 2) -> set:
    chars = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
    if len(chars) < n:
        return set(chars) if chars else set()
    return {"".join(chars[i : i + n]) for i in range(len(chars) - n + 1)}


def _search_l1_memories(l1_dir: str, query: str, limit: int = 5) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    tokens = [t for t in re.findall(r"\w+", q) if len(t) >= 2]
    q_cjk = _cjk_ngrams(q)
    hits = []
    for path in _memory_md_files(l1_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        exact = lower.count(q)
        token_hits = sum(lower.count(t) for t in tokens)
        cjk_hits = 0
        if q_cjk:
            cjk_hits = len(q_cjk.intersection(_cjk_ngrams(lower)))
        score = exact * 4 + token_hits + cjk_hits * 2
        if score <= 0:
            continue

        meta, body = _split_frontmatter(text)
        preview = _pick_preview_line(body, q, tokens)
        if len(preview) > 220:
            preview = preview[:220] + "..."
        hits.append(
            {
                "layer": "l1",
                "path": str(path),
                "name": meta.get("name", path.stem),
                "type": meta.get("type", ""),
                "score": score,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                "preview": preview,
            }
        )

    hits.sort(key=lambda h: (h["score"], h["modified_at"]), reverse=True)
    return hits[:limit]


def _bridge_state_path(l1_dir: str) -> Path:
    return Path(l1_dir) / ".mempalace_bridge_state.json"


def _load_bridge_state(l1_dir: str) -> dict:
    state_path = _bridge_state_path(l1_dir)
    if not state_path.exists():
        return {"promoted": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"promoted": {}}


def _save_bridge_state(l1_dir: str, state: dict) -> None:
    state_path = _bridge_state_path(l1_dir)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_l1_memory(
    l1_dir: str,
    content: str,
    title: str,
    memory_type: str = "project",
    source: str = "bridge-service",
) -> dict:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    notes_dir = Path(l1_dir) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{now}_{_slug(title)}.md"
    note_path = notes_dir / filename

    fm_type = memory_type if memory_type in _TYPE_TO_ROOM else "project"
    note_text = (
        "---\n"
        f"name: {title}\n"
        f"description: {title}\n"
        f"type: {fm_type}\n"
        f"source: {source}\n"
        f"created_at: {datetime.now().isoformat()}\n"
        "---\n\n"
        f"{content.strip()}\n"
    )
    note_path.write_text(note_text, encoding="utf-8")

    # Keep a concise index entry for the L1 layer.
    index_path = Path(l1_dir) / "MEMORY.md"
    relative_path = f"notes/{filename}"
    index_line = f"- [{title}]({relative_path}) - {memory_type}\n"
    if index_path.exists():
        old = index_path.read_text(encoding="utf-8", errors="replace")
        if relative_path not in old:
            index_path.write_text(old.rstrip() + "\n" + index_line, encoding="utf-8")
    else:
        index_path.write_text("# MEMORY\n\n" + index_line, encoding="utf-8")

    return {
        "path": str(note_path),
        "index": str(index_path),
        "title": title,
        "type": fm_type,
    }


def _promote_l1_to_l2(
    l1_dir: str,
    wing: str,
    default_room: str,
    max_files: int = 200,
) -> dict:
    state = _load_bridge_state(l1_dir)
    promoted = state.setdefault("promoted", {})
    scanned = 0
    pushed = 0
    skipped = 0
    errors = []
    drawers = []

    for path in _memory_md_files(l1_dir, limit=max_files):
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"{path}: {e}")
            continue

        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        key = str(path)
        if promoted.get(key, {}).get("sha256") == digest:
            skipped += 1
            continue

        meta, body = _split_frontmatter(text)
        source_room = meta.get("room") or _TYPE_TO_ROOM.get(meta.get("type", ""), default_room)
        room = source_room or default_room or "general"
        result = _mcp.tool_add_drawer(
            wing=wing,
            room=room,
            content=body.strip() or text.strip(),
            source_file=str(path),
            added_by="bridge-service",
        )
        if result.get("success"):
            pushed += 1
            drawer_id = result.get("drawer_id", "")
            promoted[key] = {
                "sha256": digest,
                "drawer_id": drawer_id,
                "promoted_at": datetime.now().isoformat(),
            }
            drawers.append(drawer_id)
        else:
            errors.append(f"{path}: {result.get('error', 'unknown error')}")

    _save_bridge_state(l1_dir, state)
    return {
        "l1_dir": l1_dir,
        "wing": wing,
        "default_room": default_room,
        "scanned": scanned,
        "pushed": pushed,
        "skipped": skipped,
        "drawer_ids": drawers,
        "errors": errors,
    }


@app.get("/api/stats")
async def api_stats():
    """总抽屉数 + wings/rooms 分组统计 + 磁盘占用"""
    import os
    from collections import defaultdict
    from pathlib import Path

    col = await _run(_mcp._get_collection)
    if not col:
        raise HTTPException(503, "palace not initialized")

    total = await asyncio.to_thread(col.count)
    wing_rooms: dict = defaultdict(lambda: defaultdict(int))
    BATCH = 5000
    offset = 0
    while offset < total:
        r = await asyncio.to_thread(
            col.get, limit=BATCH, offset=offset, include=["metadatas"]
        )
        for m in r["metadatas"]:
            m = m or {}
            wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1
        offset += BATCH

    palace_dir = Path(_mcp._config.palace_path).parent
    total_bytes = 0
    try:
        for f in palace_dir.rglob("*"):
            if f.is_file():
                try:
                    total_bytes += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass

    wings = []
    for wing, rooms in sorted(wing_rooms.items()):
        wings.append({
            "wing": wing,
            "total": sum(rooms.values()),
            "rooms": [
                {"room": r, "count": c}
                for r, c in sorted(rooms.items(), key=lambda x: -x[1])
            ],
        })
    return {
        "total_drawers": total,
        "wings": wings,
        "disk_bytes": total_bytes,
    }


@app.get("/api/wings")
async def api_wings():
    return await _run(_mcp.tool_list_wings)


@app.get("/api/rooms")
async def api_rooms(wing: str = None):
    return await _run(_mcp.tool_list_rooms, wing=wing)


@app.get("/api/drawers")
async def api_list_drawers(
    wing: str = None,
    room: str = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "filed_at_desc",   # filed_at_desc | filed_at_asc | none
):
    """列出抽屉，默认按 filed_at 倒序（最新在前）。"""
    col = await _run(_mcp._get_collection)
    if not col:
        raise HTTPException(503, "palace not initialized")

    fast_sort_threshold = 5000

    # 构造 where
    conditions = []
    if wing:
        conditions.append({"wing": wing})
    if room:
        conditions.append({"room": room})
    where = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    total = await asyncio.to_thread(_count_drawers_sqlite, wing, room) if where else await asyncio.to_thread(col.count)

    # 小结果集保留全局排序；大结果集走快速分页路径，避免像 slotgame 这样的大 wing
    # 因为全量读取 metadata 而卡死 UI。
    use_global_sort = sort in ("filed_at_desc", "filed_at_asc") and total <= fast_sort_threshold

    if use_global_sort:
        get_kwargs = {"include": ["metadatas"]}
        if where:
            get_kwargs["where"] = where
        all_meta = await asyncio.to_thread(col.get, **get_kwargs)
        ids = all_meta.get("ids", [])
        metas = all_meta.get("metadatas", [])

        pairs = list(zip(ids, metas))
        rev = sort.endswith("desc")
        pairs.sort(
            key=lambda p: (p[1] or {}).get("filed_at", ""),
            reverse=rev,
        )

        page_pairs = pairs[offset : offset + limit]
        if not page_pairs:
            return {"drawers": [], "total": total, "offset": offset, "limit": limit}

        page_ids = [p[0] for p in page_pairs]
        docs = await asyncio.to_thread(
            col.get, ids=page_ids, include=["documents", "metadatas"]
        )
        by_id = {
            did: (docs["documents"][i], docs["metadatas"][i])
            for i, did in enumerate(docs["ids"])
        }
    else:
        page_kwargs = {
            "limit": limit,
            "offset": offset,
            "include": ["documents", "metadatas"],
        }
        if where:
            page_kwargs["where"] = where
        docs = await asyncio.to_thread(col.get, **page_kwargs)
        page_ids = docs.get("ids", [])
        if not page_ids:
            return {"drawers": [], "total": total, "offset": offset, "limit": limit}

        page_pairs = list(zip(page_ids, docs.get("metadatas", []), docs.get("documents", [])))
        if sort in ("filed_at_desc", "filed_at_asc"):
            rev = sort.endswith("desc")
            page_pairs.sort(
                key=lambda p: (p[1] or {}).get("filed_at", ""),
                reverse=rev,
            )
        by_id = {
            did: (doc, meta)
            for did, meta, doc in page_pairs
        }
        page_ids = [did for did, _, _ in page_pairs]

    drawers = []
    for did in page_ids:
        if did not in by_id:
            continue
        doc, meta = by_id[did]
        meta = meta or {}
        drawers.append({
            "drawer_id": did,
            "wing": meta.get("wing", ""),
            "room": meta.get("room", ""),
            "filed_at": meta.get("filed_at", ""),
            "added_by": meta.get("added_by", ""),
            "source_file": meta.get("source_file", ""),
            "content_preview": (doc[:200] + "...") if doc and len(doc) > 200 else (doc or ""),
        })
    return {
        "drawers": drawers,
        "total": total,
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "sort_mode": "global" if use_global_sort else "windowed",
    }


@app.get("/api/drawers/{drawer_id}")
async def api_get_drawer(drawer_id: str):
    res = await _run(_mcp.tool_get_drawer, drawer_id=drawer_id)
    if isinstance(res, dict) and res.get("error"):
        raise HTTPException(404, res["error"])
    return res


@app.post("/api/drawers")
async def api_add_drawer(req: Request):
    body = await req.json()
    if not body.get("wing") or not body.get("room") or not body.get("content"):
        raise HTTPException(400, "wing, room, content are required")
    return await _run(
        _mcp.tool_add_drawer,
        wing=body["wing"],
        room=body["room"],
        content=body["content"],
        source_file=body.get("source_file"),
        added_by=body.get("added_by", "web-ui"),
    )


@app.delete("/api/drawers/{drawer_id}")
async def api_delete_drawer(drawer_id: str):
    res = await _run(_mcp.tool_delete_drawer, drawer_id=drawer_id)
    if isinstance(res, dict) and not res.get("success"):
        raise HTTPException(404, res.get("error", "delete failed"))
    return res


@app.post("/api/search")
async def api_search(req: Request):
    body = await req.json()
    query = body.get("query") or ""
    if not query.strip():
        raise HTTPException(400, "query is required")
    return await _run(
        _mcp.tool_search,
        query=query,
        limit=body.get("limit", 10),
        wing=body.get("wing"),
        room=body.get("room"),
    )


@app.post("/api/check_duplicate")
async def api_check_duplicate(req: Request):
    body = await req.json()
    return await _run(
        _mcp.tool_check_duplicate,
        content=body.get("content", ""),
        threshold=body.get("threshold", 0.9),
    )


@app.get("/api/bridge/config")
async def api_bridge_config():
    """Dual-layer bridge runtime config (L1 path + MCP endpoint)."""
    return {
        "strategy": "dual-layer-memory",
        "write_policy": "l1_first_then_promote_l2",
        "query_policy": "l1_first_then_l2",
        "default_l1_dir": _default_l1_dir(),
        "mcp_endpoint": f"http://{_srv_args.host}:{_srv_args.port}/mcp",
        "health_endpoint": f"http://{_srv_args.host}:{_srv_args.port}/health",
    }


@app.post("/api/bridge/remember")
async def api_bridge_remember(req: Request):
    """Write memory into L1 file layer, optionally promote to L2 palace."""
    body = await req.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")
    promote = bool(body.get("promote", False))
    if promote and not (body.get("wing") or "").strip():
        raise HTTPException(400, "wing is required when promote=true")

    try:
        l1_dir = _resolve_l1_dir(
            l1_dir=body.get("l1_dir"),
            project_path=body.get("project_path"),
            create=True,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    memory_type = (body.get("type") or "project").strip().lower()
    title = (body.get("title") or content[:48]).strip() or "memory"

    l1_written = await asyncio.to_thread(
        _write_l1_memory,
        l1_dir,
        content,
        title,
        memory_type,
        body.get("source", "bridge-service"),
    )

    l2_result = None
    if promote:
        wing = (body.get("wing") or "").strip()
        room = (body.get("room") or _TYPE_TO_ROOM.get(memory_type, "general")).strip()
        l2_result = await _run(
            _mcp.tool_add_drawer,
            wing=wing,
            room=room,
            content=content,
            source_file=l1_written["path"],
            added_by="bridge-service",
        )

    return {
        "success": True,
        "strategy": "l1_first_then_optional_l2",
        "l1": l1_written,
        "l2": l2_result,
    }


@app.post("/api/bridge/query")
async def api_bridge_query(req: Request):
    """Bridge query: search L1 file memory first, then L2 palace memory."""
    body = await req.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    l1_limit = max(1, min(int(body.get("l1_limit", 5)), 50))
    l2_limit = max(1, min(int(body.get("l2_limit", body.get("limit", 8))), 50))

    try:
        l1_dir = _resolve_l1_dir(
            l1_dir=body.get("l1_dir"),
            project_path=body.get("project_path"),
            create=False,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    l1_hits = await asyncio.to_thread(_search_l1_memories, l1_dir, query, l1_limit)
    l2_raw = await _run(
        _mcp.tool_search,
        query=query,
        limit=l2_limit,
        wing=body.get("wing"),
        room=body.get("room"),
    )
    l2_hits = l2_raw.get("results", []) if isinstance(l2_raw, dict) else []

    merged = []
    for i, hit in enumerate(l1_hits, 1):
        merged.append(
            {
                "rank": i,
                "layer": "l1",
                "source": hit.get("path", ""),
                "score": hit.get("score", 0),
                "preview": hit.get("preview", ""),
            }
        )
    base = len(merged)
    for i, hit in enumerate(l2_hits, 1):
        merged.append(
            {
                "rank": base + i,
                "layer": "l2",
                "wing": hit.get("wing", ""),
                "room": hit.get("room", ""),
                "score": hit.get("similarity", 0),
                "source": hit.get("source_file", ""),
                "preview": (hit.get("text", "") or "")[:220],
            }
        )

    return {
        "strategy": "l1_first_then_l2",
        "query": query,
        "l1_dir": l1_dir,
        "l1_hits": l1_hits,
        "l2": l2_raw,
        "merged": merged,
    }


@app.post("/api/bridge/promote")
async def api_bridge_promote(req: Request):
    """Promote changed L1 memory notes into L2 palace drawers."""
    body = await req.json()
    wing = (body.get("wing") or "").strip()
    if not wing:
        raise HTTPException(400, "wing is required")
    default_room = (body.get("default_room") or "general").strip()
    try:
        l1_dir = _resolve_l1_dir(
            l1_dir=body.get("l1_dir"),
            project_path=body.get("project_path"),
            create=False,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    max_files = max(1, min(int(body.get("max_files", 200)), 5000))
    result = await asyncio.to_thread(
        _promote_l1_to_l2,
        l1_dir,
        wing,
        default_room,
        max_files,
    )
    return {
        "success": True,
        "strategy": "promote_l1_to_l2",
        **result,
    }


# ===== Mining (后台挖掘任务) =====

_mine_tasks: dict = {}  # task_id -> {status, log[], started_at, finished_at, params}
_mine_lock = threading.Lock()


def _mine_worker(task_id: str, project_dir: str, wing: str, limit: int, dry_run: bool):
    from mempalace import miner as _miner

    task = _mine_tasks[task_id]
    task["status"] = "running"
    task["started_at"] = time.time()

    class _LogCapture(io.TextIOBase):
        def write(self, s):
            if s and s.strip():
                with _mine_lock:
                    task["log"].append(s.rstrip("\n"))
            return len(s)

    cap = _LogCapture()
    try:
        with contextlib.redirect_stdout(cap), contextlib.redirect_stderr(cap):
            _miner.mine(
                project_dir=project_dir,
                palace_path=_mcp._config.palace_path,
                wing_override=wing or None,
                agent="web-ui",
                limit=limit,
                dry_run=dry_run,
            )
        task["status"] = "done"
    except Exception as e:
        task["status"] = "error"
        with _mine_lock:
            task["log"].append(f"ERROR: {type(e).__name__}: {e}")
    finally:
        task["finished_at"] = time.time()
        # mining 完成后清缓存
        try:
            _mcp._metadata_cache = None
        except Exception:
            pass


@app.post("/api/mine")
async def api_mine_start(req: Request):
    body = await req.json()
    project_dir = (body.get("project_dir") or "").strip()
    wing = (body.get("wing") or "").strip()
    limit = int(body.get("limit") or 0)
    dry_run = bool(body.get("dry_run", False))

    if not project_dir:
        raise HTTPException(400, "project_dir is required")
    if not os.path.isdir(os.path.expanduser(project_dir)):
        raise HTTPException(400, f"directory not found: {project_dir}")

    task_id = uuid.uuid4().hex[:12]
    _mine_tasks[task_id] = {
        "id": task_id,
        "status": "queued",
        "log": [],
        "started_at": None,
        "finished_at": None,
        "params": {"project_dir": project_dir, "wing": wing, "limit": limit, "dry_run": dry_run},
    }
    t = threading.Thread(
        target=_mine_worker,
        args=(task_id, os.path.expanduser(project_dir), wing, limit, dry_run),
        daemon=True,
    )
    t.start()
    return {"task_id": task_id, "status": "started"}


@app.get("/api/mine/{task_id}")
async def api_mine_status(task_id: str, since: int = 0):
    """轮询挖掘任务状态。since=已读到的日志行数，只返回新增的。"""
    task = _mine_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    with _mine_lock:
        log = task["log"][since:]
        total_lines = len(task["log"])
    return {
        "task_id": task_id,
        "status": task["status"],
        "log": log,
        "total_lines": total_lines,
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "params": task["params"],
    }


@app.get("/api/mine")
async def api_mine_list():
    """列出最近的 mining 任务"""
    items = []
    for tid, t in _mine_tasks.items():
        items.append({
            "id": tid,
            "status": t["status"],
            "started_at": t["started_at"],
            "finished_at": t["finished_at"],
            "params": t["params"],
            "log_lines": len(t["log"]),
        })
    items.sort(key=lambda x: x["started_at"] or 0, reverse=True)
    return {"tasks": items[:20]}


@app.get("/api/browse")
async def api_browse(path: str = "~"):
    """简易目录浏览，给前端选项目根目录用"""
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        raise HTTPException(400, f"not a directory: {path}")
    try:
        entries = []
        for name in sorted(os.listdir(p)):
            if name.startswith("."):
                continue
            full = os.path.join(p, name)
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "is_dir": True})
        return {"path": os.path.abspath(p), "parent": os.path.dirname(os.path.abspath(p)), "entries": entries}
    except PermissionError:
        raise HTTPException(403, "permission denied")


if __name__ == "__main__":
    _acquire_singleton_lock()
    uvicorn.run(
        app,
        host=_srv_args.host,
        port=_srv_args.port,
        log_level="info",
        access_log=True,
    )
