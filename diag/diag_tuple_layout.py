#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断金仓用户表元组数据区布局。
用法: python3 diag_tuple_layout.py <data_file> <n_cols>
示例: python3 diag_tuple_layout.py /docker/data/kbv9r1c10mysql/base/24576/40974 2
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
    print(f"页面布局: {page.header.get('layout')}")
    print(f"header_size={page.header_size}, pd_upper={pd_upper}, pd_special={pd_special}")
    print(f"ItemId 数量: {len(page.items)}")
    print()

    # 扫描模式找元组
    pos = pd_upper
    found = 0
    while pos + HEAP_TUPLE_HEADER_SIZE <= pd_special:
        t_xmin = struct.unpack_from("<I", raw, pos)[0]
        t_infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
        t_infomask = struct.unpack_from("<H", raw, pos + 20)[0]
        t_hoff = raw[pos + 22]
        nattrs = t_infomask2 & HEAP_NATTS_MASK

        if t_hoff < 23 or t_hoff > 256:
            pos += 4
            continue
        if nattrs != n_cols:
            pos += 4
            continue
        if t_xmin == 0 or t_xmin > 0x7FFFFFFF:
            pos += 4
            continue
        if (t_infomask & 0xFF00) == 0:
            pos += 4
            continue

        found += 1
        print(f"=== 元组 {found} @ pos={pos} ===")
        print(f"  xmin={t_xmin}, nattrs={nattrs}, infomask=0x{t_infomask:04x}, t_hoff={t_hoff}")

        # Dump 64 bytes from tuple start
        dump_end = min(pos + 64, pd_special)
        print(f"\n  原始字节 dump:")
        for i in range(0, dump_end - pos, 16):
            chunk = raw[pos+i:pos+i+16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"    +{i:3d}: {hex_part:<48s}  {ascii_part}")

        # 从 t_hoff 开始尝试多种偏移读取数据
        print(f"\n  从 t_hoff={t_hoff} 开始读取:")

        # 尝试 1: 直接从 t_hoff 读（当前实现）
        print(f"  [方案1] 直接从 t_hoff={t_hoff} 读:")
        if pos + t_hoff + 4 <= pd_special:
            id_val = struct.unpack_from("<i", raw, pos + t_hoff)[0]
            print(f"    id (offset {t_hoff}): {id_val} (0x{raw[pos+t_hoff:pos+t_hoff+4].hex()})")
        if pos + t_hoff + 5 <= pd_special:
            name_first = raw[pos + t_hoff + 4]
            if name_first & 0x80:
                vlen = struct.unpack_from(">I", raw, pos + t_hoff + 4)[0] & 0x3FFFFFFF
                name_bytes = raw[pos + t_hoff + 8: pos + t_hoff + 8 + vlen]
                print(f"    name (4B hdr, offset {t_hoff+4}): vlen={vlen} val={name_bytes}")
            else:
                vlen = name_first
                name_bytes = raw[pos + t_hoff + 5: pos + t_hoff + 5 + vlen]
                print(f"    name (1B hdr, offset {t_hoff+4}): vlen={vlen} val={name_bytes}")

        # 尝试 2: 跳过 4 字节 OID
        print(f"  [方案2] 跳过 4B OID 后从 t_hoff+4={t_hoff+4} 读:")
        if pos + t_hoff + 8 <= pd_special:
            id_val2 = struct.unpack_from("<i", raw, pos + t_hoff + 4)[0]
            print(f"    id (offset {t_hoff+4}): {id_val2} (0x{raw[pos+t_hoff+4:pos+t_hoff+8].hex()})")
        if pos + t_hoff + 9 <= pd_special:
            name_first2 = raw[pos + t_hoff + 8]
            if name_first2 & 0x80:
                vlen2 = struct.unpack_from(">I", raw, pos + t_hoff + 8)[0] & 0x3FFFFFFF
                name_bytes2 = raw[pos + t_hoff + 12: pos + t_hoff + 12 + vlen2]
                print(f"    name (4B hdr, offset {t_hoff+8}): vlen={vlen2} val={name_bytes2}")
            else:
                vlen2 = name_first2
                name_bytes2 = raw[pos + t_hoff + 9: pos + t_hoff + 9 + vlen2]
                print(f"    name (1B hdr, offset {t_hoff+8}): vlen={vlen2} val={name_bytes2}")

        # 尝试 3: t_hoff 不对，从 header 后的 align4 位置读
        for try_off in range(24, 40, 4):
            if pos + try_off + 4 > pd_special:
                break
            val = struct.unpack_from("<i", raw, pos + try_off)[0]
            if 0 < val < 1000000:
                # 可能是 id 值
                print(f"  [探索] offset {try_off}: int4={val} (0x{raw[pos+try_off:pos+try_off+4].hex()})")
                # 检查后面是否像 varlena
                if pos + try_off + 5 <= pd_special:
                    nb = raw[pos + try_off + 4]
                    if nb > 0 and nb < 127:
                        name_val = raw[pos + try_off + 5: pos + try_off + 5 + nb]
                        try:
                            name_str = name_val.decode("utf-8")
                            if name_str.isprintable():
                                print(f"          → 可能是 id={val}, name='{name_str}' ★")
                        except:
                            pass

        print()
        if found >= 5:
            break

        # 跳过当前元组
        pos += 8  # 保守跳过

    if found == 0:
        print("未找到匹配的元组")


if __name__ == "__main__":
    main()
