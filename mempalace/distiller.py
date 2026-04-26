"""Memory distillation helpers.

This module does not delete or summarize existing drawers. It scores drawers
for layer promotion and can write a small verbatim index drawer that points at
the highest-signal memories for a wing/room.
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .palace import get_collection

IMPORTANT_ROOMS = {
    "bugfix",
    "config",
    "configuration",
    "convention",
    "decision",
    "feedback",
}

REVIEW_ROOMS = {
    "design",
    "general",
    "testing",
}

SIGNAL_PATTERNS = [
    r"根因",
    r"解决方案",
    r"修复",
    r"结论",
    r"决策",
    r"约定",
    r"相关文件",
    r"现象",
    r"原因",
    r"必须",
    r"不要",
    r"路径",
    r"端口",
    r"账号",
    r"密码",
    r"token",
    r"key",
    r"error",
    r"exception",
    r"failed",
    r"fix",
    r"bug",
    r"because",
    r"decision",
    r"convention",
]

LOW_SIGNAL_PATTERNS = [
    r"node_modules",
    r"package-lock",
    r"\.min\.js",
    r"webpack",
    r"sourceMappingURL",
]


def _text_score(text: str, meta: dict) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    room = (meta.get("room") or "").lower()
    source = (meta.get("source_file") or "").lower()
    lower = text.lower()

    if room in IMPORTANT_ROOMS:
        score += 5
        reasons.append(f"important_room:{room}")
    if room in REVIEW_ROOMS:
        score -= 1
        reasons.append(f"review_room:{room}")

    hits = 0
    for pattern in SIGNAL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            hits += 1
    if hits:
        score += min(hits * 2, 10)
        reasons.append(f"signal_terms:{hits}")

    low_hits = 0
    for pattern in LOW_SIGNAL_PATTERNS:
        if re.search(pattern, source) or re.search(pattern, lower):
            low_hits += 1
    if low_hits:
        score -= min(low_hits * 3, 9)
        reasons.append(f"low_signal_terms:{low_hits}")

    length = len(text.strip())
    if 80 <= length <= 2500:
        score += 2
        reasons.append("useful_size")
    elif length < 50:
        score -= 4
        reasons.append("too_short")
    elif length > 6000:
        score -= 3
        reasons.append("very_large")

    punctuation = sum(1 for ch in text[:1000] if ch in "{}[]();,:.=<>/\\|+-_*")
    if len(text) > 300 and punctuation / max(len(text[:1000]), 1) > 0.22:
        score -= 3
        reasons.append("code_like_density")

    if source.endswith((".md", ".txt")):
        score += 1
        reasons.append("note_source")
    elif source.endswith((".js", ".ts", ".map", ".bundle")):
        score -= 1
        reasons.append("code_source")

    if "/packages/" in source or source.endswith("package.json"):
        score -= 3
        reasons.append("dependency_source")

    return score, reasons


def _tier(score: int, min_score: int) -> str:
    if score >= min_score:
        return "promote"
    if score >= 2:
        return "review"
    return "archive_candidate"


def _preview(text: str, max_chars: int = 260) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _iter_drawers(col, wing: str = None, room: str = None, limit: int = 0):
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

    total = col.count()
    seen = 0
    offset = 0
    batch_size = 1000
    while offset < total:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        ids = batch.get("ids") or []
        docs = batch.get("documents") or []
        metas = batch.get("metadatas") or []
        if not ids:
            break
        for drawer_id, doc, meta in zip(ids, docs, metas):
            yield drawer_id, doc or "", meta or {}
            seen += 1
            if limit and seen >= limit:
                return
        offset += len(ids)


def analyze(
    palace_path: str,
    wing: str = None,
    room: str = None,
    limit: int = 0,
    top_per_room: int = 8,
    min_score: int = 7,
) -> dict:
    col = get_collection(palace_path, create=False)
    rooms = defaultdict(
        lambda: {
            "total": 0,
            "promote": 0,
            "review": 0,
            "archive_candidate": 0,
            "top": [],
        }
    )

    scanned = 0
    for drawer_id, text, meta in _iter_drawers(col, wing=wing, room=room, limit=limit):
        scanned += 1
        wing_name = meta.get("wing", "?")
        room_name = meta.get("room", "?")
        score, reasons = _text_score(text, meta)
        if drawer_id.startswith("closet_"):
            score -= 8
            reasons.append("closet_index")
        tier = _tier(score, min_score)
        key = f"{wing_name}/{room_name}"
        bucket = rooms[key]
        bucket["total"] += 1
        bucket[tier] += 1
        candidate = {
            "drawer_id": drawer_id,
            "wing": wing_name,
            "room": room_name,
            "score": score,
            "tier": tier,
            "reasons": reasons,
            "source_file": meta.get("source_file", ""),
            "filed_at": meta.get("filed_at", ""),
            "preview": _preview(text),
        }
        bucket["top"].append(candidate)
        bucket["top"].sort(key=lambda item: (item["score"], item["filed_at"]), reverse=True)
        del bucket["top"][top_per_room:]

    room_items = []
    for key, value in rooms.items():
        wing_name, room_name = key.split("/", 1)
        room_items.append({"wing": wing_name, "room": room_name, **value})
    room_items.sort(key=lambda item: (item["promote"], item["review"], item["total"]), reverse=True)

    return {
        "generated_at": datetime.now().isoformat(),
        "palace_path": os.path.abspath(os.path.expanduser(palace_path)),
        "filters": {"wing": wing, "room": room, "limit": limit or None},
        "policy": {
            "mode": "verbatim_selection",
            "min_score": min_score,
            "top_per_room": top_per_room,
            "note": "Original drawers are untouched. Promote candidates are verbatim excerpts, not summaries.",
        },
        "scanned": scanned,
        "rooms": room_items,
    }


def report_markdown(report: dict) -> str:
    lines = [
        "# MemPalace Distillation Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Scanned drawers: {report['scanned']}",
        "",
        "This report selects high-signal drawers for memory layers. It does not delete original drawers.",
        "",
    ]
    for room in report["rooms"]:
        lines.append(f"## {room['wing']} / {room['room']}")
        lines.append(
            f"total={room['total']} promote={room['promote']} review={room['review']} archive_candidate={room['archive_candidate']}"
        )
        lines.append("")
        for item in room["top"]:
            reasons = ", ".join(item["reasons"])
            lines.append(f"- score={item['score']} tier={item['tier']} id={item['drawer_id']}")
            if item["source_file"]:
                lines.append(f"  source: {item['source_file']}")
            lines.append(f"  reasons: {reasons}")
            lines.append(f"  preview: {item['preview']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict, output: str = None) -> dict:
    if output:
        base = Path(output).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = Path("~/.mempalace/distill/reports").expanduser() / f"distill_{stamp}"
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def write_distilled_drawers(report: dict, palace_path: str, room_name: str = "distilled") -> list[dict]:
    col = get_collection(palace_path, create=True)
    written = []
    grouped = defaultdict(list)
    for room in report["rooms"]:
        for item in room["top"]:
            if item["tier"] == "promote":
                grouped[item["wing"]].append(item)

    for wing, items in grouped.items():
        if not items:
            continue
        lines = [
            f"[distilled-verbatim-index] wing={wing} generated_at={report['generated_at']}",
            "Original drawers remain the source of truth. Entries below are verbatim previews and drawer IDs.",
            "",
        ]
        for item in sorted(items, key=lambda x: (x["score"], x["filed_at"]), reverse=True)[:80]:
            lines.append(
                f"- [{item['room']}] score={item['score']} drawer={item['drawer_id']} source={item['source_file']}"
            )
            lines.append(f"  {item['preview']}")
        content = "\n".join(lines).strip()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
        drawer_id = f"drawer_{wing}_{room_name}_{digest}"
        col.upsert(
            ids=[drawer_id],
            documents=[content],
            metadatas=[
                {
                    "wing": wing,
                    "room": room_name,
                    "source_file": "mempalace_distiller",
                    "chunk_index": 0,
                    "added_by": "distiller",
                    "filed_at": datetime.now().isoformat(),
                    "type": "distilled_verbatim_index",
                }
            ],
        )
        written.append({"wing": wing, "room": room_name, "drawer_id": drawer_id, "entries": len(items)})
    return written


def delete_drawers_by_tier(
    report: dict,
    palace_path: str,
    tiers: tuple = ("archive_candidate",),
    batch_size: int = 200,
) -> dict:
    """Delete drawers whose tier matches one of ``tiers`` from the report.

    The report only stores the top-N candidates per room, but we want to
    delete every drawer that would fall into the matching tiers for the same
    wing/room. So we re-scan the matched rooms and rescore each drawer with
    the same logic, then delete by id in batches.

    Returns a summary {"wing/room": count}.
    """
    col = get_collection(palace_path, create=False)
    summary: dict = {}

    target_rooms = [(r["wing"], r["room"]) for r in report["rooms"]]
    min_score = report.get("policy", {}).get("min_score", 7)

    for wing, room in target_rooms:
        delete_ids: list[str] = []
        for drawer_id, text, meta in _iter_drawers(col, wing=wing, room=room):
            score, _ = _text_score(text, meta)
            if drawer_id.startswith("closet_"):
                score -= 8
            if _tier(score, min_score) in tiers:
                delete_ids.append(drawer_id)

        if not delete_ids:
            continue

        for i in range(0, len(delete_ids), batch_size):
            col.delete(ids=delete_ids[i : i + batch_size])

        summary[f"{wing}/{room}"] = len(delete_ids)

    return summary
