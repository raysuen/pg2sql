#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 --list-db 功能:
1. 构造模拟 PG 数据目录结构 (global/1262 = pg_database)
2. 构造 pg_database 堆页面 (含 HEAP_HASOID 标志)
3. 在 base/ 下创建 OID 子目录
4. 运行 --list-db 验证输出
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
from pg2sql.binary import cstring


def build_pg_database_tuple(oid: int, datname: str) -> bytes:
    """构造 pg_database 表的一行。

    pg_database 布局 (简化，只填前几个字段):
      列1: datname  name(64B, 固定长度, NUL 结尾)
      列2: datdba   oid(4B)
      列3: encoding int4(4B)
      其余字段填 0/NULL

    系统目录表有 HEAP_HASOID 标志，OID 存储在 NULL 位图之后。

    行头: 23B 固定头
    无 NULL 位图 (所有列都有值)
    OID: 4B (HEAP_HASOID)
    偏移表: 3 项 * 2B = 6B
    对齐: 23 + 4(OID) + 6(offset) = 33 → align4 = 36
    t_hoff = 36

    数据区: name(64B) + oid(4B) + int4(4B) = 72B
    """
    nattrs = 3  # 只构造 3 个字段
    has_null = False
    has_oid = True

    # 数据区
    name_bytes = datname.encode("utf-8")
    name_field = name_bytes + b"\x00" * (64 - len(name_bytes))  # NAMEDATALEN=64
    datdba_field = struct.pack("<I", 10)  # postgres 用户 OID
    encoding_field = struct.pack("<i", 6)  # UTF8

    data = name_field + datdba_field + encoding_field

    # t_hoff 计算
    # 固定头 23B + 无 null 位图 + OID 4B = 27B → 对齐到 4 = 28B
    # 偏移表 3*2=6B → 28+6=34B → 对齐到 4 = 36B
    # 但实际上偏移表紧跟 OID 之后，不需要额外对齐
    # t_hoff = align4(23 + 0 + 4) + 6 = 28 + 6 = 34 → align4 = 36
    # 不对，t_hoff 是数据区起始位置
    # 布局: [header 23B][OID 4B = 27B][padding to 28B][offset_table 6B][padding to 32B]
    # 等等，让我重新算:
    # [header 23B] → 偏移 23
    # [OID 4B] → 偏移 23+4=27
    # [padding to align4] → 27→28 (1B padding)
    # [null bitmap] → 无 (0B)
    # 偏移表紧跟在 null bitmap 之后:
    # [offset_table 3*2=6B] → 偏移 28, 结束 34
    # [padding to align4] → 34→36 (2B padding)
    # t_hoff = 36, 数据从 36 开始

    # 不对，OID 和 null bitmap 的位置关系:
    # 正确布局: [header 23B] [null bitmap] [OID] [padding] [offset_table] [data]
    # 无 null bitmap: [header 23B] [OID 4B] = 27 → align4 = 28
    # [offset_table 6B] = 28+6 = 34 → align4 = 36
    # t_hoff = 36

    # t_hoff 计算:
    # [header 23B] → 偏移 23
    # [OID 4B] → 从 align4(23)=24 开始 → 24-27
    # [offset_table 6B] → 28-33
    # [padding to align4] → 34→36 (2B)
    # t_hoff = 36, 数据从 36 开始

    t_hoff = 36

    buf = bytearray(t_hoff + len(data))

    # 写行头
    struct.pack_into("<I", buf, 0, 1)          # t_xmin = 1
    struct.pack_into("<I", buf, 4, 0)          # t_xmax = 0
    struct.pack_into("<I", buf, 8, 0)          # t_field3 = 0
    struct.pack_into("<I", buf, 12, 0)         # t_ctid block = 0
    struct.pack_into("<H", buf, 16, 1)         # t_ctid offset = 1
    struct.pack_into("<H", buf, 18, nattrs)    # t_infomask2 = 3
    infomask = HEAP_XMAX_INVALID | HEAP_XMIN_FROZEN | HEAP_HASOID
    struct.pack_into("<H", buf, 20, infomask)  # t_infomask
    buf[22] = t_hoff                            # t_hoff

    # OID: 在 align4(23+null_bitmap)=24 处
    struct.pack_into("<I", buf, 24, oid)       # OID = oid

    # 偏移表: 在 OID 之后，从 28 开始
    # off[0] = 0  (datname, 64B)
    # off[1] = 64 (datdba, 4B)
    # off[2] = 68 (encoding, 4B)
    struct.pack_into("<HHH", buf, 28, 0, 64, 68)

    # 数据区: 从 t_hoff=36 开始
    buf[36:] = data

    return bytes(buf)


