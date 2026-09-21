# -*- coding: utf-8 -*-
# version: 2.8
"""
pg2sql.catalog
表结构元数据管理。

两种来源：
1. JSON 元数据文件（推荐）：由 export_meta.sql 在在线实例导出，
   包含表名、列名、列类型 oid、typmod、是否可空、主键、attalign/attbyval 等。
2. 离线解析 PG 数据目录的 pg_class/pg_attribute/pg_type 堆文件（尽力而为）。

v2.0（评审 P1-4 修复）：
  - PG 版本感知：从数据目录 PG_VERSION 文件探测主版本；系统目录解析
    按版本使用精确布局（PG16 已实证 108B 固定布局；PG14/15 与 12/13
    各差一个 attcompression/attstattarget 移位；PG<=11 OID 在 t_hoff-4）
  - 系统目录 = [OID 首列] + 用户列（PG12+，OID 位于 t_hoff 数据区首 4B，
    实测 pg_type bool 行 OID=16 在 t_data[0:4]）；旧布局常量全部补 OID 首列
  - pg_attribute 提取 attalign/attbyval/attstorage/attisdropped，
    供 heapfile 按 attalign 逐列对齐（P1-3）与 dropped 列占位（P1-5）
  - JSON 元数据 type_oid/len/attnum/typmod 显式 int 强转兜底（P1-1）
"""
import json
import os
import struct

from .page import Page, PAGE_SIZE, detect_page_size
from .tuple import HeapTuple
from .types import (
    TYPE_NAMES, BPCHAROID, VARCHAROID, NUMERICOID, BITOID, VARBITOID,
    TIMEOID, TIMETZOID, TIMESTAMPOID, TIMESTAMPTZOID,
)

# 关键系统目录 OID
PG_CLASS_OID = 1259
PG_ATTRIBUTE_OID = 1249
PG_TYPE_OID = 1247
PG_NAMESPACE_OID = 2615

# 系统目录在数据目录中的文件位置
PG_CLASS_RELFILE = 1259
PG_ATTRIBUTE_RELFILE = 1249
PG_TYPE_RELFILE = 1247
PG_ENUM_RELFILE = 3501  # pg_enum（各版本固定）

# pg_attribute 常量
ATTNUM_DROPPED = -1  # atttypmod<0 或 attnum<0 表示已删除列


def detect_pg_version(datadir: str) -> int:
    """从数据目录（或其上级）的 PG_VERSION / SYS_VERSION 文件探测主版本号。

    返回 int 主版本（如 16）；找不到返回 0（未知，调用方降级）。
    PG_VERSION 内容形如 "16.4\n"；金仓 KingbaseES 无 PG_VERSION，
    用 SYS_VERSION（内容形如 "12\n"，即其继承的 PG 内核主版本）。
    """
    d = os.path.abspath(datadir)
    for _ in range(6):
        for name in ("PG_VERSION", "SYS_VERSION"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        ver = f.read().strip()
                    major = ver.split(".")[0]
                    return int(major)
                except Exception:
                    return 0
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return 0


def is_kingbase_datadir(datadir: str) -> bool:
    """判断数据目录是否为金仓 KingbaseES（存在 SYS_VERSION 而非 PG_VERSION）。

    金仓与标准 PG 的系统目录存在实测差异（见 _pg_attribute_layout），
    必须显式识别后再选择布局，不能仅凭主版本（12）判断。
    """
    d = os.path.abspath(datadir)
    for _ in range(6):
        pgv = os.path.join(d, "PG_VERSION")
        sysv = os.path.join(d, "SYS_VERSION")
        if os.path.isfile(sysv):
            return True
        if os.path.isfile(pgv):
            return False
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return False

# ----------------------------------------------------------------------
# 系统目录列布局（版本感知，P1-4）
# 布局元组: (attlen, is_varlena, attalign)
# ----------------------------------------------------------------------

# pg_class 前 16 个用户列（各版本 PG11-16 前 16 列一致）
_PG_CLASS_COLS_16 = [
    (64, False, "c"),  # relname
    (4, False, "i"),   # relnamespace
    (4, False, "i"),   # reltype
    (4, False, "i"),   # reloftype
    (4, False, "i"),   # relowner
    (4, False, "i"),   # relam
    (4, False, "i"),   # relfilenode
    (4, False, "i"),   # reltablespace
    (4, False, "i"),   # relpages
    (4, False, "i"),   # reltuples：官方 pg_class.reltuples 为 float4（4B）；
                       #  原实现误作 float8，导致 relallvisible 起全部错位、
                       #  relkind 恒读 0x00（PG17.5 415 行与金仓实测均暴露）
    (4, False, "i"),   # relallvisible
    (4, False, "i"),   # reltoastrelid
    (1, False, "c"),   # relhasindex
    (1, False, "c"),   # relisshared
    (1, False, "c"),   # relpersistence
    (1, False, "c"),   # relkind
]

# pg_namespace 用户列（各版本一致）
_PG_NAMESPACE_COLS = [
    (64, False, "c"),  # nspname
    (4, False, "i"),   # nspowner
]

# PG18+：relallvisible 后新增 relallfrozen(int4)，relkind 顺延为用户列 17
_PG_CLASS_COLS_18 = _PG_CLASS_COLS_16[:11] + [(4, False, "i")] + _PG_CLASS_COLS_16[11:]

# pg_database 用户列（datname 第一列）
_PG_DATABASE_COLS = [
    (64, False, "c"),  # datname
    (4, False, "i"),   # datdba
]


def _with_oid(cols):
    """PG12+ 系统目录 = [OID 4B 首列] + 用户列（OID 位于 t_hoff 数据区首 4B）。"""
    return [(4, False, "i")] + list(cols)


def _pg_attribute_layout(version: int, is_kingbase: bool = False):
    """pg_attribute 全列布局（含末尾 4 个可空 varlena 列）。

    版本差异（对照各版 src/include/catalog/pg_attribute.h）：
      PG16:   attstattarget 移到 attinhcount 之后、attcollation 之前
      PG14+: 有 attcompression（在 attstorage 之后）
      PG12+: 有 attgenerated
      PG<=11: attstorage 在 attalign 之前、无 attgenerated/attcompression
      KingbaseES（内核 PG12，实测 base/24576/1249 页 42 行态）：
        与 PG12 有三处不同——
        1) attndims 为 int4（4B，PG12 为 int2）
        2) attstorage 在 attalign 之前（PG<=11 风格）
        3) 无 attcompression/attgenerated 之外的其他额外固定列
        固定部分实测 108B；attcacheoff 在存储中为 -1。
    """
    if is_kingbase:
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (4, False, "i"), (2, False, "s"), (2, False, "s"),    # stattarg len num
            (4, False, "i"), (4, False, "i"), (4, False, "i"),    # ndims(int4!) cacheoff typmod
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # byval storage align
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # notnull hasdef hasmissing
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # identity generated isdropped
            (1, False, "c"), (2, False, "s"), (4, False, "i"),    # islocal inhcount collation
        ]
        return fixed + [(-1, True, "i")] * 4
    if version >= 18:
        # PG18: 删除 attcacheoff；atttypmod(int4) 移至 attnum 后、attndims(int2) 前；
        #       attstattarget(int2) 在 attcollation 后（同 PG17 位置）
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (2, False, "s"), (2, False, "s"), (4, False, "i"),    # len num typmod
            (2, False, "s"), (1, False, "c"), (1, False, "c"),    # ndims byval align
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # storage compression notnull
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # hasdef hasmissing identity
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # generated isdropped islocal
            (2, False, "s"), (4, False, "i"), (2, False, "s"),    # inhcount collation stattarg
        ]
    elif version == 17:
        # PG17: attstattarget 移到 attcollation 之后（与 PG16 交换位置）
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (2, False, "s"), (2, False, "s"), (4, False, "i"),    # len num cacheoff
            (4, False, "i"), (2, False, "s"), (1, False, "c"),    # typmod ndims byval
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # align storage compression
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # notnull hasdef hasmissing
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # identity generated isdropped
            (1, False, "c"), (2, False, "s"), (4, False, "i"),    # islocal inhcount collation
            (2, False, "s"),                                      # stattarg
        ]
    elif version >= 16:
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (2, False, "s"), (2, False, "s"), (4, False, "i"),    # len num cacheoff
            (4, False, "i"), (2, False, "s"), (1, False, "c"),    # typmod ndims byval
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # align storage compression
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # notnull hasdef hasmissing
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # identity generated isdropped
            (1, False, "c"), (2, False, "s"), (2, False, "s"),    # islocal inhcount stattarg
            (4, False, "i"),                                      # collation
        ]
    elif version >= 14:
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (4, False, "i"), (2, False, "s"), (2, False, "s"),    # stattarg len num
            (2, False, "s"), (4, False, "i"), (4, False, "i"),    # ndims cacheoff typmod
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # byval align storage
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # compression notnull hasdef
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # hasmissing identity generated
            (1, False, "c"), (1, False, "c"), (4, False, "i"),    # isdropped islocal inhcount(int4!)
            (4, False, "i"),                                      # collation
        ]
    elif version >= 12:
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (4, False, "i"), (2, False, "s"), (2, False, "s"),    # stattarg len num
            (2, False, "s"), (4, False, "i"), (4, False, "i"),    # ndims cacheoff typmod
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # byval storage align（PG12/13 实测：storage 在 align 前）
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # notnull hasdef hasmissing
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # identity generated isdropped
            (1, False, "c"), (4, False, "i"), (4, False, "i"),    # islocal inhcount(int4!) collation
        ]
    else:
        # PG<=11（尽力而为：attstorage 在 attalign 前、无 attgenerated）
        fixed = [
            (4, False, "i"), (64, False, "c"), (4, False, "i"),   # relid name typid
            (4, False, "i"), (2, False, "s"), (2, False, "s"),    # stattarg len num
            (2, False, "s"), (4, False, "i"), (4, False, "i"),    # ndims cacheoff typmod
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # byval storage align
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # notnull hasdef hasmissing
            (1, False, "c"), (1, False, "c"), (1, False, "c"),    # identity isdropped islocal
            (2, False, "s"), (4, False, "i"),                     # inhcount collation
        ]
    # 末尾 4 个可空 varlena: attmissingval(12+)、attacl、attoptions、attfdwoptions
    varlena_tail = [(-1, True, "i")] * 4
    if version < 12:
        # PG<=11 无 attmissingval
        varlena_tail = [(-1, True, "i")] * 3
    return fixed + varlena_tail


