#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 PG 真实磁盘格式全要素（P0 修复回归，对照 pg_filedump/PDU 复核）。

覆盖修复项:
 1. 4B varlena 小端头: 180 字符中文（540B payload → 4B 头）
    —— 修复前用大端 + bit7 分派，是 test_50col 真实数据 53% 值损坏的根因
 2. NULL 位图真实语义: bit 置 1 = 非空，清 0 = NULL（修复前语义相反）
 3. numeric 磁盘格式: long/short、负数、NaN、Infinity（修复前误用网络协议格式）
 4. varlena 对齐填充: 奇数 1B 值后跟 4B 值 → PG 写填充零字节，读取需 INTALIGN 跳过
 5. 压缩 TOAST: PGLZ/LZ4 解压 + tcinfo 校验 + extsize<rawsize-4 判压缩
 6. 未压缩 TOAST: 真实 PG 约定 extsize = rawsize - 4
 7. 外联指针字段顺序自适应: 标准 [rawsize][extinfo]（含 PG14+ method 高 2 位）
    与金仓标注 [extsize][rawsize] 两种顺序
 8. 可见性: 插入已回滚（XMIN_INVALID）任何模式不输出; 行锁（XMAX_LOCK_ONLY）视为活行
 9. t_hoff 一致性校验（MAXALIGN(23 + 位图) == t_hoff）
10. 并发模式与单进程输出逐行一致（含全部上述要素）
"""
import os
import sys
import json
import struct
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pg2sql.page import PAGE_SIZE
from pg2sql.tuple import (
    HEAP_XMIN_COMMITTED, HEAP_XMIN_INVALID, HEAP_XMAX_INVALID,
    HEAP_XMAX_COMMITTED, HEAP_XMAX_LOCK_ONLY, HEAP_HASNULL, HEAP_HASVARWIDTH,
)
from pg2sql.heapfile import HeapFile, _build_col_lengths
from pg2sql.catalog import TableMeta, Column
from pg2sql.types import INT4OID, VARCHAROID, NUMERICOID, TEXTOID
from pg2sql.binary import (
    varlena_parse, parse_external_pointer, pglz_decompress, lz4_block_decompress,
)

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".temp", "realfmt_test")

MAIN_RFN = 51008
TOASTRELID = 51012
VID_BASE = 60000  # valueid 基数: VID_BASE + row*10 + col


# ======================================================================
# 构造工具（金仓页格式: 页头 48B, psv@44, version 5; 元组带 ItemId）
# ======================================================================

def make_kb_page(pd_lower, pd_upper, pd_special, version=5):
    page = bytearray(PAGE_SIZE)
    struct.pack_into("<H", page, 38, pd_lower)
    struct.pack_into("<H", page, 40, pd_upper)
    struct.pack_into("<H", page, 42, pd_special)
    psv = (PAGE_SIZE & ~0x7F) | (version & 0x7F)
    struct.pack_into("<H", page, 44, psv)
    return page


def build_page_with_items(tuples_data):
    header_size = 48
    pd_special = PAGE_SIZE
    aligned = [(len(t) + 7) & ~7 for t in tuples_data]
    pd_upper = pd_special - sum(aligned)
    pd_lower = header_size + 4 * len(tuples_data)
    assert pd_lower <= pd_upper
    page = make_kb_page(pd_lower, pd_upper, pd_special)
    pos = pd_upper
    for i, td in enumerate(tuples_data):
        item_raw = pos | (1 << 15) | (len(td) << 17)
        struct.pack_into("<I", page, header_size + 4 * i, item_raw)
        page[pos:pos + len(td)] = td
        pos += aligned[i]
    return bytes(page)


def make_varlena_1b(data: bytes) -> bytes:
    total = 1 + len(data)
    assert total < 128
    return bytes([(total << 1) | 1]) + data


def make_varlena_4b(data: bytes) -> bytes:
    """PG 真实格式: 小端 word = total << 2 (tag 低 2 位 = 00)"""
    return struct.pack("<I", (4 + len(data)) << 2) + data


def make_tuple(xmin, xmax, nattrs, data, null_bitmap, infomask_extra=0):
    """金仓元组: MAXALIGN(23 + 位图) 头 + 连续数据区。"""
    t_hoff = (23 + len(null_bitmap) + 7) & ~7
    buf = bytearray(t_hoff)
    struct.pack_into("<I", buf, 0, xmin)
    struct.pack_into("<I", buf, 4, xmax)
    struct.pack_into("<H", buf, 18, nattrs)
    # 修复原仓库 bug：infomask（偏移 20）从未写入，导致 aborted/locked/
    # HASNULL 等状态全部丢失（可见性断言与 NULL 位图读不出正确语义）
    struct.pack_into("<H", buf, 20,
                     infomask_extra
                     | (HEAP_HASNULL if null_bitmap else 0)
                     | HEAP_HASVARWIDTH)  # 测试数据均含 varlena 列
    buf[22] = t_hoff
    buf[23:23 + len(null_bitmap)] = null_bitmap
    buf += data
    return bytes(buf)


def null_bitmap_for(nattrs, null_cols):
    """PG 真实语义: bit 置 1 = 非空。null_cols 中的列清零。"""
    nbytes = (nattrs + 7) // 8
    bits = 0
    for i in range(nattrs):
        if i not in null_cols:
            bits |= 1 << i
    return bits.to_bytes(nbytes, "little")


# ======================================================================
# TOAST 构造
# ======================================================================

def pglz_compress_test(data: bytes) -> bytes:
    """测试用 PGLZ 压缩器: 字面 + 连续重复字节用 match(off=1)。
    生成合法流并覆盖解压器的字面/匹配/扩展长度路径。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        ctrl_pos = len(out)
        out.append(0)
        ctrl = 0
        for bit in range(8):
            if i >= n:
                break
            if i >= 1 and data[i - 1] == data[i]:
                run = 1
                while i + run < n and data[i + run] == data[i] and run < 273:
                    run += 1
                # 官方 pglz_out_tag 字节序（pg_lzcompress.c）：
                #   buf[0] = (off_high<<4) | code（code<15 → len-3；==15 表示扩展）
                #   buf[1] = off_low
                #   buf[2] = len-18（仅 code==15 时，单字节）
                # （修复原仓库测试压缩器错误：旧实现写 (code, ext, off)，解码器按官方顺序读）
                code = run - 3
                if code < 15:
                    out.append(code)          # off=1 → off_high=0
                    out.append(1)             # off_low
                else:
                    out.append(0x0F)          # 长度码 15（off_high=0）
                    out.append(1)             # off_low
                    out.append(code - 15)     # 扩展长度（≤255）
                ctrl |= 1 << bit
                i += run
            else:
                out.append(data[i])
                i += 1
        out[ctrl_pos] = ctrl
    return bytes(out)