def build_page_with_tuples(tuples: list) -> bytes:
    """构造一个 8KB 页面，包含多行 tuple。"""
    page = bytearray(PAGE_SIZE)
    n = len(tuples)

    # 从页尾向前放置 tuple
    item_ids = []
    data_upper = PAGE_SIZE
    for tup_bytes in tuples:
        data_upper -= len(tup_bytes)
        off = data_upper
        flags = 1  # NORMAL
        length = len(tup_bytes)
        item_id_raw = (off & 0x7FFF) | ((flags & 0x03) << 15) | ((length & 0x7FFF) << 17)
        item_ids.append(item_id_raw)

    pd_lower = 24 + n * 4
    pd_upper = data_upper

    # 写页头 (PG 8.3+ 布局)
    struct.pack_into("<Q", page, 0, 0)           # pd_lsn
    struct.pack_into("<H", page, 8, 0)           # pd_checksum
    struct.pack_into("<H", page, 10, 0)          # pd_flags
    struct.pack_into("<H", page, 12, pd_lower)   # pd_lower
    struct.pack_into("<H", page, 14, pd_upper)   # pd_upper
    struct.pack_into("<H", page, 16, PAGE_SIZE)  # pd_special
    struct.pack_into("<H", page, 18, PAGE_SIZE | PG_PAGE_VERSION)  # pd_pagesize_version

    # 写 ItemId 数组
    for i, raw_id in enumerate(item_ids):
        struct.pack_into("<I", page, 24 + i * 4, raw_id)

    # 写 tuple 数据
    cur = PAGE_SIZE
    for tup_bytes in tuples:
        cur -= len(tup_bytes)
        page[cur : cur + len(tup_bytes)] = tup_bytes

    return bytes(page)


def main():
    test_dir = os.path.join(PROJECT, ".temp", "listdb_test")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    # 创建模拟 PG 数据目录结构
    pgdata = os.path.join(test_dir, "pgdata")
    base_dir = os.path.join(pgdata, "base")
    global_dir = os.path.join(pgdata, "global")
    os.makedirs(base_dir)
    os.makedirs(global_dir)

    # 创建 base/ 下的 OID 目录
    db_oids = [1, 4, 5, 16384, 16385]
    for oid in db_oids:
        os.makedirs(os.path.join(base_dir, str(oid)))

    # 构造 pg_database 堆文件 (global/1262)
    db_entries = [
        (1, "template1"),
        (4, "template0"),
        (5, "postgres"),
        (16384, "testdb"),
        (16385, "myapp"),
    ]

    tuples = [build_pg_database_tuple(oid, name) for oid, name in db_entries]
    page_data = build_page_with_tuples(tuples)

    pg_db_path = os.path.join(global_dir, "1262")
    with open(pg_db_path, "wb") as f:
        f.write(page_data)

    print(f"模拟数据目录: {pgdata}")
    print(f"base/ 目录: {base_dir}")
    print(f"pg_database 文件: {pg_db_path}")
    print()

    # 运行 --list-db (通过位置参数指定 base/ 目录)
    print("========== python3 main.py <base_dir> --list-db ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py '{base_dir}' --list-db --verbose 2>&1")

    print()

    # 运行 --list-db (通过 --datadir 指定 PG 数据目录)
    print("========== python3 main.py --datadir <pgdata> --list-db ==========")
    os.system(f"cd '{PROJECT}' && python3 main.py --datadir '{pgdata}' --list-db 2>&1")


if __name__ == "__main__":
    main()
