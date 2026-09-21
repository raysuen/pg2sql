# -*- coding: utf-8 -*-
# version: 1.7
"""
pg2sql.page
PostgreSQL 堆文件页面解析（支持任意 block size）。
参考 PostgreSQL 源码 src/include/storage/bufpage.h, src/include/storage/itemid.h
兼容金仓(KingbaseES) page version 5 的非标准页头布局。

页大小自动探测（v1.7）:
  PG 8.3+ 页头 pd_pagesize_version 编码 = page_size | version
  （bufpage.h: PageSetPageSizeAndVersion / PageGetPageSize = psv & 0xFF00，
   16KB 页 -> psv 高 9 位 = 0x4000，32KB -> 0x8000）。
  Page(page_size=None) 时从页头自动读取页大小，无需调用方告知；
  自定义 BLCKSZ 编译（16KB/32KB）的实例可直接解析。

v1.6（对照 pg_filedump 复核）:
  - 标准布局补 pd_special 校验（pd_upper <= pd_special <= PAGE_SIZE）
  - 宽松探测 version 范围收窄为 4-5（v1-3 为 8.2 前远古布局，本工具不支持）
v1.5: 模块级布局缓存 —— 同一文件的页面布局一致，首次探测成功后记住
(psv_offset, version, header_size)，后续页面直接快速解析。
"""
import struct

from .binary import u16

# 默认页大小 (PG 标准 BLCKSZ)
PAGE_SIZE = 8192

# 支持的页大小（PG --with-blocksize 可选 1/2/4/8/16/32 KB；本工具支持 >= 8KB）
KNOWN_PAGE_SIZES = (8192, 16384, 32768)

# 标准页头大小 (PG 8.3+, 含 pd_checksum)
PAGE_HEADER_SIZE = 24

# ItemId 4 字节: lp_off(15bit) | lp_flags(2bit) | lp_len(15bit)
ITEMID_UNUSED = 0
ITEMID_NORMAL = 1
ITEMID_REDIRECT = 2
ITEMID_DEAD = 3

# 支持的页版本
PG_PAGE_VERSION = 4
KB_PAGE_VERSION = 5  # 金仓 KingbaseES

# 模块级布局缓存: 记住最近一次成功解析的页头布局。
# 同一文件的页面布局一致；缓存校验失败自动回退三阶段探测，安全。
_LAYOUT_CACHE = {"psv_offset": None, "version": None, "header_size": None,
                 "page_size": None}


def _update_layout_cache(psv_offset, version, header_size, page_size):
    _LAYOUT_CACHE["psv_offset"] = psv_offset
    _LAYOUT_CACHE["version"] = version
    _LAYOUT_CACHE["header_size"] = header_size
    _LAYOUT_CACHE["page_size"] = page_size


def detect_page_size(raw: bytes):
    """从页头自动探测页面大小（字节）。

    扫描前 128 字节寻找 pd_pagesize_version：
      version = psv & 0x00FF（PG 4 / 金仓 5）
      size    = psv & 0xFF00（bufpage.h PageGetPageSize 语义，字节数）
    候选 size 必须属于 KNOWN_PAGE_SIZES 且指针链（lower<=upper<=special<=size）
    合理。返回页大小；未找到返回 None。
    """
    if len(raw) < 130:
        return None
    for off in range(8, 128, 2):
        psv = u16(raw, off)
        version = psv & 0xFF
        size = psv & 0xFF00
        if version not in (PG_PAGE_VERSION, KB_PAGE_VERSION):
            continue
        if size not in KNOWN_PAGE_SIZES:
            continue
        if off < 6:
            continue
        pd_lower = u16(raw, off - 6)
        pd_upper = u16(raw, off - 4)
        pd_special = u16(raw, off - 2)
        header_end = (off + 2 + 3) & ~3
        if not (header_end <= pd_lower <= pd_upper <= pd_special <= size):
            continue
        return size
    return None


class ItemId:
    __slots__ = ("off", "flags", "len", "index")

    def __init__(self, index, raw: int):
        self.index = index
        self.off = raw & 0x7FFF
        self.flags = (raw >> 15) & 0x03
        self.len = (raw >> 17) & 0x7FFF


