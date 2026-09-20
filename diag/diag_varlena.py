#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断多列长文本 varlena 编码问题。
用法: python3 diag/diag_varlena.py <data_file> <n_cols>
"""
import sys
import os
import struct

def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <data_file> <n_cols>")
        sys.exit(1)

    path = sys.argv[1]
    n_cols = int(sys.argv[2])

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.tuple import HeapTuple, HEAP_TUPLE_HEADER_SIZE, HEAP_NATTS_MASK

    with open(path, "rb") as f:
        raw = f.read(PAGE_SIZE)

    page = Page(0, raw)
    if not page.has_valid_layout:
        print(f"页面解析失败: {page.error}")
        return

    pd_upper = page.header.get("upper", 0)
    pd_special = page.header.get("special", PAGE_SIZE)
    print(f"页面: pd_upper={pd_upper}, pd_special={pd_special}, items={len(page.items)}")

    # 扫描模式找元组
    pos = pd_upper
    found = 0
    while pos + HEAP_TUPLE_HEADER_SIZE <= pd_special and found < 2:
        t_xmin = struct.unpack_from("<I", raw, pos)[0]
        t_infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
        t_infomask = struct.unpack_from("<H", raw, pos + 20)[0]
        t_hoff = raw[pos + 22]
        nattrs = t_infomask2 & HEAP_NATTS_MASK

        if t_hoff < 23 or t_hoff > 256 or nattrs != n_cols or t_xmin == 0 or (t_infomask & 0xFF00) == 0:
            pos += 4
            continue

        found += 1
        print(f"\n=== 元组 {found} @ pos={pos} ===")
        print(f"xmin={t_xmin}, nattrs={nattrs}, infomask=0x{t_infomask:04x}, t_hoff={t_hoff}")

        # 逐字节分析数据区开头的 varlena 头
        print(f"\n数据区逐 varlena 分析（前 10 列）:")
        dpos = pos + t_hoff
        for i in range(min(10, n_cols)):
            if dpos >= pd_special:
                print(f"  col{i}: 超出数据区")
                break
            first = raw[dpos]
            if first & 0x80:
                # 4B 头
                total = struct.unpack_from(">I", raw, dpos)[0] & 0x3FFFFFFF
                print(f"  col{i} @+{dpos-(pos+t_hoff)}: 4B头 first=0x{first:02x} total={total} data_len={total-4}")
                if 0 < total < 10000 and dpos + total <= pd_special:
                    data = raw[dpos+4:dpos+total]
                    preview = data[:20].decode("utf-8", errors="replace")
                    print(f"         预览: {preview}... (len={len(data)})")
                dpos += total if 0 < total < 10000 else 1
            else:
                # 1B 头: VARSIZE = first >> 1, data_len = VARSIZE - 1
                total = (first >> 1) & 0x7F
                data_len = max(total - 1, 0)
                print(f"  col{i} @+{dpos-(pos+t_hoff)}: 1B头 first=0x{first:02x} VARSIZE={total} data_len={data_len}")
                if data_len > 0 and dpos + 1 + data_len <= pd_special:
                    data = raw[dpos+1:dpos+1+data_len]
                    preview = data[:20].decode("utf-8", errors="replace")
                    print(f"         预览: {preview}... (len={len(data)})")
                dpos += total if total > 0 else 1

        # 也看原始字节
        print(f"\n数据区前 128 字节:")
        dump_start = pos + t_hoff
        for i in range(0, min(128, pd_special - dump_start), 16):
            chunk = raw[dump_start+i:dump_start+i+16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  +{i:3d}: {hex_part:<48s}  {ascii_part}")

        # 元组跳过（保守）
        pos += 8

    if found == 0:
        print("未找到匹配元组")


if __name__ == "__main__":
    main()
