# Local Dual-Layer Memory Service

This service keeps MCP capability for AI agents and adds a dual-layer memory bridge:

- L1: file memory (memdir style markdown notes)
- L2: MemPalace drawers (wing/room semantic retrieval)

## Start / Stop

```bash
./scripts/start_memory_service.sh
./scripts/status_memory_service.sh
./scripts/stop_memory_service.sh
# if another manager started the same port:
./scripts/stop_memory_service.sh --force
```

On macOS, `start_memory_service.sh` uses launchd by default and writes:

- `~/Library/LaunchAgents/com.mempalace.mcp-http.plist`

The launch agent points to `scripts/mempalace_service_entry.sh` inside the
checkout, so pulling new code and restarting the service upgrades the backend
without rebuilding the app bundle. Use this after moving the checkout or
changing the service command:

```bash
./scripts/start_memory_service.sh --reinstall
```

For one-off development without launchd:

```bash
./scripts/start_memory_service.sh --direct
```

Optional start args:

```bash
./scripts/start_memory_service.sh \
  --host 127.0.0.1 \
  --port 47291 \
  --palace /custom/palace/path \
  --l1-dir ~/.claude/memory-bridge
```

## MCP Endpoint (Copilot / VS Code)

HTTP MCP endpoint:

- `http://127.0.0.1:47291/mcp`

Recommended workspace MCP config (direct HTTP):

```json
{
  "servers": {
    "mempalace": {
      "type": "http",
      "url": "http://127.0.0.1:47291/mcp"
    }
  }
}
```

If you still need stdio fallback, keep using `proxy.py`.

`proxy.py` now prefers the launchd-managed singleton and only falls back to a
direct start when no launch agent exists.

## Distillation Workflow

Distillation is conservative: original drawers remain untouched. The command
scores drawers for layer promotion and writes a report with high-signal
verbatim previews.

```bash
mempalace distill --wing mshl --limit 2000
```

When the report looks good, write compact index drawers:

```bash
mempalace distill --wing mshl --min-score 7 --write
```

This creates `distilled` room entries containing drawer IDs and verbatim
previews. It does not delete noisy rooms such as `testing` or topic-specific
scratch rooms.

## Bridge APIs

### 1) Remember (L1 first, optional L2 promotion)

```bash
curl -X POST http://127.0.0.1:47291/api/bridge/remember \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "用户偏好：所有上线前先跑回归检查",
    "title": "上线流程偏好",
    "type": "feedback",
    "project_path": "/Users/wangpeng/Codes/Other/mempalace/cc-haha",
    "promote": false
  }'
```

### 2) Query (L1 first, then L2)

```bash
curl -X POST http://127.0.0.1:47291/api/bridge/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "上线前要做什么检查",
    "project_path": "/Users/wangpeng/Codes/Other/mempalace/cc-haha",
    "wing": "clawx",
    "l1_limit": 5,
    "l2_limit": 8
  }'
```

### 3) Promote (incremental L1 -> L2)

```bash
curl -X POST http://127.0.0.1:47291/api/bridge/promote \
  -H 'Content-Type: application/json' \
  -d '{
    "project_path": "/Users/wangpeng/Codes/Other/mempalace/cc-haha",
    "wing": "clawx",
    "default_room": "decision",
    "max_files": 200
  }'
```

Promotion state is tracked in:

- `<l1_dir>/.mempalace_bridge_state.json`

This prevents duplicate promotion of unchanged notes.
