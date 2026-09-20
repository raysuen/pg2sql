#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试金仓 50 列 + TOAST 外联指针场景。

模拟特征：
1. id 为 int8（8 字节）
2. 列值 62B（1B 头 0x7F）内联
3. 部分列是 18 字节金仓外联指针 [01 12][extsize][rawsize][valueid][toastrelid]

预期: 字段不错位，外联列识别为 external，内联列正常解码
"""
import os
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE, Page
from pg2sql.tuple import (
    HeapTuple, HEAP_TUPLE_HEADER_SIZE, HEAP_NATTS_MASK,
    HEAP_XMIN_COMMITTED, HEAP_XMAX_INVALID,
)
from pg2sql.heapfile import (
    HeapFile, _extract_fields_direct, _build_col_lengths, _check_external,
    _is_kb_external, KB_EXTERNAL_SIZE,
)
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


def make_varlena_1b(text: str) -> bytes:
    encoded = text.encode("utf-8")
    total_size = 1 + len(encoded)
    header = (total_size << 1) | 1
    assert header < 128, f"varlena too long for 1B header: {header}"
    return bytes([header]) + encoded


def make_kb_external_ptr(rawsize: int, valueid: int, toastrelid: int) -> bytes:
    """金仓 TOAST 外联指针: [01 12][extsize 4B][rawsize 4B][valueid 4B][toastrelid 4B] 小端"""
    extsize = rawsize + 4  # 观察到的规律
    buf = bytearray(b"\x01\x12")
    buf += struct.pack("<I", extsize)
    buf += struct.pack("<I", rawsize)
    buf += struct.pack("<I", valueid)
    buf += struct.pack("<I", toastrelid)
    assert len(buf) == KB_EXTERNAL_SIZE
    return bytes(buf)


def test_50col_with_external():
    print("=" * 60)
    print("测试: 50 列（id int8 + 49 varchar），含外联指针")
    print("=" * 60)

    # 模拟数据: id=1, col1=abc62, col2=外联, col3=中文, col4=外联, col5=abc62...
    n_cols = 50
    columns = [Column("id", INT8OID, 8, 1, -1, notnull=True)]
    for i in range(2, n_cols + 1):
        columns.append(Column(f"col{i-1}", VARCHAROID, -1, i, -1, False))
    table_meta = TableMeta("24576", "ray", "test_50col", 41008, columns)

    abc62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    # abc62 是 62 字符 = 62 字节 (1B 头 0x7F)
    cn31 = "一二三四五六七八九十春夏秋冬风云雨雪江河湖海山川日月星辰家国城市花草树木鸟鱼虫兽红蓝黑白高低长短快慢"[:31]

    # 构造数据区
    data = bytearray()
    data += struct.pack("<q", 1)  # id = 1 (int8, 8 字节)
    expected = [None] * n_cols
    expected[0] = "1"

    # col1 = abc62 内联
    data += make_varlena_1b(abc62)
    expected[1] = abc62
    # col2 = 外联指针
    data += make_kb_external_ptr(62, 41068, 41012)
    expected[2] = "__EXTERNAL__"
    # col3 = 中文 93B 内联 (1B 头, 93+1=94, (94<<1)|1=189=0xBD < 128? 189 > 127!)
    # 94 > 63 超过 1B 头容量 → 应该用 4B 头。这里用 62B 内的中文
    cn_short = "一二三四五六七八九十春夏秋冬风云雨雪江河湖海山川日月星辰家"[:20]  # 20 字 = 60B
    data += make_varlena_1b(cn_short)
    expected[3] = cn_short
    # col4 = 外联指针
    data += make_kb_external_ptr(150, 41044, 41012)
    expected[4] = "__EXTERNAL__"
    # col5~col49 = abc62 内联（col5 是第 6 个字段 → expected[5]）
    for i in range(5, n_cols):
        data += make_varlena_1b(abc62)
        expected[i] = abc62

    # 构造元组: t_hoff = 24 (23 头 + 1 padding, 无 NULL 位图)
    t_hoff = 24
    nattrs = n_cols
    infomask = HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID  # 0x0900
    # HASVARWIDTH 0x0002 + HASEXTERNAL 0x0004 = 0x0906
    infomask |= 0x0002 | 0x0004

    tup_buf = bytearray(t_hoff)
    struct.pack_into("<I", tup_buf, 0, 955)      # xmin
    struct.pack_into("<I", tup_buf, 4, 0)        # xmax
    struct.pack_into("<I", tup_buf, 8, 0)        # cid
    struct.pack_into("<H", tup_buf, 12, 0)       # ctid block
    struct.pack_into("<H", tup_buf, 16, 1)       # ctid off
    struct.pack_into("<H", tup_buf, 18, nattrs)  # infomask2
    struct.pack_into("<H", tup_buf, 20, infomask)
    tup_buf[22] = t_hoff
    tup_buf += data
    tuple_data = bytes(tup_buf)

    # 构造页面
    header_size = 48
    pd_lower = header_size
    pd_special = PAGE_SIZE
    total_data = (len(tuple_data) + 7) & ~7
    pd_upper = pd_special - total_data

    page = make_kb_page(pd_lower, pd_upper, pd_special)
    page[pd_upper:pd_upper + len(tuple_data)] = tuple_data

    test_file = os.path.join(os.path.dirname(__file__), ".temp", "test_50col.dat")
    os.makedirs(os.path.dirname(test_file), exist_ok=True)
    with open(test_file, "wb") as f:
        f.write(page)

    # 验证
    hf = HeapFile(test_file)
    col_lengths = _build_col_lengths(table_meta)
    print(f"  col_lengths[0:3]: {col_lengths[:3]}")

    # 找元组（扫描模式）
    scan_tuples = list(hf._iter_tuples_scan(n_expected_cols=n_cols, col_lengths=col_lengths))
    print(f"  扫描找到 {len(scan_tuples)} 个元组")
    assert len(scan_tuples) == 1

    pageno, pos, tup = scan_tuples[0]
    nulls = tup.get_nulls()
    fields = _extract_fields_direct(tup.raw, tup.t_hoff, nulls, col_lengths)
    print(f"  提取到 {len(fields)} 个字段")

    # 验证字段
    from pg2sql.types import decode_value
    ok = 0
    for i, f in enumerate(fields):
        if f is None:
            raise AssertionError(f"col{i} 不应为 None")
        if expected[i] == "__EXTERNAL__":
            payload, is_ext, ext_info = _check_external(f)
            assert is_ext, f"col{i} 应为外联，实际 payload={payload[:20]}"
            assert ext_info["toastrelid"] == 41012, f"col{i} toastrelid 错: {ext_info}"
            print(f"  col{i}: 外联 ✓ rawsize={ext_info['rawsize']} valueid={ext_info['valueid']} toastrelid={ext_info['toastrelid']}")
            ok += 1
        else:
            # 用类型解码器（id 是 int8，其他是 varchar varlena）
            val = decode_value(table_meta.columns[i].atttypid, f)
            assert val == expected[i], f"col{i} 期望 {expected[i][:20]}... 实际 {val[:20]}..."
            if i < 6:
                print(f"  col{i}: {val[:30]}{'...' if len(val) > 30 else ''} ✓")
            ok += 1

    assert ok == n_cols, f"验证通过 {ok}/{n_cols}"
    print(f"\n  全部 {n_cols} 列验证通过!")
    print("  ✓ 外联指针正确识别（不再错位）")
    print("  ✓ 内联 varlena 正确解码")
    print("  ✓ int8 id 正确读取\n")


def test_external_detection():
    """测试 _is_kb_external 的边界判断"""
    print("=" * 60)
    print("测试: 外联指针识别边界")
    print("=" * 60)

    # 正确的外联指针
    ptr = make_kb_external_ptr(62, 41068, 41012)
    assert _is_kb_external(ptr + b"\x00" * 10, 0), "应识别为外联"
    print("  有效指针 ✓")

    # 1B 头空值 (0x01) + 后续数据不是 0x12
    not_ptr = b"\x01" + b"\x7f" + b"abc"
    assert not _is_kb_external(not_ptr, 0), "不应识别为外联"
    print("  空值+普通头 不误判 ✓")

    # 1B 头 (VARSIZE=9, 0x12) 的普通值
    not_ptr2 = b"\x12" + b"abcdefgh"
    assert not _is_kb_external(not_ptr2, 0), "不应识别为外联"
    print("  普通 1B 头 不误判 ✓")

    # 01 12 后跟不合理的字段值
    bad = b"\x01\x12" + struct.pack("<I", 0) * 4
    assert not _is_kb_external(bad, 0), "零值字段不应识别为外联"
    print("  零值指针 不误判 ✓")

    print("  ✓ 边界测试全部通过\n")


if __name__ == "__main__":
    test_external_detection()
    test_50col_with_external()
    print("=" * 60)
    print("全部测试通过!")
    print("=" * 60)