# pg_attribute 字段索引（按版本，供快速访问）
def _attr_idx(version: int, is_kingbase: bool = False):
    """返回 {字段名: 列索引}。"""
    if is_kingbase:
        # 金仓实测（V8 PG8.4 代际与 V9 PG12 代际同用扩展布局，见 _pg_attribute_layout）：
        #   attstorage 在 attalign 前；固定区为
        #   relid name typid stattarg len num ndims(int4) cacheoff typmod byval
        #   storage align notnull hasdef hasmissing identity generated isdropped
        #   islocal inhcount collation —— 故 attisdropped=17、attcollation=20。
        # 曾用 16/19（PG12 标准位），V8 dropped 列因此未识别而泄漏进 DDL。
        return dict(attrelid=0, attname=1, atttypid=2, attstattarget=3, attlen=4,
                    attnum=5, attndims=6, attcacheoff=7, atttypmod=8, attbyval=9,
                    attstorage=10, attalign=11, attnotnull=12, attisdropped=17,
                    attcollation=20)
    if version >= 18:
        return dict(attrelid=0, attname=1, atttypid=2, attlen=3, attnum=4,
                    atttypmod=5, attndims=6, attbyval=7, attalign=8,
                    attstorage=9, attnotnull=11, attisdropped=16,
                    attstattarget=20, attcollation=19)
    if version == 17:
        return dict(attrelid=0, attname=1, atttypid=2, attlen=3, attnum=4,
                    attcacheoff=5, atttypmod=6, attndims=7, attbyval=8,
                    attalign=9, attstorage=10, attnotnull=12, attisdropped=17,
                    attstattarget=21, attcollation=20)
    if version >= 16:
        return dict(attrelid=0, attname=1, atttypid=2, attlen=3, attnum=4,
                    attcacheoff=5, atttypmod=6, attndims=7, attbyval=8,
                    attalign=9, attstorage=10, attnotnull=12, attisdropped=17,
                    attstattarget=20, attcollation=21)
    if version >= 14:
        return dict(attrelid=0, attname=1, atttypid=2, attstattarget=3, attlen=4,
                    attnum=5, attndims=6, attcacheoff=7, atttypmod=8, attbyval=9,
                    attalign=10, attstorage=11, attnotnull=13, attisdropped=18,
                    attcollation=21)
    if version >= 12:
        return dict(attrelid=0, attname=1, atttypid=2, attstattarget=3, attlen=4,
                    attnum=5, attndims=6, attcacheoff=7, atttypmod=8, attbyval=9,
                    attstorage=10, attalign=11, attnotnull=12, attisdropped=17,
                    attcollation=20)
    return dict(attrelid=0, attname=1, atttypid=2, attstattarget=3, attlen=4,
                attnum=5, attndims=6, attcacheoff=7, atttypmod=8, attbyval=9,
                attstorage=10, attalign=11, attnotnull=12, attisdropped=16,
                attcollation=19)


class AttrRow:
    """pg_attribute 一行的解析结果（字段名访问，避免索引魔法）。"""

    __slots__ = ("attrelid", "attname", "atttypid", "attlen", "attnum",
                 "atttypmod", "attbyval", "attalign", "attstorage",
                 "attnotnull", "attisdropped", "raw_fields")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _attr_fields(tup: HeapTuple, version: int, is_kingbase: bool = False):
    """提取 pg_attribute 元组字段 → AttrRow；失败返回 None。

    使用版本感知全列布局（含末尾可空 varlena），并校验 attalign/attstorage
    语义，防止布局错位时误解析。
    """
    try:
        layout = _pg_attribute_layout(version, is_kingbase)
        fields = tup.get_fields(layout)
        if not fields or len(fields) < 9:
            return None
        idx = _attr_idx(version, is_kingbase)
        # 语义校验：attalign ∈ c/s/i/d、attstorage ∈ p/e/m/x，避免错位
        aalign = fields[idx["attalign"]]
        astorage = fields[idx["attstorage"]]
        if not aalign or aalign[:1] not in (b"c", b"s", b"i", b"d"):
            return None
        if not astorage or astorage[:1] not in (b"p", b"e", b"m", b"x"):
            return None

        def _u32(i):
            f = fields[i]
            return struct.unpack("<I", f[:4])[0] if f and len(f) >= 4 else 0

        def _i16(i):
            f = fields[i]
            return struct.unpack("<h", f[:2])[0] if f and len(f) >= 2 else 0

        def _i32(i):
            f = fields[i]
            return struct.unpack("<i", f[:4])[0] if f and len(f) >= 4 else -1

        def _c(i):
            f = fields[i]
            return f[:1].decode("latin-1") if f else ""

        def _b(i):
            """系统目录 bool 字段：磁盘为 C bool（0x01=true / 0x00=false），
            兼容 ASCII 't'/'f' 形式（评审 P1-5 实证 dropped 行 attisdropped=0x01）。"""
            f = fields[i]
            if not f:
                return False
            v = f[0]
            return v in (1, 0x74, ord("t"))

        attname = fields[idx["attname"]]
        name = attname.split(b"\x00")[0].decode("utf-8", errors="replace") if attname else ""
        return AttrRow(
            attrelid=_u32(idx["attrelid"]),
            attname=name,
            atttypid=_u32(idx["atttypid"]),
            attlen=_i16(idx["attlen"]),
            attnum=_i16(idx["attnum"]),
            atttypmod=_i32(idx["atttypmod"]),
            attbyval=_b(idx["attbyval"]),
            attalign=_c(idx["attalign"]),
            attstorage=_c(idx["attstorage"]),
            attnotnull=_b(idx["attnotnull"]),
            attisdropped=_b(idx["attisdropped"]),
            raw_fields=fields,
        )
    except Exception:
        return None


