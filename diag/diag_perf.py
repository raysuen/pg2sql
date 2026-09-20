#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断大文件导出性能：分段计时定位瓶颈。
用法: python3 diag/diag_perf.py <main_file> [toast_file]
"""
import sys
import os
import time
import struct

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <main_file> [toast_file]")
        sys.exit(1)

    main_file = sys.argv[1]
    toast_file = sys.argv[2] if len(sys.argv) > 2 else None

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.tuple import HeapTuple

    # ============== 1. 主表逐段解析计时 ==============
    print("=" * 70)
    print(f"Part 1: 主表解析性能 ({main_file})")
    print("=" * 70)
    file_size = os.path.getsize(main_file)
    npages = file_size // PAGE_SIZE
    print(f"文件大小: {file_size // 1048576}MB, {npages} 页")

    t_total_page = 0.0
    t_total_scan = 0.0
    n_items = 0
    n_scan_tuples = 0
    pages_checked = min(npages, 200)  # 取样前 200 页

    with open(main_file, "rb") as f:
        for pageno in range(pages_checked):
            raw = f.read(PAGE_SIZE)
            if len(raw) < PAGE_SIZE:
                break

            t0 = time.time()
            page = Page(pageno, raw)
            t_total_page += time.time() - t0

            if not page.has_valid_layout:
                continue

            # ItemId 模式
            n_items += len(page.items)

            # 模拟阶段 2/3: 数据区扫描（worker 的回退路径）
            if not page.items:
                pd_upper = page.header.get("upper", 0)
                pd_special = page.header.get("special", PAGE_SIZE)
                if pd_upper >= page.header_size and pd_upper < pd_special:
                    t0 = time.time()
                    pos = pd_upper
                    while pos + 23 <= pd_special:
                        t_xmin = struct.unpack_from("<I", raw, pos)[0]
                        t_infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
                        t_infomask = struct.unpack_from("<H", raw, pos + 20)[0]
                        t_hoff = raw[pos + 22]
                        nattrs = t_infomask2 & 0x07FF
                        if (t_hoff < 23 or t_hoff > 256 or nattrs == 0
                                or t_xmin == 0 or t_xmin > 0x7FFFFFFF
                                or (t_infomask & 0xFF00) == 0):
                            pos += 4
                            continue
                        try:
                            tup = HeapTuple(raw[pos:pd_special])
                        except Exception:
                            pos += 4
                            continue
                        n_scan_tuples += 1
                        pos += 32  # 保守跳过
                    t_total_scan += time.time() - t0

    print(f"取样 {pages_checked} 页:")
    print(f"  页头解析总耗时: {t_total_page:.3f}s ({t_total_page/pages_checked*1000:.2f} ms/页)")
    print(f"  ItemId 总数: {n_items} (平均 {n_items/pages_checked:.1f}/页)")
    print(f"  数据区扫描总耗时: {t_total_scan:.3f}s")
    est_full = (t_total_page + t_total_scan) / pages_checked * npages
    print(f"  → 推算全文件单遍: {est_full:.1f}s")

    # ============== 2. ItemId 模式解析一行计时 ==============
    print()
    print("=" * 70)
    print("Part 2: 元组解析性能（ItemId 模式取前 100 个元组）")
    print("=" * 70)
    from pg2sql.heapfile import _build_col_lengths, _extract_fields_direct
    from pg2sql.catalog import TableMeta, Column

    # 51 列: id int8 + 50 varchar
    columns = [Column("id", 20, 8, 1, -1, True)]
    for i in range(1, 50):
        columns.append(Column(f"col{i}", 1043, -1, i + 1, -1, False))
    tm = TableMeta("24576", "ray", "test_50col", 41008, columns)
    col_lengths = _build_col_lengths(tm)

    t0 = time.time()
    n_parsed = 0
    with open(main_file, "rb") as f:
        for pageno in range(min(npages, 50)):
            raw = f.read(PAGE_SIZE)
            if len(raw) < PAGE_SIZE:
                break
            page = Page(pageno, raw)
            if not page.has_valid_layout:
                continue
            for item in page.items:
                if item.flags != 1:
                    continue
                data = raw[item.off:item.off + item.len]
                try:
                    tup = HeapTuple(data)
                except Exception:
                    continue
                if not tup.is_live:
                    continue
                nulls = tup.get_nulls()
                fields = _extract_fields_direct(tup.raw, tup.t_hoff, nulls, col_lengths)
                n_parsed += 1
                if n_parsed >= 100:
                    break
            if n_parsed >= 100:
                break
    t1 = time.time()
    if n_parsed:
        print(f"  解析 {n_parsed} 个元组: {t1-t0:.3f}s ({(t1-t0)/n_parsed*1000:.2f} ms/元组)")
        # 推算全表
        est_rows = npages * 4  # 估计每页 4 个元组
        print(f"  → 推算 {est_rows} 行全解析: {(t1-t0)/n_parsed*est_rows:.1f}s")
    else:
        print(f"  ItemId 模式未解析到元组!")

    # ============== 3. TOAST 索引性能 ==============
    if toast_file and os.path.exists(toast_file):
        print()
        print("=" * 70)
        print(f"Part 3: TOAST 索引性能 ({toast_file})")
        print("=" * 70)
        from pg2sql.toast import ToastFile
        tf = ToastFile(toast_file)
        toast_size = os.path.getsize(toast_file)
        toast_pages = toast_size // PAGE_SIZE
        print(f"文件大小: {toast_size // 1048576}MB, {toast_pages} 页")

        # 取样前 500 页计时
        t0 = time.time()
        tf2 = ToastFile(toast_file)
        tf2._pos_index = {}
        n_sample = min(toast_pages, 500)
        with open(toast_file, "rb") as f:
            for pageno in range(n_sample):
                raw = f.read(PAGE_SIZE)
                if len(raw) < PAGE_SIZE:
                    break
                tf2._index_page(raw, pageno, full=False)
        t1 = time.time()
        print(f"  取样 {n_sample} 页索引: {t1-t0:.3f}s ({(t1-t0)/n_sample*1000:.2f} ms/页)")
        print(f"  → 推算全文件 build_index (v1.14 主进程仅建一次, worker 共享): {(t1-t0)/n_sample*toast_pages:.1f}s")

    # ============== 4. 检查主表每页 ItemId 数量分布 ==============
    print()
    print("=" * 70)
    print("Part 4: 主表页面特征（判断走 ItemId 还是扫描模式）")
    print("=" * 70)
    n_with_items = 0
    n_without = 0
    nattrs_set = set()
    with open(main_file, "rb") as f:
        for pageno in range(min(npages, 100)):
            raw = f.read(PAGE_SIZE)
            if len(raw) < PAGE_SIZE:
                break
            page = Page(pageno, raw)
            if not page.has_valid_layout:
                continue
            if page.items:
                n_with_items += 1
                for item in page.items[:3]:
                    data = raw[item.off:item.off+item.len]
                    try:
                        tup = HeapTuple(data)
                        nattrs_set.add(tup.nattrs)
                    except Exception:
                        pass
            else:
                n_without += 1
    print(f"  前 100 页: {n_with_items} 页有 ItemId, {n_without} 页无")
    print(f"  元组 nattrs 分布: {sorted(nattrs_set)}")


if __name__ == "__main__":
    main()
