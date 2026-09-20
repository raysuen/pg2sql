#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试金仓(KingbaseES)格式页面的元组扫描模式。

模拟金仓的特征：
1. page version 5（pd_pagesize_version 在偏移 44 而非 18）
2. ItemId 数组为空或无效（迫使回退到扫描模式）
3. 无偏移表（t_hoff 直接指向数据区）
4. 数据区按列声明顺序连续排列

测试表: test02 (id int4, name varchar(20))
预期: 3 行数据 (1, 'alice'), (2, 'bob'), (3, 'charlie')
"""
import os
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE, Page
from pg2sql.tuple import HeapTuple, HEAP_TUPLE_HEADER_SIZE, HEAP_NATTS_MASK, HEAP_HASNULL, HEAP_HASOID, HEAP_XMIN_COMMITTED, HEAP_XMIN_FROZEN, HEAP_XMAX_INVALID
from pg2sql.heapfile import HeapFile, _extract_fields_direct, _build_col_lengths
from pg2sql.catalog import TableMeta, Column
from pg2sql.types import INT4OID, VARCHAROID
from pg2sql.binary import align4

# --- 测试辅助 ---

def make_kb_page_header(pd_lower, pd_upper, pd_special, version=5):
    """构造金仓 page version 5 的页头。

    金仓页头布局：psv 在偏移 44，pd_lower/upper/special 在 38/40/42。
    page.py 的 _try_auto_detect_layout 扫描到 psv 后，向前推 6 字节读取这三个字段。
    header_end = align4(44 + 2) = 48
    """
    header = bytearray(PAGE_SIZE)
    # LSN: 全零 (offset 0-7)
    # pd_lower/upper/special 位于 psv 前面 6 字节
    # psv 在 offset 44, 所以 pd_lower=38, pd_upper=40, pd_special=42
    struct.pack_into("<H", header, 38, pd_lower)
    struct.pack_into("<H", header, 40, pd_upper)
    struct.pack_into("<H", header, 42, pd_special)
    # pd_pagesize_version at offset 44
    psv = (PAGE_SIZE & ~0x7F) | (version & 0x7F)
    struct.pack_into("<H", header, 44, psv)
    return header


def make_kb_tuple(xmin, xmax, nattrs, infomask, t_hoff, null_bitmap=None, data=b"", hasoid=False):
    """构造金仓风格的元组（无偏移表）。

    Header: 23 bytes (xmin, xmax, t_field3, t_ctid, infomask2, infomask, t_hoff)
    + null bitmap (if HEAP_HASNULL)
    + OID (if hasoid, even if HEAP_HASOID not set for 金仓)
    + padding to align4
    + data area (no offset table)
    """
    buf = bytearray(HEAP_TUPLE_HEADER_SIZE)
    struct.pack_into("<I", buf, 0, xmin)       # t_xmin
    struct.pack_into("<I", buf, 4, xmax)       # t_xmax
    struct.pack_into("<I", buf, 8, 0)          # t_field3 (cid)
    # t_ctid: (block, offset) = (0, 1) for first tuple
    struct.pack_into("<H", buf, 12, 0)          # block high
    struct.pack_into("<H", buf, 14, 0)          # block low
    struct.pack_into("<H", buf, 16, 1)          # offset

    infomask2 = nattrs  # nattrs in low 11 bits
    struct.pack_into("<H", buf, 18, infomask2)
    struct.pack_into("<H", buf, 20, infomask)
    buf[22] = t_hoff

    # null bitmap
    if null_bitmap is not None and (infomask & HEAP_HASNULL):
        buf.extend(null_bitmap)

    # OID (for system catalogs)
    if hasoid:
        # Pad to align4 after null bitmap
        while len(buf) % 4 != 0:
            buf.append(0)
        buf.extend(struct.pack("<I", 0))  # placeholder OID

    # Pad to t_hoff
    while len(buf) < t_hoff:
        buf.append(0)

    # Data area (no offset table!)
    buf.extend(data)

    return bytes(buf)


def make_varlena_1b(text: str) -> bytes:
    """1-byte header varlena (PG/KingbaseES format)

    VARSIZE = total_size (header + data), stored as (total_size << 1) with bit 0 = 0.
    For KingbaseES, bit 0 = 1: header = (total_size << 1) | 1
    Both formats decode the same way: VARSIZE = header >> 1, data_len = VARSIZE - 1
    """
    encoded = text.encode("utf-8")
    total_size = 1 + len(encoded)  # 1B header + data
    header = (total_size << 1) | 1  # KingbaseES format: bit 0 = 1
    assert header < 128, f"varlena too long for 1B header: {header}"
    return bytes([header]) + encoded


def make_varlena_4b(text: str) -> bytes:
    """4-byte header varlena (PG 真实格式: 小端, word = total_size << 2, tag 低 2 位 = 00)"""
    encoded = text.encode("utf-8")
    total_size = 4 + len(encoded)
    import struct as _s
    hdr = _s.pack("<I", total_size << 2)
    return hdr + encoded


def test_scan_mode_basic():
    """测试基本扫描模式：3 行数据，2 列 (int4, varchar)"""
    print("=" * 60)
    print("测试 1: 基本扫描模式 (3 行, 2 列)")
    print("=" * 60)

    # 表元数据: test02 (id int4, name varchar(20))
    columns = [
        Column("id", INT4OID, 4, 1, -1, notnull=True),
        Column("name", VARCHAROID, -1, 2, 24, notnull=False),
    ]
    table_meta = TableMeta("24576", "ray", "test02", 40974, columns)

    # 构造 3 个元组
    tuples_data = []
    for i, name in enumerate(["alice", "bob", "charlie"], start=1):
        # 数据区: id (int4, 4B) + name (varchar, 1B header + payload)
        id_bytes = struct.pack("<i", i)
        name_bytes = make_varlena_1b(name)
        data = id_bytes + name_bytes

        # t_hoff: 23 (header) + 0 (no null bitmap) + 0 (no OID) → align4 = 24
        t_hoff = 24
        infomask = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID  # 0x0100 | 0x0800 = 0x0900
        nattrs = 2

        tup = make_kb_tuple(
            xmin=1000 + i, xmax=0, nattrs=nattrs,
            infomask=infomask, t_hoff=t_hoff, data=data
        )
        tuples_data.append(tup)

    # 构造页面: 金仓 page version 5, ItemId 为空
    # 数据区从 pd_upper 开始，向前生长
    # 页头大小: 48 字节 (46 对齐到 4)
    header_size = 48
    pd_lower = header_size  # 没有 ItemId
    pd_special = PAGE_SIZE

    # 计算元组总大小（每个元组按 MAXALIGN(8) 对齐）
    tuple_sizes = [len(t) for t in tuples_data]
    total_data = sum((s + 7) & ~7 for s in tuple_sizes)  # MAXALIGN each tuple
    pd_upper = pd_special - total_data

    # 构造页面
    page = make_kb_page_header(pd_lower, pd_upper, pd_special, version=5)
    # 写入元组数据（每个元组 MAXALIGN(8) 对齐）
    pos = pd_upper
    for tup_data in tuples_data:
        page[pos:pos + len(tup_data)] = tup_data
        pos += (len(tup_data) + 7) & ~7  # MAXALIGN(8)

    # 保存到临时文件
    test_file = os.path.join(os.path.dirname(__file__), ".temp", "test_kb_scan.dat")
    os.makedirs(os.path.dirname(test_file), exist_ok=True)
    with open(test_file, "wb") as f:
        f.write(page)

    # 验证页面解析
    pg = Page(0, bytes(page))
    assert pg.has_valid_layout, f"页面解析失败: {pg.error}"
    print(f"  页面布局: {pg.header.get('layout', 'unknown')}")
    print(f"  version: {pg.header.get('version')}, header_size: {pg.header_size}")
    print(f"  pd_lower: {pg.header['lower']}, pd_upper: {pg.header['upper']}, pd_special: {pg.header['special']}")
    print(f"  ItemId 数量: {len(pg.items)} (预期 0，金仓格式)")

    # 测试 HeapFile 解析
    hf = HeapFile(test_file)
    col_lengths = _build_col_lengths(table_meta)
    print(f"  col_lengths: {col_lengths}")

    # 先试标准模式
    standard_tuples = list(hf.iter_tuples(include_deleted=True))
    print(f"  标准模式找到 {len(standard_tuples)} 个元组")

    # 测试扫描模式
    col_lengths = _build_col_lengths(table_meta)
    scan_tuples = list(hf._iter_tuples_scan(n_expected_cols=2, col_lengths=col_lengths))
    print(f"  扫描模式找到 {len(scan_tuples)} 个元组")

    assert len(scan_tuples) == 3, f"期望 3 个元组，实际 {len(scan_tuples)}"

    # 验证元组内容
    for i, (pageno, pos, tup) in enumerate(scan_tuples):
        nulls = tup.get_nulls()
        fields = _extract_fields_direct(tup.raw, tup.t_hoff, nulls, col_lengths)
        # 解码
        id_val = struct.unpack("<i", fields[0][:4])[0]
        name_raw = fields[1]
        # varlena decode
        first = name_raw[0]
        if first & 0x80:
            vlen = struct.unpack(">I", name_raw[:4])[0] & 0x3FFFFFFF
            name_val = name_raw[4:4+vlen].decode("utf-8")
        else:
            name_val = name_raw[1:1+first].decode("utf-8")

        expected_names = ["alice", "bob", "charlie"]
        print(f"  元组 {i}: id={id_val}, name={name_val}, t_hoff={tup.t_hoff}, nattrs={tup.nattrs}")
        assert id_val == i + 1, f"id 期望 {i+1}, 实际 {id_val}"
        assert name_val == expected_names[i], f"name 期望 {expected_names[i]}, 实际 {name_val}"

    # 测试 dump_rows
    print("\n  测试 dump_rows:")
    rows = list(hf.dump_rows(table_meta))
    print(f"  dump_rows 返回 {len(rows)} 行")
    for row in rows:
        print(f"    ctid={row['ctid']} values={row['values']} deleted={row['deleted']}")

    assert len(rows) == 3, f"dump_rows 期望 3 行, 实际 {len(rows)}"
    assert rows[0]["values"] == ["1", "alice"], f"第一行: {rows[0]['values']}"
    assert rows[1]["values"] == ["2", "bob"], f"第二行: {rows[1]['values']}"
    assert rows[2]["values"] == ["3", "charlie"], f"第三行: {rows[2]['values']}"

    print("\n  ✓ 测试通过!\n")
    return test_file


def test_scan_mode_with_nulls():
    """测试含 NULL 值的扫描模式"""
    print("=" * 60)
    print("测试 2: 扫描模式含 NULL 值")
    print("=" * 60)

    columns = [
        Column("id", INT4OID, 4, 1, -1, notnull=True),
        Column("name", VARCHAROID, -1, 2, 24, notnull=False),
        Column("age", INT4OID, 4, 3, -1, notnull=False),
    ]
    table_meta = TableMeta("24576", "ray", "test_nulls", 40975, columns)

    # 元组 1: (1, 'alice', 30) - 无 NULL
    # 元组 2: (2, NULL, 25) - name 为 NULL
    # 元组 3: (3, 'charlie', NULL) - age 为 NULL

    tuples_data = []

    # 元组 1: 无 NULL, 但需要包含 HEAP_HASNULL 标志（因为有 NULL 位图）
    # 数据布局: id(4B) + varlena(1+5=6B) + padding(2B) + age(4B) = 16B
    # age 需 4 字节对齐：4+6=10 → 对齐到 12
    data1 = struct.pack("<i", 1) + make_varlena_1b("alice") + b'\x00\x00' + struct.pack("<i", 30)
    # nattrs=3, null bitmap: 1 byte, 0b00000000 (no nulls)
    # t_hoff = align4(23 + 1) = 24
    t_hoff1 = 24
    infomask1 = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | HEAP_HASNULL  # 0x0901
    # infomask2 = nattrs | (no flags) = 3
    tup1 = make_kb_tuple(
        xmin=1001, xmax=0, nattrs=3,
        infomask=infomask1, t_hoff=t_hoff1,
        null_bitmap=bytes([0x07]), data=data1
    )
    tuples_data.append(tup1)

    # 元组 2: name 为 NULL (bit 1 清零，其余置位；PG 语义 bit置1=非空)
    # null bitmap: 0b00000101 = 0x05
    # data: id(4B) + age(4B), name skipped (NULL 不占空间)
    # age 紧跟 id，4 字节已对齐
    data2 = struct.pack("<i", 2) + struct.pack("<i", 25)
    t_hoff2 = 24
    infomask2 = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | HEAP_HASNULL  # 0x0901
    tup2 = make_kb_tuple(
        xmin=1002, xmax=0, nattrs=3,
        infomask=infomask2, t_hoff=t_hoff2,
        null_bitmap=bytes([0x05]), data=data2
    )
    tuples_data.append(tup2)

    # 元组 3: age 为 NULL (bit 2 清零)
    # null bitmap: 0b00000011 = 0x03
    # data: id(4B) + name(varlena)
    data3 = struct.pack("<i", 3) + make_varlena_1b("charlie")
    t_hoff3 = 24
    infomask3 = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | HEAP_HASNULL  # 0x0901
    tup3 = make_kb_tuple(
        xmin=1003, xmax=0, nattrs=3,
        infomask=infomask3, t_hoff=t_hoff3,
        null_bitmap=bytes([0x03]), data=data3
    )
    tuples_data.append(tup3)

    # 构造页面
    header_size = 48
    pd_lower = header_size
    pd_special = PAGE_SIZE
    total_data = sum((len(t) + 7) & ~7 for t in tuples_data)  # MAXALIGN each
    pd_upper = pd_special - total_data

    page = make_kb_page_header(pd_lower, pd_upper, pd_special, version=5)
    pos = pd_upper
    for tup_data in tuples_data:
        page[pos:pos + len(tup_data)] = tup_data
        pos += (len(tup_data) + 7) & ~7  # MAXALIGN(8)

    test_file = os.path.join(os.path.dirname(__file__), ".temp", "test_kb_nulls.dat")
    with open(test_file, "wb") as f:
        f.write(page)

    hf = HeapFile(test_file)
    col_lengths = _build_col_lengths(table_meta)
    print(f"  col_lengths: {col_lengths}")

    scan_tuples = list(hf._iter_tuples_scan(n_expected_cols=3))
    print(f"  扫描模式找到 {len(scan_tuples)} 个元组")
    assert len(scan_tuples) == 3, f"期望 3 个元组，实际 {len(scan_tuples)}"

    rows = list(hf.dump_rows(table_meta))
    print(f"  dump_rows 返回 {len(rows)} 行")
    for row in rows:
        print(f"    values={row['values']}")

    assert rows[0]["values"] == ["1", "alice", "30"], f"第一行: {rows[0]['values']}"
    assert rows[1]["values"] == ["2", None, "25"], f"第二行: {rows[1]['values']}"
    assert rows[2]["values"] == ["3", "charlie", None], f"第三行: {rows[2]['values']}"

    print("\n  ✓ 测试通过!\n")


def test_scan_mode_to_sql():
    """测试扫描模式生成 SQL"""
    print("=" * 60)
    print("测试 3: 扫描模式生成 SQL/CSV")
    print("=" * 60)

    columns = [
        Column("id", INT4OID, 4, 1, -1, notnull=True),
        Column("name", VARCHAROID, -1, 2, 24, notnull=False),
    ]
    table_meta = TableMeta("24576", "ray", "test02", 40974, columns)

    tuples_data = []
    for i, name in enumerate(["alice", "bob", "中文测试"], start=1):
        data = struct.pack("<i", i) + make_varlena_1b(name)
        t_hoff = 24
        infomask = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID
        tup = make_kb_tuple(
            xmin=1000 + i, xmax=0, nattrs=2,
            infomask=infomask, t_hoff=t_hoff, data=data
        )
        tuples_data.append(tup)

    header_size = 48
    pd_lower = header_size
    pd_special = PAGE_SIZE
    total_data = sum((len(t) + 7) & ~7 for t in tuples_data)  # MAXALIGN each
    pd_upper = pd_special - total_data

    page = make_kb_page_header(pd_lower, pd_upper, pd_special, version=5)
    pos = pd_upper
    for tup_data in tuples_data:
        page[pos:pos + len(tup_data)] = tup_data
        pos += (len(tup_data) + 7) & ~7  # MAXALIGN(8)

    test_file = os.path.join(os.path.dirname(__file__), ".temp", "test_kb_sql.dat")
    with open(test_file, "wb") as f:
        f.write(page)

    hf = HeapFile(test_file)

    # 测试 SQL 输出
    print("  SQL 输出:")
    sqls = list(hf.to_sql(table_meta))
    for s in sqls:
        print(f"    {s}")

    assert len(sqls) == 3, f"期望 3 条 SQL, 实际 {len(sqls)}"
    assert "alice" in sqls[0]
    assert "中文测试" in sqls[2]

    # 测试 CSV 输出
    print("\n  CSV 输出:")
    csvs = list(hf.to_data(table_meta))
    for c in csvs:
        print(f"    {c}")

    assert len(csvs) == 3

    # 测试 count
    rows = list(hf.dump_rows(table_meta))
    assert len(rows) == 3

    print("\n  ✓ 测试通过!\n")


def test_standard_pg_mode_still_works():
    """验证标准 PG 格式仍然使用 ItemId 模式"""
    print("=" * 60)
    print("测试 4: 标准 PG 格式仍正常工作")
    print("=" * 60)

    # 使用已有的合成测试文件
    test_file = os.path.join(os.path.dirname(__file__), ".temp", "test_heap.dat")
    meta_file = os.path.join(os.path.dirname(__file__), ".temp", "meta.json")

    if not os.path.exists(test_file) or not os.path.exists(meta_file):
        print("  跳过: 需要先运行 test_synthetic.py 生成测试数据")
        return

    # 用 main.py 测试
    import subprocess
    result = subprocess.run(
        [sys.executable, "main.py", test_file, "--catalog-json", meta_file, "--sql"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
    )
    print(f"  stdout: {result.stdout[:200]}")
    if result.returncode != 0:
        print(f"  stderr: {result.stderr[:200]}")

    assert "INSERT INTO" in result.stdout, "标准 PG 格式应输出 INSERT 语句"
    assert "alice" in result.stdout

    print("\n  ✓ 测试通过!\n")


if __name__ == "__main__":
    test_scan_mode_basic()
    test_scan_mode_with_nulls()
    test_scan_mode_to_sql()
    test_standard_pg_mode_still_works()
    print("=" * 60)
    print("全部测试通过!")
    print("=" * 60)