def lz4_compress_repeat(pattern: bytes, total: int) -> bytes:
    """测试用 LZ4 压缩器: 首段字面 = pattern，其余用 match(off=len(pattern))。
    要求 total 是 len(pattern) 的整数倍。"""
    assert total % len(pattern) == 0
    match_len = total - len(pattern)
    out = bytearray()
    # token: lit=8 以内直接编码（pattern 长度假定 <= 15）
    lit_code = len(pattern)
    if match_len - 4 < 15:
        token = (lit_code << 4) | (match_len - 4)
    else:
        token = (lit_code << 4) | 15
        ext = match_len - 4 - 15
        while ext > 255:
            out.extend([255])
            ext -= 255
        out.extend([ext])  # 占位，最终拼在末尾
        # 重新组织: token 后是 literals + offset + ext
    # 简化: token 的字面段就放 pattern
    if match_len - 4 >= 15:
        # 重构（ext 已在 out 里，顺序调整）
        ext_bytes = bytes(out)
        out = bytearray()
        out.append(token)
        out += pattern
        out += struct.pack("<H", len(pattern))
        out += ext_bytes
    else:
        out.append(token)
        out += pattern
        out += struct.pack("<H", len(pattern))
    return bytes(out)


def make_external_ptr_std(rawsize, extsize, method, valueid, toastrelid):
    """标准 PG 顺序外联指针: [rawsize][extinfo(extsize | method<<30)][valueid][toastrelid]"""
    extinfo = (extsize & 0x3FFFFFFF) | ((method & 0x03) << 30)
    return b"\x01\x12" + struct.pack("<IIII", rawsize, extinfo, valueid, toastrelid)


def make_external_ptr_kb(extsize, rawsize, valueid, toastrelid):
    """金仓标注顺序外联指针: [extsize][rawsize][valueid][toastrelid]"""
    return b"\x01\x12" + struct.pack("<IIII", extsize, rawsize, valueid, toastrelid)


def make_numeric_long(neg, dscale, weight, digits):
    payload = struct.pack("<Hh" + "H" * len(digits),
                          (0x4000 if neg else 0) | dscale, weight, *digits)
    return payload


