#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合成测试: 构造一个符合 PostgreSQL 8KB 堆页面格式的数据文件，
包含一张表 (id int4, name text) 的若干行数据，
然后用 pg2sql 解析验证。
"""
import struct
import os
import sys
import json

# 让 import 能找到 pg2sql
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from pg2sql.page import PAGE_SIZE, PAGE_HEADER_SIZE, PG_PAGE_VERSION
from pg2sql.tuple import (
    HEAP_TUPLE_HEADER_SIZE, HEAP_HASNULL, HEAP_XMAX_INVALID,
    HEAP_XMIN_FROZEN,
)

PAGE_SIZE_DEFAULT = PAGE_SIZE  # 8192


def build_page(rows: list) -> bytes:
    """rows: [(id_value, text_value), ...]
    构造一个合法的 8KB 堆页面。

    页面布局:
      [PageHeaderData 24B]
      [ItemId 数组 (每个 4B)]
      [空闲空间]
      [Tuple 数据 (从页尾向前填充)]
      [pd_special 区域 (0 长度)]
    """
    page = bytearray(PAGE_SIZE_DEFAULT)

    # --- 页头 ---
    # pd_lsn (8B) = 0
    # pd_flags (2B) = 0
    # pd_lower (2B) = 24 + n*4
    # pd_upper (2B) = 从尾部开始
    # pd_special (2B) = PAGE_SIZE (无 special 区域)
    # pd_pagesize_version (2B) = PAGE_SIZE | version
    n = len(rows)

    # 先计算每行 tuple 的大小并放置
    tuples = []
    for row_id, (id_val, text_val) in enumerate(rows):
        tup = build_tuple(id_val, text_val, frozen=True)
        tuples.append(tup)

    # 布局: ItemId 从 offset 24 开始，每个 4B
    itemid_area = 24
    data_upper = PAGE_SIZE_DEFAULT  # special = 0

    # 从页尾向前放置 tuple 数据
    item_ids = []
    for i, tup_bytes in enumerate(tuples):
        data_upper -= len(tup_bytes)
        # ItemId 位域 (小端): bits 0-14=lp_off, bits 15-16=lp_flags, bits 17-31=lp_len
        off = data_upper
        flags = 1  # NORMAL
        length = len(tup_bytes)
        item_id_raw = (off & 0x7FFF) | ((flags & 0x03) << 15) | ((length & 0x7FFF) << 17)
        item_ids.append(item_id_raw)

    pd_lower = itemid_area + n * 4
    pd_upper = data_upper

    # 写页头 (PG 8.3+ 布局，含 pd_checksum)
    struct.pack_into("<Q", page, 0, 0)           # pd_lsn
    struct.pack_into("<H", page, 8, 0)           # pd_checksum
    struct.pack_into("<H", page, 10, 0)          # pd_flags
    struct.pack_into("<H", page, 12, pd_lower)   # pd_lower
    struct.pack_into("<H", page, 14, pd_upper)   # pd_upper
    struct.pack_into("<H", page, 16, PAGE_SIZE_DEFAULT)  # pd_special
    struct.pack_into("<H", page, 18, PAGE_SIZE_DEFAULT | PG_PAGE_VERSION)  # pd_pagesize_version

    # 写 ItemId 数组
    for i, raw_id in enumerate(item_ids):
        struct.pack_into("<I", page, itemid_area + i * 4, raw_id)

    # 写 tuple 数据
    for i, tup_bytes in enumerate(tuples):
        off = data_upper + i * 0  # 已经算好
        page[off : off + len(tup_bytes)] = tup_bytes
        # 每个 tuple 的 off 不同，需要单独放置
        # 其实上面 data_upper 是递减的，所以需要调整
        break

    # 正确放置: 重新计算
    cur_upper = PAGE_SIZE_DEFAULT
    for i, tup_bytes in enumerate(tuples):
        cur_upper -= len(tup_bytes)
        page[cur_upper : cur_upper + len(tup_bytes)] = tup_bytes

    return bytes(page)


def build_tuple(id_val: int, text_val: str, frozen=True) -> bytes:
    """构造一个 HeapTuple:
    列1: id int4 (oid=23, 固定长度 4B)
    列2: name text (oid=25, varlena)

    HeapTupleHeaderData 布局 (23 字节固定头):
      偏移 0:  t_xmin     (4B) - 插入事务 ID
      偏移 4:  t_xmax     (4B) - 删除/锁事务 ID
      偏移 8:  t_field3   (4B) - union(t_cid, t_xvac)
      偏移 12: t_ctid     (6B) - 当前元组 ID (4B block + 2B offset)
      偏移 18: t_infomask2 (2B) - 属性数 + flags
      偏移 20: t_infomask  (2B) - 状态标志
      偏移 22: t_hoff     (1B) - 数据区起始偏移

    无 NULL 位图 → 无额外字节
    偏移表: 2 项 * 2B = 4B，从偏移 24 开始 (23 对齐到 4 = 24)
    t_hoff = 28 (24 + 4)
    数据区: int4(4B) + text_varlena(1B头 + text_bytes)
    """
    # 文本值的 varlena 编码 (1 字节短头)
    # PG/KingbaseES 1B 头: VARSIZE = total_size (含头), header = (total_size << 1) | 1
    text_bytes = text_val.encode("utf-8")
    total_size = 1 + len(text_bytes)  # 1B header + data
    if total_size < 128:
        header = (total_size << 1) | 1  # bit 0 = 1
        text_varlena = bytes([header]) + text_bytes
    else:
        total_size = 4 + len(text_bytes)
        hdr = (0x80000000 | total_size).to_bytes(4, "big")
        text_varlena = hdr + text_bytes

    # 数据区
    data = struct.pack("<i", id_val) + text_varlena

    # infomask
    infomask = HEAP_XMAX_INVALID  # 0x0800 (xmax 无效 = 未删除)
    if frozen:
        infomask |= HEAP_XMIN_FROZEN  # 0x0400

    # t_hoff: 23(固定头) + 1(padding 到 24) + 4(偏移表) = 28
    t_hoff = 28

    # 构造完整 tuple
    buf = bytearray(t_hoff + len(data))

    # 写固定头 (按正确偏移)
    struct.pack_into("<I", buf, 0, 1)          # t_xmin = 1
    struct.pack_into("<I", buf, 4, 0)          # t_xmax = 0
    struct.pack_into("<I", buf, 8, 0)          # t_field3 = 0 (t_cid)
    # t_ctid: 6 bytes at offset 12 (block 0 + offset 1)
    struct.pack_into("<I", buf, 12, 0)         # block id = 0
    struct.pack_into("<H", buf, 16, 1)         # offset = 1
    struct.pack_into("<H", buf, 18, 2)        # t_infomask2 = 2 (属性数)
    struct.pack_into("<H", buf, 20, infomask)  # t_infomask
    buf[22] = t_hoff                           # t_hoff = 28
    # buf[23] = padding (0)

    # 偏移表: 2 项，从偏移 24 开始 (23 对齐到 24)
    # off[0] = 0 → 列1 (int4) 起始偏移 = t_hoff + 0
    # off[1] = 4 → 列2 (text)  起始偏移 = t_hoff + 4
    struct.pack_into("<HH", buf, 24, 0, 4)

    # 数据区: 从偏移 28 (t_hoff) 开始
    buf[28:] = data

    return bytes(buf)


def main():
    test_dir = os.path.join(os.path.dirname(PROJECT), "pg2sql", ".temp")
    os.makedirs(test_dir, exist_ok=True)

    # 构造测试数据
    rows = [
        (1, "alice"),
        (2, "bob"),
        (3, "charlie"),
        (4, "中文测试"),
        (5, "hello world"),
    ]

    # 构建页面
    page_data = build_page(rows)

    # 写入文件 (单页)
    data_file = os.path.join(test_dir, "test_heap.dat")
    with open(data_file, "wb") as f:
        f.write(page_data)

    # 构造元数据 JSON
    meta = {
        "database": "testdb",
        "tables": [
            {
                "schema": "public",
                "table": "users",
                "relfilenode": 16384,
                "toastrelid": 0,
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type_oid": 23, "len": 4, "attnum": 1, "typmod": -1, "notnull": True},
                    {"name": "name", "type_oid": 25, "len": -1, "attnum": 2, "typmod": -1, "notnull": False},
                ],
            }
        ],
    }
    meta_file = os.path.join(test_dir, "meta.json")
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"测试文件: {data_file}")
    print(f"元数据文件: {meta_file}")

    # 运行 pg2sql
    print("\n========== pg2sql --ddl --sql ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --catalog-json '{meta_file}' --ddl --sql --verbose 2>&1")

    print("\n========== pg2sql --data ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --catalog-json '{meta_file}' --data 2>&1")

    print("\n========== pg2sql --list-tables ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py --catalog-json '{meta_file}' --list-tables 2>&1")


if __name__ == "__main__":
    main()
