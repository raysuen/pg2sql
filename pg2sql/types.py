# -*- coding: utf-8 -*-
# version: 2.4
"""
pg2sql.types
PostgreSQL 内置类型解码。将字段原始字节解码为可打印/可导入的 SQL 文本值。
参考 PostgreSQL src/backend/utils/adt/ 相关输出函数。

v1.6（对照 PG numeric.h / PDU decode.c 复核）:
  - numeric 改为磁盘格式（旧版误用网络协议格式）:
    long [n_sign_dscale][weight][digits] / short [n_header][digits]，
    原生小端，支持 NaN/Infinity、负数、任意 dscale
  - varlena payload 提取改用小端 tag 分派，内联压缩（4B tag=10）自动解压
  - timestamp/timestamptz/date 支持 ±infinity
  - tid 的 block 号改为 4 字节（旧版误读 2 字节）
"""
import datetime
import json
import struct
import uuid as _uuid

from .binary import (
    cstring, hexstr, varlena_parse, parse_external_pointer,
    toast_decompress, decode_bytes,
    VARLENA_1B, VARLENA_4B, VARLENA_4B_COMPRESSED, VARLENA_EXTERNAL,
)

# ---------------- 类型 OID（PostgreSQL 内置） ----------------
BOOLOID = 16
BYTEAOID = 17
CHAROID = 18
NAMEOID = 19
INT8OID = 20
INT2OID = 21
INT2VECTOROID = 22
INT4OID = 23
REGPROCOID = 24
TEXTOID = 25
OIDOID = 26
TIDOID = 27
XIDOID = 28
CIDOID = 29
OIDVECTOROID = 30
PG_DDL_COMMANDOID = 32
JSONOID = 114
XMLOID = 142
PG_NODE_TREEOID = 194
PG_NDISTINCTOID = 3361
PG_DEPENDENCIESOID = 3402
PG_MCV_LISTOID = 5017
CIDROID = 650
FLOAT4OID = 700
FLOAT8OID = 701
UNKNOWNOID = 705
CIRCLEOID = 718
MONEYOID = 790
MACADDROID = 829
INETOID = 869
MACADDR8OID = 774
BPCHAROID = 1042
VARCHAROID = 1043
DATEOID = 1082
TIMEOID = 1083
TIMESTAMPOID = 1114
TIMESTAMPTZOID = 1184
INTERVALOID = 1186
TIMETZOID = 1266
BITOID = 1560
VARBITOID = 1562
NUMERICOID = 1700
UUIDOID = 2950
JSONBOID = 3802
ACLITEMOID = 1033  # aclitem 元素类型（aclitem[]=1034）

# 类型名（用于 DDL）
TYPE_NAMES = {
    BOOLOID: "bool",
    BYTEAOID: "bytea",
    CHAROID: "char",
    NAMEOID: "name",
    INT8OID: "bigint",
    INT2OID: "smallint",
    INT4OID: "integer",
    REGPROCOID: "regproc",
    TEXTOID: "text",
    OIDOID: "oid",
    TIDOID: "tid",
    XIDOID: "xid",
    CIDOID: "cid",
    JSONOID: "json",
    XMLOID: "xml",
    FLOAT4OID: "real",
    FLOAT8OID: "double precision",
    UNKNOWNOID: "unknown",
    MONEYOID: "money",
    MACADDROID: "macaddr",
    INETOID: "inet",
    CIDROID: "cidr",
    MACADDR8OID: "macaddr8",
    BPCHAROID: "character",
    VARCHAROID: "character varying",
    DATEOID: "date",
    TIMEOID: "time",
    TIMESTAMPOID: "timestamp",
    TIMESTAMPTZOID: "timestamptz",
    INTERVALOID: "interval",
    TIMETZOID: "timetz",
    BITOID: "bit",
    VARBITOID: "varbit",
    NUMERICOID: "numeric",
    UUIDOID: "uuid",
    JSONBOID: "jsonb",
    INT2VECTOROID: "int2vector",
    OIDVECTOROID: "oidvector",
    PG_NODE_TREEOID: "pg_node_tree",
    PG_DDL_COMMANDOID: "pg_ddl_command",
    # 常见数组类型（P1-6）
    1000: "bool[]",
    1001: "bytea[]",
    1002: "char[]",
    1003: "name[]",
    1005: "smallint[]",
    1006: "int2vector[]",
    1007: "integer[]",
    1008: "regproc[]",
    1009: "text[]",
    1010: "tid[]",
    1011: "xid[]",
    1012: "cid[]",
    1013: "oidvector[]",
    1014: "character[]",
    1015: "character varying[]",
    1016: "bigint[]",
    1021: "real[]",
    1022: "double precision[]",
    1024: "oid[]",
    1027: "macaddr[]",
    1028: "inet[]",
    1040: "macaddr8[]",
    1115: "timestamp[]",
    1182: "date[]",
    1183: "time[]",
    1185: "timestamptz[]",
    1187: "interval[]",
    1231: "numeric[]",
    1270: "timetz[]",
    1561: "bit[]",
    1563: "varbit[]",
    2951: "uuid[]",
    3807: "jsonb[]",
}

# 需要带 typmod 长度/精度信息的类型
TYPEMOD_TYPES = {
    BPCHAROID, VARCHAROID, NUMERICOID, BITOID, VARBITOID, TIMEOID, TIMETZOID,
}

# varlena 类型（可能 TOAST 外联）
VARLENA_TYPES = {
    TEXTOID, VARCHAROID, BPCHAROID, BYTEAOID, JSONOID, JSONBOID, NUMERICOID,
    BITOID, VARBITOID, PG_NODE_TREEOID, XMLOID, PG_DDL_COMMANDOID,
    PG_NDISTINCTOID, PG_DEPENDENCIESOID, PG_MCV_LISTOID,
}


# --------------------------------------------------------------------------
# varlena 工具
# --------------------------------------------------------------------------