def _class_fields(tup: HeapTuple, version: int):
    """提取 pg_class 元组 → (oid, relname, relnamespace, relfilenode, relkind) 或 None。

    PG18+：布局 = [OID 4B] + 用户 17 列（relallvisible 后新增 relallfrozen，
            relkind 为用户列 17 / fields[17]）。
    PG12-17：布局 = [OID 4B] + 用户 16 列（relkind 在最后一列 fields[16]）。
    PG<=11：OID 在 t_hoff-4，用户列从 t_hoff 起。
    """
    try:
        if version >= 18:
            layout = _with_oid(_PG_CLASS_COLS_18)
            fields = tup.get_fields(layout)
            # 至少需覆盖 oid..relfilenode（索引 0-7）
            if not fields or len(fields) < 8:
                return None
            oid_f = fields[0]
            if not oid_f or len(oid_f) < 4:
                return None
            oid = struct.unpack("<I", oid_f[:4])[0]
            relname_f = fields[1]
            relnamespace_f = fields[2]
            relfilenode_f = fields[7]
            relkind_f = fields[17] if len(fields) > 17 else None
        elif version >= 12:
            layout = _with_oid(_PG_CLASS_COLS_16)
            fields = tup.get_fields(layout)
            # 至少需覆盖 oid..relfilenode（索引 0-7）；relkind 缺失时默认 'r'
            if not fields or len(fields) < 8:
                return None
            oid_f = fields[0]
            if not oid_f or len(oid_f) < 4:
                return None
            oid = struct.unpack("<I", oid_f[:4])[0]
            relname_f = fields[1]
            relnamespace_f = fields[2]
            relfilenode_f = fields[7]
            relkind_f = fields[16] if len(fields) > 16 else None
        else:
            fields = tup.get_fields(_PG_CLASS_COLS_16)
            if not fields or len(fields) < 14:
                return None
            oid = tup.get_oid()
            relname_f = fields[0]
            relnamespace_f = fields[1]
            relfilenode_f = fields[6]
            relkind_f = fields[15] if len(fields) > 15 else None
        if not relname_f:
            return None
        relname = relname_f.split(b"\x00")[0].decode("utf-8", errors="replace")
        relnamespace = struct.unpack("<I", relnamespace_f[:4])[0] if relnamespace_f and len(relnamespace_f) >= 4 else 0
        relfilenode = struct.unpack("<I", relfilenode_f[:4])[0] if relfilenode_f and len(relfilenode_f) >= 4 else 0
        if relfilenode == 0:
            relfilenode = oid
        relkind = "r"
        if relkind_f and len(relkind_f) >= 1:
            relkind = chr(relkind_f[0])
        return (oid, relname, relnamespace, relfilenode, relkind)
    except Exception:
        return None


def _ns_fields(tup: HeapTuple, version: int):
    """提取 pg_namespace 元组 → (oid, nspname) 或 None。"""
    try:
        if version >= 12:
            layout = _with_oid(_PG_NAMESPACE_COLS)
        else:
            layout = _PG_NAMESPACE_COLS
        fields = tup.get_fields(layout)
        if not fields:
            return None
        if version >= 12:
            oid_f, name_f = fields[0], fields[1]
        else:
            oid_f, name_f = b"", fields[0]
        oid = struct.unpack("<I", oid_f[:4])[0] if oid_f and len(oid_f) >= 4 else tup.get_oid()
        name = name_f.split(b"\x00")[0].decode("utf-8", errors="replace") if name_f else ""
        if not name:
            return None
        return (oid, name)
    except Exception:
        return None


class Column:
    __slots__ = ("name", "atttypid", "attlen", "attnum", "typmod", "notnull",
                 "attdropped", "attalign", "attbyval", "attstorage")

    def __init__(self, name, atttypid, attlen, attnum, typmod, notnull=False,
                 attdropped=False, attalign=None, attbyval=True, attstorage="x"):
        self.name = name
        self.atttypid = atttypid
        self.attlen = attlen  # 固定长度类型为正数，varlena 为 -1，cstring 为 -2
        self.attnum = attnum
        self.typmod = typmod
        self.notnull = notnull
        self.attdropped = attdropped
        # P1-3: 列对齐/传值/存储属性（磁盘布局按 attalign 推进）
        self.attalign = attalign
        self.attbyval = attbyval
        self.attstorage = attstorage


class TableMeta:
    """一张表的元数据。"""

    def __init__(self, dbname, schema, relname, relfilenode, columns, relkind="r",
                 hastoast=False, toastrelid=0, primary_key=None, type_names=None,
                 role_map=None):
        self.dbname = dbname
        self.schema = schema
        self.relname = relname
        self.relfilenode = relfilenode
        self.columns = columns  # List[Column]
        self.relkind = relkind  # r=普通表
        self.hastoast = hastoast
        self.toastrelid = toastrelid
        self.primary_key = primary_key or []
        # 类型名映射：默认内置 PG 名称；离线目录模式可注入 pg_type 实表映射
        # （金仓含 PG 没有的类型，如 4659=int16、1034=_aclitem）
        self.type_names = dict(TYPE_NAMES)
        if type_names:
            self.type_names.update(type_names)
        # 角色名映射（1260）：aclitem[] 列解码用
        self.role_map = dict(role_map or {})

    @property
    def full_name(self):
        return f"{self.schema}.{self.relname}"

    def col_type_sql(self, col: Column) -> str:
        """列的 SQL 类型表示（含 typmod）"""
        base = self.type_names.get(col.atttypid)
        if base is None:
            base = f"oid:{col.atttypid}"
        if col.atttypid in (BPCHAROID, VARCHAROID):
            # typmod = 长度 + 4
            if col.typmod and col.typmod >= 4:
                return f"{base}({col.typmod - 4})"
            return base
        if col.atttypid == NUMERICOID:
            if col.typmod and col.typmod >= 4:
                tmp = col.typmod - 4
                prec = (tmp >> 16) & 0xFFFF
                scale = tmp & 0xFFFF
                return f"numeric({prec},{scale})"
            return "numeric"
        if col.atttypid in (BITOID, VARBITOID):
            if col.typmod:
                return f"{base}({col.typmod})"
            return base
        if col.atttypid in (TIMEOID, TIMETZOID, TIMESTAMPOID, TIMESTAMPTZOID):
            if col.typmod and col.typmod >= 0:
                return f"{base}({col.typmod})"
            return base
        return base

    def generate_ddl(self) -> str:
        """生成 CREATE TABLE 语句（枚举列前置 CREATE TYPE 定义）。"""
        from .types import ENUM_MAP
        pre = ""
        seen = set()
        for c in self.columns:
            if c.attdropped:
                continue
            m = ENUM_MAP.get(c.atttypid)
            if m is None or c.atttypid in seen:
                continue
            seen.add(c.atttypid)
            tname = self.col_type_sql(c)
            labels = ", ".join(
                "'%s'" % lab.replace("'", "''") for lab in m.values())
            pre += f'CREATE TYPE "{tname}" AS ENUM ({labels});\n'
        cols = []
        for c in self.columns:
            if c.attdropped:
                continue
            s = f'  "{c.name}" {self.col_type_sql(c)}'
            if c.notnull:
                s += " NOT NULL"
            cols.append(s)
        if self.primary_key:
            pks = ", ".join(f'"{p}"' for p in self.primary_key)
            cols.append(f"  PRIMARY KEY ({pks})")
        return (
            pre
            + f'CREATE TABLE "{self.schema}"."{self.relname}" (\n'
            + ",\n".join(cols)
            + "\n);"
        )


# --------------------------------------------------------------------------
# JSON 元数据加载
# --------------------------------------------------------------------------

