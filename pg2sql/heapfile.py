# -*- coding: utf-8 -*-
# version: 2.1
"""
pg2sql.heapfile
堆文件读取与导出引擎：遍历页面、提取元组、关联 TOAST、坏页容错。
兼容金仓(KingbaseES)的非标准 ItemId 格式：当 ItemId 解析无结果时，
自动回退到数据区扫描模式定位元组（按元组头特征扫描 pd_upper~pd_special 区域）。

v1.7（对照 PG varatt.h / pg_filedump / PDU 复核）：
  - varlena 提取改为 PG 真实格式：小端 4B 头 + 低 2 位 tag 分派，
    含对齐填充处理（att_align_pointer：填充零字节后 INTALIGN）
  - 外联指针解析用 binary.parse_external_pointer（rawsize/extsize 顺序自适应）
  - TOAST 重组后接入 PGLZ/LZ4 解压 + 长度校验
  - varlena 类型判定扩展为 attlen==-1 或 VARLENA_TYPES（数组等可外联）
  - 扫描模式增加 t_hoff 一致性校验（pg_filedump 同款）
"""
import os
import struct

from .page import Page, PAGE_SIZE, ITEMID_NORMAL
from .tuple import (
    HeapTuple, HEAP_TUPLE_HEADER_SIZE, HEAP_NATTS_MASK,
    HEAP_HASNULL, HEAP_HASOID,
)
from .types import decode_value, VARLENA_TYPES
from .binary import (
    align4, varlena_parse, parse_external_pointer,
    rebuild_varlena, toast_decompress,
    VARLENA_EXTERNAL, VARLENA_1B, VARLENA_4B, VARLENA_4B_COMPRESSED,
)


# --------------------------------------------------------------------------
# 无偏移表字段提取（PG/金仓磁盘真实格式）
# --------------------------------------------------------------------------

# TOAST 外联指针: 以 0x01 0x12 开头，共 18 字节（即 PG 标准的 varattrib_1b_e）
KB_EXTERNAL_MARKER = b"\x01\x12"
KB_EXTERNAL_SIZE = 18


def _is_kb_external(raw, offset):
    """判断 offset 处是否是 TOAST 外联指针（即 PG 标准 varattrib_1b_e，金仓同样使用）。"""
    return parse_external_pointer(raw, offset) is not None


# 对齐尺寸（PG att_align 常量，参考 pg_type.h typalign）
ALIGN_SIZES = {"c": 1, "s": 2, "i": 4, "d": 8}

# 少数固定长度类型的 typlen 与 typalign 不一致，按类型修正
# （name=64 对齐 'c'、uuid=16 对齐 'c'、tid=6 对齐 's'、oidvector/int2vector 对齐 'i'）
_FIXED_ALIGN_OVERRIDES = {
    19: "c",    # name
    18: "c",    # char
    2950: "c",  # uuid
    27: "s",    # tid
    30: "i",    # oidvector
    22: "i",    # int2vector
}


def _col_align(col) -> str:
    """列的 attalign（'c'/'s'/'i'/'d'），缺失时按 attlen 与已知类型推断。"""
    if getattr(col, "attalign", None) in ALIGN_SIZES:
        return col.attalign
    if col.atttypid in _FIXED_ALIGN_OVERRIDES:
        return _FIXED_ALIGN_OVERRIDES[col.atttypid]
    alen = col.attlen
    if alen <= 0:
        return "i"          # varlena 默认 'i'（核心类型无 'd' varlena）
    if alen >= 8:
        return "d"
    if alen >= 4:
        return "i"
    if alen >= 2:
        return "s"
    return "c"


def _layout_align(attlen, attalign):
    """布局元组 (attlen, is_varlena, attalign) 的对齐尺寸；attalign 可为 None。"""
    if attalign in ALIGN_SIZES:
        return ALIGN_SIZES[attalign]
    if attlen <= 0:
        return 4
    if attlen >= 8:
        return 8
    if attlen >= 4:
        return 4
    if attlen >= 2:
        return 2
    return 1