class Page:
    """一个数据页（8KB / 16KB / 32KB）"""

    __slots__ = (
        "pageno", "raw", "header", "items",
        "has_valid_layout", "error",
        "header_size", "page_size",
    )

    def __init__(self, pageno: int, raw: bytes, page_size=None):
        self.pageno = pageno
        self.raw = raw
        self.header = {}
        self.items = []
        self.has_valid_layout = False
        self.error = ""
        self.header_size = 24
        self.page_size = page_size
        self._parse()

    def _parse(self):
        if self.page_size is None:
            # 自动探测页大小：优先页头编码，兜底用实际块长
            self.page_size = detect_page_size(self.raw)
            if self.page_size is None:
                self.page_size = len(self.raw)
        if len(self.raw) != self.page_size:
            self.error = f"page size {len(self.raw)} != {self.page_size}"
            return

        # 0. 快速路径: 用布局缓存直接解析（同文件后续页面免三阶段探测）
        if self._try_cached_layout():
            return

        # 1. 尝试标准 PG 布局 (version 4, 页头 24B)
        if self._try_standard_layout():
            return

        # 2. 尝试金仓布局: 扫描 pd_pagesize_version 字段位置
        if self._try_auto_detect_layout():
            return

        # 3. 尝试宽松探测: 扫描任意 version + size 组合
        if self._try_loose_detect_layout():
            return

        self.error = "无法识别页面布局"

    def _try_cached_layout(self) -> bool:
        """用上次成功解析的布局参数快速解析本页。

        一次 unpack 校验代替三阶段循环探测；校验失败返回 False 回退全量探测。
        """
        psv_off = _LAYOUT_CACHE["psv_offset"]
        version = _LAYOUT_CACHE["version"]
        header_end = _LAYOUT_CACHE["header_size"]
        page_size = _LAYOUT_CACHE["page_size"]
        if psv_off is None or page_size != self.page_size:
            return False

        psv = u16(self.raw, psv_off)
        if (psv & 0xFF) != version or (psv & 0xFF00) != self.page_size:
            return False

        pd_lower = u16(self.raw, psv_off - 6)
        pd_upper = u16(self.raw, psv_off - 4)
        pd_special = u16(self.raw, psv_off - 2)

        if not (header_end <= pd_lower <= pd_upper <= pd_special <= self.page_size):
            return False

        self.header_size = header_end
        self.header = {
            "lsn": self.raw[0:8].hex(),
            "lower": pd_lower,
            "upper": pd_upper,
            "special": pd_special,
            "pagesize_version": psv,
            "version": version,
            "layout": f"cached (psv_offset={psv_off}, header={header_end})",
        }
        self._parse_items(pd_lower)
        self.has_valid_layout = True
        return True

    def _try_standard_layout(self) -> bool:
        """标准 PG 8.3+ 布局: 24B 页头, version 4"""
        psv = u16(self.raw, 18)
        version = psv & 0xFF
        size = psv & 0xFF00
        if version != PG_PAGE_VERSION or size != self.page_size:
            return False

        pd_lower = u16(self.raw, 12)
        pd_upper = u16(self.raw, 14)
        pd_special = u16(self.raw, 16)

        if not (24 <= pd_lower <= pd_upper <= pd_special <= self.page_size):
            return False

        self.header_size = 24
        self.header = {
            "lsn": self.raw[0:8].hex(),
            "checksum": u16(self.raw, 8),
            "flags": u16(self.raw, 10),
            "lower": pd_lower,
            "upper": pd_upper,
            "special": pd_special,
            "pagesize_version": psv,
            "version": version,
            "layout": "standard",
        }
        self._parse_items(pd_lower)
        self.has_valid_layout = True
        _update_layout_cache(18, version, 24, self.page_size)
        return True

    def _try_auto_detect_layout(self) -> bool:
        """自动探测页头布局: 在前 64 字节中搜索 pd_pagesize_version。

        金仓等非标准 PG 分支可能使用不同页头大小和版本号。
        找到 pd_pagesize_version 后，向前推 6 字节即为 pd_lower。
        """
        for off in range(8, 64, 2):
            psv = u16(self.raw, off)
            version = psv & 0xFF
            size = psv & 0xFF00

            # 接受 version 4 或 5, size 必须等于本页页大小
            if version not in (PG_PAGE_VERSION, KB_PAGE_VERSION):
                continue
            if size != self.page_size:
                continue

            # psv 前面 6 字节应该是 pd_lower, pd_upper, pd_special
            if off < 6:
                continue
            pd_lower = u16(self.raw, off - 6)
            pd_upper = u16(self.raw, off - 4)
            pd_special = u16(self.raw, off - 2)

            # 校验: lower <= upper <= special <= page_size
            # 且 lower 至少要能放得下页头 + 至少 0 个 item
            header_end = off + 2  # psv 字段之后
            # 对齐到 4 字节
            header_end = (header_end + 3) & ~3

            if not (header_end <= pd_lower <= pd_upper <= pd_special <= self.page_size):
                continue

            self.header_size = header_end
            self.header = {
                "lsn": self.raw[0:8].hex(),
                "lower": pd_lower,
                "upper": pd_upper,
                "special": pd_special,
                "pagesize_version": psv,
                "version": version,
                "layout": f"auto (psv_offset={off}, header={header_end})",
            }
            self._parse_items(pd_lower)
            self.has_valid_layout = True
            _update_layout_cache(off, version, header_end, self.page_size)
            return True

        return False

    def _try_loose_detect_layout(self) -> bool:
        """宽松探测: 在前 128 字节中搜索 version(4~5) + size 组合。

        当标准探测和自动探测都失败时，可能是因为:
        - 金仓使用了非标准 version 号
        - 页头布局完全不同
        - pd_pagesize_version 的编码方式不同

        策略: 在前 128 字节中搜索任何看起来像 pd_pagesize_version 的字段，
        接受 version 4-5 和 size == 本页页大小的组合。
        (v1-3 为 PG 8.2 前的远古布局，元组头布局不同，本工具不支持)
        """
        for off in range(8, 128, 2):
            if off + 2 > len(self.raw):
                break
            psv = u16(self.raw, off)
            version = psv & 0xFF
            size = psv & 0xFF00

            # 接受 version 4-5, size 必须等于本页页大小
            if version not in (PG_PAGE_VERSION, KB_PAGE_VERSION):
                continue
            if size != self.page_size:
                continue

            # psv 前面 6 字节应该是 pd_lower, pd_upper, pd_special
            if off < 6:
                continue
            pd_lower = u16(self.raw, off - 6)
            pd_upper = u16(self.raw, off - 4)
            pd_special = u16(self.raw, off - 2)

            # 校验
            header_end = off + 2
            header_end = (header_end + 3) & ~3

            if not (header_end <= pd_lower <= pd_upper <= pd_special <= self.page_size):
                continue

            self.header_size = header_end
            self.header = {
                "lsn": self.raw[0:8].hex(),
                "lower": pd_lower,
                "upper": pd_upper,
                "special": pd_special,
                "pagesize_version": psv,
                "version": version,
                "layout": f"loose (psv_offset={off}, header={header_end}, version={version})",
            }
            self._parse_items(pd_lower)
            self.has_valid_layout = True
            _update_layout_cache(off, version, header_end, self.page_size)
            return True

        return False

    def _parse_items(self, pd_lower: int):
        """从 ItemId 数组解析 item 列表。"""
        n_items = (pd_lower - self.header_size) // 4
        items = []
        for i in range(n_items):
            offset = self.header_size + i * 4
            if offset + 4 > pd_lower:
                break
            raw_id = u16(self.raw, offset) | (u16(self.raw, offset + 2) << 16)
            it = ItemId(i + 1, raw_id)
            if it.flags == ITEMID_UNUSED:
                continue
            # 校验 off 和 len 在合理范围内
            if it.off >= self.page_size or it.len > self.page_size:
                continue
            if it.off + it.len > self.page_size:
                continue
            items.append(it)
        self.items = items


def get_item_offset(page: Page, itemid: ItemId) -> int:
    return itemid.off


def get_item_data(page: Page, itemid: ItemId) -> bytes:
    return page.raw[itemid.off : itemid.off + itemid.len]
