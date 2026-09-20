#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断金仓 pg_attribute 的列布局，验证 atttypmod 偏移是否正确。
用法: python3 diag_attribute.py <db_dir> <table_oid>
示例: python3 diag_attribute.py /docker/data/kbv9r1c10mysql/base/24576 40974
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <db_dir> <table_oid>")
        print(f"示例: {sys.argv[0]} /docker/data/kbv9r1c10mysql/base/24576 40974")
        sys.exit(1)
    
    db_dir = sys.argv[1]
    target_oid = int(sys.argv[2])
    
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.binary import cstring
    
    # 找到 pg_attribute 文件
    pg_attr_path = os.path.join(db_dir, "1249")
    if not os.path.exists(pg_attr_path):
        # 自动探测
        for name in sorted(os.listdir(db_dir)):
            if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
                continue
            if not name.isdigit():
                continue
            fpath = os.path.join(db_dir, name)
            if os.path.getsize(fpath) < 8192:
                continue
            # 检查是否像 pg_attribute
            with open(fpath, "rb") as f:
                raw = f.read(PAGE_SIZE)
            page = Page(0, raw)
            if not page.has_valid_layout:
                continue
            for item in page.items:
                if item.flags != 1:
                    continue
                data = raw[item.off:item.off + item.len]
                if len(data) < 8:
                    continue
                # pg_attribute 第一列是 attrelid (oid), 第二列是 attname (name)
                attrelid = struct.unpack_from("<I", data, 0)[0]
                if attrelid > 0:
                    # 检查第二列是否像列名
                    name_bytes = data[4:68]
                    nm = cstring(name_bytes)
                    if nm and (nm[0].isalpha() or nm[0] == '_'):
                        pg_attr_path = fpath
                        print(f"探测到 pg_attribute: {fpath}")
                        break
            if pg_attr_path != os.path.join(db_dir, "1249"):
                break
    
    print(f"pg_attribute 文件: {pg_attr_path}")
    print(f"目标表 OID: {target_oid}")
    print()
    
    # 也尝试数据区扫描模式
    from pg2sql.catalog import _scan_data_region
    
    print("=== 数据区扫描模式 ===")
    found = 0
    for pageno, pd_upper, pd_special, raw in _scan_data_region(pg_attr_path):
        pos = pd_upper
        while pos + 80 <= pd_special:
            # 模式 A: [OID 4B][attrelid 4B][attname 64B][atttypid 4B][attcollation 4B][attlen 2B][attnum 2B]...
            attrelid_a = struct.unpack_from("<I", raw, pos + 4)[0]
            name_a = cstring(raw[pos + 8 : pos + 72])
            
            # 模式 B: [attrelid 4B][attname 64B][atttypid 4B][attcollation 4B][attlen 2B][attnum 2B]...
            attrelid_b = struct.unpack_from("<I", raw, pos)[0]
            name_b = cstring(raw[pos + 4 : pos + 68])
            
            matched = False
            
            # 模式 A
            if attrelid_a == target_oid and name_a and len(name_a) > 0:
                first = name_a[0]
                if (first.isalpha() or first == '_') and pos + 92 <= pd_special:
                    atttypid = struct.unpack_from("<I", raw, pos + 72)[0]
                    attcollation = struct.unpack_from("<I", raw, pos + 76)[0]
                    attlen = struct.unpack_from("<h", raw, pos + 80)[0]
                    attnum = struct.unpack_from("<h", raw, pos + 82)[0]
                    attcacheoff = struct.unpack_from("<i", raw, pos + 84)[0]
                    atttypmod = struct.unpack_from("<i", raw, pos + 88)[0]
                    
                    if 1 <= attnum <= 1000:
                        print(f"  模式A pos={pos}: attnum={attnum} name={name_a} typid={atttypid} coll={attcollation} len={attlen} cacheoff={attcacheoff} typmod={atttypmod}")
                        if atttypmod > 0:
                            print(f"    → typmod={atttypmod}, varchar长度={atttypmod-4 if atttypmod >= 4 else '?'}")
                        found += 1
                        matched = True
            
            # 模式 B
            if not matched and attrelid_b == target_oid and name_b and len(name_b) > 0:
                first = name_b[0]
                if (first.isalpha() or first == '_') and pos + 88 <= pd_special:
                    atttypid = struct.unpack_from("<I", raw, pos + 68)[0]
                    attcollation = struct.unpack_from("<I", raw, pos + 72)[0]
                    attlen = struct.unpack_from("<h", raw, pos + 76)[0]
                    attnum = struct.unpack_from("<h", raw, pos + 78)[0]
                    attcacheoff = struct.unpack_from("<i", raw, pos + 80)[0]
                    atttypmod = struct.unpack_from("<i", raw, pos + 84)[0]
                    
                    if 1 <= attnum <= 1000:
                        print(f"  模式B pos={pos}: attnum={attnum} name={name_b} typid={atttypid} coll={attcollation} len={attlen} cacheoff={attcacheoff} typmod={atttypmod}")
                        if atttypmod > 0:
                            print(f"    → typmod={atttypmod}, varchar长度={atttypmod-4 if atttypmod >= 4 else '?'}")
                        found += 1
                        matched = True
            
            pos += 4
        if found >= 10:
            break
    
    if found == 0:
        print("  未找到匹配的列定义")
    
    # 也尝试 dump 原始字节来人工检查偏移
    print()
    print("=== 原始字节 dump (第一个匹配行) ===")
    for pageno, pd_upper, pd_special, raw in _scan_data_region(pg_attr_path):
        pos = pd_upper
        while pos + 80 <= pd_special:
            attrelid_b = struct.unpack_from("<I", raw, pos)[0]
            if attrelid_b == target_oid:
                name_b = cstring(raw[pos + 4 : pos + 68])
                if name_b and (name_b[0].isalpha() or name_b[0] == '_'):
                    print(f"模式B pos={pos}, attrelid={attrelid_b}, name={name_b}")
                    # dump 100 字节
                    end = min(pos + 100, pd_special)
                    for i in range(0, end - pos, 16):
                        chunk = raw[pos+i:pos+i+16]
                        hex_part = " ".join(f"{b:02x}" for b in chunk)
                        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                        print(f"  +{i:3d}: {hex_part:<48s}  {ascii_part}")
                    
                    # 也尝试模式 A
                    attrelid_a = struct.unpack_from("<I", raw, pos + 4)[0]
                    if attrelid_a == target_oid:
                        name_a = cstring(raw[pos + 8 : pos + 72])
                        if name_a and (name_a[0].isalpha() or name_a[0] == '_'):
                            print(f"\n模式A pos={pos}, oid={struct.unpack_from('<I', raw, pos)[0]}, attrelid={attrelid_a}, name={name_a}")
                            for i in range(0, min(100, pd_special - pos), 16):
                                chunk = raw[pos+i:pos+i+16]
                                hex_part = " ".join(f"{b:02x}" for b in chunk)
                                ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                                print(f"  +{i:3d}: {hex_part:<48s}  {ascii_part}")
                    return
            pos += 4
        break
    
    print("未找到目标 OID 的行")


if __name__ == "__main__":
    main()