def _extract_fields_direct(raw, t_hoff, nulls, col_lengths, align8=True):
    """顺序提取数据区字段（PG 真实磁盘格式：无偏移表，字段从 t_hoff 起连续排列）。

    col_lengths: [(attlen, is_varlena, [attalign]), ...]（第三项可选，None 时按
    attlen 推断）；与 heap_deform_tuple 语义一致：

    - 固定长度列 att_align_nominal：按 attalign 无条件对齐（'c'=1,'s'=2,'i'=4,'d'=8）
    - varlena 列 att_align_pointer：仅当当前位置未对齐时跳对齐（位置判定，
      不再用"首字节为 0 判填充"——合法 4B 头 varlena 首字节可能为 0）
    - 外联指针（0x01 0x12 开头 18B）整体提取，由解码层关联 TOAST
    - NULL 列不占空间，通过 nulls 位图跳过

    raw: 元组原始字节
    t_hoff: 数据区起始偏移
    nulls: NULL 位图列表（True = NULL）
    col_lengths: [(attlen, is_varlena[, attalign]), ...]

    返回字段字节列表（NULL 为 None）
    """
    pos = 0
    fields = []
    nraw = len(raw)
    for i, item in enumerate(col_lengths):
        if len(item) >= 3:
            attlen, is_varlena, attalign = item[0], item[1], item[2]
        else:
            attlen, is_varlena = item[0], item[1]
            attalign = None
        align = _layout_align(attlen, attalign)
        if i < len(nulls) and nulls[i]:
            fields.append(None)
            continue

        if is_varlena:
            # PG att_align_pointer: 先看头——1B 短头不需要对齐（实测 t_arr 两列
            # 间无填充）；4B 头才在当前位置错位时补齐
            offset = t_hoff + pos
            if offset >= nraw:
                fields.append(None)
                break
            if raw[offset] & 1 == 0:  # 4B 头：错位时 INTALIGN
                if align > 1 and (t_hoff + pos) % align != 0:
                    pos = (pos + align - 1) & ~(align - 1)
                    offset = t_hoff + pos
                    if offset >= nraw:
                        fields.append(None)
                        break
            kind, total, _, _ = varlena_parse(raw, offset)
            if kind == VARLENA_EXTERNAL:
                fields.append(raw[offset:offset + KB_EXTERNAL_SIZE])
                pos += KB_EXTERNAL_SIZE
            elif kind == VARLENA_1B:
                fields.append(raw[offset:offset + total])
                pos += total
            elif kind in (VARLENA_4B, VARLENA_4B_COMPRESSED):
                fields.append(raw[offset:offset + total])
                pos += total
            else:
                # 无法识别的 varlena 头：记录 None，后续字段已不可信
                fields.append(None)
                break
        else:
            # 固定长度列：att_align_nominal 按 attalign 无条件对齐
            if align > 1:
                pos = (pos + align - 1) & ~(align - 1)
            offset = t_hoff + pos
            if offset + attlen > nraw:
                fields.append(None)
                break
            fields.append(raw[offset:offset + attlen])
            pos += attlen
    return fields


def _build_col_lengths(table_meta):
    """从 TableMeta 构建列长度信息列表 [(attlen, is_varlena, attalign), ...]。

    P1-5：dropped 列在磁盘上仍按原 attlen/attalign 占位推进（heap_deform_tuple
    对 attisdropped 无特判，仅结果不返回），故这里对 dropped 列同样按真实布局
    推进，仅由 _decode_fields 在输出层过滤。
    """
    col_lengths = []
    for col in table_meta.columns:
        is_varlena = (col.attlen == -1 or col.atttypid in VARLENA_TYPES)
        attlen = col.attlen if col.attlen is not None and col.attlen > 0 else (0 if col.attlen is not None and col.attlen >= -1 else 0)
        if attlen < 0:
            attlen = 0  # varlena：长度由自身头决定，此处仅占位
        col_lengths.append((attlen, is_varlena, _col_align(col)))
    return col_lengths