# 角色名映射（离线解析 pg_authid 注入，供 aclitem[] 解码；空则输出 oid:N 占位）
ROLE_NAME_MAP: dict = {}


def set_role_name_map(m: dict):
    global ROLE_NAME_MAP
    ROLE_NAME_MAP = dict(m or {})


# aclitem 权限位 → 字符。
# 标准 PG：PG12-14 = "arwdDxtXUCTc"（12 位）；PG15 加 's'(SET)/'A'(ALTER SYSTEM)；
# PG17 加 'm'(MAINTAIN)。取并集可正确解码 PG12-17 全部权限位（含金仓实测的
# 's' 位，说明 KB 内核已并入 PG15 的 ACL 补丁）。
# 注意：含 'm' 的 ACL 只能导入 PG17+（PG16 及以下 aclparse 不识别该字符）。
_ACL_CHARS = "arwdDxtXUCTcsAm"


def decode_aclitem(b: bytes) -> str:
    """aclitem（12B 定长）：grantor(4) grantee(4) privs(4) → grantee=privs/grantor。

    PG ACL 文本格式为 `grantee=privs/grantor`（aclparse：getid→grantee，
    '=' 后为权限字符，'/' 后为 grantor）；grantee==0 表示 PUBLIC（输出为空）。
    角色名查 ROLE_NAME_MAP，缺失时用 oid:N 占位（离线无法解析名字的场景，
    保证语法合法）。
    """
    if len(b) < 12:
        return "oid:0"
    grantor = int.from_bytes(b[0:4], "little")
    grantee = int.from_bytes(b[4:8], "little")
    privs = int.from_bytes(b[8:12], "little")
    e = "" if grantee == 0 else ROLE_NAME_MAP.get(grantee, f"oid:{grantee}")
    chars = "".join(ch for i, ch in enumerate(_ACL_CHARS) if privs & (1 << i))
    # grantor==0（金仓 world 默认授权）省略 grantor，aclparse 回退到超级用户
    if grantor == 0:
        return f"{e}={chars}"
    g = ROLE_NAME_MAP.get(grantor, f"oid:{grantor}")
    return f"{e}={chars}/{g}"


def _var(b: bytes):
    """_var 的别名，调用 _var_payload。保持向后兼容。"""
    return _var_payload(b)


def _var_payload(b: bytes):
    """从字段字节中取出 varlena payload。

    返回 (payload_bytes, is_external, external_info_dict or None)
    对 TOAST 外联引用，payload_bytes 为空，external 信息放入 dict。
    内联压缩（4B 头 tag=10）自动解压（PGLZ/LZ4）。

    PG 真实格式（小端）:
      0x01 0x12     → 外联指针 18B
      首字节 & 0x01 → 1B 短头，VARSIZE = first >> 1
      低 2 位 00/10 → 4B 头（未压缩/内联压缩），VARSIZE = (u32le >> 2)
    """
    if not b:
        return (b"", False, None)
    kind, total, _, _ = varlena_parse(b, 0)
    if kind == VARLENA_EXTERNAL:
        ext = parse_external_pointer(b, 0)
        if ext is not None:
            return (b"", True, ext)
        return (b"", False, None)
    if kind == VARLENA_1B:
        return (b[1:1 + max(total - 1, 0)], False, None)
    if kind == VARLENA_4B:
        return (b[4:4 + max(total - 4, 0)], False, None)
    if kind == VARLENA_4B_COMPRESSED:
        # 内联压缩: payload 前 4B 小端 tcinfo（低 30 位原始长度，高 2 位方法）
        comp = b[4:total]
        if len(comp) >= 4:
            rawlen = struct.unpack_from("<I", comp, 0)[0] & 0x3FFFFFFF
            data = toast_decompress(comp, rawlen)
            if data is not None:
                return (data, False, None)
        return (b"", False, None)
    return (b"", False, None)


# --------------------------------------------------------------------------
# 基础解码器（输入为 varlena payload 或固定长度二进制）
# --------------------------------------------------------------------------

def decode_bool(b: bytes) -> str:
    """bool 列 → 'true'/'false'。

    磁盘上 bool 为 C 布尔（1 字节：0x01=true / 0x00=false），'t'/'f'
    仅是文本 I/O 形式；兼容旧版误存 ASCII 的情况。
    """
    if not b:
        return "false"
    v = b[0]
    if v in (1, 0x74, ord("t"), 0x31):  # 1 / 't' / 'T'
        return "true"
    return "false"


def decode_int2(b: bytes) -> str:
    return str(struct.unpack("<h", b[:2])[0])


def decode_int4(b: bytes) -> str:
    return str(struct.unpack("<i", b[:4])[0])


def decode_int8(b: bytes) -> str:
    return str(struct.unpack("<q", b[:8])[0])


def decode_float4(b: bytes) -> str:
    v = struct.unpack("<f", b[:4])[0]
    return _fmt_float(v)


def decode_float8(b: bytes) -> str:
    v = struct.unpack("<d", b[:8])[0]
    return _fmt_float(v)


def _fmt_float(v: float) -> str:
    """PG float 文本输出：NaN / Infinity / -Infinity（非 Python repr 小写）。"""
    import math
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    return repr(v)


def _decode_varlena_text(b: bytes) -> str:
    payload, _, _ = _var(b)
    return decode_bytes(payload)


def decode_text(b: bytes) -> str:
    return _decode_varlena_text(b)


def decode_name(b: bytes) -> str:
    return cstring(b)


def decode_bpchar(b: bytes) -> str:
    return _decode_varlena_text(b)


def decode_varchar(b: bytes) -> str:
    return _decode_varlena_text(b)


def decode_bytea(b: bytes) -> str:
    payload, _, _ = _var(b)
    return "\\x" + payload.hex()


def decode_oid(b: bytes) -> str:
    return str(struct.unpack("<I", b[:4])[0])


