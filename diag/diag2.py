#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本 v2: 全页扫描，找到真实数据的位置。
用法: python3 diag2.py <data_file>
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 2:
        print(f"用法: python3 {sys.argv[0]} <data_file>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        sys.exit(1)

    with open(path, "rb") as f:
        data = f.read()

    print(f"文件: {path}")
    print(f"大小: {len(data)} bytes")
    print()

    # 1. 在整个文件中搜索已知字符串
    known_strings = [b"template1", b"template0", b"postgres", b"kingbase",
                     b"pg_class", b"pg_type", b"pg_attribute",
                     b"public", b"pg_catalog"]
    print("=== 搜索已知字符串 ===")
    for s in known_strings:
        positions = []
        start = 0
        while True:
            pos = data.find(s, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + 1
        if positions:
            print(f"  {s.decode():20s} found at offsets: {positions}")
    print()

    # 2. 按 256 字节分块显示非零区域
    print("=== 非零数据区域 ===")
    chunk_size = 256
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i+chunk_size]
        if any(b != 0 for b in chunk):
            # 找到非零字节的范围
            first_nz = next(j for j in range(len(chunk)) if chunk[j] != 0)
            last_nz = len(chunk) - 1 - next(j for j in range(len(chunk)-1, -1, -1) if chunk[j] != 0)
            print(f"  offset {i:5d}-{i+last_nz:5d} (非零: {first_nz}-{last_nz}):")
            # 显示前 64 字节的 hex
            hex_str = " ".join(f"{b:02x}" for b in chunk[first_nz:min(first_nz+64, len(chunk))])
            print(f"    {hex_str}")
            # 尝试显示 ASCII
            ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[first_nz:first_nz+64])
            print(f"    {ascii_str}")
    print()

    # 3. 对每个找到的字符串位置，分析附近的元组结构
    print("=== 字符串位置附近的元组分析 ===")
    for s in [b"template1", b"postgres", b"kingbase"]:
        pos = data.find(s)
        if pos == -1:
            continue
        print(f"\n  字符串 '{s.decode()}' 在偏移 {pos}")

        # 在该位置之前搜索可能的元组头
        # 元组头 23 字节，数据区在 t_hoff 之后
        # name 字段是 64 字节固定长度
        # 所以元组头可能在 pos - 64 - 偏移表 - null位图 附近
        # 向前搜索最多 200 字节
        for back in range(0, 200, 1):
            tup_start = pos - back
            if tup_start < 24:
                continue
            if tup_start + 23 > len(data):
                continue
            # 检查是否像元组头
            xmin = struct.unpack_from('<I', data, tup_start)[0]
            if xmin == 0 or xmin > 100000000:
                continue
            infomask2 = struct.unpack_from('<H', data, tup_start + 18)[0]
            nattrs = infomask2 & 0x07FF
            if nattrs < 1 or nattrs > 100:
                continue
            infomask = struct.unpack_from('<H', data, tup_start + 20)[0]
            t_hoff = data[tup_start + 22]
            if t_hoff < 23 or t_hoff > 200:
                continue

            print(f"  可能的元组头在偏移 {tup_start} (字符串前 {back} 字节):")
            print(f"    xmin={xmin}, xmax={struct.unpack_from('<I', data, tup_start+4)[0]}")
            print(f"    infomask2=0x{infomask2:04x} (nattrs={nattrs})")
            print(f"    infomask=0x{infomask:04x}")
            print(f"      XMIN_COMMITTED={bool(infomask & 0x0100)}")
            print(f"      XMIN_INVALID={bool(infomask & 0x0200)}")
            print(f"      XMIN_FROZEN={(infomask & 0x0300) == 0x0300}")
            print(f"      XMAX_INVALID={bool(infomask & 0x0800)}")
            print(f"      HASOID={bool(infomask & 0x0008)}")
            print(f"    t_hoff={t_hoff}")

            # 检查 OID
            has_oid = bool(infomask & 0x0008)
            has_null = bool(infomask & 0x0001)
            null_len = (nattrs + 7) // 8 if has_null else 0
            oid_off = tup_start + ((23 + null_len + 3) & ~3)
            if has_oid and oid_off + 4 <= len(data):
                oid_val = struct.unpack_from('<I', data, oid_off)[0]
                print(f"    OID (at offset {oid_off - tup_start}): {oid_val}")

            # 显示偏移表
            off_table_start = tup_start + ((23 + null_len + (4 if has_oid else 0) + 3) & ~3)
            print(f"    偏移表 (从相对偏移 {off_table_start - tup_start} 开始):")
            for j in range(min(nattrs, 5)):
                off_val = struct.unpack_from('<H', data, off_table_start + j * 2)[0]
                print(f"      off[{j}] = {off_val}")

            # 显示前几个字节的数据区
            data_start = tup_start + t_hoff
            if data_start + 64 <= len(data):
                print(f"    数据区 (从相对偏移 {t_hoff} 开始):")
                print(f"      {' '.join(f'{b:02x}' for b in data[data_start:data_start+32])}")
                print(f"      {''.join(chr(b) if 32<=b<127 else '.' for b in data[data_start:data_start+32])}")

            break  # 找到一个合理的就够了

    # 4. 额外: 显示文件开头 512 字节的完整 hex dump
    print("\n=== 前 512 字节 hex dump ===")
    for i in range(0, min(512, len(data)), 16):
        chunk = data[i:i+16]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {i:04x}: {hex_str:<48s}  {ascii_str}")

    # 5. 检查页头偏移的可能变体
    print("\n=== 页头偏移探测 ===")
    # 尝试在不同偏移处找 pd_pagesize_version
    for off in range(0, 40, 2):
        val = struct.unpack_from('<H', data, off)[0]
        if val != 0 and (val & ~0x07) == 8192:
            version = val & 0x07
            print(f"  offset {off}: pd_pagesize_version=0x{val:04x} (size={val & ~0x07}, version={version})")
            # 尝试用这个偏移推断页头布局
            # pd_pagesize_version 通常在页头末尾
            # 标准布局: offset 18 (含checksum) 或 16 (不含)
            if off == 18:
                print("    → 标准布局 (含 checksum)")
            elif off == 16:
                print("    → 旧布局 (不含 checksum)")
            else:
                print(f"    → 非标准布局! pagesize_version 在偏移 {off}")

    # 也检查是否有其他页大小
    for off in range(0, 40, 2):
        val = struct.unpack_from('<H', data, off)[0]
        if val != 0 and (val & ~0x07) in [4096, 16384, 32768]:
            print(f"  offset {off}: 可能的 pagesize_version=0x{val:04x} (size={val & ~0x07}, version={val & 0x07})")


if __name__ == "__main__":
    main()
