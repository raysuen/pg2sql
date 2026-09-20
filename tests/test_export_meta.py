#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 --export-meta 功能:
1. 复用 test_auto_discover 构造的模拟数据库目录
2. 用 --export-meta 离线生成 meta.json
3. 用 --catalog-json 加载该 JSON 验证数据解析
4. 对比直接自动发现和经 JSON 中转两种路径的输出是否一致
"""
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# 复用 test_auto_discover 的构造逻辑
from tests.test_auto_discover import (
    build_page_with_tuples, build_sys_tuple, build_user_tuple,
    build_attr_tuple,
)

import struct
import shutil


def main():
    test_dir = os.path.join(PROJECT, ".temp", "export_meta_test")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    db_dir = os.path.join(test_dir, "base", "16384")
    os.makedirs(db_dir)
    # PG_VERSION 位于数据目录根（真实布局），探测依赖它选择 PG12 布局
    with open(os.path.join(test_dir, "PG_VERSION"), "w") as f:
        f.write("12\n")

    # 1. 构造 pg_namespace (2615)
    ns_tuples = [
        build_sys_tuple(11, [("name", "pg_catalog"), ("oid", 10), ("oid", 0)]),
        build_sys_tuple(2200, [("name", "public"), ("oid", 10), ("oid", 0)]),
    ]
    with open(os.path.join(db_dir, "2615"), "wb") as f:
        f.write(build_page_with_tuples(ns_tuples))

    # 2. 构造 pg_class (1259) - 14列
    pg_class_tuples = [
        build_sys_tuple(1259, [
            ("name", "pg_class"), ("oid", 11), ("oid", 71), ("oid", 0),
            ("oid", 10), ("oid", 0), ("oid", 1259), ("oid", 0),
            ("int4", 1), ("float4", 3.0), ("int4", 0),
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
        build_sys_tuple(16387, [
            ("name", "orders"), ("oid", 2200), ("oid", 16388), ("oid", 0),
            ("oid", 10), ("oid", 0), ("oid", 16387), ("oid", 0),
            ("int4", 1), ("float4", 0.0), ("int4", 0),
            ("oid", 0), ("bool", False), ("char", "r"),
        ]),
    ]
    with open(os.path.join(db_dir, "1259"), "wb") as f:
        f.write(build_page_with_tuples(pg_class_tuples))

    # 3. 构造 pg_attribute (1249) - 定稿 PG12 布局
    pg_attr_tuples = []
    # users 表: id int4, name text
    for attnum, (attname, typoid, attlen, notnull) in enumerate([
        ("id",   23, 4, True),
        ("name", 25, -1, False),
    ], start=1):
        pg_attr_tuples.append(build_attr_tuple(16384, attname, typoid, attlen,
                                               attnum, attnotnull=notnull))
    # orders 表: order_id int4, amount numeric
    for attnum, (attname, typoid, attlen, notnull) in enumerate([
        ("order_id", 23, 4, True),
        ("amount",   1700, -1, False),
    ], start=1):
        pg_attr_tuples.append(build_attr_tuple(16387, attname, typoid, attlen,
                                               attnum, attnotnull=notnull))
    # pg_class 自身几列
    for attnum, (attname, typoid, attlen) in enumerate([
        ("relname", 19, 64), ("relnamespace", 26, 4), ("reltype", 26, 4),
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
    meta_output = os.path.join(test_dir, "meta_exported.json")

    print(f"模拟数据库目录: {db_dir}")
    print(f"用户表数据文件: {data_file}")
    print(f"导出 JSON 目标: {meta_output}")
    print()

    # 5. 测试 --export-meta 导出 JSON
    print("=" * 60)
    print("步骤 1: python3 main.py <db_dir> --export-meta -o <json> --verbose")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{db_dir}' --export-meta -o '{meta_output}' --verbose 2>&1")

    print()

    # 6. 查看导出的 JSON 内容
    print("=" * 60)
    print("步骤 2: 查看导出的 JSON 内容")
    print("=" * 60)
    with open(meta_output, "r") as f:
        content = f.read()
        # 只打印前 40 行预览
        lines = content.split("\n")
        print("\n".join(lines[:40]))
        if len(lines) > 40:
            print(f"... (共 {len(lines)} 行)")

    print()

    # 7. 用导出的 JSON 作为 --catalog-json 解析数据
    print("=" * 60)
    print("步骤 3: python3 main.py <data_file> --catalog-json <json> --ddl --sql")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --catalog-json '{meta_output}' --table-name users --ddl --sql --verbose 2>&1")

    print()

    # 8. 对比: 直接自动发现（不带 --catalog-json）
    print("=" * 60)
    print("步骤 4: 对比 - 直接自动发现 (无 --catalog-json)")
    print("=" * 60)
    os.system(f"cd '{PROJECT}' && python3 main.py '{data_file}' --ddl --sql --verbose 2>&1")


if __name__ == "__main__":
    main()