def decode_xid(b: bytes) -> str:
    return str(struct.unpack("<I", b[:4])[0])


def decode_cid(b: bytes) -> str:
    return str(struct.unpack("<I", b[:4])[0])


def decode_tid(b: bytes) -> str:
    blk = struct.unpack("<I", b[:4])[0]
    off = struct.unpack("<H", b[4:6])[0]
    return f"({blk},{off})"


def decode_date(b: bytes) -> str:
    days = struct.unpack("<i", b[:4])[0]
    if days == 0x7FFFFFFF:
        return "infinity"
    if days == -0x80000000:
        return "-infinity"
    try:
        d = datetime.date(2000, 1, 1) + datetime.timedelta(days=days)
        return d.isoformat()
    except (OverflowError, ValueError):
        return f"date(days={days})"


def _time_from_us(us: int) -> str:
    us %= 86400_000000
    h = us // 3600_000_000
    us %= 3600_000_000
    m = us // 60_000_000
    us %= 60_000_000
    s = us // 1_000_000
    micro = us % 1_000_000
    if micro:
        return f"{h:02d}:{m:02d}:{s:02d}.{micro:06d}".rstrip("0")
    return f"{h:02d}:{m:02d}:{s:02d}"


def decode_time(b: bytes) -> str:
    us = struct.unpack("<q", b[:8])[0]
    return _time_from_us(us)


def decode_timestamp(b: bytes) -> str:
    us = struct.unpack("<q", b[:8])[0]
    if us == 0x7FFFFFFFFFFFFFFF:
        return "infinity"
    if us == -0x8000000000000000:
        return "-infinity"
    try:
        dt = datetime.datetime(2000, 1, 1) + datetime.timedelta(microseconds=us)
        return dt.isoformat(sep=" ")
    except (OverflowError, ValueError):
        return f"timestamp(us={us})"


def decode_timestamptz(b: bytes) -> str:
    us = struct.unpack("<q", b[:8])[0]
    if us == 0x7FFFFFFFFFFFFFFF:
        return "infinity"
    if us == -0x8000000000000000:
        return "-infinity"
    try:
        dt = datetime.datetime(2000, 1, 1) + datetime.timedelta(microseconds=us)
        return dt.isoformat(sep=" ") + "+00"
    except (OverflowError, ValueError):
        return f"timestamptz(us={us})"


def decode_timetz(b: bytes) -> str:
    us = struct.unpack("<q", b[:8])[0]
    zone = struct.unpack("<i", b[8:12])[0]
    t = _time_from_us(us)
    # PG datetime.c EncodeTimezone：磁盘 zone 与显示符号相反
    # （"TZ is negated compared to sign we wish to display"），
    # zone<=0 显示 '+', zone>0 显示 '-'。
    sign = "-" if zone >= 0 else "+"
    zone = abs(zone)
    return f"{t}{sign}{zone//3600:02d}:{zone%3600//60:02d}"


def decode_interval(b: bytes) -> str:
    """PG interval 文本输出（对照 datetime.c EncodeInterval）。

    磁盘：time 微秒 int64 + day int32 + month int32（可负）。
    输出："1 year 2 mons 3 days 04:05:06.789"，全零 "00:00:00"。
    """
    time_us = struct.unpack("<q", b[:8])[0]
    day = struct.unpack("<i", b[8:12])[0]
    month = struct.unpack("<i", b[12:16])[0]
    # C 整除语义（向零截断）：PG interval2tm 用 / 与 %，-13 mons → -1 years -1 mons；
    # Python divmod(-13,12)=(-2,11) 会错标年份（数值等价但文本不同）
    year = int(month / 12)
    month -= year * 12
    parts = []
    if year:
        parts.append(f"{year} year" if year == 1 else f"{year} years")
    if month:
        parts.append(f"{month} mon" if month == 1 else f"{month} mons")
    if day:
        parts.append(f"{day} day" if abs(day) == 1 else f"{day} days")
    sign = "-" if time_us < 0 else ""
    t = abs(time_us)
    us = t % 1000000
    total_s = t // 1000000
    hh, rem = divmod(total_s, 3600)
    mm, ss = divmod(rem, 60)
    time_s = f"{sign}{hh:02d}:{mm:02d}:{ss:02d}"
    if us:
        time_s += f".{us:06d}".rstrip("0")
    if time_us != 0:
        # 仅时间非零时追加 HH:MM:SS；纯年月日（time=0）不追加 00:00:00（PG 行为）
        parts.append(time_s)
    return " ".join(parts) if parts else "00:00:00"


