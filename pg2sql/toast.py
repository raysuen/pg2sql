# -*- coding: utf-8 -*-
# version: 1.9
"""
pg2sql.toast
TOAST 表解析与重组。
TOAST 表结构固定为:
    chunk_id (oid) | chunk_seq (int4) | chunk_data (bytea)
TOAST 表文件同样为堆文件，通过主表字段中的外部引用 (valueid/toastrelid) 关联。

兼容标准 PG 和金仓（无偏移表，数据区连续排列）两种格式。

大文件性能设计（针对慢磁盘优化）:
  - 轻量索引: build_index() 只记录 (valueid, seq) -> (页号, 元组偏移)
  - 重组: fetch_and_reassemble() 按需读取 chunk payload
  - 页级 LRU 缓存: 一页常含数十个 chunk（实测 ~42/页），
    _load_page_payloads() 每页只读一次 + 解析一次，同页全部 chunk 共享
  - mmap 读页: 无 seek/read 系统调用，OS fault-around 预读
  - 全量模式: load() 流式扫描建 payload 索引（不整文件驻留内存）

v1.9（对照 PG varatt.h 复核）:
  - chunk_data 的 varlena 头改为小端 tag 分派（旧版 4B 头误用大端 + bit7）
  - 元组提取统一走顺序布局（TOAST_COL_LENGTHS）
  - fetch_and_reassemble 可选 expected_size 校验（chunk 缺失检测）
"""
import os
import struct
import mmap as _mmap_mod
from collections import OrderedDict

from .page import Page, PAGE_SIZE, ITEMID_NORMAL
from .tuple import HeapTuple
from .binary import varlena_parse, VARLENA_1B, VARLENA_4B, VARLENA_4B_COMPRESSED

# pg_toast 表 chunk 列布局固定：
#   chunk_id   oid   (4B)
#   chunk_seq  int4  (4B)
#   chunk_data bytea (varlena)

TOAST_COL_LENGTHS = [(4, False), (4, False), (0, True)]  # (attlen, is_varlena)

_ZERO64 = b"\x00" * 64  # 全零页快速判定

# 页级 payload 缓存默认容量（页数）。8192B * 4096 = 32MB
DEFAULT_PAGE_CACHE_PAGES = 4096


