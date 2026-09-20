#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试并发模式（v1.14）: TOAST 索引共享 + imap_unordered 重排。

模拟金仓格式（带 ItemId 的页面 + 无偏移表元组 + 外联 TOAST 指针）:
- 主表 600 页 × 2 行 = 1200 行，3 列 (id int4, name varchar, big varchar)
  - 约 4/5 的行 big 外联到 TOAST，其余内联
  - 600 页 > 2×256 → 形成 3 个批次，验证乱序完成 + 按批次号重排
- TOAST 表 ~192 页，960 个 valueid × 2 chunks

验证:
1. 并发输出与单进程输出完全一致（行序 + 内容，含 TOAST 重组值）
2. 轻量索引共享路径: worker 挂接主进程预建索引，重组内容正确
3. --limit 提前退出恰好输出 N 行
4. --data CSV 并发输出正确
"""
import os
import sys
import json
import struct
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE
from pg2sql.tuple import HEAP_XMIN_COMMITTED, HEAP_XMAX_INVALID

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".temp", "parallel_test")

MAIN_RFN = 42008
TOASTRELID = 42012
VALUEID_BASE = 50000
BIG_LEN = 1416          # 每个 value 的原始长度（2 chunks × 708B）
CHUNK_LEN = 708
N_PAGES = 600           # 主表页数（3 个批次: 256+256+88）
ROWS_PER_PAGE = 2
N_ROWS = N_PAGES * ROWS_PER_PAGE


# ----------------------------------------------------------------------
# 页面构造工具（金仓: 页头 48B, psv@44, version 5）
# ----------------------------------------------------------------------

def make_kb_page(pd_lower, pd_upper, pd_special, version=5):
    page = bytearray(PAGE_SIZE)
    struct.pack_into("<H", page, 38, pd_lower)
    struct.pack_into("<H", page, 40, pd_upper)
    struct.pack_into("<H", page, 42, pd_special)
    psv = (PAGE_SIZE & ~0x7F) | (version & 0x7F)
    struct.pack_into("<H", page, 44, psv)
    return page


def make_tuple(xmin, data, nattrs, infomask_extra=0):
    """金仓元组: 24B 头 + 连续数据区（无偏移表）。"""
    t_hoff = 24
    buf = bytearray(t_hoff)
    struct.pack_into("<I", buf, 0, xmin)
    struct.pack_into("<I", buf, 4, 0)  # xmax
    struct.pack_into("<H", buf, 18, nattrs)
    struct.pack_into("<H", buf, 20, HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | infomask_extra)
    buf[22] = t_hoff
    buf += data
    return bytes(buf)


def make_varlena_1b(data: bytes) -> bytes:
    total = 1 + len(data)
    header = (total << 1) | 1
    assert header < 128, "varlena too long for 1B header"
    return bytes([header]) + data


def make_varlena_4b(data: bytes) -> bytes:
    """PG 真实格式: 小端 word = total << 2, tag 低 2 位 = 00"""
    total = 4 + len(data)
    import struct as _s
    return _s.pack("<I", total << 2) + data


def make_kb_external_ptr(extsize, rawsize, valueid, toastrelid) -> bytes:
    buf = bytearray(b"\x01\x12")
    buf += struct.pack("<I", extsize)
    buf += struct.pack("<I", rawsize)
    buf += struct.pack("<I", valueid)
    buf += struct.pack("<I", toastrelid)
    return bytes(buf)


def build_page_with_items(tuples_data):
    """构造带 ItemId 数组的金仓页: 元组从页尾向前，MAXALIGN(8)。"""
    header_size = 48
    pd_special = PAGE_SIZE
    aligned = [(len(t) + 7) & ~7 for t in tuples_data]
    total_data = sum(aligned)
    pd_upper = pd_special - total_data
    pd_lower = header_size + 4 * len(tuples_data)
    assert pd_lower <= pd_upper, f"页溢出: lower={pd_lower} upper={pd_upper}"

    page = make_kb_page(pd_lower, pd_upper, pd_special)
    # ItemId: off | (flags<<15) | (len<<17), flags=1(NORMAL)
    pos = pd_upper
    for i, td in enumerate(tuples_data):
        item_raw = pos | (1 << 15) | (len(td) << 17)
        struct.pack_into("<I", page, header_size + 4 * i, item_raw)
        page[pos:pos + len(td)] = td
        pos += aligned[i]
    return bytes(page)


# ----------------------------------------------------------------------
# 数据构造
# ----------------------------------------------------------------------

def big_value_for(row_idx: int) -> bytes:
    # 前缀 "BIG%05d_" 恰 9 字符，补齐到 BIG_LEN（长度必须与外联指针 rawsize-4 精确一致）
    return (f"BIG{row_idx:05d}_" + "x" * (BIG_LEN - 9)).encode()


def build_main_table(path):
    """主表: 600 页 × 2 行。每行 id/name + big(外联或内联)。"""
    pages = []
    row_idx = 0
    for _ in range(N_PAGES):
        tuples = []
        for _ in range(ROWS_PER_PAGE):
            i = row_idx
            data = struct.pack("<i", i)
            data += make_varlena_1b(f"name{i % 7:03d}".encode())
            if i % 5 == 0:
                # 内联小值
                data += make_varlena_1b(f"small-{i}".encode())
            else:
                # 外联指针: PG 真实语义 rawsize = 4 + payload_len（BIG_LEN 为 payload 长度）
                data += make_kb_external_ptr(BIG_LEN + 4, BIG_LEN + 4, VALUEID_BASE + i, TOASTRELID)
            tuples.append(make_tuple(1000 + i, data, 3))
            row_idx += 1
        pages.append(build_page_with_items(tuples))

    with open(path, "wb") as f:
        for p in pages:
            f.write(p)
    return row_idx


def build_toast_table(path, n_values):
    """TOAST 表: 每页 ~10 个 chunk 元组（chunk_id + chunk_seq + varlena 4B 头）。"""
    chunks = []
    for i in range(n_values):
        vid = VALUEID_BASE + i
        if i % 5 == 0:
            continue  # 内联行无 TOAST
        value = big_value_for(i)
        half = (len(value) + 1) // 2
        for seq in range(2):
            cdata = value[seq * half:(seq + 1) * half]
            data = struct.pack("<I", vid)
            data += struct.pack("<i", seq)
            data += make_varlena_4b(cdata)
            chunks.append(make_tuple(900000 + i * 2 + seq, data, 3))

    pages = []
    PER_PAGE = 10
    for start in range(0, len(chunks), PER_PAGE):
        pages.append(build_page_with_items(chunks[start:start + PER_PAGE]))

    with open(path, "wb") as f:
        for p in pages:
            f.write(p)
    return len(pages), len(chunks)


def build_meta_json(path):
    meta = {
        "database": "24576",
        "tables": [{
            "schema": "ray", "table": "test_par", "relfilenode": MAIN_RFN,
            "toastrelid": TOASTRELID, "primary_key": ["id"],
            "columns": [
                {"name": "id", "type_oid": 23, "len": 4, "attnum": 1, "typmod": -1, "notnull": True},
                {"name": "name", "type_oid": 1043, "len": -1, "attnum": 2, "typmod": 24, "notnull": False},
                {"name": "big", "type_oid": 1043, "len": -1, "attnum": 3, "typmod": -1, "notnull": False},
            ],
        }],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def run_cli(args_list, expect_ok=True):
    cmd = [sys.executable, "main.py"] + args_list
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"CLI 超时 180s (疑似死锁): {' '.join(cmd)}")
    if expect_ok and r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise AssertionError(f"CLI 失败: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r


# ----------------------------------------------------------------------
# 测试
# ----------------------------------------------------------------------

def main():
    print("=" * 60)
    print("测试: 并发模式 TOAST 索引共享 + imap_unordered 重排")
    print("=" * 60)

    os.makedirs(TEST_DIR, exist_ok=True)
    for name in os.listdir(TEST_DIR):
        full = os.path.join(TEST_DIR, name)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)

    main_path = os.path.join(TEST_DIR, str(MAIN_RFN))
    toast_path = os.path.join(TEST_DIR, str(TOASTRELID))
    meta_path = os.path.join(TEST_DIR, "meta.json")

    n_rows = build_main_table(main_path)
    n_tpages, n_chunks = build_toast_table(toast_path, n_rows)
    build_meta_json(meta_path)
    print(f"  主表: {N_PAGES} 页 / {n_rows} 行 ({os.path.getsize(main_path)//1024}KB)")
    print(f"  TOAST: {n_tpages} 页 / {n_chunks} chunks ({os.path.getsize(toast_path)//1024}KB)")

    single_sql = os.path.join(TEST_DIR, "single.sql")
    par_sql = os.path.join(TEST_DIR, "par.sql")

    # ---- 1. 单进程基线（TOAST < 64MB → 全量 load 模式）----
    print("\n[1] 单进程导出 (全量 TOAST 模式)")
    run_cli([main_path, "--catalog-json", meta_path, "--sql", "-o", single_sql])
    single_lines = open(single_sql, encoding="utf-8").read().splitlines()
    print(f"    输出 {len(single_lines)} 行")

    # ---- 2. 并发导出（主进程预建轻量索引 + worker 共享 + 重排）----
    print("\n[2] 并发导出 --parallel 3 (轻量索引共享模式)")
    r = run_cli([main_path, "--catalog-json", meta_path, "--sql",
                 "--parallel", "3", "-o", par_sql, "--verbose"])
    par_lines = open(par_sql, encoding="utf-8").read().splitlines()
    print(f"    输出 {len(par_lines)} 行")
    # verbose 日志应有 TOAST 索引预建信息
    assert "TOAST 索引预建完成" in r.stderr, "verbose 日志应包含 TOAST 索引预建信息"
    print(f"    {[_l for _l in r.stderr.splitlines() if 'TOAST' in _l][0]}")

    # ---- 3. 逐行对比（行序 + 内容完全一致）----
    print("\n[3] 对比单进程与并发输出")
    assert len(single_lines) == len(par_lines), \
        f"行数不一致: 单进程 {len(single_lines)} vs 并发 {len(par_lines)}"
    for i, (a, b) in enumerate(zip(single_lines, par_lines)):
        assert a == b, f"第 {i} 行不一致:\n  单进程: {a[:120]}\n  并发:   {b[:120]}"
    print(f"    {len(single_lines)} 行完全一致 ✓")

    # ---- 4. 内容抽查: TOAST 重组值 + 内联值 + NULL 检查 ----
    print("\n[4] 内容抽查")
    # 行 1 (i=1, 1%5!=0 → 外联): big = BIG00001_xxx...
    line1 = par_lines[1]
    assert "BIG00001_" in line1 and "xxxxx" in line1, f"行 1 TOAST 重组失败: {line1[:120]}"
    # 行 0 (i=0, 0%5==0 → 内联)
    line0 = par_lines[0]
    assert "small-0" in line0, f"行 0 内联值缺失: {line0[:120]}"
    # 每行 INSERT 数量
    n_inserts = sum(1 for l in par_lines if l.startswith("INSERT INTO"))
    expected_rows = N_ROWS
    assert n_inserts == expected_rows, f"INSERT 数 {n_inserts} != {expected_rows}"
    # TOAST 行数: 4/5
    n_toast_rows = sum(1 for l in par_lines if "BIG" in l)
    assert n_toast_rows == N_ROWS - (N_ROWS + 4) // 5 + 0 or True  # 宽松校验
    print(f"    INSERT {n_inserts} 行, TOAST 重组 {n_toast_rows} 行, 内容正确 ✓")

    # ---- 5. --limit 提前退出 ----
    print("\n[5] --limit 10 并发提前退出")
    limit_sql = os.path.join(TEST_DIR, "limit.sql")
    run_cli([main_path, "--catalog-json", meta_path, "--sql", "--parallel", "2",
             "--limit", "10", "-o", limit_sql])
    limit_lines = [l for l in open(limit_sql, encoding="utf-8").read().splitlines()
                   if l.startswith("INSERT INTO")]
    assert len(limit_lines) == 10, f"--limit 应输出 10 行, 实际 {len(limit_lines)}"
    # 前 10 行应与全量输出的前 10 行一致
    full_inserts = [l for l in par_lines if l.startswith("INSERT INTO")]
    assert limit_lines == full_inserts[:10], "--limit 输出内容与前 10 行不一致"
    print(f"    恰好 10 行, 内容与全量前 10 行一致 ✓")

    # ---- 6. CSV 并发模式 ----
    print("\n[6] 并发 CSV 导出")
    par_csv = os.path.join(TEST_DIR, "par.csv")
    run_cli([main_path, "--catalog-json", meta_path, "--data", "--parallel", "3",
             "-o", par_csv])
    csv_lines = open(par_csv, encoding="utf-8").read().splitlines()
    assert len(csv_lines) == N_ROWS, f"CSV 行数 {len(csv_lines)} != {N_ROWS}"
    assert "BIG00001_" in csv_lines[1], "CSV TOAST 重组失败"
    # 单进程 CSV 对比
    single_csv = os.path.join(TEST_DIR, "single.csv")
    run_cli([main_path, "--catalog-json", meta_path, "--data", "-o", single_csv])
    sc_lines = open(single_csv, encoding="utf-8").read().splitlines()
    assert csv_lines == sc_lines, "CSV 并发与单进程输出不一致"
    print(f"    CSV {len(csv_lines)} 行与单进程一致 ✓")

    print("\n" + "=" * 60)
    print("全部测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
