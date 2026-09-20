#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试自动发现表结构功能（无需 --catalog-json）:
1. 构造模拟 base/16384/ 目录
2. 构造 pg_namespace (2615)、pg_class (1259)、pg_attribute (1249) 堆文件
3. 构造用户表 (16384=users) 堆文件，含 int4 + text 两列
4. 运行 pg2sql 不带 --catalog-json，验证自动发现 + DDL + SQL 输出
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


# ======================================================================
# 通用页面构造工具
# ======================================================================

def build_page_with_tuples(tuples):
    page = bytearray(PAGE_SIZE)
    n = len(tuples)
    item_ids = []
    data_upper = PAGE_SIZE
    for tup_bytes in tuples:
        data_upper -= len(tup_bytes)
        off = data_upper
        item_id_raw = (off & 0x7FFF) | (1 << 15) | ((len(tup_bytes) & 0x7FFF) << 17)
        item_ids.append(item_id_raw)
    pd_lower = 24 + n * 4
    pd_upper = data_upper
    struct.pack_into("<Q", page, 0, 0)
    struct.pack_into("<H", page, 8, 0)
    struct.pack_into("<H", page, 10, 0)
    struct.pack_into("<H", page, 12, pd_lower)
    struct.pack_into("<H", page, 14, pd_upper)
    struct.pack_into("<H", page, 16, PAGE_SIZE)
    struct.pack_into("<H", page, 18, PAGE_SIZE | PG_PAGE_VERSION)
    for i, raw_id in enumerate(item_ids):
        struct.pack_into("<I", page, 24 + i * 4, raw_id)
    cur = PAGE_SIZE
    for tup_bytes in tuples:
        cur -= len(tup_bytes)
        page[cur : cur + len(tup_bytes)] = tup_bytes
    return bytes(page)


# ======================================================================
# 构造系统目录行（通用：HEAP_HASOID + 固定长度列）
# ======================================================================

def build_sys_tuple(oid, columns_data):
    """构造一个带 HEAP_HASOID 的系统目录行。

    columns_data: [(type, value), ...]
      type: 'name'(64B C string), 'oid'(4B), 'int2'(2B), 'int4'(4B),
            'bool'(1B), 'char'(1B), 'float4'(4B)
    """
    nattrs = len(columns_data)

    # 编码每列（按 PG 对齐规则填充：与 heap_deform_tuple/att_align 语义一致）
    ALIGN = {"name": 1, "oid": 4, "int2": 2, "int4": 4,
             "bool": 1, "char": 1, "float4": 4}

    def _align(pos, ctype):
        a = ALIGN[ctype]
        return (pos + a - 1) & ~(a - 1)

    encoded = []
    pos = 0
    for ctype, val in columns_data:
        pos = _align(pos, ctype)
        if ctype == "name":
            b = val.encode("utf-8") if isinstance(val, str) else val
            raw = b + b"\x00" * (64 - len(b))
        elif ctype == "oid":
            raw = struct.pack("<I", val)
        elif ctype == "int2":
            raw = struct.pack("<h", val)
        elif ctype == "int4":
            raw = struct.pack("<i", val)
        elif ctype == "bool":
            raw = bytes([1 if val else 0])
        elif ctype == "char":
            raw = bytes([ord(val) if isinstance(val, str) else val])
        elif ctype == "float4":
            raw = struct.pack("<f", val)
        else:
            raise ValueError(f"unknown type: {ctype}")
        # 对齐填充字节（0x00）
        if pos > len(b"".join(encoded)):
            pad = pos - len(b"".join(encoded))
            encoded.append(b"\x00" * pad)
        encoded.append(raw)
        pos += len(raw)

    # PG12+ 目录存储：OID 是数据区第一个属性（属性 0），
    # t_infomask2 的 nattrs 含 oid（对照 _class_fields/_ns_fields 的 _with_oid 布局）
    data = struct.pack("<I", oid) + b"".join(encoded)
    nattrs_all = nattrs + 1

    # 布局: [header 23B][OID 4B at 24-27（兼容 PG<=11 读取）][offset_table n*2B at 28+][data at t_hoff]
    off_table_start = 28
    off_table_len = nattrs_all * 2
    t_hoff = align4(off_table_start + off_table_len)

    buf = bytearray(t_hoff + len(data))

    # 行头
    struct.pack_into("<I", buf, 0, 1)       # t_xmin
    struct.pack_into("<I", buf, 4, 0)       # t_xmax
    struct.pack_into("<I", buf, 8, 0)       # t_field3
    struct.pack_into("<I", buf, 12, 0)      # t_ctid block
    struct.pack_into("<H", buf, 16, 1)      # t_ctid offset
    struct.pack_into("<H", buf, 18, nattrs_all)  # t_infomask2
    infomask = HEAP_XMAX_INVALID | HEAP_XMIN_FROZEN | HEAP_HASOID
    struct.pack_into("<H", buf, 20, infomask)
    buf[22] = t_hoff

    # OID（PG<=11 兼容槽）
    struct.pack_into("<I", buf, 24, oid)

    # 偏移表（含 oid 属性 0）
    offset = 0
    entries = [struct.pack("<I", oid)] + [b"".join(encoded)]
    # 简化：偏移表仅占位，解析器不使用
    for i in range(nattrs_all):
        struct.pack_into("<H", buf, off_table_start + i * 2, 0)

    # 数据
    buf[t_hoff:] = data

    # MAXALIGN(8) 填充：真实 PG 元组按 8 字节对齐，
    # _scan_pg_class 的 +4 步进扫描依赖元组起点对齐
    while len(buf) % 8:
        buf.append(0)

    return bytes(buf)