def load_meta_json(path: str) -> dict:
    """加载 export_meta.sql 导出的 JSON。

    格式：
    {
      "database": "db1",
      "pg_version": 160000,   // 可选：export_meta.sql 导出（P2-1 位图门控）
      "tables": [
        {
          "schema": "public", "table": "t1", "relfilenode": 16384,
          "toastrelid": 16387,
          "primary_key": ["id"],
          "columns": [
            {"name": "id", "type_oid": 23, "len": 4, "attnum": 1, "typmod": -1,
             "notnull": true, "dropped": false, "attalign": "i", "attbyval": true},
            ...
          ]
        }
      ]
    }
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tables = {}
    dbname = data.get("database", "unknown")

    def _to_int(v, default=0):
        # P1-1: 旧版导出脚本会把数值序列化成字符串，统一强转
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    for t in data.get("tables", []):
        cols = []
        for c in t.get("columns", []):
            cols.append(
                Column(
                    name=c["name"],
                    atttypid=_to_int(c.get("type_oid"), 0),
                    attlen=_to_int(c.get("len"), -1),
                    attnum=_to_int(c.get("attnum"), 0),
                    typmod=_to_int(c.get("typmod"), -1),
                    notnull=bool(c.get("notnull", False)),
                    attdropped=bool(c.get("dropped", False)),
                    attalign=c.get("attalign") or None,
                    attbyval=bool(c.get("attbyval", True)),
                    attstorage=c.get("attstorage") or "x",
                )
            )
        tm = TableMeta(
            dbname=dbname,
            schema=t.get("schema", "public"),
            relname=t["table"],
            relfilenode=_to_int(t.get("relfilenode"), 0),
            columns=cols,
            toastrelid=_to_int(t.get("toastrelid"), 0),
            primary_key=t.get("primary_key", []),
        )
        tables[tm.full_name] = tm
        # 也按 relfilenode 索引（仅当与 full_name 不同时）
        if tm.relfilenode:
            key = str(tm.relfilenode)
            if key not in tables:
                tables[key] = tm
    # P2-1: 元数据记录 PG 主版本（server_version_num，如 160004）
    return {
        "database": dbname,
        "pg_version": _to_int(data.get("pg_version", 160000), 160000) or 160000,
        "tables": tables,
    }


# --------------------------------------------------------------------------
# 离线解析 PG 数据目录（尽力而为）
# --------------------------------------------------------------------------

def _read_pages(path: str):
    """按页切分文件，返回 [(pageno, raw)]（自动探测页大小，支持 16KB/32KB）"""
    out = []
    size = os.path.getsize(path)
    if size == 0:
        return out
    with open(path, "rb") as f:
        ps = detect_page_size(f.read(130)) or PAGE_SIZE
        f.seek(0)
        pageno = 0
        while True:
            raw = f.read(ps)
            if not raw:
                break
            if len(raw) < ps:
                break
            out.append((pageno, raw))
            pageno += 1
    return out


def _iter_tuples(path: str, pg_version: int = 12, is_kingbase: bool = False):
    """遍历堆文件产出 (pageno, offset, HeapTuple)。

    pg_version: PG 主版本（默认 12+），影响 OID 位置与 NULL 位图语义。
    is_kingbase: 金仓标志，穿透到 HeapTuple（布局差异见 _pg_attribute_layout）。
    只产出可见（live）元组：死元组（已删除/回滚）不得参与目录解析，
    否则未 VACUUM 的删除历史会生成幽灵表/幽灵列（金仓实测多版本残留）。
    """
    for pageno, raw in _read_pages(path):
        page = Page(pageno, raw)
        if not page.has_valid_layout:
            continue
        for item in page.items:
            if item.flags != 1:  # 只处理 normal
                continue
            data = raw[item.off : item.off + item.len]
            try:
                tup = HeapTuple(data, pg_version=pg_version, is_kingbase=is_kingbase)
            except Exception:
                continue
            if not tup.is_live:
                continue
            yield pageno, item.index, tup


def load_enum_map(db_dir: str, version: int = 0):
    """读 base/{db}/3501（pg_enum）构建 {枚举类型 oid: {成员 oid: 标签}}，
    注入 types.ENUM_MAP 供枚举列解码。文件缺失（如金仓无 pg_enum）时清空映射。

    pg_enum 布局各版本一致（PG12-18）：
      [OID 4B][enumtypid 4B][enumsortorder float4 4B][enumlabel name 64B]
    """
    from .types import set_enum_map
    from .binary import cstring
    path = os.path.join(db_dir, str(PG_ENUM_RELFILE))
    if not os.path.isfile(path):
        set_enum_map({})
        return
    # pg_enum 是含 oid 用户列的普通表：数据区布局即 [oid][enumtypid][enumsortorder][enumlabel]
    # （勿套 _with_oid，否则 oid 重复、label 偏移错位）
    layout = [(4, False, "i"), (4, False, "i"), (4, False, "f"), (64, False, "c")]
    m = {}
    try:
        for _pn, _off, tup in _iter_tuples(path, version or 12, False):
            f = tup.get_fields(layout)
            if not f or len(f) < 4:
                continue
            enum_typid = struct.unpack("<I", f[1][:4])[0]
            label = cstring(f[3])
            if not label:
                continue
            m.setdefault(enum_typid, {})[struct.unpack("<I", f[0][:4])[0]] = label
    except Exception:
        pass
    set_enum_map(m)


def _db_oid_of(db_dir: str) -> int:
    """从 base/{oid} 目录路径取数据库 OID；非数字目录名返回 0（无 pg_type 映射）。"""
    b = os.path.basename(os.path.abspath(db_dir))
    return int(b) if b.isdigit() else 0


def build_type_name_map(db_dir: str) -> dict:
    """从数据库目录 base/{db_oid}/1247（pg_type/sys_type）构建 {oid: typname}。

    覆盖硬编码 TYPE_NAMES 之外的数据库自定义类型（金仓 int16/_aclitem 等）。
    失败（文件缺失/无法解析）返回空 dict，不阻断主流程。
    """
    type_map = {}
    path = os.path.join(db_dir, "1247")
    if not os.path.isfile(path):
        return type_map
    version = detect_pg_version(db_dir) or 12
    is_kb = is_kingbase_datadir(db_dir)
    from .binary import u32, cstring
    try:
        for _p, _o, tup in _iter_tuples(path, version, is_kb):
            f = tup.get_fields([(4, False, "i"), (64, False, "c")])
            if not f or len(f) < 2:
                continue
            oid = u32(f[0])
            name = cstring(f[1])
            if oid and name:
                type_map[oid] = name
    except Exception:
        return {}
    # 金仓 int16（128 位整数）PG 无对应类型，导入 PG 时用 numeric 承接
    if is_kb and 4659 in type_map:
        type_map[4659] = "numeric"
    return type_map


def build_role_name_map(db_dir: str) -> dict:
    """从数据库目录 base/{oid}/1260（pg_authid/sys_authid）构建 {oid: rolname}。

    供 aclitem[] 列解码（relacl 等）把 grantor/grantee OID 还原为角色名；
    金仓与 PG 的 pg_authid 前两字段均为 [oid(4B), rolname(name 64B)]。
    失败返回空 dict（解码退化为 oid:N 占位）。
    """
    role_map = {}
    # pg_authid 是共享目录（global/1260）；个别库内也可能存在本地副本
    candidates = [os.path.join(db_dir, "1260"),
                  os.path.join(os.path.dirname(os.path.abspath(db_dir)), "..", "global", "1260"),
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(db_dir))), "global", "1260")]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        return role_map
    version = detect_pg_version(db_dir) or 12
    is_kb = is_kingbase_datadir(db_dir)
    from .binary import u32, cstring
    try:
        for _p, _o, tup in _iter_tuples(path, version, is_kb):
            f = tup.get_fields([(4, False, "i"), (64, False, "c")])
            if not f or len(f) < 2:
                continue
            oid = u32(f[0])
            name = cstring(f[1])
            if oid and name:
                role_map[oid] = name
    except Exception:
        return {}
    return role_map


def parse_catalog_offline(datadir: str, db_oid: int = 5) -> dict:
    """离线解析数据目录中的 pg_class / pg_attribute，重建表结构。

    注意：这是尽力而为的实现，复杂场景（>=256 列、combo cid 等）可能不准。
    db_oid: 目标库的 OID（系统库实测：template1=1、template0=4、postgres=5；
            用户库需从 pg_database 查询，如 PG16.4 新建库通常为 16384 起）。
    """
    version = detect_pg_version(datadir) or 16
    is_kb = is_kingbase_datadir(datadir)
    base = os.path.join(datadir, "base", str(db_oid))
    type_map = build_type_name_map(base)
    pg_attribute_path = os.path.join(base, str(PG_ATTRIBUTE_RELFILE))
    pg_class_path = os.path.join(base, str(PG_CLASS_RELFILE))

    if not os.path.exists(pg_attribute_path):
        raise FileNotFoundError(f"pg_attribute not found: {pg_attribute_path}")

    # 先解析 pg_attribute（列信息；版本感知布局，P1-4）
    att_by_rel = {}
    for pageno, offno, tup in _iter_tuples(pg_attribute_path, version, is_kb):
        ar = _attr_fields(tup, version, is_kb)
        if ar is None or ar.attnum <= 0:
            continue
        att_by_rel.setdefault(ar.attrelid, []).append(
            Column(
                ar.attname, ar.atttypid, ar.attlen, ar.attnum, ar.atttypmod,
                notnull=ar.attnotnull, attdropped=ar.attisdropped,
                attalign=ar.attalign, attbyval=ar.attbyval,
                attstorage=ar.attstorage,
            )
        )

    # 解析 pg_class（表名、命名空间与 relfilenode）
    tables = {}
    ns_name = {}
    ns_path = os.path.join(base, str(PG_NAMESPACE_RELFILE))
    if os.path.exists(ns_path):
        for _p, _o, tup in _iter_tuples(ns_path, version, is_kb):
            ns = _ns_fields(tup, version)
            if ns:
                ns_name[ns[0]] = ns[1]
    if os.path.exists(pg_class_path):
        for pageno, offno, tup in _iter_tuples(pg_class_path, version, is_kb):
            try:
                row = _class_fields(tup, version)
                if row is None:
                    continue
                oid, relname, relnamespace, relfilenode, relkind = row
            except Exception:
                continue
            columns = att_by_rel.get(relfilenode or oid, [])
            if not columns:
                continue
            columns.sort(key=lambda c: c.attnum)
            tm = TableMeta(
                dbname=str(db_oid),
                schema=ns_name.get(relnamespace, "public"),
                relname=relname,
                relfilenode=relfilenode or oid,
                columns=columns,
                relkind=relkind,
                type_names=type_map,
                role_map=build_role_name_map(base),
            )
            tables[tm.full_name] = tm
    return {
        "database": str(db_oid),
        "pg_version": version,
        "tables": tables,
    }


# --------------------------------------------------------------------------
# 自动发现表结构（无需 --catalog-json）
# --------------------------------------------------------------------------

# 系统目录 relfilenode 常量（标准 PostgreSQL）
PG_NAMESPACE_RELFILE = 2615

# pg_class 中必然出现的已知系统表名
_KNOWN_PG_CLASS_NAMES = {
    "pg_class", "pg_attribute", "pg_type", "pg_namespace",
    "pg_proc", "pg_index", "pg_constraint", "pg_authid",
    "pg_database", "pg_tablespace", "pg_depend",
}

# pg_database 中必然出现的已知数据库名
_KNOWN_DB_NAMES = {
    "template1", "template0", "postgres", "kingbase", "security",
}


def _detect_sys_file(directory, standard_oid, known_names, label, page_size=8192):
    """在目录中查找系统目录文件。先试标准 OID，找不到则扫描内容。

    返回文件路径，找不到返回 None。
    """
    # 1. 先试标准 OID
    std_path = os.path.join(directory, str(standard_oid))
    if os.path.exists(std_path):
        return std_path

    if not os.path.isdir(directory):
        return None

    # 2. 扫描所有数字命名文件
    for name in sorted(os.listdir(directory)):
        if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
            continue
        if not name.isdigit():
            continue
        fpath = os.path.join(directory, name)
        if not os.path.isfile(fpath) or os.path.getsize(fpath) < page_size:
            continue
        # 检查内容：解析前 2 页，看第一列 name 是否含已知名称
        try:
            match = 0
            checked = 0
            with open(fpath, "rb") as f:
                for _ in range(2):
                    raw = f.read(page_size)
                    if len(raw) < page_size:
                        break
                    page = Page(0, raw, page_size=page_size)
                    if not page.has_valid_layout:
                        continue
                    for item in page.items:
                        if item.flags != 1:
                            continue
                        data = raw[item.off : item.off + item.len]
                        tup = HeapTuple(data)
                        if not tup.is_live:
                            continue
                        fields = tup.get_fields([(64, False)])
                        if not fields or fields[0] is None:
                            continue
                        nm = fields[0].split(b"\x00")[0].decode("utf-8", errors="replace")
                        checked += 1
                        if nm in known_names:
                            match += 1
                        if match >= 2:
                            return fpath
                        if checked > 50:
                            break
        except Exception:
            continue

    return None


def _detect_pg_attribute(directory, standard_oid, target_oid, page_size=8192):
    """探测 pg_attribute 文件。pg_attribute 的特征:
    第一列(attrelid)是 oid，值为表 OID；第二列(attname)是 name 类型。
    检查文件中是否有行的第一列值 == target_oid。
    """
    # 1. 先试标准 OID
    std_path = os.path.join(directory, str(standard_oid))
    if os.path.exists(std_path):
        return std_path

    if not os.path.isdir(directory):
        return None

    # 2. 扫描所有数字命名文件
    for name in sorted(os.listdir(directory)):
        if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
            continue
        if not name.isdigit():
            continue
        fpath = os.path.join(directory, name)
        if not os.path.isfile(fpath) or os.path.getsize(fpath) < page_size:
            continue
        try:
            with open(fpath, "rb") as f:
                raw = f.read(page_size)
                if len(raw) < page_size:
                    continue
                page = Page(0, raw, page_size=page_size)
                if not page.has_valid_layout:
                    continue
                count = 0
                for item in page.items:
                    if item.flags != 1:
                        continue
                    data = raw[item.off : item.off + item.len]
                    tup = HeapTuple(data)
                    if not tup.is_live:
                        continue
                    fields = tup.get_fields([(4, False), (64, False)])
                    if len(fields) < 2:
                        continue
                    # 第一列 attrelid 是 oid (4B)
                    if fields[0] and len(fields[0]) >= 4:
                        attrelid = struct.unpack("<I", fields[0][:4])[0]
                        # 第二列 attname 是 name (64B C string)
                        if fields[1] and len(fields[1]) >= 1:
                            attname = fields[1].split(b"\x00")[0].decode("utf-8", errors="replace")
                            # pg_attribute 应有大量行，且列名是合理的标识符
                            if attrelid == target_oid or count >= 5:
                                return fpath
                            count += 1
                    if count > 100:
                        break
        except Exception:
            continue

    return None


def _probe_page_size(path: str) -> int:
    """从文件页头探测页大小；失败回退 8192。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(130)
        ps = detect_page_size(raw)
        if ps is not None:
            return ps
        size = os.path.getsize(path)
        return size if 0 < size <= 32768 else PAGE_SIZE
    except OSError:
        return PAGE_SIZE