def _numeric_disk_to_str(payload: bytes) -> str:
    """PG 磁盘格式 numeric 解码（参考 numeric.h NumericData + PDU decode.c）。

    Long 格式: [n_sign_dscale 2B][weight 2B][digits 2B×n] 原生小端
      - n_sign_dscale 高 2 位 flag: 00=正 01=负 10=short 11=special
      - 特殊值: 头 == 0xC000(NaN) / 0xD000(+Inf) / 0xF000(-Inf)
      - 低 14 位 = dscale
    Short 格式（PG 9.5+，typmod 有限时使用）:
      [n_header 2B][digits 2B×n]
      - bit 13 (0x2000): 符号
      - bits 7-11 (0x1F80, shift 7): dscale
      - bit 6 (0x0040): weight 符号
      - bits 0-5 (0x003F): weight 绝对值
    digits: base-10000，digit[i] 位权 10^(4*(weight-i))
    """
    if len(payload) < 2:
        return "0"
    header = struct.unpack_from("<H", payload, 0)[0]
    flags = header & 0xC000

    if flags == 0xC000:
        # 特殊值
        if header == 0xD000:
            return "Infinity"
        if header == 0xF000:
            return "-Infinity"
        return "NaN"

    if flags == 0x8000:
        # Short 格式
        neg = bool(header & 0x2000)
        dscale = (header & 0x1F80) >> 7
        # 官方 NUMERIC_SHORT_WEIGHT（numeric.c）：负数 weight 为补码扩展
        #   (~NUMERIC_SHORT_WEIGHT_MASK | bits)，不是符号-幅值；
        #   例：-0.01 n_short=0xA17F → weight=-1（旧实现误读为 -63，P1-6 回归发现）
        weight = (((~0x3F) | (header & 0x3F)) & 0xFFFF) if (header & 0x0040) else (header & 0x3F)
        if weight & 0x8000:
            weight -= 0x10000
        digits_raw = payload[2:]
    else:
        # Long 格式
        if len(payload) < 4:
            return "0"
        neg = bool(header & 0x4000)
        dscale = header & 0x3FFF
        weight = struct.unpack_from("<h", payload, 2)[0]
        digits_raw = payload[4:]

    ndigits = len(digits_raw) // 2
    digits = struct.unpack_from("<%dH" % ndigits, digits_raw) if ndigits else ()

    # ---- 整数部分（参考 numeric_out / get_str_from_var）----
    if weight < 0:
        int_str = "0"
        frac_start = weight + 1  # 可能为负（虚拟前导零组）
    else:
        parts = []
        for i in range(weight + 1):
            parts.append(digits[i] if i < ndigits else 0)
        if parts:
            int_str = str(parts[0])
            for d in parts[1:]:
                int_str += "%04d" % d
            int_str = int_str.lstrip("0") or "0"
        else:
            int_str = "0"
        frac_start = weight + 1

    # ---- 小数部分（到 dscale 位）----
    if dscale > 0:
        ngroups = (dscale + 3) // 4
        frac = ""
        for g in range(ngroups):
            di = frac_start + g
            d = digits[di] if 0 <= di < ndigits else 0
            frac += "%04d" % d
        frac = frac[:dscale]
        result = int_str + "." + frac
    else:
        result = int_str

    if neg:
        result = "-" + result
    return result


def decode_numeric(b: bytes) -> str:
    payload, _, _ = _var(b)
    return _numeric_disk_to_str(payload)


def decode_uuid(b: bytes) -> str:
    return str(_uuid.UUID(bytes=b[:16]))


def decode_json(b: bytes) -> str:
    return _decode_varlena_text(b)


# --------------------------------------------------------------------------
# jsonb 解码（P1-6）
# 参考 PG16 jsonb.h / jsonb_util.c（/tmp/jsonb.h、/tmp/jsonb_util.c 实证）：
#   容器头: 小端 uint32，高 nibble 为容器类型
#     JB_FSCALAR=0x10000000（裸标量）| JB_FOBJECT=0x20000000 | JB_FARRAY=0x40000000
#     低 28 位 = count；裸标量 = FSCALAR|FARRAY 的单元素数组（count=1）
#   JEntry 数组紧随容器头：
#     对象 = 2*count 个（前 count 个为键，后 count 个为值，非交错）
#     数组/标量 = count 个；每项小端 uint32
#     bit31=HAS_OFF(0x80000000) bit28-30=类型 bit0-27=OFFLEN
#     类型: 0=string 0x10000000=numeric 0x20000000=false 0x30000000=true
#           0x40000000=null 0x50000000=container
#   数据区在 JEntry 数组之后；偏移推进（getJsonbOffset）：
#     从 index-1 往回，先累加 OFFLEN、遇 HAS_OFF 项则取该值并停止
#   numeric 值在数据区按 INTALIGN 4 对齐存放（fillJsonbValue 对 offset 取
#     INTALIGN），整条 numeric varlena（含自身头）原样嵌入
#   容器值同样 INTALIGN 对齐，长度 = offlen - (INTALIGN(offset)-offset)
# --------------------------------------------------------------------------

JB_FSCALAR = 0x10000000
JB_FOBJECT = 0x20000000
JB_FARRAY = 0x40000000
JENTRY_HAS_OFF = 0x80000000
JENTRY_TYPEMASK = 0x70000000
JENTRY_ISNUMERIC = 0x10000000
JENTRY_ISBOOL_FALSE = 0x20000000
JENTRY_ISBOOL_TRUE = 0x30000000
JENTRY_ISNULL = 0x40000000
JENTRY_ISCONTAINER = 0x50000000


def _jsonb_scalar_to_text(je, payload, data_off, s, e):
    """单个 jsonb 标量/容器 → JSON 文本。data 区域为 payload[data_off+s : data_off+e]。"""
    t = je & JENTRY_TYPEMASK
    if t == 0:  # string
        return json.dumps(decode_bytes(payload[data_off + s:data_off + e]),
                          ensure_ascii=False)
    if t == JENTRY_ISNUMERIC:
        vp, _, _ = _var_payload(payload[data_off + ((s + 3) & ~3):data_off + e])
        return _numeric_disk_to_str(vp)
    if t == JENTRY_ISBOOL_TRUE:
        return "true"
    if t == JENTRY_ISBOOL_FALSE:
        return "false"
    if t == JENTRY_ISNULL:
        return "null"
    if t == JENTRY_ISCONTAINER:
        return _jsonb_container_to_text(payload[data_off + ((s + 3) & ~3):data_off + e])
    raise ValueError("jsonb: unknown JEntry type 0x%x" % t)


