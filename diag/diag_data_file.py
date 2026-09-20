#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断金仓用户表数据文件全零问题。
列出 pg_class 中所有用户表及其 relfilenode，检查对应文件是否有数据。
用法: python3 diag_data_file.py <db_dir>
示例: python3 diag_data_file.py /docker/data/kbv9r1c10mysql/base/24576
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <db_dir>")
        sys.exit(1)

    db_dir = sys.argv[1]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.catalog import _scan_pg_class, _detect_sys_file, _KNOWN_PG_CLASS_NAMES

    # 找到 pg_class
    pg_class_path = _detect_sys_file(db_dir, 1259, _KNOWN_PG_CLASS_NAMES, "pg_class")
    if not pg_class_path:
        print("未找到 pg_class 文件!")
        return

    print(f"pg_class: {pg_class_path}")
    print()

    # 扫描 pg_class 获取所有表
    entries = _scan_pg_class(pg_class_path)

    print(f"{'OID':<10} {'relname':<30} {'relfilenode':<12} {'relkind':<8} {'文件大小':<10} {'非零字节':<10} {'状态'}")
    print("-" * 100)

    user_tables = []
    for oid, relname, relns, rfn, relkind in entries:
        # 只看用户表（relkind='r' 或 relkind 为空/未知）
        filepath = os.path.join(db_dir, str(rfn))
        fsize = 0
        nonzero = 0
        status = ""

        if os.path.exists(filepath):
            fsize = os.path.getsize(filepath)
            if fsize > 0:
                with open(filepath, "rb") as f:
                    first_page = f.read(min(fsize, 8192))
                nonzero = sum(1 for b in first_page if b != 0)
                if nonzero == 0:
                    status = "⚠ 全零文件"
                else:
                    status = "有数据"
            else:
                status = "空文件"
        else:
            status = "文件不存在"

        # 只显示用户表（排除系统表）
        is_system = relname.startswith("pg_") or relname.startswith("_") or rfn < 12000
        if not is_system or relname in ("test01", "test02"):
            print(f"{oid:<10} {relname:<30} {rfn:<12} {relkind!r:<8} {fsize:<10} {nonzero:<10} {status}")
            if not is_system and nonzero > 0:
                user_tables.append((oid, relname, rfn, fsize, nonzero))

    print()
    if user_tables:
        print(f"有数据的用户表:")
        for oid, relname, rfn, fsize, nonzero in user_tables:
            print(f"  {relname}: relfilenode={rfn}, 文件={db_dir}/{rfn}, 大小={fsize}, 非零字节={nonzero}")
            print(f"    尝试: python3 pg2sql/main.py {db_dir}/{rfn} --ddl --sql --verbose")
    else:
        print("没有找到有数据的用户表!")
        print()
        print("可能的原因:")
        print("  1. 数据未落盘（在 WAL 中未 checkpoint）")
        print("  2. 表刚创建但未插入数据")
        print("  3. 数据已被 TRUNCATE 或 DROP")
        print()
        print("请在金仓库中执行以下 SQL 确认:")
        print("  SELECT count(*) FROM ray.test02;")
        print("  SELECT pg_relation_filepath('ray.test02');")
        print("  SELECT oid, relname, relfilenode FROM pg_class WHERE relname LIKE 'test%';")


if __name__ == "__main__":
    main()