def build_attr_tuple(attrelid, attname, atttypid, attlen, attnum,
                     attalign="i", attstorage="p", attnotnull=True,
                     attisdropped=False):
    """构造 pg_attribute 行（PG12 定稿布局，21 固定列 + 4 空 varlena）。

    布局对照 pg2sql/catalog.py `_pg_attribute_layout(version>=12)`:
      relid(4) name(64) typid(4) stattarg(4) len(2) num(2) ndims(2)
      cacheoff(4) typmod(4) byval(1) align(1) storage(1) notnull(1)
      hasdef(1) hasmissing(1) identity(1) generated(1) isdropped(1)
      islocal(1) inhcount(2) collation(4) + 4 个可空 varlena
    """
    return build_sys_tuple(0, [
        ("oid", attrelid),        # attrelid
        ("name", attname),        # attname
        ("oid", atttypid),        # atttypid
        ("int4", -1),             # attstattarget
        ("int2", attlen),         # attlen
        ("int2", attnum),         # attnum
        ("int2", 0),              # attndims
        ("int4", -1),             # attcacheoff
        ("int4", -1),             # atttypmod
        ("char", 1),              # attbyval (bool 磁盘为 0x01)
        ("char", attalign),       # attalign
        ("char", attstorage),     # attstorage
        ("char", 1 if attnotnull else 0),   # attnotnull
        ("char", 0),              # atthasdef
        ("char", 0),              # atthasmissing
        ("char", 0),              # attidentity
        ("char", 0),              # attgenerated
        ("char", 1 if attisdropped else 0),  # attisdropped
        ("char", 1),              # attislocal
        ("int2", 0),              # attinhcount
        ("oid", 0),               # attcollation
    ])


# ======================================================================
# 构造用户表行（int4 + text，复用已有的逻辑）
# ======================================================================

def build_user_tuple(id_val, text_val):
    """构造 users 表的行: id int4, name text (无 HEAP_HASOID)"""
    text_bytes = text_val.encode("utf-8")
    total_size = 1 + len(text_bytes)
    if total_size < 128:
        header = (total_size << 1) | 1
        text_varlena = bytes([header]) + text_bytes
    else:
        total_size = 4 + len(text_bytes)
        hdr = (0x80000000 | total_size).to_bytes(4, "big")
        text_varlena = hdr + text_bytes

    data = struct.pack("<i", id_val) + text_varlena
    t_hoff = 28
    buf = bytearray(t_hoff + len(data))

    struct.pack_into("<I", buf, 0, 1)       # t_xmin
    struct.pack_into("<I", buf, 4, 0)       # t_xmax
    struct.pack_into("<I", buf, 8, 0)       # t_field3
    struct.pack_into("<I", buf, 12, 0)      # t_ctid block
    struct.pack_into("<H", buf, 16, 1)      # t_ctid offset
    struct.pack_into("<H", buf, 18, 2)      # t_infomask2 = 2
    struct.pack_into("<H", buf, 20, HEAP_XMAX_INVALID | HEAP_XMIN_FROZEN)
    buf[22] = t_hoff
    struct.pack_into("<HH", buf, 24, 0, 4)  # 偏移表
    buf[28:] = data
    return bytes(buf)


# ======================================================================
# 主测试流程
# ======================================================================