def _jsonb_container_to_text(payload: bytes) -> str:
    """jsonb varlena payload → 合法 JSON 文本（与 PG jsonb_out 一致）。"""
    if len(payload) < 4:
        raise ValueError("jsonb payload too short")
    header = struct.unpack_from("<I", payload, 0)[0]
    flags = header & 0xF0000000
    count = header & 0x0FFFFFFF
    scalar = bool(flags & JB_FSCALAR)
    is_obj = bool(flags & JB_FOBJECT)
    n_ent = count * 2 if is_obj else count
    data_off = 4 + 4 * n_ent
    if data_off > len(payload):
        raise ValueError("jsonb jentry array out of range")
    ents = [struct.unpack_from("<I", payload, 4 + 4 * i)[0] for i in range(n_ent)]

    def get_offset(idx):
        # PG getJsonbOffset 权威语义（jsonb_util.c + JBE_ADVANCE_OFFSET）：
        # 从数据区起点顺序推进到 idx 前一项——HAS_OFF 项存"该元素终点
        # 绝对偏移"（重置 offset），无 HAS_OFF 项存长度（累加）。
        off = 0
        for i in range(idx):
            if ents[i] & JENTRY_HAS_OFF:
                off = ents[i] & 0x0FFFFFFF
            else:
                off += ents[i] & 0x0FFFFFFF
        return off

    def jlen(idx):
        # PG getJsonbLength：HAS_OFF 项长度 = 终点偏移 - 起点偏移；
        # 无 HAS_OFF 项直接取 offlen
        if ents[idx] & JENTRY_HAS_OFF:
            return (ents[idx] & 0x0FFFFFFF) - get_offset(idx)
        return ents[idx] & 0x0FFFFFFF

    def emit(idx):
        s = get_offset(idx)
        return _jsonb_scalar_to_text(ents[idx], payload, data_off, s, s + jlen(idx))

    if scalar:
        return emit(0)
    if is_obj:
        pairs = []
        key_off = 0
        val_off = get_offset(count)  # 值区起点 = 键区累计终点
        for i in range(count):
            klen = jlen(i)
            key = decode_bytes(payload[data_off + key_off:data_off + key_off + klen])
            vs = get_offset(count + i)
            v = _jsonb_scalar_to_text(ents[count + i], payload, data_off, vs, vs + jlen(count + i))
            pairs.append((key, v))
            if ents[i] & JENTRY_HAS_OFF:
                key_off = ents[i] & 0x0FFFFFFF
            else:
                key_off += ents[i] & 0x0FFFFFFF
            if ents[count + i] & JENTRY_HAS_OFF:
                val_off = ents[count + i] & 0x0FFFFFFF
            else:
                val_off += ents[count + i] & 0x0FFFFFFF
        return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + ": " + v for k, v in pairs) + "}"
    return "[" + ", ".join(emit(i) for i in range(count)) + "]"


def decode_jsonb(b: bytes) -> str:
    """jsonb 列 → JSON 文本（替换原纯文本解码，P1-6）。"""
    payload, _, _ = _var(b)
    return _jsonb_container_to_text(payload)


# --------------------------------------------------------------------------
# 数组解码（P1-6）
# 参考 array.h / arrayfuncs.c：磁盘格式
#   [ndim int32][dataoffset int32][elemtype oid int32]
#   [2*ndim 个 int32: dims... lbs...]
#   若 dataoffset>0: [NULL 位图 dataoffset 字节]（bit=1 → NULL）
#   随后元素区：固定长度元素按 attalign 对齐；varlena 元素
#   （1B 短头按原样紧凑存放，4B 头前补对齐）
# 输出 PG 数组字面量，如 {1,2,3} / {{a,b},{c,d}} / {NULL,"x"}
# --------------------------------------------------------------------------

# 常见数组元素类型 → (attlen, attalign, 解码函数)
ARRAY_ELEM_INFO = {
    BOOLOID: (1, "c", lambda b: decode_bool(b)),
    BYTEAOID: (-1, "i", None),        # 元素为 bytea → 退化为文本
    CHAROID: (1, "c", lambda b: decode_char(b)),
    NAMEOID: (64, "c", lambda b: decode_name(b)),
    INT8OID: (8, "d", lambda b: decode_int8(b)),
    INT2OID: (2, "s", lambda b: decode_int2(b)),
    INT4OID: (4, "i", lambda b: decode_int4(b)),
    TEXTOID: (-1, "i", None),
    OIDOID: (4, "i", lambda b: decode_oid(b)),
    FLOAT4OID: (4, "i", lambda b: decode_float4(b)),
    FLOAT8OID: (8, "d", lambda b: decode_float8(b)),
    UNKNOWNOID: (-1, "i", None),
    BPCHAROID: (-1, "i", None),
    VARCHAROID: (-1, "i", None),
    DATEOID: (4, "i", lambda b: decode_date(b)),
    TIMEOID: (8, "d", lambda b: decode_time(b)),
    TIMESTAMPOID: (8, "d", lambda b: decode_timestamp(b)),
    TIMESTAMPTZOID: (8, "d", lambda b: decode_timestamptz(b)),
    TIMETZOID: (12, "d", lambda b: decode_timetz(b)),
    INTERVALOID: (16, "d", lambda b: decode_interval(b)),
    NUMERICOID: (-1, "i", None),
    UUIDOID: (16, "c", lambda b: decode_uuid(b)),
    JSONBOID: (-1, "i", lambda b: decode_jsonb(b)),
    ACLITEMOID: (12, "i", lambda b: decode_aclitem(b)),
}

# 常见数组类型 OID（pg_type typarray 直接对应）
ARRAY_TYPE_OIDS = {
    1000,  # bool[]
    1001,  # bytea[]
    1002,  # char[]
    1003,  # name[]
    1005,  # int2[]
    1006,  # int2vector[]
    1007,  # int4[]
    1008,  # regproc[]
    1009,  # text[]
    1010,  # tid[]
    1011,  # xid[]
    1012,  # cid[]
    1013,  # oidvector[]
    1014,  # bpchar[]
    1015,  # varchar[]
    1016,  # int8[]
    1017,  # point[]
    1018,  # lseg[]
    1019,  # path[]
    1020,  # box[]
    1021,  # float4[]
    1022,  # float8[]
    1023,  # polygon[]
    1024,  # oid[]
    1027,  # macaddr[]
    1028,  # inet[]
    1040,  # macaddr8[]
    1041,  # inet[]（PG 与金仓实测 oid=1041；旧注释误标为 aclitem[]）
    1034,  # aclitem[]（PG/金仓实测 oid=1034）
    1115,  # timestamp[]
    1182,  # date[]
    1183,  # time[]
    1185,  # timestamptz[]
    1187,  # interval[]
    1231,  # numeric[]
    1263,  # cstring[]
    1270,  # timetz[]
    1561,  # bit[]
    1563,  # varbit[]
    2951,  # uuid[]
    3807,  # jsonb[]
}