class ToastFile:
    """一个 TOAST 表文件。

    两种模式:
      - 轻量模式: build_index() 建位置索引（几 MB 内存），重组时按需读页
      - 全量模式: load() 建 payload 索引（数据全内存），重组零 IO

    重组统一入口 fetch_and_reassemble()，自动选择模式。
    """

    def __init__(self, path, page_size=PAGE_SIZE,
                 page_cache_pages=DEFAULT_PAGE_CACHE_PAGES):
        self.path = path
        self.page_size = page_size
        self.pages = []  # 兼容保留（不再填充，流式处理）
        self._chunk_index = {}  # valueid -> [(seq, payload), ...] (全量模式)
        self._pos_index = {}    # valueid -> [(seq, (pageno, off)), ...] (轻量模式)
        self._file = None       # 按需读取的文件句柄
        self._mm = None         # mmap 只读映射（懒初始化）
        self._mode_hint = None  # 'offset' | 'direct' 元组解析模式记忆
        # 页级 payload LRU 缓存: pageno -> {offset: payload}
        self._page_payloads = OrderedDict()
        self._page_cache_max = page_cache_pages

    # ------------------------------------------------------------------
    # 页读取（mmap 优先，普通 read 回退）
    # ------------------------------------------------------------------

    def _ensure_file(self):
        if self._file is None:
            self._file = open(self.path, "rb")
        return self._file

    def _ensure_mmap(self):
        """懒初始化 mmap 只读映射。失败回退普通 read。"""
        if self._mm is None:
            try:
                f = self._ensure_file()
                size = os.fstat(f.fileno()).st_size
                if size >= self.page_size:
                    self._mm = _mmap_mod.mmap(f.fileno(), 0,
                                              access=_mmap_mod.ACCESS_READ)
            except (OSError, ValueError):
                self._mm = None
        return self._mm

    def _read_page(self, pageno):
        """读一页原始字节。mmap 切片（无系统调用）优先。"""
        mm = self._ensure_mmap()
        if mm is not None:
            start = pageno * self.page_size
            return mm[start : start + self.page_size]
        f = self._ensure_file()
        f.seek(pageno * self.page_size)
        return f.read(self.page_size)

    # ------------------------------------------------------------------
    # 全量模式（小文件 / --toast-cache full）
    # ------------------------------------------------------------------

    def load(self, max_pages=0):
        """流式全量加载: 建 payload 完整索引。

        v1.8: 逐页读取处理，不把整个文件驻留 self.pages（内存省一半）。
        max_pages > 0 时只读前 N 页（用于轻量验证）。
        """
        self.pages = []
        self._chunk_index = {}
        file_size = os.path.getsize(self.path)
        npages = file_size // self.page_size
        if max_pages:
            npages = min(npages, max_pages)
        with open(self.path, "rb") as f:
            for pageno in range(npages):
                raw = f.read(self.page_size)
                if len(raw) < self.page_size:
                    break
                if raw[:64] == _ZERO64:
                    continue  # 全零页快速跳过
                self._index_page(raw, pageno, full=True)
        for vid in self._chunk_index:
            self._chunk_index[vid].sort(key=lambda c: c[0])
        return self

    def _build_index(self):
        """兼容保留: 从 self.pages 建索引（外部注入页数据时）。"""
        self._chunk_index = {}
        for pageno, raw in enumerate(self.pages):
            self._index_page(raw, pageno, full=True)
        for vid in self._chunk_index:
            self._chunk_index[vid].sort(key=lambda c: c[0])

    # ------------------------------------------------------------------
    # 轻量索引模式（大文件默认）
    # ------------------------------------------------------------------

    def build_index(self, verbose=False, log=None):
        """扫描整个文件建立位置索引 (valueid, seq) -> (页号, 偏移)。

        只记录位置不读 payload，内存占用极小。
        全零页（新分配未使用）直接跳过，不进页头探测。
        """
        self._pos_index = {}
        file_size = os.path.getsize(self.path)
        npages = file_size // self.page_size
        with open(self.path, "rb") as f:
            for pageno in range(npages):
                raw = f.read(self.page_size)
                if len(raw) < self.page_size:
                    break
                if raw[:64] == _ZERO64:
                    continue  # 全零页快速跳过（免页头探测）
                self._index_page(raw, pageno, full=False)
                if verbose and log and pageno % 2000 == 0 and pageno > 0:
                    log(f"  TOAST 索引: {pageno}/{npages} 页...")
        for vid in self._pos_index:
            self._pos_index[vid].sort(key=lambda c: c[0])
        return self

    def _index_page(self, raw, pageno, full):
        """解析一页并加入索引。full=True 存 payload，full=False 存位置。"""
        target = self._chunk_index if full else self._pos_index
        for off, chunk_id, chunk_seq, payload in self._extract_page_chunks(
                raw, pageno, want_payload=full):
            if full:
                target.setdefault(chunk_id, []).append((chunk_seq, payload))
            else:
                target.setdefault(chunk_id, []).append((chunk_seq, (pageno, off)))

    def _extract_page_chunks(self, raw, pageno, want_payload=True):
        """解析一页，返回 [(offset, chunk_id, seq, payload_or_None), ...]。

        offset 为元组在页内的起始位置（ItemId 路径=item.off，扫描路径=扫描 pos），
        与位置索引记录的偏移一致。

        ItemId 路径优先（模式记忆: 偏移表/直接提取只在首次尝试），
        本页无有效结果时回退数据区扫描。
        want_payload=False 时只校验不切片（轻量索引用）。
        """
        page = Page(pageno, raw)
        if not page.has_valid_layout:
            return []

        out = []
        try_direct = (self._mode_hint == "direct")

        # ---- 阶段 1+2: ItemId 定位元组 ----
        for item in page.items:
            if item.flags != ITEMID_NORMAL:
                continue
            item_data = raw[item.off : item.off + item.len]
            try:
                tup = HeapTuple(item_data)
            except Exception:
                continue
            if tup.nattrs != 3:
                continue
            chunk_id = None
            chunk_seq = None
            payload = None
            if not try_direct:
                # 顺序提取（传入 TOAST 列布局）
                nulls = tup.get_nulls()
                from .heapfile import _extract_fields_direct
                fields = _extract_fields_direct(
                    tup.raw, tup.t_hoff, nulls, TOAST_COL_LENGTHS
                )
                chunk_id, chunk_seq, payload = self._fields_to_chunk(fields)
                if chunk_id is not None:
                    self._mode_hint = "direct"
            if chunk_id is None:
                # 回退: get_fields（同样为顺序提取，保留独立入口便于诊断）
                fields = tup.get_fields(TOAST_COL_LENGTHS)
                chunk_id, chunk_seq, payload = self._fields_to_chunk(fields)
                if chunk_id is not None:
                    self._mode_hint = "offset"
            if chunk_id is not None:
                try_direct = True
                out.append((item.off, chunk_id, chunk_seq,
                            payload if want_payload else None))
        if out:
            return out

        # ---- 阶段 3: 数据区扫描（无 ItemId / ItemId 不兼容）----
        pd_upper = page.header.get("upper", 0)
        pd_special = page.header.get("special", self.page_size)
        if pd_upper < page.header_size or pd_upper >= pd_special:
            return []
        from .heapfile import _calculate_tuple_size

        pos = pd_upper
        while pos + 23 <= pd_special:
            t_xmin = struct.unpack_from("<I", raw, pos)[0]
            t_infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
            t_infomask = struct.unpack_from("<H", raw, pos + 20)[0]
            t_hoff = raw[pos + 22]
            nattrs = t_infomask2 & 0x07FF
            if (t_hoff < 23 or t_hoff > 256 or nattrs != 3
                    or t_xmin == 0 or t_xmin > 0x7FFFFFFF
                    or (t_infomask & 0xFF00) == 0):
                pos += 4
                continue
            try:
                tup = HeapTuple(raw[pos:pd_special])
            except Exception:
                pos += 4
                continue
            if tup.t_hoff != t_hoff or tup.nattrs != nattrs:
                pos += 4
                continue
            nulls = tup.get_nulls()
            if want_payload:
                from .heapfile import _extract_fields_direct
                fields = _extract_fields_direct(
                    tup.raw, tup.t_hoff, nulls, TOAST_COL_LENGTHS
                )
                chunk_id, chunk_seq, payload = self._fields_to_chunk(fields)
            else:
                chunk_id, chunk_seq = self._toast_light_tuple(
                    tup.raw, tup.t_hoff, nulls)
            if chunk_id is not None:
                out.append((pos, chunk_id, chunk_seq,
                            payload if want_payload else None))
            actual_size = _calculate_tuple_size(tup, TOAST_COL_LENGTHS)
            next_pos = (actual_size + 7) & ~7
            pos += max(next_pos, 8)
        return out

    # ------------------------------------------------------------------
    # 重组（页级 LRU 缓存）
    # ------------------------------------------------------------------

    def fetch_chunks_by_valueid(self, valueid: int):
        """全量模式: 返回 [(seq, payload), ...]。轻量模式返回空。"""
        return self._chunk_index.get(valueid, [])

    def get_all_chunk_ids(self):
        if self._chunk_index:
            return list(self._chunk_index.keys())
        return list(self._pos_index.keys())

    def _load_page_payloads(self, pageno):
        """加载一页全部 chunk payload，返回 {offset: payload}。

        LRU 缓存: 同页数十个 chunk 只读一次、解析一次（v1.8 核心优化）。
        同一行的多个 valueid 的 chunk 通常聚集在相邻页，命中率极高。
        """
        cached = self._page_payloads.get(pageno)
        if cached is not None:
            self._page_payloads.move_to_end(pageno)
            return cached
        raw = self._read_page(pageno)
        result = {}
        if len(raw) == self.page_size:
            for off, chunk_id, chunk_seq, payload in self._extract_page_chunks(
                    raw, pageno, want_payload=True):
                if payload is not None:
                    result[off] = payload
        self._page_payloads[pageno] = result
        if len(self._page_payloads) > self._page_cache_max:
            self._page_payloads.popitem(last=False)  # 淘汰最久未访问页
        return result

    def _read_chunk_payload(self, pageno, offset):
        """轻量模式: 读一个 chunk 的 payload（走页级缓存）。"""
        payloads = self._load_page_payloads(pageno)
        p = payloads.get(offset)
        if p is not None:
            return p
        # 罕见回退: 页缓存未含该 offset（索引与页解析模式不一致）
        raw = self._read_page(pageno)
        if len(raw) < self.page_size:
            return None
        page = Page(pageno, raw)
        if not page.has_valid_layout:
            return None
        try:
            tup = HeapTuple(raw[offset:page.header.get("special", self.page_size)])
        except Exception:
            return None
        from .heapfile import _extract_fields_direct
        nulls = tup.get_nulls()
        fields = _extract_fields_direct(
            tup.raw, tup.t_hoff, nulls, TOAST_COL_LENGTHS
        )
        chunk_id, chunk_seq, payload = self._fields_to_chunk(fields)
        return payload

    def fetch_and_reassemble(self, valueid: int, expected_size: int = 0):
        """重组一个 valueid 的所有 chunk，返回完整 payload（或 None）。

        自动选择模式: 全量索引直接拼接；位置索引按需读页（页级缓存）。
        expected_size > 0 时校验重组总长，不符（chunk 缺失/损坏）返回 None。
        """
        # 全量模式
        chunks = self._chunk_index.get(valueid)
        if chunks:
            payload = b"".join(c[1] for c in chunks)
            if expected_size and len(payload) != expected_size:
                return None
            return payload

        # 轻量模式: 索引项为 (seq, (pageno, offset))
        positions = self._pos_index.get(valueid)
        if not positions:
            return None
        parts = []
        for seq, pos_info in positions:
            pageno, offset = pos_info
            payload = self._read_chunk_payload(pageno, offset)
            if payload is None:
                continue
            parts.append(payload)
        if not parts:
            return None
        payload = b"".join(parts)
        if expected_size and len(payload) != expected_size:
            return None
        return payload

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _toast_light_tuple(self, raw, t_hoff, nulls):
        """轻量提取 TOAST 元组: chunk_id + chunk_seq，校验第三列非空但不复制 payload。

        TOAST 表三列布局固定 (chunk_id 4B + chunk_seq 4B = 8B 天然对齐)，
        可直接 unpack，比通用 _extract_fields_direct 快且零 payload 切片。
        返回 (chunk_id, chunk_seq) 或 (None, None)。
        """
        off = t_hoff
        if off + 8 > len(raw):
            return (None, None)
        if nulls and (nulls[0] or nulls[1]):
            return (None, None)
        chunk_id = struct.unpack_from("<I", raw, off)[0]
        chunk_seq = struct.unpack_from("<i", raw, off + 4)[0]
        # 合理性校验（与 _fields_to_chunk 一致）
        if not (1 <= chunk_id <= 100000000):
            return (None, None)
        if not (0 <= chunk_seq <= 1000000):
            return (None, None)
        # 第 3 列 chunk_data (varlena): 只校验头有效（小端 tag 分派），不切片
        if nulls and len(nulls) > 2 and nulls[2]:
            return (None, None)
        voff = off + 8
        if voff >= len(raw):
            return (None, None)
        kind, total, _, _ = varlena_parse(raw, voff)
        if kind is None:
            return (None, None)
        if kind == VARLENA_1B and total <= 1:
            return (None, None)
        if kind in (VARLENA_4B, VARLENA_4B_COMPRESSED) and total <= 4:
            return (None, None)
        return (chunk_id, chunk_seq)

    def _fields_to_chunk(self, fields):
        """从 3 个字段字节提取 chunk。兼容金仓 varlena 头。

        含合理性校验: chunk_id/chunk_seq 必须在合理范围，
        防止偏移表模式解析金仓数据产生的垃圾被误当 chunk。
        """
        if len(fields) < 3 or fields[0] is None or fields[1] is None:
            return (None, None, None)
        if len(fields[0]) < 4 or len(fields[1]) < 4:
            return (None, None, None)
        try:
            chunk_id = struct.unpack("<I", fields[0][:4])[0]
            chunk_seq = struct.unpack("<i", fields[1][:4])[0]
        except (struct.error, TypeError):
            return (None, None, None)
        # 合理性校验
        if not (1 <= chunk_id <= 100000000):
            return (None, None, None)
        if not (0 <= chunk_seq <= 1000000):
            return (None, None, None)
        # chunk_data 是 bytea varlena，去头（兼容金仓 1B/4B 头）
        payload = _varlena_payload(fields[2]) if fields[2] else b""
        if not payload:
            return (None, None, None)
        return (chunk_id, chunk_seq, payload)

    def close(self):
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._file:
            self._file.close()
            self._file = None
        self._page_payloads.clear()


def _varlena_payload(b: bytes):
    """去 varlena 头（PG 真实格式：小端 4B 头 + 低 2 位 tag 分派）。"""
    if not b:
        return b""
    kind, total, _, _ = varlena_parse(b, 0)
    if kind == VARLENA_1B:
        return b[1:1 + max(total - 1, 0)]
    if kind in (VARLENA_4B, VARLENA_4B_COMPRESSED):
        return b[4:4 + max(total - 4, 0)]
    return b""
