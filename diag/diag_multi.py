#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 51 列表：先 dump 页面头部和 items，再逐 item 分析"""
import sys, os, struct

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <data_file>")
        sys.exit(1)
    path = sys.argv[1]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.tuple import HeapTuple

    with open(path, "rb") as f:
        raw = f.read(PAGE_SIZE)

    page = Page(0, raw)
    if not page.has_valid_layout:
        print(f"页面解析失败: {page.error}")
        return

    print(f"页面: header_size={page.header_size}, pd_lower={page.header['lower']}, pd_upper={page.header['upper']}, pd_special={page.header['special']}")
    print(f"ItemId 数量: {len(page.items)}")
    for item in page.items:
        print(f"  item: index={item.index} off={item.off} len={item.len} flags={item.flags}")
    print()

    # 逐 item 解析
    for item in page.items[:3]:
        data = raw[item.off:item.off + item.len]
        try:
            tup = HeapTuple(data)
        except Exception as e:
            print(f"item {item.index}: 解析失败 {e}")
            continue

        print(f"=== item {item.index}: off={item.off} len={item.len} ===")
        print(f"  xmin={tup.t_xmin} xmax={tup.t_xmax}")
        print(f"  infomask2=0x{tup.t_infomask2:04x} nattrs={tup.nattrs}")
        print(f"  infomask=0x{tup.t_infomask:04x} t_hoff={tup.t_hoff}")
        hasnull = bool(tup.t_infomask & 0x0001)
        print(f"  HEAP_HASNULL={hasnull}")
        nulls = tup.get_nulls()
        n_null = sum(1 for x in nulls if x)
        print(f"  NULL 数量: {n_null}")
        print(f"  raw 长度: {len(data)}")

        # dump t_hoff 附近的字节
        print(f"\n  元组头 23 字节 + t_hoff={tup.t_hoff}:")
        for i in range(0, min(80, len(data)), 16):
            chunk = data[i:i+16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"    +{i:3d}: {hex_part:<48s}  {ascii_part}")

        # 分析数据区 varlena 序列（假设从 t_hoff 开始连续 varlena）
        print(f"\n  数据区 varlena 序列分析:")
        dpos = tup.t_hoff
        col = 0
        while dpos < len(data) - 1 and col < 15:
            first = data[dpos]
            if first & 0x80:
                total = struct.unpack_from(">I", data, dpos)[0] & 0x3FFFFFFF
                if total <= 0 or total > 10000:
                    print(f"    col{col}: 4B头异常 total={total}, 停止")
                    break
                vdata = data[dpos+4:dpos+total]
                preview = vdata[:24].decode("utf-8", errors="replace")
                print(f"    col{col}: 4B头 total={total} data_len={total-4} 预览={preview[:24]}")
                dpos += total
            else:
                total = (first >> 1) & 0x7F
                if total == 0:
                    print(f"    col{col}: 1B头 total=0 (空), 停止")
                    break
                vdata = data[dpos+1:dpos+total]
                preview = vdata[:24].decode("utf-8", errors="replace")
                print(f"    col{col}: 1B头 first=0x{first:02x} total={total} data_len={total-1} 预览={preview[:24]}")
                dpos += total
            col += 1
        print()

if __name__ == "__main__":
    main()