# 数组类型 OID → 元素类型 OID（常见映射；缺失时按 element type OID=0 降级文本）
ARRAY_ELEM_OID = {
    1000: 16, 1001: 17, 1002: 18, 1003: 19, 1005: 21, 1006: 22,
    1007: 23, 1008: 24, 1009: 25, 1010: 27, 1011: 28, 1012: 29,
    1013: 30, 1014: 1042, 1015: 1043, 1016: 20, 1021: 700, 1022: 701,
    1024: 26, 1027: 829, 1028: 869, 1040: 774, 1115: 1114, 1182: 1082,
    1183: 1083, 1185: 1184, 1187: 1186, 1231: 1700, 1263: 2275,
    1270: 1266, 1561: 1560, 1563: 1562, 2951: 2950, 3807: 3802,
    1034: 1033,
}


# 对齐尺寸（PG att_align，参考 pg_type.h typalign）
ALIGN_SIZES = {"c": 1, "s": 2, "i": 4, "d": 8}


def _array_elem_text(payload: bytes, pos: int, elem_oid: int):
    """读数组元素区一个元素，返回 (text, next_pos)。"""
    info = ARRAY_ELEM_INFO.get(elem_oid)
    if info is None:
        # 未知元素类型：按 varlena 文本退化（多数场景足够）
        first = payload[pos]
        if first & 1:
            total = first >> 1
            return (decode_bytes(payload[pos + 1:pos + total]),
                    pos + total)
        total = struct.unpack_from("<I", payload, pos)[0] >> 2
        return (decode_bytes(payload[pos + 4:pos + total]),
                pos + total)
    alen, aalign, fn = info
    if alen == -1:
        # varlena 元素：PG16 数组写盘（construct_md_array 用 att_align_nominal）
        # 对 varlena 元素无条件按 elmalign 对齐，短值统一扩展为 4B 头
        # （实测 t_arr text[] 元素 0x14 头、elem2 对齐到 28）
        first = payload[pos]
        if first & 1:  # 1B 短头（历史/金仓数据可能保留，紧凑存放）
            total = first >> 1
            seg = payload[pos + 1:pos + total]
            return ((fn(seg) if fn else decode_bytes(seg)), pos + total)
        a = ALIGN_SIZES.get(aalign, 4)
        if a > 1 and pos % a != 0:
            pos = (pos + a - 1) & ~(a - 1)
        total = struct.unpack_from("<I", payload, pos)[0] >> 2
        seg = payload[pos + 4:pos + total]
        return ((fn(seg) if fn else decode_bytes(seg)), pos + total)
    # 固定长度元素：att_align_nominal
    a = ALIGN_SIZES.get(aalign, 1)
    if a > 1:
        pos = (pos + a - 1) & ~(a - 1)
    seg = payload[pos:pos + alen]
    if fn:
        return (fn(seg), pos + alen)
    return (decode_bytes(seg), pos + alen)


