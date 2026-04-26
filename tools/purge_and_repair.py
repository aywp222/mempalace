#!/usr/bin/env python3
"""
purge_and_repair.py — Surgical palace cleanup.

Two responsibilities:
  1) Purge entire (wing[, room]) targets from the drawers/closets collections
     using ChromaDB's metadata API (verbatim — no vector dependency).
  2) Repair HNSW/SQLite divergence: orphan SQLite metadata rows whose
     embedding_id is not present in the HNSW index pickle. These orphans
     cause Chroma's filtered vector queries to raise
     `Internal error: Error finding id ...`, which the searcher currently
     masks via `_fallback_filtered_bm25`. We fix the cause, not the symptom.

Run with the http_server stopped. Idempotent.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sqlite3
import sys
from pathlib import Path

PALACE_PATH = os.path.expanduser("~/.mempalace/palace")
DB_PATH = os.path.join(PALACE_PATH, "chroma.sqlite3")

DRAWERS_COLLECTION = "mempalace_drawers"
CLOSETS_COLLECTION = "mempalace_closets"


def _segments(conn: sqlite3.Connection) -> dict:
    """Return {collection_name: {'vector': seg_id, 'metadata': seg_id}}."""
    cols = {cid: name for cid, name in conn.execute("SELECT id, name FROM collections")}
    out: dict = {}
    for sid, stype, scope, cid in conn.execute(
        "SELECT id, type, scope, collection FROM segments"
    ):
        name = cols.get(cid)
        if not name:
            continue
        out.setdefault(name, {})[scope.lower()] = sid
    return out


def _load_hnsw_ids(palace: str, hnsw_seg: str) -> set:
    pickle_path = os.path.join(palace, hnsw_seg, "index_metadata.pickle")
    if not os.path.isfile(pickle_path):
        return set()
    with open(pickle_path, "rb") as f:
        meta = pickle.load(f)
    label_map = getattr(meta, "id_to_label", None)
    if label_map is None and isinstance(meta, dict):
        label_map = meta.get("id_to_label", {})
    return set(label_map or {})


def purge_targets(targets: list[tuple[str, str | None]], dry_run: bool = True) -> dict:
    """Delete drawers + closets matching (wing, room) tuples.

    Uses the chroma client API so HNSW deletions cascade correctly via the
    queue. Run BEFORE the orphan repair so newly orphaned ids are also
    swept by the repair pass.
    """
    import chromadb

    client = chromadb.PersistentClient(path=PALACE_PATH)
    drawers = client.get_collection(DRAWERS_COLLECTION)
    try:
        closets = client.get_collection(CLOSETS_COLLECTION)
    except Exception:
        closets = None

    summary: dict = {"targets": [], "drawers_deleted": 0, "closets_deleted": 0}

    for wing, room in targets:
        if room:
            where = {"$and": [{"wing": wing}, {"room": room}]}
        else:
            where = {"wing": wing}

        d_hits = drawers.get(where=where, include=[])
        d_count = len(d_hits["ids"])

        c_count = 0
        if closets is not None:
            try:
                c_hits = closets.get(where=where, include=[])
                c_count = len(c_hits["ids"])
            except Exception:
                c_count = 0

        summary["targets"].append(
            {"wing": wing, "room": room, "drawers": d_count, "closets": c_count}
        )
        summary["drawers_deleted"] += d_count
        summary["closets_deleted"] += c_count

        if not dry_run and d_count:
            drawers.delete(where=where)
        if not dry_run and closets is not None and c_count:
            try:
                closets.delete(where=where)
            except Exception as e:
                print(f"  closet delete warning ({wing}/{room}): {e}", file=sys.stderr)

    if not dry_run:
        # Make sure the queue is materialized before repair touches segments.
        # A cheap count() flush works in practice.
        drawers.count()
        if closets is not None:
            closets.count()

    return summary


def repair_hnsw_metadata_orphans(dry_run: bool = True) -> dict:
    """Remove SQLite metadata rows whose embedding_id is missing from HNSW.

    These orphans are the root cause of `Error finding id` during filtered
    vector queries. They typically appear when a previous write batch crashed
    after appending to SQLite but before the HNSW append succeeded, or after
    a forced HNSW restore from an older snapshot.
    """
    with sqlite3.connect(DB_PATH) as conn:
        segs = _segments(conn)
        result: dict = {"collections": {}}

        for cname in (DRAWERS_COLLECTION, CLOSETS_COLLECTION):
            seg_pair = segs.get(cname)
            if not seg_pair:
                continue
            hnsw_seg = seg_pair.get("vector")
            meta_seg = seg_pair.get("metadata")
            if not hnsw_seg or not meta_seg:
                continue

            hnsw_ids = _load_hnsw_ids(PALACE_PATH, hnsw_seg)
            sql_count = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE segment_id=?", (meta_seg,)
            ).fetchone()[0]

            rows = conn.execute(
                "SELECT id, embedding_id FROM embeddings WHERE segment_id=?",
                (meta_seg,),
            ).fetchall()
            orphan_ids = [rid for rid, eid in rows if eid not in hnsw_ids]

            entry = {
                "hnsw_count": len(hnsw_ids),
                "sqlite_count": sql_count,
                "orphans": len(orphan_ids),
            }

            if orphan_ids and not dry_run:
                cur = conn.cursor()
                BATCH = 500
                deleted_meta = 0
                deleted_emb = 0
                for i in range(0, len(orphan_ids), BATCH):
                    batch = orphan_ids[i : i + BATCH]
                    placeholders = ",".join("?" * len(batch))
                    cur.execute(
                        f"DELETE FROM embedding_metadata WHERE id IN ({placeholders})",
                        batch,
                    )
                    deleted_meta += cur.rowcount
                    cur.execute(
                        f"DELETE FROM embeddings WHERE id IN ({placeholders})",
                        batch,
                    )
                    deleted_emb += cur.rowcount
                entry["deleted_metadata_rows"] = deleted_meta
                entry["deleted_embedding_rows"] = deleted_emb

                # Reconcile max_seq_id for the HNSW segment to whatever the
                # metadata segment now holds. Otherwise the HNSW cursor races
                # ahead of metadata and writes wedge again.
                actual_max = conn.execute(
                    "SELECT MAX(seq_id) FROM embeddings WHERE segment_id=?",
                    (meta_seg,),
                ).fetchone()[0] or 0
                conn.execute(
                    "UPDATE max_seq_id SET seq_id=? WHERE segment_id=?",
                    (actual_max, hnsw_seg),
                )
                entry["max_seq_id_synced_to"] = actual_max

            result["collections"][cname] = entry

        if not dry_run:
            conn.commit()

    return result


# Default purge targets per the user request (figure-1 boxed rooms + lanhu_mcp
# anywhere it appears). Each entry is (wing, room | None for whole wing).
DEFAULT_TARGETS: list[tuple[str, str | None]] = [
    ("AutoBuilder", None),
    ("mshl", "musshigclientproj"),
    ("mshl", "muushig"),
    ("mshl", "日期选择器"),
    ("mshl", "炫酷的shaderloading"),
    # lanhu_mcp room cleanup, applied to every wing it appears in.
    # We resolve dynamically below to avoid hard-coding wing.
]


def resolve_lanhu_mcp_targets() -> list[tuple[str, str]]:
    import chromadb

    client = chromadb.PersistentClient(path=PALACE_PATH)
    drawers = client.get_collection(DRAWERS_COLLECTION)
    hits = drawers.get(where={"room": "lanhu_mcp"}, include=["metadatas"])
    wings = sorted({(m or {}).get("wing", "") for m in hits["metadatas"] or []})
    return [(w, "lanhu_mcp") for w in wings if w]


def main():
    p = argparse.ArgumentParser(description="Purge palace targets and repair HNSW divergence.")
    p.add_argument("--write", action="store_true", help="Apply changes (default: dry run).")
    p.add_argument("--purge-only", action="store_true", help="Skip repair pass.")
    p.add_argument("--repair-only", action="store_true", help="Skip purge pass.")
    p.add_argument(
        "--extra-target",
        action="append",
        default=[],
        metavar="WING[/ROOM]",
        help="Add an explicit purge target (repeatable).",
    )
    args = p.parse_args()

    dry_run = not args.write

    targets = list(DEFAULT_TARGETS)
    targets.extend(resolve_lanhu_mcp_targets())
    for raw in args.extra_target:
        if "/" in raw:
            w, r = raw.split("/", 1)
            targets.append((w, r))
        else:
            targets.append((raw, None))

    print(f"== Mode: {'WRITE' if not dry_run else 'DRY RUN'} ==")
    print(f"Palace: {PALACE_PATH}")
    print()

    if not args.repair_only:
        print("--- Purge pass ---")
        ps = purge_targets(targets, dry_run=dry_run)
        for t in ps["targets"]:
            label = f"{t['wing']}/{t['room']}" if t["room"] else t["wing"]
            print(f"  {label:60s}  drawers={t['drawers']:>6d}  closets={t['closets']:>5d}")
        print(
            f"  TOTAL  drawers={ps['drawers_deleted']}  closets={ps['closets_deleted']}"
        )
        print()

    if not args.purge_only:
        print("--- Repair pass ---")
        rs = repair_hnsw_metadata_orphans(dry_run=dry_run)
        for cname, entry in rs["collections"].items():
            print(f"  [{cname}]")
            for k, v in entry.items():
                print(f"    {k}: {v}")
        print()

    if dry_run:
        print("Dry run complete. Re-run with --write to apply.")


if __name__ == "__main__":
    main()
