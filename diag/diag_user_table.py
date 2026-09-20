#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断金仓用户表数据文件的页头布局。
用法: python3 diag_user_table.py <data_file>
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <data_file>")
        sys.exit(1)
    
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        sys.exit(1)
    
    filesize = os.path.getsize(path)
    print(f"文件: {path}")
    print(f"文件大小: {filesize} 字节 ({filesize // 8192} 页 @ 8192, {filesize // 16384} 页 @ 16384)")
    print()
    
    with open(path, "rb") as f:
        raw = f.read(min(filesize, 8192))
    
    print(f"前 64 字节 hex dump:")
    for i in range(0, min(64, len(raw)), 16):
        hex_part = " ".join(f"{b:02x}" for b in raw[i:i+16])
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in raw[i:i+16])
        print(f"  {i:04x}: {hex_part:<48s}  {ascii_part}")
    
    print()
    
    # 检查标准 PG 页头 (version 4, psv at offset 18)
    print("=== 标准 PG 布局 (psv @ offset 18) ===")
    if len(raw) >= 20:
        psv_18 = struct.unpack_from("<H", raw, 18)[0]
        version_18 = psv_18 & 0x7F
        size_18 = psv_18 & ~0x7F
        print(f"  psv=0x{psv_18:04x}, version={version_18}, size={size_18}")
        if version_18 == 4 and size_18 == 8192:
            pd_lower = struct.unpack_from("<H", raw, 12)[0]
            pd_upper = struct.unpack_from("<H", raw, 14)[0]
            pd_special = struct.unpack_from("<H", raw, 16)[0]
            print(f"  pd_lower={pd_lower}, pd_upper={pd_upper}, pd_special={pd_special}")
            print(f"  合法: {24 <= pd_lower <= pd_upper <= pd_special <= 8192}")
    
    # 扫描前 64 字节找 psv
    print()
    print("=== 自动探测扫描 (offset 8~62) ===")
    for off in range(8, 64, 2):
        if off + 2 > len(raw):
            break
        psv = struct.unpack_from("<H", raw, off)[0]
        version = psv & 0x7F
        size = psv & ~0x7F
        if version in (4, 5) and size == 8192:
            print(f"  ** 找到 psv @ offset {off}: version={version}, size={size}")
            if off >= 6:
                pd_lower = struct.unpack_from("<H", raw, off - 6)[0]
                pd_upper = struct.unpack_from("<H", raw, off - 4)[0]
                pd_special = struct.unpack_from("<H", raw, off - 2)[0]
                header_end = (off + 2 + 3) & ~3
                print(f"  pd_lower={pd_lower}, pd_upper={pd_upper}, pd_special={pd_special}")
                print(f"  header_end={header_end}")
                print(f"  合法: {header_end <= pd_lower <= pd_upper <= pd_special <= 8192}")
    
    # 也尝试 16KB 页面
    print()
    print("=== 16KB 页面检查 ===")
    if filesize >= 16384:
        with open(path, "rb") as f:
            raw16 = f.read(16384)
        for off in range(8, 128, 2):
            if off + 2 > len(raw16):
                break
            psv = struct.unpack_from("<H", raw16, off)[0]
            version = psv & 0x7F
            size = psv & ~0x7F
            if version in (4, 5) and size == 16384:
                print(f"  ** 找到 psv @ offset {off}: version={version}, size=16384")
                if off >= 6:
                    pd_lower = struct.unpack_from("<H", raw16, off - 6)[0]
                    pd_upper = struct.unpack_from("<H", raw16, off - 4)[0]
                    pd_special = struct.unpack_from("<H", raw16, off - 2)[0]
                    print(f"  pd_lower={pd_lower}, pd_upper={pd_upper}, pd_special={pd_special}")
    
    # 检查数据区内容（尝试找到元组头特征）
    print()
    print("=== 数据区扫描 (寻找元组头) ===")
    # 在整个页面中搜索可能的元组头
    # 元组头特征: xmin(4B) 合理值, infomask2(2B) 低11位=列数, infomask(2B) 高字节有标志
    found = 0
    for pos in range(24, len(raw) - 23, 4):
        xmin = struct.unpack_from("<I", raw, pos)[0]
        if xmin == 0 or xmin > 0x7FFFFFFF:
            continue
        infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
        infomask = struct.unpack_from("<H", raw, pos + 20)[0]
        t_hoff = raw[pos + 22] if pos + 22 < len(raw) else 0
        nattrs = infomask2 & 0x07FF
        if 1 <= nattrs <= 100 and 23 <= t_hoff <= 256 and (infomask & 0xFF00) != 0:
            print(f"  pos={pos}: xmin={xmin}, nattrs={nattrs}, infomask=0x{infomask:04x}, t_hoff={t_hoff}")
            # 打印后续字节
            end = min(pos + t_hoff + 32, len(raw))
            hex_dump = " ".join(f"{b:02x}" for b in raw[pos:end])
            print(f"    data: {hex_dump}")
            found += 1
            if found >= 5:
                break
    
    if not found:
        print("  未找到任何匹配的元组头特征")
        # 尝试更宽松的搜索
        print()
        print("=== 宽松搜索 (仅看 xmin 合理值) ===")
        for pos in range(0, len(raw) - 23, 4):
            xmin = struct.unpack_from("<I", raw, pos)[0]
            if 100 <= xmin <= 10000000:
                infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
                nattrs = infomask2 & 0x07FF
                if 1 <= nattrs <= 100:
                    infomask = struct.unpack_from("<H", raw, pos + 20)[0]
                    t_hoff = raw[pos + 22]
                    print(f"  pos={pos}: xmin={xmin}, nattrs={nattrs}, infomask=0x{infomask:04x}, t_hoff={t_hoff}")
                    found += 1
                    if found >= 3:
                        break

    # 也检查 pg2sql Page 解析结果
    print()
    print("=== pg2sql Page 解析 ===")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    try:
        from pg2sql.page import Page, PAGE_SIZE
        page = Page(0, raw[:PAGE_SIZE])
        print(f"  has_valid_layout: {page.has_valid_layout}")
        print(f"  error: {page.error}")
        print(f"  header: {page.header}")
        print(f"  header_size: {page.header_size}")
        print(f"  items: {len(page.items)}")
    except Exception as e:
        print(f"  解析异常: {e}")


if __name__ == "__main__":
    main()
