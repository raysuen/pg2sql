#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断金仓 pg_attribute 列布局 + 用户表文件查找。
用法: python3 diag_v2.py <db_dir> <table_oid_or_relfilenode>
示例: python3 diag_v2.py /docker/data/kbv9r1c10mysql/base/24576 40974
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <db_dir> <table_oid>")
        sys.exit(1)

    db_dir = sys.argv[1]
    target_oid = int(sys.argv[2])

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.binary import cstring
    from pg2sql.catalog import _scan_data_region

    # ==================================================================
    # Part 1: 诊断 pg_attribute 列布局
    # ==================================================================
    print("=" * 70)
    print("Part 1: pg_attribute 列布局诊断")
    print("=" * 70)

    pg_attr_path = os.path.join(db_dir, "1249")
    if not os.path.exists(pg_attr_path):
        print("pg_attribute 文件不存在!")
        return

    # 找到 "name" 列的行并 dump 原始字节
    print(f"\n在 pg_attribute 中搜索 attrelid={target_oid} 的行...\n")

    for pageno, pd_upper, pd_special, raw in _scan_data_region(pg_attr_path):
        pos = pd_upper
        while pos + 80 <= pd_special:
            # 检查模式 B: attrelid 在 pos
            attrelid = struct.unpack_from("<I", raw, pos)[0]
            if attrelid == target_oid:
                name = cstring(raw[pos + 4 : pos + 68])
                if name and (name[0].isalpha() or name[0] == '_'):
                    print(f"找到行: pos={pos}, attrelid={attrelid}, name={name}")
                    # Dump 120 字节
                    dump_len = min(120, pd_special - pos)
                    print(f"\n原始字节 dump ({dump_len} 字节):")
                    for i in range(0, dump_len, 16):
                        chunk = raw[pos+i:pos+i+16]
                        hex_part = " ".join(f"{b:02x}" for b in chunk)
                        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                        print(f"  +{i:3d}: {hex_part:<48s}  {ascii_part}")

                    # 逐偏移解读
                    print(f"\n逐偏移解读:")
                    if pos + 68 <= pd_special:
                        atttypid = struct.unpack_from("<I", raw, pos + 68)[0]
                        print(f"  pos+68 (atttypid):    {atttypid} (oid)")
                    if pos + 72 <= pd_special:
                        val72 = struct.unpack_from("<I", raw, pos + 72)[0]
                        print(f"  pos+72 (attcollation?): {val72} (0x{val72:08x})")
                    if pos + 76 <= pd_special:
                        val76 = struct.unpack_from("<h", raw, pos + 76)[0]
                        print(f"  pos+76 (attlen?):      {val76} (int16)")
                    if pos + 78 <= pd_special:
                        val78 = struct.unpack_from("<h", raw, pos + 78)[0]
                        print(f"  pos+78 (attnum?):      {val78} (int16)")
                    # 尝试多个可能的 atttypmod 位置
                    for off in range(80, min(100, pd_special - pos), 2):
                        if pos + off + 4 <= pd_special:
                            val = struct.unpack_from("<i", raw, pos + off)[0]
                            if val > 0 and val < 10000:
                                print(f"  pos+{off} (???):          {val} ← 可能是 typmod!")

                    # 搜索 typmod=24 (varchar(20) → typmod=20+4=24)
                    print(f"\n在 80~120 字节范围内搜索 typmod=24:")
                    for off in range(80, min(120, pd_special - pos)):
                        if pos + off + 4 <= pd_special:
                            val = struct.unpack_from("<i", raw, pos + off)[0]
                            if val == 24:
                                print(f"  ★ 找到 typmod=24 在 pos+{off}")
                                # 反推布局
                                print(f"    attcacheoff 在 pos+{off-4} 到 pos+{off-1}")
                                # 确认
                                if pos + off - 4 >= pos + 80:
                                    cacheoff = struct.unpack_from("<i", raw, pos + off - 4)[0]
                                    print(f"    attcacheoff={cacheoff}")
                    print()

            # 检查模式 A: attrelid 在 pos+4, OID 在 pos
            oid_val = struct.unpack_from("<I", raw, pos)[0]
            attrelid_a = struct.unpack_from("<I", raw, pos + 4)[0]
            if attrelid_a == target_oid and oid_val > 0 and oid_val < 10000000:
                name_a = cstring(raw[pos + 8 : pos + 72])
                if name_a and (name_a[0].isalpha() or name_a[0] == '_'):
                    print(f"找到行(模式A): pos={pos}, OID={oid_val}, attrelid={attrelid_a}, name={name_a}")
                    dump_len = min(120, pd_special - pos)
                    print(f"\n原始字节 dump ({dump_len} 字节):")
                    for i in range(0, dump_len, 16):
                        chunk = raw[pos+i:pos+i+16]
                        hex_part = " ".join(f"{b:02x}" for b in chunk)
                        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                        print(f"  +{i:3d}: {hex_part:<48s}  {ascii_part}")

                    print(f"\n逐偏移解读:")
                    if pos + 72 <= pd_special:
                        atttypid = struct.unpack_from("<I", raw, pos + 72)[0]
                        print(f"  pos+72 (atttypid):    {atttypid} (oid)")
                    if pos + 76 <= pd_special:
                        val76 = struct.unpack_from("<I", raw, pos + 76)[0]
                        print(f"  pos+76 (attcollation?): {val76} (0x{val76:08x})")
                    if pos + 80 <= pd_special:
                        val80 = struct.unpack_from("<h", raw, pos + 80)[0]
                        print(f"  pos+80 (attlen?):      {val80} (int16)")
                    if pos + 82 <= pd_special:
                        val82 = struct.unpack_from("<h", raw, pos + 82)[0]
                        print(f"  pos+82 (attnum?):      {val82} (int16)")

                    print(f"\n搜索 typmod=24:")
                    for off in range(84, min(120, pd_special - pos)):
                        if pos + off + 4 <= pd_special:
                            val = struct.unpack_from("<i", raw, pos + off)[0]
                            if val == 24:
                                print(f"  ★ 找到 typmod=24 在 pos+{off}")
                                if pos + off - 4 >= pos + 84:
                                    cacheoff = struct.unpack_from("<i", raw, pos + off - 4)[0]
                                    print(f"    attcacheoff={cacheoff} (pos+{off-4})")
                    print()

            pos += 4

    # ==================================================================
    # Part 2: 检查用户表数据文件
    # ==================================================================
    print("=" * 70)
    print("Part 2: 用户表数据文件检查")
    print("=" * 70)

    target_file = os.path.join(db_dir, str(target_oid))
    print(f"\n目标文件: {target_file}")

    if os.path.exists(target_file):
        fsize = os.path.getsize(target_file)
        print(f"文件大小: {fsize} 字节")

        with open(target_file, "rb") as f:
            raw = f.read(min(fsize, 8192))

        # 检查是否全零
        nonzero = sum(1 for b in raw if b != 0)
        print(f"非零字节数: {nonzero} / {len(raw)}")

        if nonzero == 0:
            print("⚠ 文件全为零！该 relfilenode 可能不是实际数据文件。")
    else:
        print("文件不存在!")

    # 列出数据库目录下所有非零文件
    print(f"\n数据库目录 {db_dir} 下非零数据文件:")
    print(f"{'relfilenode':<14} {'大小':<12} {'非零字节':<10} {'前4字节(hex)'}")
    print("-" * 60)

    for name in sorted(os.listdir(db_dir)):
        if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
            continue
        if not name.isdigit():
            continue
        fpath = os.path.join(db_dir, name)
        if not os.path.isfile(fpath):
            continue
        fsize = os.path.getsize(fpath)
        if fsize == 0:
            continue

        with open(fpath, "rb") as f:
            first_page = f.read(min(fsize, 8192))
        nonzero = sum(1 for b in first_page if b != 0)
        first4 = first_page[:4].hex() if len(first_page) >= 4 else ""
        print(f"{name:<14} {fsize:<12} {nonzero:<10} {first4}")

    # 特别检查: 在 pg_class 中 test02 的实际 relfilenode
    print(f"\n在 pg_class 中检查 test02 的 relfilenode:")
    pg_class_path = os.path.join(db_dir, "1259")
    if not os.path.exists(pg_class_path):
        # 自动探测
        from pg2sql.catalog import _scan_pg_class, _detect_sys_file
        _KNOWN = {"pg_class", "pg_attribute", "pg_type", "pg_namespace", "pg_proc"}
        pg_class_path = _detect_sys_file(db_dir, 1259, _KNOWN, "pg_class")
        if pg_class_path:
            print(f"  探测到 pg_class: {pg_class_path}")

    if pg_class_path and os.path.exists(pg_class_path):
        from pg2sql.catalog import _scan_pg_class
        entries = _scan_pg_class(pg_class_path)
        for oid, relname, relns, rfn, relkind in entries:
            if relname == "test02" or rfn == target_oid or oid == target_oid:
                print(f"  OID={oid} relname={relname} relfilenode={rfn} relkind={relkind}")
                print(f"  → 实际数据文件应为: {os.path.join(db_dir, str(rfn))}")
                if rfn != target_oid:
                    print(f"  ⚠ relfilenode({rfn}) != 查找的 OID({target_oid})!")
                    # 检查实际文件
                    actual_file = os.path.join(db_dir, str(rfn))
                    if os.path.exists(actual_file):
                        actual_size = os.path.getsize(actual_file)
                        with open(actual_file, "rb") as f:
                            actual_raw = f.read(min(actual_size, 8192))
                        actual_nonzero = sum(1 for b in actual_raw if b != 0)
                        print(f"  实际文件大小={actual_size}, 非零字节={actual_nonzero}")


if __name__ == "__main__":
    main()