def make_numeric_short(neg, dscale, weight, digits):
    w = abs(weight)
    header = 0x8000 | (0x2000 if neg else 0) | (dscale << 7) \
             | (0x0040 if weight < 0 else 0) | w
    return struct.pack("<H" + "H" * len(digits), header, *digits)


# ======================================================================
# 测试数据
# ======================================================================

CN_VALUE = "数" * 180          # 180 字符中文 = 540B UTF-8 → 4B varlena（原始 bug 场景）
N_COLS = 9

ROWS = [
    # (id, vis, num_payload, num_expect, nullable, bigc, bigl, bigu, bigc_kb_order)
    dict(id=1, vis="live",
         num=make_numeric_long(False, 2, 0, [123, 4500]), num_expect="123.45",
         nullable=b"hello", bigc=b"Q" * 600, bigl=b"abcdefgh" * 125, bigu=b"u" * 1600,
         kb_order=False),
    dict(id=2, vis="live",
         num=make_numeric_short(True, 0, 0, [42]), num_expect="-42",
         nullable=None, bigc=b"Z" * 500, bigl=b"wxyz" * 200, bigu=b"v" * 1200,
         kb_order=False),
    dict(id=3, vis="deleted",
         num=struct.pack("<H", 0xC000), num_expect="NaN",
         nullable=b"gone", bigc=b"P" * 400, bigl=b"1234" * 150, bigu=b"w" * 900,
         kb_order=False),
    dict(id=4, vis="aborted",
         num=make_numeric_long(False, 0, 0, [7]), num_expect="7",
         nullable=b"nope", bigc=b"A" * 300, bigl=b"ab" * 300, bigu=b"t" * 800,
         kb_order=False),
    dict(id=5, vis="locked",
         num=struct.pack("<H", 0xD000), num_expect="Infinity",
         nullable=b"ok", bigc=b"L" * 700, bigl=b"mnop" * 175, bigu=b"s" * 1500,
         kb_order=True),  # bigc 用金仓标注顺序指针（测自适应）
]


