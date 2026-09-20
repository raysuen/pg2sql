#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 --list-tables-db 功能:
1. 构造模拟 base/16384/ 数据库目录
2. 构造 pg_class (1259) 堆页面 (含 HEAP_HASOID)
3. 创建若干表 OID 文件
4. 运行 --list-tables-db 验证输出
"""
import struct
import os
import sys
import shutil

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from pg2sql.page import PAGE_SIZE, PG_PAGE_VERSION
from pg2sql.tuple import (
    HEAP_TUPLE_HEADER_SIZE, HEAP_XMAX_INVALID, HEAP_XMIN_FROZEN,
    HEAP_HASOID,
)
from pg2sql.binary import cstring, align4


def build_pg_class_tuple(oid: int, relname: str, relfilenode: int, relkind: str = "r") -> bytes:
    """构造 pg_class 表的一行。

    简化布局，只填关键列:
      列1: relname      name(64B)     - 表名
      列2: relnamespace oid(4B)       - 2200 = public
      列3: reltype      oid(4B)       - 0
      列4: reloftype    oid(4B)       - 0
      列5: relowner     oid(4B)       - 10
      列6: relam        oid(4B)       - 0
      列7: relfilenode  oid(4B)       - 文件名
      列8-13: 填 0 (6 个 oid/int4)
      列14: relkind     char(1B)      - 'r'/'v'/'i'/'S' 等

    共 14 列。HEAP_HASOID 标志置位。

    行头: 23B
    无 NULL 位图
    OID: 4B (从 align4(23)=24 开始)
    偏移表: 14 项 * 2B = 28B，从 28 开始
    t_hoff = align4(28 + 28) = align4(56) = 56

    数据区: 64 + 4*6 + 4 + 4*6 + 1 = 64+24+4+24+1 = 117B → align 到 4 = 120B
    """
    nattrs = 14
    t_hoff = 56

    # 数据区
    name_bytes = relname.encode("utf-8")
    name_field = name_bytes + b"\x00" * (64 - len(name_bytes))
    relnamespace = struct.pack("<I", 2200)  # public
    reltype = struct.pack("<I", 0)
    reloftype = struct.pack("<I", 0)
    relowner = struct.pack("<I", 10)
    relam = struct.pack("<I", 0)
    relfilenode_field = struct.pack("<I", relfilenode)
    # 列8-13: reltablespace, relpages, reltuples, relallvisible, reltoastrelid, relhasindex
    cols_8_13 = b"\x00" * (4 * 6)
    # 列14: relkind
    relkind_field = bytes([ord(relkind)])

    data = name_field + relnamespace + reltype + reloftype + relowner + relam + relfilenode_field + cols_8_13 + relkind_field

    buf = bytearray(t_hoff + len(data))

    # 写行头
    struct.pack_into("<I", buf, 0, 1)          # t_xmin
    struct.pack_into("<I", buf, 4, 0)          # t_xmax
    struct.pack_into("<I", buf, 8, 0)          # t_field3
    struct.pack_into("<I", buf, 12, 0)         # t_ctid block
    struct.pack_into("<H", buf, 16, 1)         # t_ctid offset
    struct.pack_into("<H", buf, 18, nattrs)    # t_infomask2 = 14
    infomask = HEAP_XMAX_INVALID | HEAP_XMIN_FROZEN | HEAP_HASOID
    struct.pack_into("<H", buf, 20, infomask)  # t_infomask
    buf[22] = t_hoff                           # t_hoff

    # OID: 从 align4(23)=24 开始
    struct.pack_into("<I", buf, 24, oid)

    # 偏移表: 14 项，从 28 开始
    # 列1: name(64B) off=0
    # 列2-7: 6 个 oid(4B) off=64,68,72,76,80,84
    # 列8-13: 6 个 4B off=88,92,96,100,104,108
    # 列14: char(1B) off=112
    offs = [0, 64, 68, 72, 76, 80, 84, 88, 92, 96, 100, 104, 108, 112]
    for i, off in enumerate(offs):
        struct.pack_into("<H", buf, 28 + i * 2, off)

    # 数据区: 从 t_hoff 开始
    buf[t_hoff:] = data

    return bytes(buf)


def build_page_with_tuples(tuples: list) -> bytes:
    """构造一个 8KB 页面，包含多行 tuple。"""
    page = bytearray(PAGE_SIZE)
    n = len(tuples)

    item_ids = []
    data_upper = PAGE_SIZE
    for tup_bytes in tuples:
        data_upper -= len(tup_bytes)
        off = data_upper
        flags = 1
        length = len(tup_bytes)
        item_id_raw = (off & 0x7FFF) | ((flags & 0x03) << 15) | ((length & 0x7FFF) << 17)
        item_ids.append(item_id_raw)

    pd_lower = 24 + n * 4
    pd_upper = data_upper

    struct.pack_into("<Q", page, 0, 0)
    struct.pack_into("<H", page, 8, 0)           # pd_checksum
    struct.pack_into("<H", page, 10, 0)          # pd_flags
    struct.pack_into("<H", page, 12, pd_lower)   # pd_lower
    struct.pack_into("<H", page, 14, pd_upper)   # pd_upper
    struct.pack_into("<H", page, 16, PAGE_SIZE)  # pd_special
    struct.pack_into("<H", page, 18, PAGE_SIZE | PG_PAGE_VERSION)

    for i, raw_id in enumerate(item_ids):
        struct.pack_into("<I", page, 24 + i * 4, raw_id)

    cur = PAGE_SIZE
    for tup_bytes in tuples:
        cur -= len(tup_bytes)
        page[cur : cur + len(tup_bytes)] = tup_bytes

    return bytes(page)


def main():
    test_dir = os.path.join(PROJECT, ".temp", "listtables_test")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    # 创建模拟数据库目录 base/16384/
    db_dir = os.path.join(test_dir, "base", "16384")
    os.makedirs(db_dir)

    # 构造 pg_class (1259) 堆文件
    # 模拟 5 张表:
    #   1259: pg_class 本身 (系统表)
    #   1249: pg_attribute (系统表)
    #   16384: users (用户表)
    #   16387: orders (用户表)
    #   16390: order_items (用户表)
    pg_class_entries = [
        (1259, "pg_class",      1259, "r"),
        (1249, "pg_attribute",  1249, "r"),
        (16384, "users",        16384, "r"),
        (16387, "orders",       16387, "r"),
        (16390, "order_items",  16390, "r"),
    ]

    tuples = [build_pg_class_tuple(oid, name, rfn, kind) for oid, name, rfn, kind in pg_class_entries]
    page_data = build_page_with_tuples(tuples)

    pg_class_path = os.path.join(db_dir, "1259")
    with open(pg_class_path, "wb") as f:
        f.write(page_data)

    # 创建表文件（空文件即可，只需存在）
    for oid, name, rfn, kind in pg_class_entries:
        fpath = os.path.join(db_dir, str(rfn))
        if not os.path.exists(fpath):
            with open(fpath, "wb") as f:
                f.write(b"\x00" * 0)  # 空文件

    # 额外创建一个 pg_class 中没有的文件（模拟残留/orphan）
    with open(os.path.join(db_dir, "16400"), "wb") as f:
        f.write(b"")

    # 创建辅助文件（应被排除）
    with open(os.path.join(db_dir, "16384_fsm"), "wb") as f:
        f.write(b"")
    with open(os.path.join(db_dir, "16384_vm"), "wb") as f:
        f.write(b"")

    print(f"模拟数据库目录: {db_dir}")
    print(f"pg_class 文件: {pg_class_path}")
    print()

    # 运行 --list-tables-db
    print("========== python3 main.py <db_dir> --list-tables-db --verbose ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py '{db_dir}' --list-tables-db --verbose 2>&1")


if __name__ == "__main__":
    main()
