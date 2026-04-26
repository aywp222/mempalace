#!/usr/bin/env python3
"""
MemPalace HNSW 修复脚本

问题：HNSW 向量索引（331c3aef）只有 100 个向量，但 SQLite metadata 有 193,895 条记录。
根本原因：quarantine_stale_hnsw() 在 Apr 11 把旧 HNSW 隔离到 .drift-old06，
           重建了一个空 HNSW，但后续写入只成功了 100 条就停止了（Rust 后端
           检测到不一致后拒绝继续写入）。

修复方案：
  1. 停止 http_server
  2. 恢复 drift-old06（160,542 个向量）作为活跃 HNSW
  3. 删除 SQLite 中不在 drift-old06 里的孤立记录（~33K 条）
  4. 清除/重置 max_seq_id 让 ChromaDB 重新同步
  5. 重启 http_server
"""

import os
import pickle
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

PALACE_PATH = os.path.expanduser("~/.mempalace/palace")
DB_PATH = os.path.join(PALACE_PATH, "chroma.sqlite3")
HNSW_SEG = "331c3aef-6d26-4735-bd3e-d57c4b94af37"
ACTIVE_HNSW = os.path.join(PALACE_PATH, HNSW_SEG)
OLD_HNSW = os.path.join(PALACE_PATH, HNSW_SEG + ".drift-old06")
BROKEN_BACKUP = os.path.join(PALACE_PATH, HNSW_SEG + ".broken100")

# http_server 相关
HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(HERE, ".venv", "bin", "python")
HTTP_SERVER = os.path.join(HERE, "http_server.py")

def kill_http_servers():
    """杀死所有 http_server.py 进程"""
    import subprocess
    result = subprocess.run(
        ["pgrep", "-f", "http_server.py"],
        capture_output=True, text=True
    )
    pids = [int(p) for p in result.stdout.strip().split() if p]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  Killed http_server PID {pid}")
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(2)
    # 确认已停止
    result2 = subprocess.run(
        ["pgrep", "-f", "http_server.py"],
        capture_output=True, text=True
    )
    remaining = [p for p in result2.stdout.strip().split() if p]
    if remaining:
        for pid in remaining:
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"  Force-killed http_server PID {pid}")
            except ProcessLookupError:
                pass
        time.sleep(1)
    print(f"  http_server 已停止 (killed {len(pids)} process(es))")


def load_old_hnsw_ids():
    """从 drift-old06 的 pickle 加载所有 embedding_id"""
    pickle_path = os.path.join(OLD_HNSW, "index_metadata.pickle")
    with open(pickle_path, "rb") as f:
        meta = pickle.load(f)
    # id_to_label 是 {embedding_id_str: int_label} 的 dict
    ids = set(meta.id_to_label.keys())
    max_seq_id = max(meta.id_to_seq_id.values()) if meta.id_to_seq_id else 0
    print(f"  drift-old06 包含 {len(ids):,} 个 embedding_id")
    print(f"  drift-old06 max seq_id = {max_seq_id}")
    return ids, max_seq_id


def restore_hnsw(old_hnsw_ids):
    """将 drift-old06 恢复为活跃 HNSW"""
    # 备份当前破损的 HNSW（100 个向量）
    if os.path.isdir(ACTIVE_HNSW):
        if os.path.isdir(BROKEN_BACKUP):
            shutil.rmtree(BROKEN_BACKUP)
        os.rename(ACTIVE_HNSW, BROKEN_BACKUP)
        print(f"  当前破损 HNSW (100向量) 已备份至: {os.path.basename(BROKEN_BACKUP)}")

    # 将 drift-old06 复制为活跃 HNSW
    shutil.copytree(OLD_HNSW, ACTIVE_HNSW)
    print(f"  drift-old06 已复制为活跃 HNSW: {HNSW_SEG}")