def _calculate_tuple_size(tup, col_lengths):
    """计算元组的实际字节大小（含头部 + 数据区）。与 _extract_fields_direct 对齐规则同步。"""
    nulls = tup.get_nulls()
    data_size = 0
    nraw = len(tup.raw)
    for i, item in enumerate(col_lengths):
        if len(item) >= 3:
            attlen, is_varlena, attalign = item[0], item[1], item[2]
        else:
            attlen, is_varlena = item[0], item[1]
            attalign = None
        align = _layout_align(attlen, attalign)
        if i < len(nulls) and nulls[i]:
            continue
        if is_varlena:
            offset = tup.t_hoff + data_size
            if offset >= nraw:
                break
            if tup.raw[offset] & 1 == 0:  # 4B 头：错位时 INTALIGN
                if align > 1 and (tup.t_hoff + data_size) % align != 0:
                    data_size = (data_size + align - 1) & ~(align - 1)
                    offset = tup.t_hoff + data_size
                    if offset >= nraw:
                        break
            kind, total, _, _ = varlena_parse(tup.raw, offset)
            if kind == VARLENA_EXTERNAL:
                data_size += KB_EXTERNAL_SIZE
            elif kind is not None:
                data_size += total
            else:
                break  # 无法解析，停止
        else:
            # 固定长度列：无条件按 attalign 对齐
            if align > 1:
                data_size = (data_size + align - 1) & ~(align - 1)
            data_size += attlen
    return tup.t_hoff + data_size


def _decode_fields(fields, nulls, table_meta, toast):
    """将原始字段字节解码为可打印值列表。

    P1-5：dropped 列在物理布局中照常占位推进（_build_col_lengths 已含），
    但输出层完全跳过——返回值列表只含未删除列，与 to_sql/to_data 的
    列名列表一一对应。

    varlena 判定：attlen == -1（可外联）或类型在 VARLENA_TYPES。
    TOAST 重组后若外联指针标记压缩（extsize < rawsize - 4）则解压（PGLZ/LZ4），
    并校验重组长度 == rawsize - 4（缺失 chunk 输出 __TOAST_MISSING__）。
    """
    values = []
    # 注入角色名映射（aclitem[] 解码用；无则 oid:N 占位）
    from .types import set_role_name_map
    set_role_name_map(getattr(table_meta, "role_map", None))
    for i, col in enumerate(table_meta.columns):
        if col.attdropped:
            continue
        if i >= len(fields):
            values.append(None)
            continue
        raw = fields[i]
        if raw is None:
            values.append(None)
            continue
        # 检查 TOAST 外联（attlen==-1 的数组等 varlena 类型也可外联）
        is_varlena_col = (col.attlen == -1 or col.atttypid in VARLENA_TYPES)
        if is_varlena_col and raw:
            payload, is_ext, ext_info = _check_external(raw)
            if is_ext and toast is not None:
                payload = toast.fetch_and_reassemble(ext_info["valueid"])
                if payload is None:
                    values.append("__TOAST_MISSING__")
                    continue
                expected = ext_info["rawsize"] - 4  # rawsize 含 4B varlena 头
                if ext_info.get("compressed"):
                    data = toast_decompress(payload, expected,
                                            method=ext_info.get("method", 0))
                    if data is None:
                        values.append("__TOAST_CORRUPT__")
                        continue
                    payload = data
                elif len(payload) != expected:
                    # 未压缩但长度不符 → chunk 缺失
                    values.append("__TOAST_MISSING__")
                    continue
                raw = rebuild_varlena(payload)
            elif is_ext and toast is None:
                values.append("__TOAST_MISSING__")
                continue
        decoded = decode_value(col.atttypid, raw)
        values.append(decoded)
    return values