def _probe_dir_page_size(db_dir: str) -> int:
    """从数据库目录内系统目录文件探测页大小；失败回退 8192。"""
    for oid in (1259, 1249, 1247):
        p = os.path.join(db_dir, str(oid))
        if os.path.isfile(p):
            ps = _probe_page_size(p)
            if ps is not None:
                return ps
    return PAGE_SIZE


def auto_discover_meta(data_file_path: str, page_size=None) -> dict:
    """从数据文件路径自动发现表结构。

    两阶段: 先用标准 ItemId 解析，失败则回退到数据区扫描。
    page_size=None 时自动探测（页头 pd_pagesize_version 编码）。
    """
    data_file_path = os.path.abspath(data_file_path)
    filename = os.path.basename(data_file_path)
    db_dir = os.path.dirname(data_file_path)

    if not filename.isdigit():
        raise ValueError(f"数据文件名不是数字 OID: {filename}")

    target_relfilenode = int(filename)

    if page_size is None:
        page_size = _probe_page_size(data_file_path)

    # ===== 阶段 1: 标准 ItemId 解析 =====
    try:
        meta = _auto_discover_meta_standard(data_file_path, db_dir, target_relfilenode, page_size)
        if meta:
            return meta
    except Exception:
        pass

    # ===== 阶段 2: 数据区扫描 =====
    return _auto_discover_meta_scan(db_dir, target_relfilenode, page_size)


