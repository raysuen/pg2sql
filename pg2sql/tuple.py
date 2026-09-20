# -*- coding: utf-8 -*-
# version: 1.5
"""
pg2sql.tuple
HeapTuple 解析：行头、NULL 位图、数据区。

参考 PostgreSQL src/include/access/htup_details.h, src/backend/access/common/heaptuple.c
对照 pg_filedump / PDU 复核（v1.5）：
  - NULL 位图改为 PG 真实语义（att_isnull）：bit 置 1 = 非空，清 0 = NULL
    （旧版语义相反，靠合成测试同错闭环掩盖）
  - get_fields() 改为顺序提取（PG 磁盘格式无逐字段偏移表，
    字段从 t_hoff 起连续排列；旧版"偏移表"是不存在的格式）
  - is_live 前置 XMIN_INVALID 判定（插入已回滚的死元组不再误判为活行）
  - get_oid 按 PG 版本读取（v1.9 修复，评审 P1-4）：
      PG12+  OID 位于数据区首 4 字节（t_hoff 处，HeapTupleHeaderGetOid 直接取
              t_data 首字段；系统目录表 HEAP_HASOID 且 t_hoff 已含 OID 空间）
      PG<=11 OID 位于 t_hoff - 4（对齐填充内，旧版代码的读取位置只对 PG<=11 成立）
  - get_nulls 按 PG 版本门控位图语义（评审 P2-1）：
      PG12+  bit 置 1 = 非空（att_isnull = !bit）
      PG<=11 bit 置 1 = NULL（语义相反）
"""
from .binary import u16

# HeapTupleHeaderData 固定部分 23 字节（不含 null bitmap 与对齐 padding）：
#   t_xmin(4) t_xmax(4) t_field3(4, union cid/xvac) t_ctid(6) t_infomask2(2) t_infomask(2) t_hoff(1)
#   偏移: xmin=0 xmax=4 t_field3=8 t_ctid=12 t_infomask2=18 t_infomask=20 t_hoff=22
HEAP_TUPLE_HEADER_SIZE = 23

# t_infomask 位定义 (参考 PostgreSQL htup_details.h)
HEAP_HASNULL = 0x0001
HEAP_HASVARWIDTH = 0x0002
HEAP_HASEXTERNAL = 0x0004
HEAP_HASOID = 0x0008  # 系统目录表保留 OID 列
HEAP_XMAX_KEYSHR_LOCK = 0x0010
HEAP_COMBOCID = 0x0020
HEAP_XMAX_EXCL_LOCK = 0x0040
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100  # xmin 已提交 (插入可见)
HEAP_XMIN_INVALID = 0x0200    # xmin 已回滚 (插入无效)
HEAP_XMIN_FROZEN = 0x0300      # PG 9.4+: COMMITTED | INVALID = 冻结
HEAP_XMAX_COMMITTED = 0x0400   # xmax 已提交 (删除已提交)
HEAP_XMAX_INVALID = 0x0800    # xmax 无效 (未删除)
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_UPDATED = 0x2000
HEAP_MOVED_OFF = 0x4000
HEAP_MOVED_IN = 0x8000

HEAP_NATTS_MASK = 0x07FF  # t_infomask2 低 11 位 = 属性数

# 锁标志位（xmax 是锁而非删除）
HEAP_LOCK_MASK = (HEAP_XMAX_EXCL_LOCK | HEAP_XMAX_KEYSHR_LOCK | HEAP_XMAX_LOCK_ONLY)