class HeapFile:
    """PostgreSQL 堆文件读取器。"""

    def __init__(self, path: str, page_size: int = PAGE_SIZE, pg_version: int = 12,
                 is_kingbase: bool = False):
        self.path = path
        self.page_size = page_size
        # PG 主版本：影响 NULL 位图语义（P2-1）与元组头校验（P2-2）
        self.pg_version = int(pg_version) if pg_version else 12
        # 金仓标志：sys_attribute 布局差异见 catalog._pg_attribute_layout
        self.is_kingbase = bool(is_kingbase)
        self.filesize = 0
        self.npages = 0
        self.bad_pages = []
        self._toast = None

    def set_toast(self, toast):
        """关联 TOAST 表文件对象。"""
        self._toast = toast

    # ------------------------------------------------------------------
    # 页面遍历
    # ------------------------------------------------------------------

    def iter_pages(self):
        """逐页产出 (pageno, Page)，坏页跳过但记录。"""
        with open(self.path, "rb") as f:
            self.filesize = os.fstat(f.fileno()).st_size
            self.npages = self.filesize // self.page_size
            for pageno in range(self.npages):
                raw = f.read(self.page_size)
                if len(raw) < self.page_size:
                    break
                page = Page(pageno, raw)
                if not page.has_valid_layout:
                    self.bad_pages.append(pageno)
                    continue
                yield pageno, page

    def iter_tuples(self, include_deleted=False, force=False):
        """遍历整个文件，产出 (pageno, item_index, HeapTuple)。

        include_deleted: 同时产出已删除的元组
        force: 强制遍历（忽略 B+Tree 链，逐页扫描）
        """
        for pageno, page in self.iter_pages():
            for item in page.items:
                if item.flags != ITEMID_NORMAL:
                    continue
                data = page.raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data, pg_version=self.pg_version, is_kingbase=self.is_kingbase)
                except Exception:
                    continue
                if not include_deleted and not tup.is_live:
                    continue
                yield pageno, item.index, tup

    # ------------------------------------------------------------------
    # 数据区扫描模式（金仓兼容）
    # ------------------------------------------------------------------

    def _iter_tuples_scan(self, n_expected_cols=0, col_lengths=None):
        """数据区扫描模式: 在每页的 pd_upper~pd_special 区域按元组头特征定位元组。

        适用于金仓等非标准 PG 分支的 ItemId 格式。
        当 iter_tuples() (ItemId 模式) 无结果时作为回退使用。

        n_expected_cols: 期望的列数（来自表元数据），用于验证元组头
        col_lengths: 列长度信息 [(attlen, is_varlena), ...]，用于计算元组大小以精确跳过
        """
        for pageno, page in self.iter_pages():
            pd_upper = page.header.get("upper", 0)
            pd_special = page.header.get("special", self.page_size)
            if pd_upper < page.header_size or pd_upper >= pd_special:
                continue

            raw = page.raw
            pos = pd_upper
            while pos + HEAP_TUPLE_HEADER_SIZE <= pd_special:
                # 读取元组头关键字字段
                t_xmin = struct.unpack_from("<I", raw, pos)[0]
                t_infomask2 = struct.unpack_from("<H", raw, pos + 18)[0]
                t_infomask = struct.unpack_from("<H", raw, pos + 20)[0]
                t_hoff = raw[pos + 22]
                nattrs = t_infomask2 & HEAP_NATTS_MASK

                # ---- 验证元组头有效性 ----
                # 1. t_hoff 应在合理范围内 [23, 256]
                if t_hoff < HEAP_TUPLE_HEADER_SIZE or t_hoff > 256:
                    pos += 4
                    continue
                # 2. nattrs 应为正数且不超过 PG 最大列数
                if nattrs == 0 or nattrs > 1600:
                    pos += 4
                    continue
                # 3. 如果知道期望列数，必须精确匹配
                if n_expected_cols and nattrs != n_expected_cols:
                    pos += 4
                    continue
                # 4. xmin 应为合理的事务 ID
                if t_xmin == 0 or t_xmin > 0x7FFFFFFF:
                    pos += 4
                    continue
                # 5. infomask 高字节应有已知标志位
                if t_infomask & 0xFF00 == 0:
                    pos += 4
                    continue

                # 构造 HeapTuple（传入从 pos 到 pd_special 的数据）
                tuple_data = raw[pos:pd_special]
                try:
                    tup = HeapTuple(tuple_data, pg_version=self.pg_version, is_kingbase=self.is_kingbase)
                except Exception:
                    pos += 4
                    continue

                # 二次验证：解析后的值应与原始读取一致
                if tup.t_hoff != t_hoff or tup.nattrs != nattrs:
                    pos += 4
                    continue
                # t_hoff 一致性校验（pg_filedump 同款）:
                # MAXALIGN(23 + BITMAPLEN(natts) [+4 OID]) == t_hoff
                if not tup.is_header_consistent():
                    pos += 4
                    continue

                yield pageno, pos, tup

                # 计算元组实际大小以精确跳过（避免误匹配）
                # PG 使用 MAXALIGN(8) 对齐元组起点
                if col_lengths:
                    actual_size = _calculate_tuple_size(tup, col_lengths)
                    next_pos = (actual_size + 7) & ~7  # MAXALIGN(8)
                else:
                    # 无列信息时保守跳过
                    est_size = t_hoff + nattrs
                    next_pos = (est_size + 7) & ~7  # MAXALIGN(8)
                if next_pos < 8:
                    next_pos = 8
                pos += next_pos

    # ------------------------------------------------------------------
    # 数据导出
    # ------------------------------------------------------------------

    def dump_rows(self, table_meta, include_deleted=False, only_deleted=False,
                 limit=0, force=False):
        """遍历堆文件，产出行数据字典列表。

        每行: {"ctid": "(blk,off)", "values": [v1,v2,...], "deleted": bool}

        include_deleted=True: 输出已删除+未删除的行
        only_deleted=True: 只输出已删除的行（自动隐含 include_deleted=True）
        两者都为 False: 只输出未删除的行

        策略：统一使用 _extract_fields_direct（无偏移表）提取字段，
        先试 ItemId 标准模式定位元组，若结果全空（或全部为 NULL/
        无效值）则回退到数据区扫描模式——标准模式行先收集、判定
        有效后才输出，避免"全 NULL 表"被两阶段各输出一遍而重复。
        """
        if only_deleted:
            include_deleted = True  # only_deleted 隐含 include_deleted
        toast = self._toast
        col_lengths = _build_col_lengths(table_meta)
        # 元组 nattrs = 全部列（含 dropped），扫描模式校验必须用全列数
        n_expected = len(table_meta.columns)

        # 行是否"有效"：至少一个非空非占位值（全 NULL 行不算）
        def _row_has_valid(r):
            for v in r["values"]:
                if v is not None and v != "" and v != "__TOAST_MISSING__":
                    return True
            return False

        def _build_row(tup, pos_tag):
            deleted = tup.is_deleted  # 真删除（排除 abort/锁/multi）
            if not tup.is_live and not deleted:
                return None  # 插入已回滚等死行，任何模式都不输出
            if not include_deleted and deleted:
                return None
            if only_deleted and not deleted:
                return None
            nulls = tup.get_nulls()
            fields = _extract_fields_direct(
                tup.raw, tup.t_hoff, nulls, col_lengths
            )
            values = _decode_fields(fields, nulls, table_meta, toast)
            return {
                "ctid": f"({pos_tag[0]},{pos_tag[1]})",
                "values": values,
                "deleted": deleted,
            }

        # ---- 阶段 1: 标准 ItemId 模式定位元组（收集后判定再输出）----
        standard_rows = []
        found_standard = False
        has_valid = False
        count = 0
        for pageno, idx, tup in self.iter_tuples(include_deleted=True):
            found_standard = True
            row = _build_row(tup, (pageno, idx))
            if row is None:
                continue
            standard_rows.append(row)
            if _row_has_valid(row):
                has_valid = True
            count += 1
            if limit and count >= limit:
                break

        # 标准模式有有效数据：统一输出收集的行（不切换扫描，避免重复）
        if found_standard and has_valid:
            for row in standard_rows:
                yield row
            return

        # ---- 阶段 2: 数据区扫描模式（金仓回退 / 全 NULL 表）----
        # 注意：阶段 1 收集的行在切扫描时不输出（否则全 NULL 表重复）
        count = 0
        self.bad_pages = []
        for pageno, pos, tup in self._iter_tuples_scan(
            n_expected_cols=n_expected, col_lengths=col_lengths
        ):
            row = _build_row(tup, (pageno, pos))
            if row is None:
                continue
            yield row
            count += 1
            if limit and count >= limit:
                return

    # ------------------------------------------------------------------
    # SQL 输出
    # ------------------------------------------------------------------

    def to_sql(self, table_meta, include_deleted=False, only_deleted=False, limit=0,
               complete_insert=True, replace=False, force=False):
        """生成 INSERT/REPLACE 语句。"""
        verb = "REPLACE INTO" if replace else "INSERT INTO"
        live_cols = [c for c in table_meta.columns if not c.attdropped]
        col_names = [c.name for c in live_cols]
        col_str = f"({', '.join(self._quote(c) for c in col_names)})" if complete_insert else ""
        target = f'"{table_meta.schema}"."{table_meta.relname}"'
        for row in self.dump_rows(table_meta, include_deleted, only_deleted, limit, force):
            vals = row["values"]
            sql_vals = []
            for i, v in enumerate(vals):
                col = live_cols[i] if i < len(live_cols) else None
                if v is None:
                    sql_vals.append("NULL")
                elif v == "__TOAST_MISSING__":
                    sql_vals.append("NULL")
                else:
                    sql_vals.append(self._sql_quote(v, col))
            stmt = f"{verb} {target} {col_str} VALUES ({', '.join(sql_vals)});"
            if row["deleted"]:
                stmt = f"-- DELETED ctid={row['ctid']}\n{stmt}"
            yield stmt

    def to_data(self, table_meta, include_deleted=False, only_deleted=False, limit=0,
                delimiter=",", force=False):
        """生成 COPY CSV 格式数据行（与 PG COPY ... WITH (FORMAT csv, NULL '\\N') 兼容）。

        转义规则（PG COPY csv）：
          - NULL 输出裸 \\N（配合导入时 NULL '\\N' 还原为 NULL）
          - 值中的反斜杠/tab/换行原样保留（csv 无反斜杠转义）
          - 含分隔符/换行/引号的字段用双引号包裹，内部引号双写
          - 字面量恰好为 "\\N" 时也包裹，避免被误判为 NULL
        """
        for row in self.dump_rows(table_meta, include_deleted, only_deleted, limit, force):
            parts = []
            for v in row["values"]:
                if v is None or v == "__TOAST_MISSING__":
                    parts.append("\\N")
                else:
                    raw = str(v)
                    needs_quote = (delimiter in raw or "\n" in raw or "\r" in raw
                                   or '"' in raw or raw == "\\N")
                    if needs_quote:
                        parts.append('"' + raw.replace('"', '""') + '"')
                    else:
                        parts.append(raw)
            yield delimiter.join(parts)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _quote(name: str) -> str:
        return f'"{name}"'

    @staticmethod
    def _sql_quote(value: str, col) -> str:
        """根据列类型决定加不加引号。"""
        # 不加引号的类型：布尔、整数、浮点、OID 系列
        no_quote_oids = {
            16,  # bool
            20,  # int8
            21,  # int2
            23,  # int4
            26,  # oid
            700,  # float4
            701,  # float8
            1700,  # numeric
        }  # 注: xid(28)/cid(29) 走文本输入，PG 无 int→xid 隐式转换
        if col.atttypid in no_quote_oids:
            # float 特殊值（NaN/±Infinity）PG 需要文本字面量，裸标识符非法
            if value in ("NaN", "Infinity", "-Infinity"):
                from .types import sql_string_literal
                return sql_string_literal(value)
            return value
        # 其他用 E'' 安全字面量（转义控制字节，防止换行拆断 INSERT/psql 误判命令）
        from .types import sql_string_literal
        return sql_string_literal(value)