def _auto_discover_meta_standard(data_file_path, db_dir, target_relfilenode, page_size):
    """标准 ItemId 模式解析（PG 版本感知，P1-4）。"""
    version = detect_pg_version(db_dir) or 16
    is_kb = is_kingbase_datadir(db_dir)
    _KNOWN_NS_NAMES = {"public", "pg_catalog", "pg_toast", "information_schema"}
    ns_path = _detect_sys_file(db_dir, PG_NAMESPACE_RELFILE, _KNOWN_NS_NAMES, "pg_namespace", page_size)
    ns_map = {}
    if ns_path:
        for _, _, tup in _iter_tuples(ns_path, version, is_kb):
            ns = _ns_fields(tup, version)
            if ns:
                ns_map[ns[0]] = ns[1]

    pg_class_path = _detect_sys_file(db_dir, PG_CLASS_RELFILE, _KNOWN_PG_CLASS_NAMES, "pg_class", page_size)
    if not pg_class_path:
        return None

    target_oid = None
    target_relname = None
    target_namespace = None
    target_relkind = "r"

    for _, _, tup in _iter_tuples(pg_class_path, version, is_kb):
        row = _class_fields(tup, version)
        if row is None:
            continue
        oid, relname, relnamespace, relfilenode, relkind = row
        if relfilenode == target_relfilenode:
            target_oid = oid
            target_relname = relname
            target_namespace = relnamespace
            target_relkind = relkind
            break

    if target_oid is None:
        return None

    schema_name = ns_map.get(target_namespace, "public")

    pg_attribute_path = _detect_pg_attribute(db_dir, PG_ATTRIBUTE_RELFILE, target_oid, page_size)
    if not pg_attribute_path:
        return None

    columns = []
    for _, _, tup in _iter_tuples(pg_attribute_path, version, is_kb):
        ar = _attr_fields(tup, version, is_kb)
        if ar is None or ar.attrelid != target_oid or ar.attnum <= 0:
            continue
        columns.append(Column(
            ar.attname, ar.atttypid, ar.attlen, ar.attnum, ar.atttypmod,
            notnull=ar.attnotnull, attdropped=ar.attisdropped,
            attalign=ar.attalign, attbyval=ar.attbyval, attstorage=ar.attstorage,
        ))

    if not columns:
        return None

    columns.sort(key=lambda c: c.attnum)
    tm = TableMeta(os.path.basename(db_dir), schema_name, target_relname,
                   target_relfilenode, columns, target_relkind,
                   type_names=build_type_name_map(db_dir),
                   role_map=build_role_name_map(db_dir))
    return {"database": tm.dbname, "pg_version": version,
            "tables": {tm.full_name: tm, str(tm.relfilenode): tm}}


def _auto_discover_meta_scan(db_dir, target_relfilenode, page_size):
    """数据区扫描模式解析（金仓兼容）。"""
    # 1. 扫描 pg_namespace
    _KNOWN_NS_NAMES = {"public", "pg_catalog", "pg_toast", "information_schema"}
    ns_path = _detect_sys_file(db_dir, PG_NAMESPACE_RELFILE, _KNOWN_NS_NAMES, "pg_namespace", page_size)
    ns_map = {}
    if ns_path:
        ns_map = _scan_pg_namespace(ns_path, page_size)

    # 2. 扫描 pg_class
    pg_class_path = _detect_sys_file(db_dir, PG_CLASS_RELFILE, _KNOWN_PG_CLASS_NAMES, "pg_class", page_size)
    if not pg_class_path:
        raise FileNotFoundError(f"未在 {db_dir} 中找到 pg_class 文件")

    class_entries = _scan_pg_class(pg_class_path, page_size, version=detect_pg_version(db_dir) or 0)

    target_oid = None
    target_relname = None
    target_namespace = None
    target_relkind = "r"

    for oid, relname, relns, rfn, relkind in class_entries:
        if rfn == target_relfilenode:
            target_oid = oid
            target_relname = relname
            target_namespace = relns
            target_relkind = relkind
            break

    if target_oid is None:
        raise ValueError(f"在 pg_class 中未找到 relfilenode={target_relfilenode} 的表")

    schema_name = ns_map.get(target_namespace, "public")

    # 3. 扫描 pg_attribute
    pg_attribute_path = _detect_pg_attribute(db_dir, PG_ATTRIBUTE_RELFILE, target_oid, page_size)
    if not pg_attribute_path:
        # 回退: 用标准 OID 探测
        pg_attribute_path = os.path.join(db_dir, str(PG_ATTRIBUTE_RELFILE))
        if not os.path.exists(pg_attribute_path):
            raise FileNotFoundError(f"未在 {db_dir} 中找到 pg_attribute 文件")

    columns = _scan_pg_attribute(pg_attribute_path, target_oid, page_size)

    if not columns:
        raise ValueError(f"未在 pg_attribute 中找到 attrelid={target_oid} 的列定义")

    # 4. 构建 TableMeta
    tm = TableMeta(
        dbname=os.path.basename(db_dir),
        schema=schema_name,
        relname=target_relname,
        relfilenode=target_relfilenode,
        columns=columns,
        relkind=target_relkind,
    )
    return {"database": tm.dbname, "tables": {tm.full_name: tm, str(tm.relfilenode): tm}}


def auto_discover_all_tables(db_dir: str, page_size=None) -> dict:
    """从数据库目录自动发现所有用户表的元数据。两阶段: 标准 ItemId → 数据区扫描。

    page_size=None 时自动探测（从目录内系统目录文件页头编码）。
    """
    db_dir = os.path.abspath(db_dir)

    if page_size is None:
        page_size = _probe_dir_page_size(db_dir)

    # ===== 阶段 1: 标准 ItemId 解析 =====
    try:
        result = _auto_discover_all_tables_standard(db_dir, page_size)
        if result and result["tables"]:
            return result
    except Exception:
        pass

    # ===== 阶段 2: 数据区扫描 =====
    return _auto_discover_all_tables_scan(db_dir, page_size)