class HeapTuple:
    __slots__ = (
        "raw",
        "t_xmin",
        "t_xmax",
        "t_field3",
        "t_ctid",
        "t_infomask2",
        "t_infomask",
        "t_hoff",
        "nattrs",
        "pg_version",
        "is_kingbase",
        "_fields",
        "_nulls",
        "_oid",
    )

    def __init__(self, raw: bytes, pg_version: int = 12, is_kingbase: bool = False):
        self.raw = raw
        self.is_kingbase = bool(is_kingbase)
        if len(raw) < HEAP_TUPLE_HEADER_SIZE:
            raise ValueError(f"tuple too short: {len(raw)}")
        self.t_xmin = u16(raw, 0) | (u16(raw, 2) << 16)
        self.t_xmax = u16(raw, 4) | (u16(raw, 6) << 16)
        self.t_field3 = u16(raw, 8) | (u16(raw, 10) << 16)  # union t_cid/t_xvac
        self.t_ctid = (u16(raw, 12), u16(raw, 14), u16(raw, 16))  # block(4B)+offset(2B)
        self.t_infomask2 = u16(raw, 18)
        self.t_infomask = u16(raw, 20)
        self.t_hoff = raw[22]
        self.nattrs = self.t_infomask2 & HEAP_NATTS_MASK
        # PG 主版本（P2-1/P1-4: OID 位置与 NULL 位图语义随版本变化；默认 12）
        self.pg_version = int(pg_version) if pg_version else 12
        self._fields = None
        self._nulls = None
        self._oid = None

    # ---------- 元组头自检 ----------
    def is_header_consistent(self) -> bool:
        """t_hoff 一致性校验（pg_filedump 同款规则，P2-2 版本感知）。

        PG12+：t_hoff = MAXALIGN(23 + BITMAPLEN(natts))，OID 是数据区首字段
               （不占 t_hoff 空间；实证 pg_type bool 行 t_hoff=32、bitmap=9）
        PG<=11：t_hoff = MAXALIGN(23 + BITMAPLEN(natts) [+4 HASOID])，
               OID 存于 t_hoff-4（头内对齐填充区，HeapTupleHeaderGetOid 宏）
        可过滤扫描模式下的绝大多数假元组。t_hoff 最小 24（MAXALIGN(23)）。
        """
        if self.t_hoff < 24 or self.t_hoff > 256:
            return False
        bitmap_len = (self.nattrs + 7) // 8 if (self.t_infomask & HEAP_HASNULL) else 0
        oid_len = 4 if (self.pg_version < 12 and (self.t_infomask & HEAP_HASOID)) else 0
        expected = (HEAP_TUPLE_HEADER_SIZE + bitmap_len + oid_len + 7) & ~7
        return expected == self.t_hoff

    # ---------- 可见性 ----------
    @property
    def is_insert_aborted(self) -> bool:
        """插入事务已回滚（XMIN_INVALID 且非 FROZEN）。"""
        if (self.t_infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN:
            return False
        return bool(self.t_infomask & HEAP_XMIN_INVALID
                    and not (self.t_infomask & HEAP_XMIN_COMMITTED))

    @property
    def is_deleted(self):
        """已删除：xmax 有效且非锁、非 multi。

        离线解析场景：当 xmax hint bits 未设置但 t_xmax 非零时，
        判定为已删除（删除事务已提交但 hint bits 未落盘）。
        插入已回滚的元组不参与删除判定（由 is_live 统一处理）。
        """
        if self.is_insert_aborted:
            return False  # 插入回滚的死元组不算"已删除行"
        if self.t_infomask & HEAP_XMAX_INVALID:
            return False
        if self.t_infomask & HEAP_XMAX_LOCK_ONLY:
            return False
        if self.t_infomask & HEAP_XMAX_IS_MULTI:
            return False
        if self.t_infomask & HEAP_XMAX_COMMITTED:
            return True
        # Hint bits 未设置：xmax 非零表示已删除
        if self.t_xmax != 0:
            return True
        return False

    @property
    def is_live(self):
        """可见性判定。

        优先级：
        1. XMIN_INVALID（不含 FROZEN）→ 插入已回滚，死行
        2. is_deleted（xmax 有效非锁非 multi，含 hint bits 未落盘）→ 死行
        3. FROZEN (0x0300) → 可见
        4. 其他 → 可见 (数据恢复优先)
        """
        if self.is_insert_aborted:
            return False
        if self.is_deleted:
            return False
        return True

    # ---------- OID (系统目录表) ----------
    def get_oid(self) -> int:
        """如果行头有 HEAP_HASOID 标志，返回 OID 值，否则返回 -1。

        PG12+ 实测（评审 P1-4）：OID 位于 t_hoff 处（数据区首 4 字节），
        用户列从 t_hoff + 4 开始；t_hoff = MAXALIGN(23 + 位图 + 4) 已含 OID 空间。
        PG<=11：OID 位于 t_hoff - 4（对齐填充内，t_hoff 不含 OID 空间时按
        HeapTupleHeaderGetOid 宏读取）。PG11 宏:
            *((Oid *) ((char *)(tup) + (tup)->t_hoff - sizeof(Oid)))
        """
        if self._oid is not None:
            return self._oid
        if not (self.t_infomask & HEAP_HASOID):
            self._oid = -1
            return self._oid
        if self.pg_version >= 12:
            oid_offset = self.t_hoff
        else:
            oid_offset = self.t_hoff - 4
        if oid_offset < 0 or oid_offset + 4 > len(self.raw):
            self._oid = -1
            return self._oid
        import struct
        self._oid = struct.unpack_from("<I", self.raw, oid_offset)[0]
        return self._oid

    # ---------- NULL 位图 ----------
    def get_nulls(self) -> list:
        """返回每个字段是否为 NULL（不含系统列）。

        PG 真实语义 (att_isnull)：
          PG12+  位图 bit 置 1 = 非空，清 0 = NULL（is_null = not bit）
          PG<=11 位图 bit 置 1 = NULL，清 0 = 非空（语义相反，P2-1 门控）
        位图紧跟 23B 固定头（t_bits 从偏移 23 起）。
        """
        if self._nulls is not None:
            return self._nulls
        n = self.nattrs
        if not (self.t_infomask & HEAP_HASNULL):
            self._nulls = [False] * n
            return self._nulls
        bitmap_len = (n + 7) // 8
        if HEAP_TUPLE_HEADER_SIZE + bitmap_len > len(self.raw):
            self._nulls = [False] * n  # 位图越界，按无 NULL 处理
            return self._nulls
        bitmap = self.raw[HEAP_TUPLE_HEADER_SIZE: HEAP_TUPLE_HEADER_SIZE + bitmap_len]
        res = []
        pg12 = self.pg_version >= 12
        for i in range(n):
            byte = bitmap[i // 8]
            bit = bool(byte & (1 << (i % 8)))
            if pg12:
                res.append(not bit)   # bit 置 1 = 非空 → is_null = not bit
            else:
                res.append(bit)       # PG<=11: bit 置 1 = NULL
        self._nulls = res
        return res

    # ---------- 字段数据 ----------
    def get_fields(self, col_lengths=None) -> list:
        """顺序提取数据区字段，返回字段字节列表（NULL 字段为 None）。

        PG 磁盘格式（标准 PG 与金仓一致）：无逐字段偏移表，
        字段从 t_hoff 起按列声明顺序连续排列（含类型对齐与 varlena 填充）。

        col_lengths: [(attlen, is_varlena), ...]，来自 _build_col_lengths(table_meta)
        或系统表布局常量。为 None 时按"全部 varlena"提取（仅适用于
        简单场景，正式解析应显式传布局）。

        varlena 字段保持原始编码（1/4 字节头），由类型层进一步解码。
        """
        if self._fields is not None:
            return self._fields
        from .heapfile import _extract_fields_direct
        n = self.nattrs
        if n == 0:
            self._fields = []
            return self._fields
        nulls = self.get_nulls()
        if col_lengths is None:
            # 默认布局：前 n 列全部按 varlena 处理
            col_lengths = [(0, True)] * n
        # 布局长度不足时补 varlena（防御）
        while len(col_lengths) < n:
            col_lengths = list(col_lengths) + [(0, True)]
        self._fields = _extract_fields_direct(
            self.raw, self.t_hoff, nulls, col_lengths[:n]
        )
        return self._fields

    def get_ctid(self):
        return self.t_ctid


# ---------- 便捷可见性判定 ----------
def tuple_is_visible(tup: HeapTuple) -> bool:
    return tup.is_live


def tuple_is_deleted(tup: HeapTuple) -> bool:
    return tup.is_deleted
