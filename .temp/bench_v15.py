#!/usr/bin/env python3
"""v1.8 页缓存效果基准: 模拟服务器密度（~40 chunk/页），统计 IO 次数。"""
import os, sys, time, struct, json
PROJECT = "/Users/raysuen/.local/share/TeleAgent/TeleAgent的工作空间/pg2sql"
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "tests"))

from test_parallel import make_tuple, make_varlena_4b, build_page_with_items
from pg2sql.page import PAGE_SIZE

DIR = os.path.join(PROJECT, ".temp", "bench15")
os.makedirs(DIR, exist_ok=True)
TOASTRELID = 44012
VALUEID_BASE = 70000
CHUNK_DATA_LEN = 150   # 服务器实测 ~194B/元组（含头）
CHUNKS_PER_PAGE = 40   # 服务器实测 ~42/页
N_PAGES = 480          # 3.75MB TOAST
N_VALUES = N_PAGES * CHUNKS_PER_PAGE // 2  # 每 valueid 2 chunks

# 构造 TOAST（valueid 顺序分配 → 同一 valueid 的 2 chunk 相邻，跨页）
chunks = []
val = (b"V" * 2 * CHUNK_DATA_LEN)
for i in range(N_VALUES):
    vid = VALUEID_BASE + i
    for seq in range(2):
        cdata = val[seq*CHUNK_DATA_LEN:(seq+1)*CHUNK_DATA_LEN]
        d = struct.pack("<I", vid) + struct.pack("<i", seq) + make_varlena_4b(cdata)
        chunks.append(make_tuple(900000 + i*2 + seq, d, 3))
pages = []
for s in range(0, len(chunks), CHUNKS_PER_PAGE):
    pages.append(build_page_with_items(chunks[s:s+CHUNKS_PER_PAGE]))
toast_path = os.path.join(DIR, str(TOASTRELID))
with open(toast_path, "wb") as f:
    for p in pages: f.write(p)
print(f"TOAST: {N_PAGES} 页 / {len(chunks)} chunks / {N_VALUES} valueids "
      f"({os.path.getsize(toast_path)//1024}KB, {len(chunks)//N_PAGES}/页)")

from pg2sql.toast import ToastFile

# ---- 轻量模式 + 页缓存（v1.8）----
tf = ToastFile(toast_path)
t0 = time.time()
tf.build_index()
t_build = time.time() - t0

# 统计重组期间的物理读次数（monkey-patch _read_page 计数）
real_read = tf._read_page
read_calls = {"n": 0}
def counted_read(pageno):
    read_calls["n"] += 1
    return real_read(pageno)
tf._read_page = counted_read

t0 = time.time()
ok, bad = 0, 0
for i in range(N_VALUES):
    vid = VALUEID_BASE + i
    payload = tf.fetch_and_reassemble(vid)
    if payload == val: ok += 1
    else: bad += 1
t_reas = time.time() - t0
n_reads = read_calls["n"]

print(f"\n[轻量模式 v1.8 页缓存]")
print(f"  build_index: {t_build:.2f}s")
print(f"  重组 {N_VALUES} 个 valueid ({N_VALUES*2} chunks): {t_reas:.2f}s")
print(f"  物理 _read_page 调用: {n_reads} 次 (页缓存容量 {tf._page_cache_max} 页)")
print(f"  → 若无页缓存(v1.7): 需 {N_VALUES*2} 次读 (每 chunk 一次)")
print(f"  → IO 次数削减: {N_VALUES*2/max(n_reads,1):.1f}x")
print(f"  内容校验: ok={ok} bad={bad}")
assert bad == 0 and ok == N_VALUES
tf.close()

# ---- 全量模式（--toast-cache full）----
tf2 = ToastFile(toast_path)
t0 = time.time()
tf2.load()
t_load = time.time() - t0
t0 = time.time()
ok = sum(1 for i in range(N_VALUES)
         if tf2.fetch_and_reassemble(VALUEID_BASE + i) == val)
t_reas2 = time.time() - t0
print(f"\n[全量模式 --toast-cache full]")
print(f"  load: {t_load:.2f}s, 重组 {N_VALUES} 个: {t_reas2:.2f}s, ok={ok}")
assert ok == N_VALUES
tf2.close()

# ---- LRU 抖动测试: 缓存只有 16 页，valueid 顺序访问 ----
tf3 = ToastFile(toast_path, page_cache_pages=16)
tf3._pos_index = tf._pos_index  # 复用索引（tf.close 未清 _pos_index）
tf3._pos_index = ToastFile(toast_path)  # 重新建，避免共享已关闭的 mmap 状态
tf3.build_index()
t0 = time.time()
ok = sum(1 for i in range(N_VALUES)
         if tf3.fetch_and_reassemble(VALUEID_BASE + i) == val)
t_reas3 = time.time() - t0
print(f"\n[LRU 16 页小缓存 (压力测试)]")
print(f"  重组 {N_VALUES} 个: {t_reas3:.2f}s, ok={ok}")
assert ok == N_VALUES
tf3.close()
print("\n全部校验通过")