def build_row_tuple(row_idx, row):
    """构造一行元组。返回 (tuple_bytes, {col_name: (kind, valueid, ...)})."""
    r = row
    null_cols = set()
    buf = bytearray()

    # col0: id int4
    buf += struct.pack("<i", r["id"])
    # col1: txt_cn 4B varlena（540B 中文）
    buf += make_varlena_4b(CN_VALUE.encode("utf-8"))
    # col2: num numeric（1B varlena，payload <= 9B）
    buf += make_varlena_1b(r["num"])
    # col3: nullable
    if r["nullable"] is None:
        null_cols.add(3)
    else:
        buf += make_varlena_1b(r["nullable"])
    # col4: pad_short 1B varlena（总长 3，奇数）
    buf += make_varlena_1b(b"ab")
    # col5: txt_after 4B varlena（200B）→ 需要 4 对齐填充
    if len(buf) % 4 != 0:
        buf += b"\x00" * (4 - len(buf) % 4)
    buf += make_varlena_4b(b"y" * 200)

    # col6: bigc 压缩 TOAST（PGLZ）
    vid_c = VID_BASE + row_idx * 10 + 6
    # col7: bigl 压缩 TOAST（LZ4）
    vid_l = VID_BASE + row_idx * 10 + 7
    # col8: bigu 未压缩 TOAST
    vid_u = VID_BASE + row_idx * 10 + 8

    # 压缩: extsize = 4 + len(body) (tcinfo + body)
    body_c = pglz_compress_test(r["bigc"])
    tcinfo_c = struct.pack("<I", len(r["bigc"]) | (0 << 30))  # method=0 PGLZ
    chunk_data_c = tcinfo_c + body_c
    if r["kb_order"]:
        ptr_c = make_external_ptr_kb(len(chunk_data_c), 4 + len(r["bigc"]), vid_c, TOASTRELID)
    else:
        ptr_c = make_external_ptr_std(4 + len(r["bigc"]), len(chunk_data_c), 0, vid_c, TOASTRELID)

    # LZ4: 用 bigl 的最小重复周期做 pattern（修复原仓库 bug：原实现按
    # len%8 硬选 "abcdefgh"/"wxyz"，与 "mnop"*175 等实际数据不一致导致断言失败）
    def _min_period(b):
        n = len(b)
        for p in range(1, n // 2 + 1):
            if n % p == 0 and b == b[:p] * (n // p):
                return b[:p]
        return b
    pat = _min_period(r["bigl"])
    assert len(r["bigl"]) % len(pat) == 0
    body_l = lz4_compress_repeat(pat, len(r["bigl"]))
    tcinfo_l = struct.pack("<I", len(r["bigl"]) | (1 << 30))  # method=1 LZ4
    chunk_data_l = tcinfo_l + body_l
    ptr_l = make_external_ptr_std(4 + len(r["bigl"]), len(chunk_data_l), 1, vid_l, TOASTRELID)

    # 未压缩: extsize = rawsize - 4 = payload 长度（真实 PG 约定）
    ptr_u = make_external_ptr_std(4 + len(r["bigu"]), len(r["bigu"]), 0, vid_u, TOASTRELID)

    buf += ptr_c
    buf += ptr_l
    buf += ptr_u

    # 可见性
    xmin = 1000 + r["id"]
    if r["vis"] == "live":
        xmax, extra = 0, 0
    elif r["vis"] == "deleted":
        xmax, extra = 2000 + r["id"], HEAP_XMAX_COMMITTED
    elif r["vis"] == "aborted":
        # 插入回滚: XMIN_INVALID，无 COMMITTED
        xmax, extra = 0, HEAP_XMIN_INVALID
    elif r["vis"] == "locked":
        xmax, extra = 3000 + r["id"], HEAP_XMAX_LOCK_ONLY
    else:
        raise ValueError(r["vis"])

    bitmap = null_bitmap_for(N_COLS, null_cols)
    tup = make_tuple(xmin, xmax, N_COLS, bytes(buf), bitmap, extra)
    return tup, dict(vid_c=vid_c, chunk_data_c=chunk_data_c,
                     vid_l=vid_l, chunk_data_l=chunk_data_l,
                     vid_u=vid_u, bigu=r["bigu"])


def build_all():
    os.makedirs(TEST_DIR, exist_ok=True)
    for name in os.listdir(TEST_DIR):
        full = os.path.join(TEST_DIR, name)
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)

    # 主表: 2 页（页0: 行1-3, 页1: 行4-5）
    page_groups = [[], []]
    toast_chunks = []  # (chunk_id, chunk_seq, chunk_data)
    for idx, row in enumerate(ROWS):
        tup, toasts = build_row_tuple(idx, row)
        page_groups[0 if idx < 3 else 1].append(tup)
        # 压缩值: 1 个 chunk（数据小）
        toast_chunks.append((toasts["vid_c"], 0, toasts["chunk_data_c"]))
        toast_chunks.append((toasts["vid_l"], 0, toasts["chunk_data_l"]))
        # 未压缩值: 按 700B 分 chunk
        bigu = toasts["bigu"]
        seq = 0
        for off in range(0, len(bigu), 700):
            toast_chunks.append((toasts["vid_u"], seq, bigu[off:off + 700]))
            seq += 1

    main_path = os.path.join(TEST_DIR, str(MAIN_RFN))
    with open(main_path, "wb") as f:
        for group in page_groups:
            f.write(build_page_with_items(group))

    # TOAST 表: 每页 6 个 chunk 元组
    toast_tuples = []
    for cid, seq, cdata in toast_chunks:
        data = struct.pack("<I", cid) + struct.pack("<i", seq)
        data += make_varlena_1b(cdata) if len(cdata) < 100 else make_varlena_4b(cdata)
        t_hoff = 24
        tb = bytearray(t_hoff)
        struct.pack_into("<I", tb, 0, 900000)
        struct.pack_into("<I", tb, 4, 0)
        struct.pack_into("<H", tb, 18, 3)
        struct.pack_into("<H", tb, 20, HEAP_XMIN_COMMITTED | HEAP_XMAX_INVALID | 0x0002)
        tb[22] = t_hoff
        toast_tuples.append(bytes(tb) + data)

    toast_pages = []
    for s in range(0, len(toast_tuples), 6):
        toast_pages.append(build_page_with_items(toast_tuples[s:s + 6]))
    toast_path = os.path.join(TEST_DIR, str(TOASTRELID))
    with open(toast_path, "wb") as f:
        for p in toast_pages:
            f.write(p)

    # meta.json
    cols = [
        ("id", INT4OID, 4), ("txt_cn", VARCHAROID, -1), ("num", NUMERICOID, -1),
        ("nullable", VARCHAROID, -1), ("pad_short", VARCHAROID, -1),
        ("txt_after", VARCHAROID, -1), ("bigc", TEXTOID, -1),
        ("bigl", TEXTOID, -1), ("bigu", TEXTOID, -1),
    ]
    meta = {"database": "24576", "tables": [{
        "schema": "ray", "table": "test_realfmt", "relfilenode": MAIN_RFN,
        "primary_key": ["id"],
        "columns": [{"name": n, "type_oid": t, "len": l, "attnum": i + 1,
                     "typmod": -1, "notnull": False} for i, (n, t, l) in enumerate(cols)],
    }]}
    meta_path = os.path.join(TEST_DIR, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    return main_path, toast_path, meta_path


def run_cli(args_list, expect_ok=True):
    cmd = [sys.executable, "main.py"] + args_list
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"CLI 超时: {' '.join(args_list)}")
    if expect_ok and r.returncode != 0:
        raise AssertionError(f"CLI 失败: {' '.join(args_list)}\n{r.stderr[-1500:]}")
    return r


# ======================================================================
# 测试
# ======================================================================

def main():
    print("=" * 60)
    print("测试: PG 真实磁盘格式全要素（P0 修复回归）")
    print("=" * 60)
    main_path, toast_path, meta_path = build_all()
    print(f"  主表: {main_path}")
    print(f"  TOAST: {toast_path}")

    # ---- 0. 单元级: varlena / 外联指针 / 解压器 ----
    print("\n[0] 单元级校验")
    # 4B varlena 小端 (540B 中文)
    cn_bytes = CN_VALUE.encode("utf-8")
    v4 = make_varlena_4b(cn_bytes)
    kind, total, _, plen = varlena_parse(v4)
    assert kind == "4b" and total == 544 and plen == 540, (kind, total, plen)
    # 1B varlena
    v1 = make_varlena_1b(b"ab")
    kind, total, _, plen = varlena_parse(v1)
    assert kind == "1b" and total == 3 and plen == 2
    # 外联指针（标准顺序 + method 位）
    ptr = make_external_ptr_std(1004, 19, 1, 60001, TOASTRELID)
    p = parse_external_pointer(ptr)
    assert p["rawsize"] == 1004 and p["extsize"] == 19 and p["method"] == 1 and p["compressed"]
    # 外联指针（金仓标注顺序）
    ptr_kb = make_external_ptr_kb(17, 704, 60001, TOASTRELID)
    p2 = parse_external_pointer(ptr_kb)
    assert p2["rawsize"] == 704 and p2["extsize"] == 17 and p2["compressed"], p2
    # PGLZ/LZ4 往返
    for val in (b"Q" * 600, b"Z" * 500, b"ab" * 7, b"x"):
        c = pglz_compress_test(val)
        assert pglz_decompress(c, len(val)) == val
    assert lz4_block_decompress(lz4_compress_repeat(b"abcdefgh", 1000), 1000) == b"abcdefgh" * 125
    print("    varlena/外联指针/PGLZ/LZ4 全部通过 ✓")

    # ---- 1. dump_rows 值级校验 ----
    print("\n[1] dump_rows 值级校验（默认只出活行: 1,2,5）")
    from pg2sql.toast import ToastFile
    tm = TableMeta("24576", "ray", "test_realfmt", MAIN_RFN, [
        Column(n, t, l, i + 1, -1) for i, (n, t, l) in enumerate([
            ("id", INT4OID, 4), ("txt_cn", VARCHAROID, -1), ("num", NUMERICOID, -1),
            ("nullable", VARCHAROID, -1), ("pad_short", VARCHAROID, -1),
            ("txt_after", VARCHAROID, -1), ("bigc", TEXTOID, -1),
            ("bigl", TEXTOID, -1), ("bigu", TEXTOID, -1)])
    ])
    toast = ToastFile(toast_path)
    toast.load()
    hf = HeapFile(main_path)
    hf.set_toast(toast)
    rows = list(hf.dump_rows(tm))
    ids = [r["values"][0] for r in rows]
    print(f"    输出 id: {ids}")
    assert ids == ["1", "2", "5"], f"默认应输出活行 [1,2,5]（含锁定行 5），实际 {ids}"

    by_id = {r["values"][0]: r["values"] for r in rows}
    # 行 1: 全要素
    v = by_id["1"]
    assert v[1] == CN_VALUE, f"中文 4B varlena 损坏: 得到 {len(v[1])} 字符"
    assert v[2] == "123.45", v[2]
    assert v[3] == "hello"
    assert v[4] == "ab"
    assert v[5] == "y" * 200, f"对齐后 4B varlena 损坏: {len(v[5]) if v[5] else None}"
    assert v[6] == "Q" * 600, f"PGLZ TOAST 解压失败: {None if v[6] is None else len(v[6])}"
    assert v[7] == "abcdefgh" * 125, f"LZ4 TOAST 解压失败: {None if v[7] is None else len(v[7])}"
    assert v[8] == "u" * 1600, f"未压缩 TOAST 重组失败: {None if v[8] is None else len(v[8])}"
    print("    行 1: 中文 4B varlena/numeric/对齐/PGLZ/LZ4/未压缩 TOAST 全部正确 ✓")
    # 行 2: NULL + 短格式负数 numeric
    v = by_id["2"]
    assert v[2] == "-42", v[2]
    assert v[3] is None, f"NULL 列应为 None，实际 {v[3]!r}"
    assert v[6] == "Z" * 500 and v[8] == "v" * 1200
    print("    行 2: NULL 位图语义 + numeric short 负数正确 ✓")
    # 行 5: 锁定行视为活行 + Infinity + 金仓顺序指针
    v = by_id["5"]
    assert v[2] == "Infinity", v[2]
    assert v[6] == "L" * 700, f"金仓顺序压缩指针失败: {None if v[6] is None else len(v[6])}"
    assert v[7] == "mnop" * 175
    print("    行 5: 行锁视为活行 + Infinity + 金仓顺序指针自适应 ✓")
    toast.close()

    # ---- 2. 可见性矩阵（CLI）----
    print("\n[2] 可见性矩阵（CLI 三模式）")
    get_ids = lambda out: [int(l.split("VALUES (")[1].split(",")[0])
                           for l in out.splitlines() if l.startswith("INSERT INTO")]
    r_default = run_cli([main_path, "--catalog-json", meta_path, "--sql"])
    assert get_ids(r_default.stdout) == [1, 2, 5], get_ids(r_default.stdout)
    assert "-- DELETED" not in r_default.stdout
    r_del = run_cli([main_path, "--catalog-json", meta_path, "--sql", "--deleted"])
    assert get_ids(r_del.stdout) == [1, 2, 3, 5], get_ids(r_del.stdout)
    assert r_del.stdout.count("-- DELETED") == 1
    r_only = run_cli([main_path, "--catalog-json", meta_path, "--sql", "--only-deleted"])
    assert get_ids(r_only.stdout) == [3], get_ids(r_only.stdout)
    assert "NaN" in r_only.stdout
    print("    默认[1,2,5] / --deleted[1,2,3,5] / --only-deleted[3]，abort 行(4)从不出现 ✓")

    # ---- 3. CSV 模式 NULL ----
    print("\n[3] CSV 模式")
    csv_path = os.path.join(TEST_DIR, "out.csv")
    run_cli([main_path, "--catalog-json", meta_path, "--data", "-o", csv_path])
    csv_lines = open(csv_path, encoding="utf-8").read().splitlines()
    assert len(csv_lines) == 3
    # 行 2 的 nullable(第4列) 应为 \N
    fields = csv_lines[1].split(",")
    assert fields[3] == "\\N", f"CSV NULL 应为 \\N，实际 {fields[3]!r}"
    print(f"    3 行，NULL 输出为 \\N ✓")

    # ---- 4. 并发模式与单进程逐行一致 ----
    print("\n[4] 并发模式一致性")
    single = os.path.join(TEST_DIR, "single.sql")
    par = os.path.join(TEST_DIR, "par.sql")
    run_cli([main_path, "--catalog-json", meta_path, "--sql", "-o", single])
    run_cli([main_path, "--catalog-json", meta_path, "--sql", "--parallel", "2", "-o", par])
    a = open(single, encoding="utf-8").read().splitlines()
    b = open(par, encoding="utf-8").read().splitlines()
    assert a == b, f"并发与单进程输出不一致: {len(a)} vs {len(b)} 行"
    # 并发 --only-deleted
    par_del = os.path.join(TEST_DIR, "par_del.sql")
    run_cli([main_path, "--catalog-json", meta_path, "--sql", "--only-deleted",
             "--parallel", "2", "-o", par_del])
    d = [l for l in open(par_del, encoding="utf-8").read().splitlines()
         if l.startswith("INSERT INTO")]
    assert len(d) == 1 and "NaN" in d[0]
    print(f"    并发输出 {len(b)} 行与单进程完全一致，--only-deleted 并发正确 ✓")

    print("\n" + "=" * 60)
    print("全部测试通过!")
    print("=" * 60)


if __name__ == "__main__":
    main()