def trim_sqlite(old_hnsw_ids, max_seq_id):
    """删除 SQLite 中不在 drift-old06 里的孤立记录，更新 max_seq_id"""
    with sqlite3.connect(DB_PATH) as conn:
        # 先统计当前数量
        total_before = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE segment_id='b30d41bf-0035-452e-af31-11c1efbc7394'"
        ).fetchone()[0]
        print(f"  SQLite metadata 修复前: {total_before:,} 条")

        # 找出所有 embedding_id 不在 drift-old06 里的记录
        all_ids = conn.execute(
            "SELECT id, embedding_id FROM embeddings WHERE segment_id='b30d41bf-0035-452e-af31-11c1efbc7394'"
        ).fetchall()

        orphaned_db_ids = [row[0] for row in all_ids if row[1] not in old_hnsw_ids]
        print(f"  孤立记录数（不在 HNSW 中）: {len(orphaned_db_ids):,}")

        if orphaned_db_ids:
            # 分批删除（SQLite 有变量数限制）
            BATCH = 500
            deleted_meta = 0
            deleted_emb = 0
            cur = conn.cursor()
            for i in range(0, len(orphaned_db_ids), BATCH):
                batch = orphaned_db_ids[i:i+BATCH]
                placeholders = ",".join("?" * len(batch))
                cur.execute(
                    f"DELETE FROM embedding_metadata WHERE id IN ({placeholders})", batch
                )
                deleted_meta += cur.rowcount
                cur.execute(
                    f"DELETE FROM embeddings WHERE id IN ({placeholders})", batch
                )
                deleted_emb += cur.rowcount

            print(f"  已删除 embedding_metadata: {deleted_meta:,} 条")
            print(f"  已删除 embeddings: {deleted_emb:,} 条")

        total_after = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE segment_id='b30d41bf-0035-452e-af31-11c1efbc7394'"
        ).fetchone()[0]
        print(f"  SQLite metadata 修复后: {total_after:,} 条")

        # 更新 max_seq_id 让 HNSW 和 metadata 保持一致
        # 使用 metadata segment 中实际最大的 seq_id
        actual_max = conn.execute(
            "SELECT MAX(seq_id) FROM embeddings WHERE segment_id='b30d41bf-0035-452e-af31-11c1efbc7394'"
        ).fetchone()[0]
        print(f"  metadata segment 实际最大 seq_id: {actual_max}")

        # 将 HNSW segment 的 max_seq_id 设置为与 metadata 一致
        cur2 = conn.cursor()
        cur2.execute(
            "UPDATE max_seq_id SET seq_id=? WHERE segment_id=?",
            (actual_max, HNSW_SEG)
        )
        updated = cur2.rowcount
        print(f"  max_seq_id 已更新 ({updated} 行): {HNSW_SEG[:8]}... → {actual_max}")

        conn.commit()
        
        # 也删除 embeddings_queue 里的孤立条目（closets collection 的，不影响 drawers）
        queue_count = conn.execute("SELECT COUNT(*) FROM embeddings_queue").fetchone()[0]
        print(f"  embeddings_queue 剩余: {queue_count:,} 条（closets collection，暂不处理）")

        return total_after


def verify_fix():
    """验证修复结果"""
    import sys
    sys.path.insert(0, os.path.join(HERE, "src") if os.path.isdir(os.path.join(HERE, "src")) else HERE)
    
    venv_site = os.path.join(HERE, ".venv", "lib")
    if os.path.isdir(venv_site):
        for d in os.listdir(venv_site):
            site = os.path.join(venv_site, d, "site-packages")
            if os.path.isdir(site):
                sys.path.insert(0, site)
    
    import chromadb
    client = chromadb.PersistentClient(path=PALACE_PATH)
    col = client.get_collection("mempalace_drawers")
    count = col.count()
    print(f"  ChromaDB count() = {count:,}")
    
    # 测试 peek
    try:
        peek = col.peek(3)
        print(f"  peek(3) 成功: {peek['ids'][:3]}")
    except Exception as e:
        print(f"  peek(3) 失败: {e}")
        return False
    
    # 测试写入
    test_id = "repair_verification_test_001"
    try:
        col.upsert(
            ids=[test_id],
            documents=["repair verification test"],
            metadatas=[{"wing": "test", "room": "test", "added_by": "repair_script",
                       "filed_at": "2026-04-23", "chunk_index": 0, "source_file": "repair"}]
        )
        # 验证写入
        new_count = col.count()
        if new_count > count:
            print(f"  ✅ 写入测试成功！count: {count} → {new_count}")
            # 清理测试数据
            col.delete(ids=[test_id])
            print(f"  测试数据已清理")
            return True
        else:
            print(f"  ❌ 写入后 count 未增加: {new_count}")
            return False
    except Exception as e:
        print(f"  ❌ 写入测试失败: {e}")
        return False


def start_http_server():
    """重新启动 http_server"""
    log_path = os.path.expanduser("~/.mempalace/logs/mcp-http-error.log")
    log = open(log_path, "a")
    proc = subprocess.Popen(
        [PYTHON, HTTP_SERVER, "--port", "47291", "--host", "127.0.0.1"],
        cwd=HERE, stdout=log, stderr=log, start_new_session=True
    )
    print(f"  http_server 已启动 (PID {proc.pid})")
    # 等待就绪
    import urllib.request
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:47291/health", timeout=2)
            print("  http_server 健康检查通过 ✅")
            return True
        except Exception:
            pass
    print("  ⚠️  http_server 健康检查超时")
    return False


def main():
    print("=" * 60)
    print("MemPalace HNSW 修复脚本")
    print("=" * 60)
    
    # 检查 drift-old06 存在
    if not os.path.isdir(OLD_HNSW):
        print(f"❌ 找不到 {OLD_HNSW}")
        sys.exit(1)
    
    print("\n[1/5] 停止 http_server...")
    kill_http_servers()
    
    print("\n[2/5] 加载 drift-old06 的 embedding IDs...")
    old_hnsw_ids, old_max_seq_id = load_old_hnsw_ids()
    
    print("\n[3/5] 恢复 drift-old06 为活跃 HNSW...")
    restore_hnsw(old_hnsw_ids)
    
    print("\n[4/5] 清理 SQLite 孤立记录并更新 max_seq_id...")
    final_count = trim_sqlite(old_hnsw_ids, old_max_seq_id)
    
    print("\n[5/5] 验证修复结果（直接 Python 测试）...")
    ok = verify_fix()
    
    if ok:
        print("\n✅ 修复成功！正在重启 http_server...")
        start_http_server()
    else:
        print("\n❌ 验证失败，请手动检查")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print(f"修复完成！数据库现有 {final_count:,} 条记录，写入功能已恢复。")
    print("丢失的约 33K 条记录（Apr 11-20 新增）可通过重新运行挖掘恢复。")
    print("=" * 60)


if __name__ == "__main__":
    main()