# --------------------------------------------------------------------------
# TOAST 辅助
# --------------------------------------------------------------------------

def _check_external(raw: bytes):
    """检查 varlena 是否是 TOAST 外部引用。返回 (payload, is_ext, ext_info)。

    外联指针即 PG 标准 varattrib_1b_e（0x01 0x12 开头 18B 小端），
    金仓使用同一格式；字段解析见 binary.parse_external_pointer。
    内联压缩（4B 头 tag=0x02）由 types 层解压，此处返回未解压 payload。
    """
    if not raw:
        return (raw, False, None)
    kind, total, poff, plen = varlena_parse(raw, 0)
    if kind == VARLENA_EXTERNAL:
        ext = parse_external_pointer(raw, 0)
        if ext is not None:
            return (b"", True, ext)
        return (raw, False, None)
    if kind == VARLENA_1B:
        return (raw[1:1 + max(total - 1, 0)], False, None)
    if kind in (VARLENA_4B, VARLENA_4B_COMPRESSED):
        return (raw[4:4 + max(total - 4, 0)], False, None)
    # 无法识别：原样返回由上层容错
    return (raw, False, None)


def _rebuild_varlena(payload: bytes) -> bytes:
    """兼容保留：转发到 binary.rebuild_varlena（小端 4B 头）。"""
    return rebuild_varlena(payload)
