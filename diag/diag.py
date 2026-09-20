#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本: 检查真实 PG/金仓数据文件的页面和元组解析情况。
用法: python3 diag.py <data_file>
"""
import sys
import os
import struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import Page, PAGE_SIZE
from pg2sql.tuple import HeapTuple
from pg2sql.binary import cstring


def main():
    if len(sys.argv) < 2:
        print(f"用法: python3 {sys.argv[0]} <data_file>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        sys.exit(1)

    filesize = os.path.getsize(path)
    npages = filesize // PAGE_SIZE
    print(f"文件: {path}")
    print(f"大小: {filesize} bytes, 页数: {npages}")
    print()

    with open(path, "rb") as f:
        raw = f.read(PAGE_SIZE)

    # 1. 原始页头 (24 bytes)
    print("=== 页头原始字节 (前 24 字节) ===")
    print(" ".join(f"{b:02x}" for b in raw[:24]))
    print()

    # 2. 手动解析页头 (两种布局)
    print("=== 页头解析 ===")
    # 布局 A (PG 8.3+, 含 pd_checksum):
    #   0-7: pd_lsn, 8-9: pd_checksum, 10-11: pd_flags, 12-13: pd_lower,
    #   14-15: pd_upper, 16-17: pd_special, 18-19: pd_pagesize_version
    print(f"布局 A (含 checksum, PG 8.3+):")
    print(f"  pd_lsn:            {raw[0:8].hex()}")
    print(f"  pd_checksum:       0x{struct.unpack_from('<H', raw, 8)[0]:04x}")
    print(f"  pd_flags:           0x{struct.unpack_from('<H', raw, 10)[0]:04x}")
    print(f"  pd_lower:           {struct.unpack_from('<H', raw, 12)[0]}")
    print(f"  pd_upper:           {struct.unpack_from('<H', raw, 14)[0]}")
    print(f"  pd_special:         {struct.unpack_from('<H', raw, 16)[0]}")
    psv_a = struct.unpack_from('<H', raw, 18)[0]
    print(f"  pd_pagesize_version: 0x{psv_a:04x} (size={psv_a & ~0x07}, version={psv_a & 0x07})")
    print()

    # 布局 B (无 checksum, 旧格式):
    #   0-7: pd_lsn, 8-9: pd_flags, 10-11: pd_lower, 12-13: pd_upper,
    #   14-15: pd_special, 16-17: pd_pagesize_version
    print(f"布局 B (无 checksum, 旧格式):")
    print(f"  pd_lsn:            {raw[0:8].hex()}")
    print(f"  pd_flags:           0x{struct.unpack_from('<H', raw, 8)[0]:04x}")
    print(f"  pd_lower:           {struct.unpack_from('<H', raw, 10)[0]}")
    print(f"  pd_upper:           {struct.unpack_from('<H', raw, 12)[0]}")
    print(f"  pd_special:         {struct.unpack_from('<H', raw, 14)[0]}")
    psv_b = struct.unpack_from('<H', raw, 16)[0]
    print(f"  pd_pagesize_version: 0x{psv_b:04x} (size={psv_b & ~0x07}, version={psv_b & 0x07})")
    print()

    # 3. 用 page.py 解析
    print("=== page.py 解析 ===")
    page = Page(0, raw)
    print(f"  has_valid_layout: {page.has_valid_layout}")
    print(f"  error: '{page.error}'")
    if page.has_valid_layout:
        print(f"  header: {page.header}")
        print(f"  items count: {len(page.items)}")
    print()

    if not page.has_valid_layout:
        print("页面解析失败，尝试用布局 B (无 checksum) 解析...")
        # 手动用布局 B 尝试
        pd_lower_b = struct.unpack_from('<H', raw, 10)[0]
        pd_upper_b = struct.unpack_from('<H', raw, 12)[0]
        psv_b = struct.unpack_from('<H', raw, 16)[0]
        version_b = psv_b & 0x07
        size_b = psv_b & ~0x07
        print(f"  version={version_b}, size={size_b}, lower={pd_lower_b}, upper={pd_upper_b}")
        if size_b == PAGE_SIZE and version_b == 4 and 24 <= pd_lower_b <= pd_upper_b:
            print("  布局 B 有效！页面使用旧格式（无 pd_checksum）")
            # 手动解析 items
            n_items = (pd_lower_b - 20) // 4  # 旧布局页头 20 字节
            print(f"  n_items (旧布局): {n_items}")
            items = []
            for i in range(n_items):
                item_raw = struct.unpack_from('<I', raw, 20 + i * 4)[0]
                off = item_raw & 0x7FFF
                flags = (item_raw >> 15) & 0x03
                length = (item_raw >> 17) & 0x7FFF
                if flags == 0:
                    continue
                items.append((i + 1, off, flags, length))
            print(f"  valid items: {len(items)}")
            for idx, off, flags, length in items[:5]:
                print(f"    item {idx}: off={off}, flags={flags}, len={length}")
                tup_data = raw[off:off + length]
                if len(tup_data) >= 23:
                    xmin = struct.unpack_from('<I', tup_data, 0)[0]
                    xmax = struct.unpack_from('<I', tup_data, 4)[0]
                    infomask2 = struct.unpack_from('<H', tup_data, 18)[0]
                    infomask = struct.unpack_from('<H', tup_data, 20)[0]
                    t_hoff = tup_data[22]
                    nattrs = infomask2 & 0x07FF
                    print(f"      xmin={xmin}, xmax={xmax}")
                    print(f"      infomask2=0x{infomask2:04x} (nattrs={nattrs})")
                    print(f"      infomask=0x{infomask:04x}")
                    print(f"        XMIN_COMMITTED={bool(infomask & 0x0100)}")
                    print(f"        XMIN_INVALID={bool(infomask & 0x0200)}")
                    print(f"        XMIN_FROZEN={(infomask & 0x0300) == 0x0300}")
                    print(f"        XMAX_COMMITTED={bool(infomask & 0x0400)}")
                    print(f"        XMAX_INVALID={bool(infomask & 0x0800)}")
                    print(f"        HASOID={bool(infomask & 0x0008)}")
                    print(f"      t_hoff={t_hoff}")
                    # 尝试解析第一列
                    try:
                        tup = HeapTuple(tup_data)
                        print(f"      is_live={tup.is_live}")
                        print(f"      nattrs={tup.nattrs}")
                        print(f"      get_oid={tup.get_oid()}")
                        fields = tup.get_fields()
                        if fields and fields[0]:
                            name = cstring(fields[0])
                            print(f"      field[0] (name)={name!r}")
                    except Exception as e:
                        print(f"      HeapTuple 解析失败: {e}")
        return

    # 4. 逐个 item 诊断
    print("=== 逐个 Item 诊断 ===")
    for item in page.items[:10]:
        print(f"\n  Item {item.index}: off={item.off}, flags={item.flags}, len={item.len}")
        tup_data = raw[item.off : item.off + item.len]
        if len(tup_data) < 23:
            print(f"    太短: {len(tup_data)} bytes")
            continue

        xmin = struct.unpack_from('<I', tup_data, 0)[0]
        xmax = struct.unpack_from('<I', tup_data, 4)[0]
        infomask2 = struct.unpack_from('<H', tup_data, 18)[0]
        infomask = struct.unpack_from('<H', tup_data, 20)[0]
        t_hoff = tup_data[22]
        nattrs = infomask2 & 0x07FF

        print(f"    xmin={xmin}, xmax={xmax}")
        print(f"    infomask2=0x{infomask2:04x} (nattrs={nattrs})")
        print(f"    infomask=0x{infomask:04x}")
        print(f"      XMIN_COMMITTED={bool(infomask & 0x0100)}")
        print(f"      XMIN_INVALID={bool(infomask & 0x0200)}")
        print(f"      XMIN_FROZEN={(infomask & 0x0300) == 0x0300}")
        print(f"      XMAX_COMMITTED={bool(infomask & 0x0400)}")
        print(f"      XMAX_INVALID={bool(infomask & 0x0800)}")
        print(f"      HASOID={bool(infomask & 0x0008)}")
        print(f"    t_hoff={t_hoff}")

        try:
            tup = HeapTuple(tup_data)
            print(f"    is_live={tup.is_live}")
            print(f"    get_oid={tup.get_oid()}")
            fields = tup.get_fields()
            print(f"    fields count={len(fields)}")
            if fields:
                for i, f in enumerate(fields[:3]):
                    if f is None:
                        print(f"    field[{i}] = NULL")
                    else:
                        print(f"    field[{i}] raw ({len(f)}B): {f[:20].hex()}...")
                        if i == 0:
                            try:
                                name = cstring(f)
                                print(f"    field[0] as cstring: {name!r}")
                            except:
                                pass
        except Exception as e:
            print(f"    HeapTuple 解析失败: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