def _auto_discover_all_tables_standard(db_dir, page_size):
    """标准 ItemId 模式（PG 版本感知，P1-4）。"""
    version = detect_pg_version(db_dir) or 16
    is_kb = is_kingbase_datadir(db_dir)
    _KNOWN_NS_NAMES = {"public", "pg_catalog", "pg_toast", "information_schema"}
    ns_path = _detect_sys_file(db_dir, PG_NAMESPACE_RELFILE, _KNOWN_NS_NAMES, "pg_namespace", page_size)
    ns_map = {}
    if ns_path:
        for _, _, tup in _iter_tuples(ns_path, version, is_kb):
            ns = _ns_fields(tup, version)
            if ns:
                ns_map[ns[0]] = ns[1]

    pg_class_path = _detect_sys_file(db_dir, PG_CLASS_RELFILE, _KNOWN_PG_CLASS_NAMES, "pg_class", page_size)
    class_entries = []
    if pg_class_path:
        for _, _, tup in _iter_tuples(pg_class_path, version, is_kb):
            row = _class_fields(tup, version)
            if row is None:
                continue
            class_entries.append(row)

    if not class_entries:
        return {"database": os.path.basename(db_dir), "tables": {}}

    _detect_target = class_entries[0][0] if class_entries else 0
    pg_attribute_path = _detect_pg_attribute(db_dir, PG_ATTRIBUTE_RELFILE, _detect_target, page_size)
    att_by_rel = {}
    if pg_attribute_path:
        for _, _, tup in _iter_tuples(pg_attribute_path, version, is_kb):
            ar = _attr_fields(tup, version, is_kb)
            if ar is None or ar.attnum <= 0:
                continue
            att_by_rel.setdefault(ar.attrelid, []).append(
                Column(ar.attname, ar.atttypid, ar.attlen, ar.attnum, ar.atttypmod,
                       notnull=ar.attnotnull, attdropped=ar.attisdropped,
                       attalign=ar.attalign, attbyval=ar.attbyval,
                       attstorage=ar.attstorage)
            )

    tables = {}
    for oid, relname, ns_oid, relfilenode, relkind in class_entries:
        cols = att_by_rel.get(oid, [])
        if not cols:
            continue
        cols.sort(key=lambda c: c.attnum)
        schema = ns_map.get(ns_oid, "public")
        tm = TableMeta(os.path.basename(db_dir), schema, relname, relfilenode, cols, relkind,
                       type_names=build_type_name_map(db_dir),
                   role_map=build_role_name_map(db_dir))
        tables[tm.full_name] = tm
        if tm.relfilenode:
            key = str(tm.relfilenode)
            if key not in tables:
                tables[key] = tm

    return {"database": os.path.basename(db_dir), "pg_version": version, "tables": tables}


def _auto_discover_all_tables_scan(db_dir, page_size):
    """数据区扫描模式（金仓兼容）。"""
    _KNOWN_NS_NAMES = {"public", "pg_catalog", "pg_toast", "information_schema"}
    ns_path = _detect_sys_file(db_dir, PG_NAMESPACE_RELFILE, _KNOWN_NS_NAMES, "pg_namespace", page_size)
    ns_map = {}
    if ns_path:
        ns_map = _scan_pg_namespace(ns_path, page_size)

    pg_class_path = _detect_sys_file(db_dir, PG_CLASS_RELFILE, _KNOWN_PG_CLASS_NAMES, "pg_class", page_size)
    class_entries = []
    if pg_class_path:
        class_entries = _scan_pg_class(pg_class_path, page_size, version=detect_pg_version(db_dir) or 0)

    # 一次性扫描 pg_attribute，按 attrelid 分组
    pg_attribute_path = os.path.join(db_dir, str(PG_ATTRIBUTE_RELFILE))
    if not os.path.exists(pg_attribute_path):
        pg_attribute_path = _detect_pg_attribute(db_dir, PG_ATTRIBUTE_RELFILE,
                                                  class_entries[0][0] if class_entries else 0, page_size)

    att_by_rel = _scan_pg_attribute_all(pg_attribute_path, page_size) if pg_attribute_path else {}

    tables = {}
    for oid, relname, ns_oid, relfilenode, relkind in class_entries:
        cols = att_by_rel.get(oid, [])
        if not cols:
            continue
        schema = ns_map.get(ns_oid, "public")
        tm = TableMeta(os.path.basename(db_dir), schema, relname, relfilenode, cols, relkind)
        tables[tm.full_name] = tm
        if tm.relfilenode:
            key = str(tm.relfilenode)
            if key not in tables:
                tables[key] = tm

    return {"database": os.path.basename(db_dir), "tables": tables}


# --------------------------------------------------------------------------
# 数据区扫描（兼容金仓等非标准 ItemId 格式）
# --------------------------------------------------------------------------
# 数据区扫描（兼容金仓等非标准 ItemId 格式）
# --------------------------------------------------------------------------

def _scan_data_region(path, page_size=8192):
    """逐页扫描数据区，返回 [(pageno, pd_upper, pd_special, raw_page), ...]"""
    from .page import Page, PAGE_SIZE

    results = []
    filesize = os.path.getsize(path)
    npages = filesize // page_size
    with open(path, "rb") as f:
        for pageno in range(npages):
            raw = f.read(page_size)
            if len(raw) < page_size:
                break
            page = Page(pageno, raw, page_size=page_size)
            if not page.has_valid_layout:
                continue
            pd_upper = page.header.get("upper", 0)
            pd_special = page.header.get("special", page_size)
            if pd_upper < page.header_size or pd_upper >= pd_special:
                continue
            results.append((pageno, pd_upper, pd_special, raw))
    return results


def _scan_pg_class(path, page_size=8192, version=0):
    """数据区扫描 pg_class，返回 [(oid, relname, relnamespace, relfilenode, relkind), ...]

    version: PG 主版本（0=未知，用 PG12-17 偏移 115；>=18 用 119）。
    """
    import struct as _s
    from .binary import cstring

    results = []
    for pageno, pd_upper, pd_special, raw in _scan_data_region(path, page_size):
        pos = pd_upper
        while pos + 110 <= pd_special:
            oid = _s.unpack_from("<I", raw, pos)[0]
            if oid < 1 or oid > 10000000:
                pos += 4
                continue
            name_bytes = raw[pos + 4 : pos + 68]
            name_str = cstring(name_bytes)
            if not name_str or len(name_str) < 1:
                pos += 4
                continue
            first = name_str[0]
            if not (first.isalpha() or first == '_'):
                pos += 4
                continue
            valid = True
            for ch in name_str:
                if not (ch.isalnum() or ch in ('_', '.', '-', '$')):
                    valid = False
                    break
            if not valid:
                pos += 4
                continue
            name_end = name_str.encode("utf-8")
            if len(name_end) < 64 and name_bytes[len(name_end)] != 0:
                pos += 4
                continue
            # relnamespace (列2, offset 68)
            relns = _s.unpack_from("<I", raw, pos + 68)[0]
            # relfilenode (列7, offset 88)
            rfn = _s.unpack_from("<I", raw, pos + 88)[0]
            # relkind 偏移：PG12-17 = 115（4B oid + 64B relname + 11*4B + 3B 布尔）；
            # PG18+ = 119（relallvisible 后新增 relallfrozen int4）
            rk_off = 119 if version >= 18 else 115
            relkind = "r"
            if pos + rk_off < pd_special:
                relkind = chr(raw[pos + rk_off])
            results.append((oid, name_str, relns, rfn if rfn else oid, relkind))
            pos += 4
    return results


def _scan_pg_namespace(path, page_size=8192):
    """数据区扫描 pg_namespace，返回 {oid: nspname}"""
    import struct as _s
    from .binary import cstring

    result = {}
    for pageno, pd_upper, pd_special, raw in _scan_data_region(path, page_size):
        pos = pd_upper
        while pos + 68 <= pd_special:
            oid = _s.unpack_from("<I", raw, pos)[0]
            if oid < 1 or oid > 10000000:
                pos += 4
                continue
            name_bytes = raw[pos + 4 : pos + 68]
            name_str = cstring(name_bytes)
            if not name_str:
                pos += 4
                continue
            first = name_str[0]
            if not (first.isalpha() or first == '_'):
                pos += 4
                continue
            valid = True
            for ch in name_str:
                if not (ch.isalnum() or ch in ('_',)):
                    valid = False
                    break
            if not valid:
                pos += 4
                continue
            name_end = name_str.encode("utf-8")
            if len(name_end) < 64 and name_bytes[len(name_end)] != 0:
                pos += 4
                continue
            if oid not in result:
                result[oid] = name_str
            pos += 4
    return result


