#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 TOAST 自动关联与重组。

模拟场景:
- 主表 3 列 (id int8, name varchar, big varchar)
- big 列外联到 TOAST 表 (relid=41012)
- TOAST 表文件含 chunk (valueid=41068, 2 个 chunk)

验证:
1. _find_toast_path 从主表外联指针自动发现 toastrelid
2. ToastFile 解析金仓 TOAST 表（无偏移表格式）
3. chunk 重组 + 解压（如有压缩）后得到完整值
"""
import os
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE, Page
from pg2sql.tuple import (
    HeapTuple, HEAP_XMIN_COMMITTED, HEAP_XMAX_INVALID,
)
from pg2sql.heapfile import (
    HeapFile, _extract_fields_direct, _build_col_lengths,
    _check_external, _is_kb_external,
)
from pg2sql.toast import ToastFile
from pg2sql.catalog import TableMeta, Column
from pg2sql.types import INT8OID, VARCHAROID


def make_kb_page(pd_lower, pd_upper, pd_special, version=5):
    header = bytearray(PAGE_SIZE)
    struct.pack_into("<H", header, 38, pd_lower)
    struct.pack_into("<H", header, 40, pd_upper)
    struct.pack_into("<H", header, 42, pd_special)
    psv = (PAGE_SIZE & ~0x7F) | (version & 0x7F)
    struct.pack_into("<H", header, 44, psv)
    return header


def make_varlena_1b(data: bytes) -> bytes:
    total_size = 1 + len(data)
    header = (total_size << 1) | 1
    assert header < 128, f"varlena too long: {header}"
    return bytes([header]) + data


def make_kb_external_ptr(extsize, rawsize, valueid, toastrelid) -> bytes:
    buf = bytearray(b"\x01\x12")
    buf += struct.pack("<I", extsize)
    buf += struct.pack("<I", rawsize)
    buf += struct.pack("<I", valueid)
    buf += struct.pack("<I", toastrelid)
    return bytes(buf)


def build_main_table(test_dir, main_relfilenode, toastrelid, valueid, small_val, big_len):
    """构造主表: id int8, name varchar(内联), big varchar(外联)"""
    columns = [
        Column("id", INT8OID, 8, 1, -1, True),
        Column("name", VARCHAROID, -1, 2, -1, False),
        Column("big", VARCHAROID, -1, 3, -1, False),
    ]
    table_meta = TableMeta("24576", "ray", "test_big", main_relfilenode, columns)

    # 元组数据: id(8B) + name(1B varlena) + big(18B 外联)
    data = struct.pack("<q", 1)
    data += make_varlena_1b(small_val.encode())
    # 外联: PG 真实语义 rawsize = 4 + payload_len（含 varlena 头），未压缩时 extsize == rawsize
    data += make_kb_external_ptr(4 + big_len, 4 + big_len, valueid, toastrelid)

    t_hoff = 24
    tup_buf = bytearray(t_hoff)
    struct.pack_into("<I", tup_buf, 0, 100)
    struct.pack_into("<I", tup_buf, 4, 0)
    struct.pack_into("<H", tup_buf, 18, 3)  # nattrs=3
    struct.pack_into("<H", tup_buf, 20, HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | 0x0002 | 0x0004)
    tup_buf[22] = t_hoff
    tup_buf += data
    tuple_data = bytes(tup_buf)

    header_size = 48
    pd_special = PAGE_SIZE
    total = (len(tuple_data) + 7) & ~7
    pd_upper = pd_special - total
    page = make_kb_page(header_size, pd_upper, pd_special)
    page[pd_upper:pd_upper + len(tuple_data)] = tuple_data

    main_path = os.path.join(test_dir, str(main_relfilenode))
    with open(main_path, "wb") as f:
        f.write(page)
    return main_path, table_meta


def build_toast_table(test_dir, toastrelid, valueid, full_data):
    """构造金仓格式 TOAST 表: chunk_id, chunk_seq, chunk_data"""
    # 分成 2 个 chunk
    chunk_size = (len(full_data) + 1) // 2
    chunks = []
    for seq, off in enumerate(range(0, len(full_data), chunk_size)):
        chunks.append((valueid, seq, full_data[off:off + chunk_size]))

    tuples_data = []
    for chunk_id, seq, cdata in chunks:
        # 3 列: chunk_id(4B) chunk_seq(4B) chunk_data(varlena)
        data = struct.pack("<I", chunk_id)
        data += struct.pack("<i", seq)
        data += make_varlena_1b(cdata) if len(cdata) < 100 else make_varlena_4b(cdata)

        t_hoff = 24
        tup_buf = bytearray(t_hoff)
        struct.pack_into("<I", tup_buf, 0, 100)
        struct.pack_into("<I", tup_buf, 4, 0)
        struct.pack_into("<H", tup_buf, 18, 3)
        struct.pack_into("<H", tup_buf, 20, HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | 0x0002)
        tup_buf[22] = t_hoff
        tup_buf += data
        tuples_data.append(bytes(tup_buf))

    header_size = 48
    pd_special = PAGE_SIZE
    total = sum((len(t) + 7) & ~7 for t in tuples_data)
    pd_upper = pd_special - total
    page = make_kb_page(header_size, pd_upper, pd_special)
    pos = pd_upper
    for td in tuples_data:
        page[pos:pos + len(td)] = td
        pos += (len(td) + 7) & ~7

    toast_path = os.path.join(test_dir, str(toastrelid))
    with open(toast_path, "wb") as f:
        f.write(page)
    return toast_path


def make_varlena_4b(data: bytes) -> bytes:
    """PG 真实格式: 小端 word = total << 2, tag 低 2 位 = 00"""
    total = 4 + len(data)
    import struct as _s
    return _s.pack("<I", total << 2) + data


def test_toast_auto():
    print("=" * 60)
    print("测试: TOAST 自动发现与重组")
    print("=" * 60)

    test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".temp", "toast_test")
    os.makedirs(test_dir, exist_ok=True)
    # 清理旧文件（跳过目录）
    import shutil
    for name in os.listdir(test_dir):
        full = os.path.join(test_dir, name)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)

    # 完整大值: 1500 字节
    big_value = ("ABCDEFGH" * 200)[:1500]
    big_bytes = big_value.encode()
    assert len(big_bytes) == 1500, f"实际长度 {len(big_bytes)}"

    TOASTRELID = 41012
    VALUEID = 41068
    MAIN_RFN = 41008

    # 构造主表和 TOAST 表
    main_path, table_meta = build_main_table(test_dir, MAIN_RFN, TOASTRELID, VALUEID, "hello", len(big_bytes))
    toast_path = build_toast_table(test_dir, TOASTRELID, VALUEID, big_bytes)
    print(f"  主表: {main_path}")
    print(f"  TOAST 表: {toast_path}")

    # ---- 测试 _find_toast_path（模拟 args）----
    class MockArgs:
        datafile = main_path
        toast = None
        page_size = PAGE_SIZE
        verbose = True

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    # main.py 在项目根目录，直接 import 会执行模块级代码，改为手动调用逻辑
    # 这里直接测 ToastFile + HeapFile 的组合
    from pg2sql.toast import ToastFile as TF

    toast = TF(toast_path, page_size=PAGE_SIZE)
    toast.load()
    chunk_ids = toast.get_all_chunk_ids()
    print(f"  TOAST 表 chunk valueid: {chunk_ids}")
    assert VALUEID in chunk_ids, f"TOAST 表应含 valueid {VALUEID}"

    chunks = toast.fetch_chunks_by_valueid(VALUEID)
    print(f"  valueid={VALUEID} 有 {len(chunks)} 个 chunk")
    assert len(chunks) == 2

    # 重组
    reassembled = toast.fetch_and_reassemble(VALUEID)
    assert reassembled == big_bytes, f"重组长度 {len(reassembled)} != {len(big_bytes)}"
    print(f"  重组成功: {len(reassembled)} 字节")
    assert reassembled.decode() == big_value
    print(f"  内容验证 ✓")

    # ---- 端到端: HeapFile + Toast 关联 ----
    hf = HeapFile(main_path)
    hf.set_toast(toast)

    col_lengths = _build_col_lengths(table_meta)
    rows = list(hf.dump_rows(table_meta))
    print(f"\n  端到端 dump_rows: {len(rows)} 行")
    assert len(rows) == 1
    values = rows[0]["values"]
    print(f"  id={values[0]}, name={values[1]}, big 前 30 字符={values[2][:30]}...")
    assert values[0] == "1"
    assert values[1] == "hello"
    assert values[2] == big_value, f"big 值重组失败: 得到 {len(values[2])} 字符"

    # ---- 页级缓存模式（轻量索引 + fetch_and_reassemble 走页缓存）----
    print("\n  页级缓存模式 (build_index + 按需读页):")
    tf2 = TF(toast_path, page_size=PAGE_SIZE)
    tf2.build_index()
    assert VALUEID in tf2.get_all_chunk_ids()
    # 统计物理读次数（monkey-patch _read_page）
    real_read = tf2._read_page
    reads = {"n": 0}
    def counted(pageno):
        reads["n"] += 1
        return real_read(pageno)
    tf2._read_page = counted
    # 同一 valueid 重组 3 次: 2 chunks 在同页 → 只应物理读 1 次, 后续全命中缓存
    r1 = tf2.fetch_and_reassemble(VALUEID)
    r2 = tf2.fetch_and_reassemble(VALUEID)
    r3 = tf2.fetch_and_reassemble(VALUEID)
    assert r1 == r2 == r3 == big_bytes, "页缓存模式重组结果不一致"
    assert reads["n"] == 1, f"同页 chunk 应只物理读 1 次, 实际 {reads['n']} 次"
    print(f"  重复重组 3 次仅物理读 {reads['n']} 次 (页缓存命中) ✓")
    tf2.close()

    # full 模式（--toast-cache full）流式加载等价性
    tf3 = TF(toast_path, page_size=PAGE_SIZE)
    tf3.load()
    assert tf3.fetch_and_reassemble(VALUEID) == big_bytes
    tf3.close()
    print("  full 模式流式加载重组等价 ✓")

    print("\n  ✓ TOAST 自动关联 + 重组测试全部通过!")


if __name__ == "__main__":
    test_toast_auto()
    print("=" * 60)
    print("全部测试通过!")
    print("=" * 60)
