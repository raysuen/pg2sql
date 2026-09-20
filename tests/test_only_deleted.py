#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 --only-deleted: 只导出已删除的行。

构造含 3 个活行 + 2 个已删除行的金仓格式数据文件，验证:
1. 默认模式（无 --deleted / --only-deleted）: 只输出 3 行活行
2. --deleted: 输出全部 5 行（活 + 删除）
3. --only-deleted: 只输出 2 行删除行
4. --only-deleted --count: 统计为 2
5. --only-deleted --parallel 2: 并发模式只输出 2 行删除行
6. --only-deleted --data: CSV 模式只输出 2 行
"""
import os
import sys
import struct
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE
from pg2sql.tuple import HEAP_XMIN_COMMITTED, HEAP_XMAX_INVALID, HEAP_XMAX_COMMITTED
from pg2sql.catalog import TableMeta, Column
from pg2sql.types import INT4OID, VARCHAROID

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".temp", "deleted_test")


def make_kb_page(pd_lower, pd_upper, pd_special, version=5):
    page = bytearray(PAGE_SIZE)
    struct.pack_into("<H", page, 38, pd_lower)
    struct.pack_into("<H", page, 40, pd_upper)
    struct.pack_into("<H", page, 42, pd_special)
    psv = (PAGE_SIZE & ~0x7F) | (version & 0x7F)
    struct.pack_into("<H", page, 44, psv)
    return page


def make_varlena_1b(data: bytes) -> bytes:
    total = 1 + len(data)
    header = (total << 1) | 1
    assert header < 128
    return bytes([header]) + data


def make_tuple(xmin, xmax, nattrs, data, infomask_extra=0):
    """金仓元组: 24B 头 + 数据。xmax != 0 表示已删除。"""
    t_hoff = 24
    buf = bytearray(t_hoff)
    struct.pack_into("<I", buf, 0, xmin)
    struct.pack_into("<I", buf, 4, xmax)
    struct.pack_into("<H", buf, 18, nattrs)
    if xmax == 0:
        infomask = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | infomask_extra
    else:
        # 已删除: xmax 已提交
        infomask = HEAP_XMIN_COMMITTED | HEAP_XMAX_COMMITTED | infomask_extra
    struct.pack_into("<H", buf, 20, infomask)
    buf[22] = t_hoff
    buf += data
    return bytes(buf)


def build_page_with_items(tuples_data):
    header_size = 48
    pd_special = PAGE_SIZE
    aligned = [(len(t) + 7) & ~7 for t in tuples_data]
    total_data = sum(aligned)
    pd_upper = pd_special - total_data
    pd_lower = header_size + 4 * len(tuples_data)
    assert pd_lower <= pd_upper

    page = make_kb_page(pd_lower, pd_upper, pd_special)
    pos = pd_upper
    for i, td in enumerate(tuples_data):
        item_raw = pos | (1 << 15) | (len(td) << 17)
        struct.pack_into("<I", page, header_size + 4 * i, item_raw)
        page[pos:pos + len(td)] = td
        pos += aligned[i]
    return bytes(page)


def build_test_file():
    """5 行: id=1~3 活行, id=4~5 已删除行。"""
    columns = [
        Column("id", INT4OID, 4, 1, -1, True),
        Column("name", VARCHAROID, -1, 2, 24, False),
    ]
    table_meta = TableMeta("24576", "ray", "test_del", 46008, columns)

    tuples = []
    # 活行 1~3
    for i in range(1, 4):
        data = struct.pack("<i", i) + make_varlena_1b(f"live{i}".encode())
        tuples.append(make_tuple(1000 + i, 0, 2, data))
    # 已删除行 4~5
    for i in range(4, 6):
        data = struct.pack("<i", i) + make_varlena_1b(f"deleted{i}".encode())
        tuples.append(make_tuple(1000 + i, 2000 + i, 2, data))

    # 分成 2 页（测试多页扫描）
    pages = []
    pages.append(build_page_with_items(tuples[:3]))
    pages.append(build_page_with_items(tuples[3:]))

    path = os.path.join(TEST_DIR, "46008")
    with open(path, "wb") as f:
        for p in pages:
            f.write(p)

    # meta.json
    import json
    meta = {
        "database": "24576",
        "tables": [{
            "schema": "ray", "table": "test_del", "relfilenode": 46008,
            "primary_key": ["id"],
            "columns": [
                {"name": "id", "type_oid": 23, "len": 4, "attnum": 1, "typmod": -1, "notnull": True},
                {"name": "name", "type_oid": 1043, "len": -1, "attnum": 2, "typmod": 24, "notnull": False},
            ],
        }],
    }
    meta_path = os.path.join(TEST_DIR, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    return path, meta_path, table_meta


def run_cli(args_list, expect_ok=True):
    cmd = [sys.executable, "main.py"] + args_list
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"CLI 超时: {' '.join(cmd)}")
    if expect_ok and r.returncode != 0:
        raise AssertionError(f"CLI 失败: {' '.join(cmd)}\n{r.stderr[-1000:]}")
    return r


def main():
    print("=" * 60)
    print("测试: --only-deleted 只导出已删除的行")
    print("=" * 60)

    import shutil
    os.makedirs(TEST_DIR, exist_ok=True)
    for name in os.listdir(TEST_DIR):
        full = os.path.join(TEST_DIR, name)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)

    data_file, meta_file, tm = build_test_file()
    print(f"  数据文件: {data_file} (5 行: 3 活 + 2 删除)")

    def get_insert_ids(output):
        """从 SQL 输出提取 id 值列表"""
        ids = []
        for line in output.splitlines():
            if line.startswith("INSERT INTO") or line.startswith("REPLACE INTO"):
                # 提取 VALUES 后第一个值 (id)
                import re
                m = re.search(r"VALUES \((\d+),", line)
                if m:
                    ids.append(int(m.group(1)))
        return ids

    # ---- 1. 默认模式: 只输出活行 ----
    print("\n[1] 默认模式 (只输出活行)")
    r = run_cli([data_file, "--catalog-json", meta_file, "--sql"])
    ids = get_insert_ids(r.stdout)
    print(f"    输出 id: {ids}")
    assert ids == [1, 2, 3], f"默认应只有活行 [1,2,3], 实际 {ids}"
    assert "-- DELETED" not in r.stdout, "默认模式不应有 DELETED 标记"
    print(f"    3 行活行, 无 DELETED 标记 ✓")

    # ---- 2. --deleted: 活行 + 删除行 ----
    print("\n[2] --deleted (活行 + 删除行)")
    r = run_cli([data_file, "--catalog-json", meta_file, "--sql", "--deleted"])
    ids = get_insert_ids(r.stdout)
    print(f"    输出 id: {ids}")
    assert ids == [1, 2, 3, 4, 5], f"--deleted 应输出全部 [1,2,3,4,5], 实际 {ids}"
    assert r.stdout.count("-- DELETED") == 2, f"应有 2 个 DELETED 标记, 实际 {r.stdout.count('-- DELETED')}"
    print(f"    5 行 (3 活 + 2 删除), 2 个 DELETED 标记 ✓")

    # ---- 3. --only-deleted: 只输出删除行 ----
    print("\n[3] --only-deleted (只输出删除行)")
    r = run_cli([data_file, "--catalog-json", meta_file, "--sql", "--only-deleted"])
    ids = get_insert_ids(r.stdout)
    print(f"    输出 id: {ids}")
    assert ids == [4, 5], f"--only-deleted 应只有 [4,5], 实际 {ids}"
    assert r.stdout.count("-- DELETED") == 2, f"应有 2 个 DELETED 标记"
    # 确认不含活行
    for live_id in ["'live1'", "'live2'", "'live3'"]:
        assert live_id not in r.stdout, f"--only-deleted 不应含活行数据 {live_id}"
    print(f"    2 行删除行, 无活行数据 ✓")

    # ---- 4. --only-deleted --count: 统计删除行数 ----
    print("\n[4] --only-deleted --count")
    r = run_cli([data_file, "--catalog-json", meta_file, "--count", "--only-deleted", "-o", os.path.join(TEST_DIR, "count.txt")])
    count_output = open(os.path.join(TEST_DIR, "count.txt")).read().strip()
    print(f"    输出: {count_output}")
    assert "2" in count_output, f"应统计 2 行删除, 实际 {count_output}"
    print(f"    统计 2 行 ✓")

    # ---- 5. --only-deleted --parallel 2: 并发模式 ----
    print("\n[5] --only-deleted --parallel 2 (并发模式)")
    out_path = os.path.join(TEST_DIR, "par_del.sql")
    r = run_cli([data_file, "--catalog-json", meta_file, "--sql", "--only-deleted",
                 "--parallel", "2", "-o", out_path, "--verbose"])
    content = open(out_path, encoding="utf-8").read()
    ids = get_insert_ids(content)
    print(f"    输出 id: {ids}")
    assert ids == [4, 5], f"并发 --only-deleted 应只有 [4,5], 实际 {ids}"
    assert content.count("-- DELETED") == 2
    print(f"    2 行删除行 (并发) ✓")

    # ---- 6. --only-deleted --data: CSV 模式 ----
    print("\n[6] --only-deleted --data (CSV 模式)")
    out_csv = os.path.join(TEST_DIR, "del.csv")
    r = run_cli([data_file, "--catalog-json", meta_file, "--data", "--only-deleted", "-o", out_csv])
    csv_lines = open(out_csv, encoding="utf-8").read().strip().splitlines()
    print(f"    CSV 行数: {len(csv_lines)}")
    assert len(csv_lines) == 2, f"CSV 应 2 行, 实际 {len(csv_lines)}"
    assert "deleted4" in csv_lines[0], f"第一行应含 deleted4: {csv_lines[0]}"
    assert "deleted5" in csv_lines[1], f"第二行应含 deleted5: {csv_lines[1]}"
    print(f"    2 行 CSV, 内容正确 ✓")

    # ---- 7. 对比: 默认 vs --only-deleted 互斥 ----
    print("\n[7] 互斥验证: 默认 + --only-deleted 无重叠")
    r_default = run_cli([data_file, "--catalog-json", meta_file, "--sql"])
    r_deleted = run_cli([data_file, "--catalog-json", meta_file, "--sql", "--only-deleted"])
    default_ids = set(get_insert_ids(r_default.stdout))
    deleted_ids = set(get_insert_ids(r_deleted.stdout))
    assert default_ids & deleted_ids == set(), "默认输出与 --only-deleted 输出不应有交集"
    assert default_ids | deleted_ids == {1, 2, 3, 4, 5}, "两者合并应覆盖全部 5 行"
    print(f"    默认 {default_ids} ∩ --only-deleted {deleted_ids} = ∅ ✓")
    print(f"    合并 = {{1,2,3,4,5}} 全覆盖 ✓")

    print("\n" + "=" * 60)
    print("全部测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
