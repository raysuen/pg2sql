#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断 pg_attribute 文件的数据区布局。
用法: python3 diag_attr.py <pg_attribute_file> [target_attrelid]
"""
import sys
import os
import struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import Page, PAGE_SIZE
from pg2sql.binary import cstring


def main():
    if len(sys.argv) < 2:
        print(f"用法: python3 {sys.argv[0]} <pg_attribute_file> [target_attrelid]")
        sys.exit(1)

    path = sys.argv[1]
    target = int(sys.argv[2]) if len(sys.argv) > 2 else None

    with open(path, "rb") as f:
        data = f.read()

    filesize = len(data)
    npages = filesize // PAGE_SIZE
    print(f"文件: {path}")
    print(f"大小: {filesize} bytes, 页数: {npages}")
    print()

    # 搜索已知列名
    known_col_names = [
        b"attrelid", b"attname", b"atttypid", b"attlen", b"attnum",
        b"relname", b"relnamespace", b"nspname",
        b"id", b"name", b"oid",
    ]

    print("=== 搜索已知字符串 ===")
    for s in known_col_names:
        pos = data.find(s)
        if pos != -1:
            print(f"  {s.decode():20s} found at offset {pos}")
            # 打印附近 80 字节
            start = max(0, pos - 16)
            end = min(len(data), pos + 80)
            chunk = data[start:end]
            print(f"    hex: {' '.join(f'{b:02x}' for b in chunk[:48])}")
            print(f"    ascii: {''.join(chr(b) if 32<=b<127 else '.' for b in chunk[:48])}")
    print()

    # 逐页分析数据区
    for pageno in range(min(npages, 3)):
        raw = data[pageno * PAGE_SIZE : (pageno + 1) * PAGE_SIZE]
        page = Page(pageno, raw)
        if not page.has_valid_layout:
            print(f"=== 页 {pageno}: 解析失败: {page.error} ===")
            continue

        pd_upper = page.header.get("upper", 0)
        pd_special = page.header.get("special", PAGE_SIZE)
        print(f"=== 页 {pageno}: upper={pd_upper} special={pd_special} 数据区大小={pd_special - pd_upper} ===")

        # 在数据区中扫描
        pos = pd_upper
        found = 0
        while pos + 8 <= pd_special and found < 20:
            # 读取前 8 字节，尝试两种模式
            v0 = struct.unpack_from("<I", raw, pos)[0]
            v1 = struct.unpack_from("<I", raw, pos + 4)[0]

            # 模式 A: [OID 4B][attrelid 4B][attname 64B]
            # 此时 v0=OID, v1=attrelid
            # 模式 B: [attrelid 4B][attname 64B]
            # 此时 v0=attrelid, 接下来是 name

            # 检查模式 B: v0 是 attrelid, pos+4 开始是 name(64B)
            if 1 <= v0 <= 10000000 and pos + 68 <= pd_special:
                name_b = cstring(raw[pos + 4 : pos + 68])
                if name_b and (name_b[0].isalpha() or name_b[0] == '_'):
                    # 看起来像 pg_attribute 行
                    # 继续读 atttypid, attlen, attnum
                    atttypid = struct.unpack_from("<I", raw, pos + 68)[0] if pos + 72 <= pd_special else 0
                    attlen = struct.unpack_from("<h", raw, pos + 72)[0] if pos + 74 <= pd_special else 0
                    attnum = struct.unpack_from("<h", raw, pos + 74)[0] if pos + 76 <= pd_special else 0
                    print(f"  [B] offset={pos}: attrelid={v0} attname={name_b!r} atttypid={atttypid} attlen={attlen} attnum={attnum}")
                    if target and v0 == target:
                        print(f"    *** 匹配目标 attrelid={target}! ***")
                    found += 1
                    pos += 4
                    continue

            # 检查模式 A: v0=OID, v1=attrelid, pos+8 开始是 name(64B)
            if 1 <= v0 <= 10000000 and 1 <= v1 <= 10000000 and pos + 72 <= pd_special:
                name_a = cstring(raw[pos + 8 : pos + 72])
                if name_a and (name_a[0].isalpha() or name_a[0] == '_'):
                    atttypid = struct.unpack_from("<I", raw, pos + 72)[0] if pos + 76 <= pd_special else 0
                    attlen = struct.unpack_from("<h", raw, pos + 76)[0] if pos + 78 <= pd_special else 0
                    attnum = struct.unpack_from("<h", raw, pos + 78)[0] if pos + 80 <= pd_special else 0
                    print(f"  [A] offset={pos}: oid={v0} attrelid={v1} attname={name_a!r} atttypid={atttypid} attlen={attlen} attnum={attnum}")
                    if target and v1 == target:
                        print(f"    *** 匹配目标 attrelid={target}! ***")
                    found += 1
                    pos += 4
                    continue

            pos += 4

        if found == 0:
            # 打印数据区前 128 字节的 hex dump
            print(f"  未找到匹配，打印数据区前 128 字节:")
            dump_start = pd_upper
            for i in range(dump_start, min(dump_start + 128, pd_special), 16):
                chunk = raw[i:i+16]
                hex_str = " ".join(f"{b:02x}" for b in chunk)
                ascii_str = "".join(chr(b) if 32<=b<127 else "." for b in chunk)
                print(f"    {i:04x}: {hex_str:<48s}  {ascii_str}")
        print()

    # 额外: 搜索 target_attrelid 的字节模式
    if target:
        target_bytes = struct.pack("<I", target)
        print(f"=== 搜索 attrelid={target} (bytes: {target_bytes.hex()}) ===")
        pos = 0
        count = 0
        while count < 10:
            pos = data.find(target_bytes, pos)
            if pos == -1:
                break
            # 检查附近是否有 name 类型数据
            # 模式 B: target_bytes 在 pos, name 在 pos+4
            if pos + 68 <= len(data):
                name_b = cstring(data[pos + 4 : pos + 68])
                if name_b and name_b[0].isalpha():
                    print(f"  [B] offset={pos}: attrelid={target} attname={name_b!r}")
                    count += 1
            # 模式 A: target_bytes 在 pos+4 (attrelid), OID 在 pos
            if pos >= 4 and pos + 72 <= len(data):
                name_a = cstring(data[pos + 4 : pos + 68])  # 如果 OID 在前
                if name_a and name_a[0].isalpha():
                    pass  # 已在上面处理
            pos += 1
        if count == 0:
            print(f"  未找到 attrelid={target} 的匹配")


if __name__ == "__main__":
    main()