def decode_array(b: bytes) -> str:
    """数组列 → PG 数组字面量文本（P1-6）。

    元素 NULL 位图（dataoffset>0 时存在）与多维 dims/lbs 均按 array.h 处理；
    输出形如 {1,2,3} / {{a,b},{c,d}}，元素含特殊字符时按 PG array_out 规则
    双引号包裹并转义。
    """
    payload, _, _ = _var(b)
    if len(payload) < 12:
        return "{}"
    ndim = struct.unpack_from("<i", payload, 0)[0]
    dataoffset = struct.unpack_from("<i", payload, 4)[0]
    elemtype = struct.unpack_from("<I", payload, 8)[0]
    if ndim <= 0 or ndim > 6:
        # 非法/0 维数组按空数组处理
        return "{}"
    dims = [struct.unpack_from("<i", payload, 12 + 4 * i)[0] for i in range(ndim)]
    body_off = 12 + 8 * ndim
    nelems = 1
    for d in dims:
        if d <= 0:
            return "{}"
        nelems *= d

    nulls = None
    if dataoffset > 0:
        bm_len = (nelems + 7) // 8
        bm = payload[body_off:body_off + bm_len]
        # PG 数组位图：bit=1 → 非空元素（arrayfuncs.CopyArrayEls 中 NULL 元素
        # 位图位保持 0，与 tuple 位图语义一致）；旧实现取反导致含 NULL 数组错乱
        nulls = [not bool(bm[i // 8] & (1 << (i % 8))) for i in range(nelems)]
        # 元素区起点（相对 payload）= dataoffset - 4：dataoffset 按 4B vl_len_
        # ArrayType 布局计算（ARR_OVERHEAD_WITHNULLS），payload 不含 vl_len_，
        # 1B/4B 头通用减 4（实测 PG18 {1,NULL,3} dataoffset=32 → 元素区 28）
        elem_off = dataoffset - 4
        # 元素区按 4 对齐（dataoffset 通常已 MAXALIGN，双保险）
        elem_off = (elem_off + 3) & ~3
    else:
        elem_off = body_off

    texts = []
    pos = elem_off
    for i in range(nelems):
        if nulls and nulls[i]:
            texts.append("NULL")
            continue
        try:
            t, pos = _array_elem_text(payload, pos, elemtype)
        except Exception:
            texts.append("__ARRAY_CORRUPT__")
            break
        texts.append(t)

    # 按 dims 组装嵌套花括号（行主序）
    def nest(items, dims_idx):
        d = dims[dims_idx]
        if dims_idx == ndim - 1:
            return items[:d], d
        out = []
        cnt = 0
        rest = d
        for _ in range(d):
            chunk, used = nest(items[cnt:], dims_idx + 1)
            cnt += used
            out.append(chunk)
        return out, cnt

    if ndim == 1:
        elems = texts
    else:
        elems, _ = nest(texts, 0)

    def fmt(items):
        if isinstance(items, list) and items and isinstance(items[0], list):
            return "{" + ",".join(fmt(x) for x in items) + "}"
        return "{" + ",".join(_array_quote(x) for x in items) + "}"

    return fmt(elems)


def _array_quote(s: str) -> str:
    """PG array_out 元素转义：含特殊字符时双引号包裹。"""
    if s == "NULL":
        return s
    needs = not s or any(ch in s for ch in ',{}"\\') or s[0].isspace() or s[-1].isspace()
    if not needs:
        return s
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s + '"'


def decode_inet(b: bytes, force_cidr: bool = False) -> str:
    """inet/cidr → 文本。PG18 权威磁盘格式（inet.h inet_struct）：

        1B/4B varlena 头 + family(1) + bits(1) + ipaddr(4|16)

    无 is_cidr/nb 字段（7.4 起移除，addr 长度由 family 推导，恒用 1B 头存储）；
    旧实现把 varlena 头当 family 且按旧 4 字段布局解，导致输出原始 hex。
    """
    payload, _, _ = _var(b)
    if len(payload) < 2:
        return ""
    family = payload[0]
    bits = payload[1]
    if family not in (2, 3):
        return ""
    nbytes = 4 if family == 2 else 16
    addr = payload[2:2 + nbytes]
    if family == 2:
        s = ".".join(str(x) for x in addr)
    else:
        s = ":".join(f"{addr[i] << 8 | addr[i+1]:x}" for i in range(0, len(addr), 2))
    # PG inet_out：非默认掩码（v4=/32、v6=/128）输出 /bits；cidr 恒输出
    default_bits = 32 if family == 2 else 128
    if force_cidr or bits != default_bits:
        return f"{s}/{bits}"
    return s


def decode_cidr(b: bytes) -> str:
    return decode_inet(b, force_cidr=True)


def decode_macaddr(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b[:6])


def decode_macaddr8(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b[:8])


def _bits_to_str(payload: bytes) -> str:
    if len(payload) < 4:
        return ""
    nbits = struct.unpack("<i", payload[:4])[0]
    data = payload[4:]
    out = []
    for i in range(nbits):
        byte = data[i // 8]
        out.append("1" if byte & (1 << (7 - (i % 8))) else "0")
    return "".join(out)


def decode_bit(b: bytes) -> str:
    payload, _, _ = _var(b)
    return _bits_to_str(payload)


def decode_money(b: bytes) -> str:
    # v2.3: 整数除法避免大金额浮点精度损失（int64 微元 / 100 转 float
    # 在 >2^53/100 微元时精度丢失；PG money 最大 ~9.2e18 微元）
    v = struct.unpack("<q", b[:8])[0]
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}{v // 100}.{v % 100:02d}"


# --------------------------------------------------------------------------
# 几何类型（PG geometric types，磁盘均为小端 IEEE float8）
#   point(600) 16B: x y          lseg(601) 32B: x1 y1 x2 y2
#   path(602)  varlena: int32 npts + int32 closed + npts*16B
#   box(603)   32B: 高右(x1,y1) 低左(x2,y2)   polygon(604) varlena: npts + npts*16B
#   line(628)  24B: A B C        circle(718) 24B: 圆心(x,y) + 半径 r
# 输出格式对齐 PG 各 *_out：point (x,y)；lseg [(x1,y1),(x2,y2)]；
#   box (x1,y1),(x2,y2)；path closed "((..))" open "[(..)]"；polygon "((..))"；
#   line {A,B,C}；circle <(x,y),r>
# --------------------------------------------------------------------------
def _geom_pt(b: bytes, off: int) -> str:
    x, y = struct.unpack_from("<dd", b, off)
    return f"({_fmt_float(x)},{_fmt_float(y)})"


def decode_point(b: bytes) -> str:
    return _geom_pt(b, 0) if len(b) >= 16 else ""


def decode_lseg(b: bytes) -> str:
    if len(b) < 32:
        return ""
    return f"[{_geom_pt(b, 0)},{_geom_pt(b, 16)}]"


def decode_box(b: bytes) -> str:
    if len(b) < 32:
        return ""
    return f"{_geom_pt(b, 0)},{_geom_pt(b, 16)}"


def decode_path(b: bytes) -> str:
    payload, _, _ = _var(b)
    if len(payload) < 12:
        return ""
    # 磁盘 PATH = npts(int32) + closed(bool+3B pad) + dummy(int32) + points*16B
    # （geo_decls.h PATH 结构；points 从 offset 12 起，头共 12B）
    npts, closed = struct.unpack_from("<ii", payload, 0)
    if npts <= 0 or len(payload) < 12 + 16 * npts:
        return ""
    pts = [_geom_pt(payload, 12 + 16 * i) for i in range(npts)]
    if closed:
        return "(" + ",".join(pts) + ")"
    return "[" + ",".join(pts) + "]"


def decode_polygon(b: bytes) -> str:
    payload, _, _ = _var(b)
    if len(payload) < 36:
        return ""
    # 磁盘 POLYGON = npts(int32) + boundbox(BOX 32B) + points*16B
    # （geo_decls.h POLYGON 结构；points 从 offset 36 起）
    npts = struct.unpack_from("<i", payload, 0)[0]
    if npts <= 0 or len(payload) < 36 + 16 * npts:
        return ""
    pts = [_geom_pt(payload, 36 + 16 * i) for i in range(npts)]
    return "(" + ",".join(pts) + ")"


def decode_line(b: bytes) -> str:
    if len(b) < 24:
        return ""
    a, bb, c = struct.unpack_from("<ddd", b, 0)
    return f"{{{_fmt_float(a)},{_fmt_float(bb)},{_fmt_float(c)}}}"


def decode_circle(b: bytes) -> str:
    if len(b) < 24:
        return ""
    x, y, r = struct.unpack_from("<ddd", b, 0)
    return f"<({_fmt_float(x)},{_fmt_float(y)}),{_fmt_float(r)}>"


def decode_char(b: bytes) -> str:
    return chr(b[0]) if b else ""


def decode_unknown(b: bytes) -> str:
    return decode_bytes(b)


def decode_pg_node_tree(b: bytes) -> str:
    return _decode_varlena_text(b)


def decode_oidvector(b: bytes) -> str:
    n = len(b) // 4
    return " ".join(str(struct.unpack("<I", b[i * 4 : i * 4 + 4])[0]) for i in range(n))


def decode_int2vector(b: bytes) -> str:
    n = len(b) // 2
    return " ".join(str(struct.unpack("<h", b[i * 2 : i * 2 + 2])[0]) for i in range(n))


def decode_default(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return "\\x" + b.hex()


def sql_string_literal(v: str) -> str:
    """把值转成安全的 SQL 字符串字面量。

    常规文本（无单引号/反斜杠/控制字节）输出 'value'（与历史格式一致）；
    含需要转义的字符时用 E'' 风格：单引号 ''、反斜杠 \\\\、\n/\r/\t
    转义序列，其余控制字节（0x01-0x08 等）用 \\xNN 转义。
    防止值中的换行/控制字节把 INSERT 语句拆断（psql 会把行首 \\ 当命令），
    同时保证含二进制残留的字段值（如未解码的 aclitem[] 原始字节）
    输出为语法合法的 SQL。
    """
    needs_escape = False
    for ch in v:
        o = ord(ch)
        if ch == "'" or ch == "\\" or o < 32 or o == 127:
            needs_escape = True
            break
    if not needs_escape:
        return "'" + v + "'"
    out = ["E'"]
    for ch in v:
        o = ord(ch)
        if ch == "'":
            out.append("''")
        elif ch == "\\":
            out.append("\\\\")
        elif o == 10:
            out.append("\\n")
        elif o == 13:
            out.append("\\r")
        elif o == 9:
            out.append("\\t")
        elif o < 32 or o == 127:
            out.append("\\x%02X" % o)
        else:
            out.append(ch)
    out.append("'")
    return "".join(out)


def decode_int16(b: bytes) -> str:
    """金仓 int16（4659）：16 字节小端有符号整数。"""
    return str(int.from_bytes(b[:16], "little", signed=True))


# OID -> 解码函数
DECODERS = {
    BOOLOID: decode_bool,
    INT2OID: decode_int2,
    INT4OID: decode_int4,
    INT8OID: decode_int8,
    4659: decode_int16,
    FLOAT4OID: decode_float4,
    FLOAT8OID: decode_float8,
    TEXTOID: decode_text,
    NAMEOID: decode_name,
    BPCHAROID: decode_bpchar,
    VARCHAROID: decode_varchar,
    BYTEAOID: decode_bytea,
    OIDOID: decode_oid,
    XIDOID: decode_xid,
    CIDOID: decode_cid,
    TIDOID: decode_tid,
    DATEOID: decode_date,
    8020: decode_timestamp,  # 金仓 oracle DATE：8B 微秒，epoch 2000-01-01（同 PG timestamp）
    TIMEOID: decode_time,
    TIMESTAMPOID: decode_timestamp,
    TIMESTAMPTZOID: decode_timestamptz,
    TIMETZOID: decode_timetz,
    INTERVALOID: decode_interval,
    NUMERICOID: decode_numeric,
    UUIDOID: decode_uuid,
    JSONOID: decode_json,
    JSONBOID: decode_jsonb,  # P1-6: jsonb 二进制解码（原误挂 decode_json 纯文本）
    INETOID: decode_inet,
    CIDROID: decode_cidr,
    MACADDROID: decode_macaddr,
    MACADDR8OID: decode_macaddr8,
    BITOID: decode_bit,
    VARBITOID: decode_bit,
    MONEYOID: decode_money,
    CHAROID: decode_char,
    UNKNOWNOID: decode_unknown,
    PG_NODE_TREEOID: decode_pg_node_tree,
    OIDVECTOROID: decode_oidvector,
    INT2VECTOROID: decode_int2vector,
    # 几何类型（600/601/602/603/604/628/718）
    600: decode_point,
    601: decode_lseg,
    602: decode_path,
    603: decode_box,
    604: decode_polygon,
    628: decode_line,
    CIRCLEOID: decode_circle,
}
# P1-6: 数组类型统一走 decode_array
for _oid in ARRAY_TYPE_OIDS:
    DECODERS[_oid] = decode_array


def decode_value(oid: int, raw: bytes) -> str:
    """按 OID 解码字段值（raw 为已去 TOAST 的原始数据或原始二进制）。"""
    if raw is None:
        return "NULL"
    # 枚举类型：磁盘存枚举成员 oid（int4），查 pg_enum 映射转标签
    enum_map = ENUM_MAP.get(oid)
    if enum_map is not None and len(raw) >= 4:
        member_oid = struct.unpack("<I", raw[:4])[0]
        return enum_map.get(member_oid, str(member_oid))
    dec = DECODERS.get(oid, decode_default)
    try:
        return dec(raw)
    except Exception:
        return decode_default(raw)


# 枚举映射：{枚举类型 oid: {成员 oid: 标签}}，由 catalog.load_enum_map 注入
ENUM_MAP: dict = {}


def set_enum_map(m: dict) -> None:
    """注入 pg_enum 映射（导出前调用；无枚举时传空 dict 清空）。"""
    global ENUM_MAP
    ENUM_MAP = dict(m)