def main():
    test_dir = os.path.join(PROJECT, ".temp", "autodiscover_test")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    db_dir = os.path.join(test_dir, "base", "16384")
    os.makedirs(db_dir)
    # PG_VERSION 位于数据目录根（真实布局），探测依赖它选择 PG12 布局
    with open(os.path.join(test_dir, "PG_VERSION"), "w") as f:
        f.write("12\n")

    # 1. 构造 pg_namespace (2615)
    #    列: nspname name(64), nspowner oid, nspacl oid[]
    ns_tuples = [
        build_sys_tuple(11, [("name", "pg_catalog"), ("oid", 10), ("oid", 0)]),
        build_sys_tuple(2200, [("name", "public"), ("oid", 10), ("oid", 0)]),
    ]
    with open(os.path.join(db_dir, "2615"), "wb") as f:
        f.write(build_page_with_tuples(ns_tuples))

    # 2. 构造 pg_class (1259)
    #    简化 14 列:
    #    relname name, relnamespace oid, reltype oid, reloftype oid,
    #    relowner oid, relam oid, relfilenode oid, reltablespace oid,
    #    relpages int4, reltuples float4, relallvisible int4,
    #    reltoastrelid oid, relhasindex bool, relkind char
    pg_class_tuples = [
        build_sys_tuple(1259, [
            ("name", "pg_class"), ("oid", 11), ("oid", 71), ("oid", 0),
            ("oid", 10), ("oid", 0), ("oid", 1259), ("oid", 0),
            ("int4", 1), ("float4", 5.0), ("int4", 0),
            ("oid", 0), ("bool", True), ("char", "r"),
        ]),
        build_sys_tuple(1249, [
            ("name", "pg_attribute"), ("oid", 11), ("oid", 75), ("oid", 0),
            ("oid", 10), ("oid", 0), ("oid", 1249), ("oid", 0),
            ("int4", 1), ("float4", 10.0), ("int4", 0),
            ("oid", 0), ("bool", True), ("char", "r"),
        ]),
        build_sys_tuple(16384, [
            ("name", "users"), ("oid", 2200), ("oid", 16385), ("oid", 0),
            ("oid", 10), ("oid", 0), ("oid", 16384), ("oid", 0),
            ("int4", 1), ("float4", 5.0), ("int4", 0),
            ("oid", 0), ("bool", False), ("char", "r"),
        ]),
    ]
    with open(os.path.join(db_dir, "1259"), "wb") as f:
        f.write(build_page_with_tuples(pg_class_tuples))

    # 3. 构造 pg_attribute (1249)
    #    定稿 PG12 布局（见 build_attr_tuple）
    pg_attr_tuples = []
    # users 表 (attrelid=16384) 的两列
    for attnum, (attname, typoid, attlen, notnull) in enumerate([
        ("id",   23, 4, True),   # int4
        ("name", 25, -1, False),  # text
    ], start=1):
        pg_attr_tuples.append(build_attr_tuple(16384, attname, typoid, attlen,
                                               attnum, attnotnull=notnull))
    # pg_class 自身的几列 (attrelid=1259)
    for attnum, (attname, typoid, attlen) in enumerate([
        ("relname", 19, 64),
        ("relnamespace", 26, 4),
        ("reltype", 26, 4),
    ], start=1):
        pg_attr_tuples.append(build_attr_tuple(1259, attname, typoid, attlen,
                                               attnum))

    with open(os.path.join(db_dir, "1249"), "wb") as f:
        f.write(build_page_with_tuples(pg_attr_tuples))

    # 4. 构造用户表数据文件 (16384=users)
    user_tuples = [
        build_user_tuple(1, "alice"),
        build_user_tuple(2, "bob"),
        build_user_tuple(3, "中文测试"),
    ]
    with open(os.path.join(db_dir, "16384"), "wb") as f:
        f.write(build_page_with_tuples(user_tuples))

    data_file = os.path.join(db_dir, "16384")
    print(f"模拟数据库目录: {db_dir}")
    print(f"用户表数据文件: {data_file}")
    print()

    # 5. 测试: 不带 --catalog-json，直接解析
    print("=" * 60)
    print("测试 1: python3 main.py <data_file> --ddl --sql --verbose")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --ddl --sql --verbose 2>&1")

    print()
    print("=" * 60)
    print("测试 2: python3 main.py <data_file> --data")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --data 2>&1")

    print()
    print("=" * 60)
    print("测试 3: python3 main.py <db_dir> --list-tables-db --verbose")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{db_dir}' --list-tables-db --verbose 2>&1")


if __name__ == "__main__":
    main()