def _scan_pg_attribute(path, target_oid, page_size=8192):
    """数据区扫描 pg_attribute，返回 target_oid 对应的 [Column]。

    金仓 pg_attribute 列顺序:
      attrelid(oid,4B) attname(name,64B) atttypid(oid,4B) attcollation(oid,4B)
      attlen(int2,2B) attnum(int2,2B) attcacheoff(int4,4B)
      额外字段(int4,4B,值恒-1,可能为attstattarget)
      atttypmod(int4,4B) attbyval(bool,1B) attalign(char,1B)
      attstorage(char,1B) attnotnull(bool,1B) ...

    模式 A (带 OID 在数据区开头): [OID 4B] + 上述
    模式 B (无 OID): 上述
    """
    import struct as _s
    from .binary import cstring

    columns = []
    seen_attnums = set()

    for pageno, pd_upper, pd_special, raw in _scan_data_region(path, page_size):
        pos = pd_upper
        while pos + 80 <= pd_special:
            # 模式 A: [OID 4B][attrelid 4B][attname 64B][atttypid 4B][attcollation 4B][attlen 2B][attnum 2B]
            attrelid_a = _s.unpack_from("<I", raw, pos + 4)[0]
            name_a = cstring(raw[pos + 8 : pos + 72])

            # 模式 B: [attrelid 4B][attname 64B][atttypid 4B][attcollation 4B][attlen 2B][attnum 2B]
            attrelid_b = _s.unpack_from("<I", raw, pos)[0]
            name_b = cstring(raw[pos + 4 : pos + 68])

            matched = False

            # 模式 A: attlen 在 pos+80, attnum 在 pos+82, attcacheoff 在 pos+84,
            #         额外字段在 pos+88, atttypmod 在 pos+92, attnotnull 在 pos+99
            if attrelid_a == target_oid and name_a and len(name_a) > 0:
                first = name_a[0]
                if first.isalpha() or first == '_':
                    if pos + 84 <= pd_special:
                        atttypid = _s.unpack_from("<I", raw, pos + 72)[0]
                        attlen = _s.unpack_from("<h", raw, pos + 80)[0]
                        attnum = _s.unpack_from("<h", raw, pos + 82)[0]
                        if 1 <= attnum <= 1000 and attnum not in seen_attnums:
                            atttypmod = _s.unpack_from("<i", raw, pos + 92)[0] if pos + 96 <= pd_special else -1
                            notnull = False
                            if pos + 99 <= pd_special:
                                notnull = raw[pos + 99] == 1
                            columns.append(Column(name_a, atttypid, attlen, attnum, atttypmod, notnull))
                            seen_attnums.add(attnum)
                            matched = True

            # 模式 B: attlen 在 pos+76, attnum 在 pos+78, attcacheoff 在 pos+80,
            #         额外字段在 pos+84, atttypmod 在 pos+88, attnotnull 在 pos+95
            if not matched and attrelid_b == target_oid and name_b and len(name_b) > 0:
                first = name_b[0]
                if first.isalpha() or first == '_':
                    if pos + 80 <= pd_special:
                        atttypid = _s.unpack_from("<I", raw, pos + 68)[0]
                        attlen = _s.unpack_from("<h", raw, pos + 76)[0]
                        attnum = _s.unpack_from("<h", raw, pos + 78)[0]
                        if 1 <= attnum <= 1000 and attnum not in seen_attnums:
                            atttypmod = _s.unpack_from("<i", raw, pos + 88)[0] if pos + 92 <= pd_special else -1
                            notnull = False
                            if pos + 95 <= pd_special:
                                notnull = raw[pos + 95] == 1
                            columns.append(Column(name_b, atttypid, attlen, attnum, atttypmod, notnull))
                            seen_attnums.add(attnum)
                            matched = True

            pos += 4

    columns.sort(key=lambda c: c.attnum)
    return columns


def _scan_pg_attribute_all(path, page_size=8192):
    """一次性扫描 pg_attribute，按 attrelid 分组返回 {attrelid: [Column]}。

    比逐表调用 _scan_pg_attribute 快 N 倍（只遍历一次文件）。
    """
    import struct as _s
    from .binary import cstring

    result = {}
    seen = {}  # attrelid -> set(attnum)，防止 +4 步进在同一元组的错位位置重复匹配

    for pageno, pd_upper, pd_special, raw in _scan_data_region(path, page_size):
        pos = pd_upper
        while pos + 80 <= pd_special:
            # 模式 A: [OID 4B][attrelid 4B][attname 64B]...
            attrelid_a = _s.unpack_from("<I", raw, pos + 4)[0]
            name_a = cstring(raw[pos + 8 : pos + 72])

            # 模式 B: [attrelid 4B][attname 64B]...
            attrelid_b = _s.unpack_from("<I", raw, pos)[0]
            name_b = cstring(raw[pos + 4 : pos + 68])

            matched = False

            # 模式 A
            if name_a and len(name_a) > 0 and (name_a[0].isalpha() or name_a[0] == '_'):
                if pos + 96 <= pd_special:
                    attnum = _s.unpack_from("<h", raw, pos + 82)[0]
                    if 1 <= attnum <= 1000 and attnum not in seen.setdefault(attrelid_a, set()):
                        atttypid = _s.unpack_from("<I", raw, pos + 72)[0]
                        attlen = _s.unpack_from("<h", raw, pos + 80)[0]
                        atttypmod = _s.unpack_from("<i", raw, pos + 92)[0] if pos + 96 <= pd_special else -1
                        notnull = raw[pos + 99] == 1 if pos + 99 <= pd_special else False
                        col = Column(name_a, atttypid, attlen, attnum, atttypmod, notnull)
                        result.setdefault(attrelid_a, []).append(col)
                        seen[attrelid_a].add(attnum)
                        matched = True

            # 模式 B
            if not matched and name_b and len(name_b) > 0:
                first = name_b[0]
                if first.isalpha() or first == '_':
                    if pos + 92 <= pd_special:
                        attnum = _s.unpack_from("<h", raw, pos + 78)[0]
                        if 1 <= attnum <= 1000 and attnum not in seen.setdefault(attrelid_b, set()):
                            atttypid = _s.unpack_from("<I", raw, pos + 68)[0]
                            attlen = _s.unpack_from("<h", raw, pos + 76)[0]
                            atttypmod = _s.unpack_from("<i", raw, pos + 88)[0] if pos + 92 <= pd_special else -1
                            notnull = raw[pos + 95] == 1 if pos + 95 <= pd_special else False
                            col = Column(name_b, atttypid, attlen, attnum, atttypmod, notnull)
                            result.setdefault(attrelid_b, []).append(col)
                            seen[attrelid_b].add(attnum)
                            matched = True

            pos += 4

    # 每张表的列按 attnum 排序
    for relid in result:
        result[relid].sort(key=lambda c: c.attnum)

    return result

def export_meta_to_json(db_dir: str, page_size=None) -> dict:
    """从数据库目录离线解析系统表，生成与 export_meta.sql 兼容的 JSON。

    db_dir: 如 /pgdata/base/16384/
    page_size: None=自动探测（页头编码）
    返回: {"database": "16384", "pg_version": 16, "tables": [...]}
    """
    meta = auto_discover_all_tables(db_dir, page_size=page_size)
    tables_json = []
    seen = set()
    for key, tm in meta["tables"].items():
        unique = f"{tm.schema}.{tm.relname}"
        if unique in seen:
            continue
        seen.add(unique)
        columns_json = []
        for c in tm.columns:
            if c.attdropped:
                continue
            columns_json.append({
                "name": c.name,
                "type_oid": c.atttypid,
                "len": c.attlen,
                "attnum": c.attnum,
                "typmod": c.typmod,
                "notnull": c.notnull,
                "dropped": c.attdropped,
                "attalign": c.attalign,
                "attbyval": c.attbyval,
                "attstorage": c.attstorage,
            })
        tables_json.append({
            "schema": tm.schema,
            "table": tm.relname,
            "relfilenode": tm.relfilenode,
            "toastrelid": tm.toastrelid if hasattr(tm, "toastrelid") else 0,
            "primary_key": tm.primary_key or [],
            "columns": columns_json,
        })
    return {
        "database": meta["database"],
        "pg_version": meta.get("pg_version", 16),
        "tables": tables_json,
    }
