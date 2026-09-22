# -*- coding: utf-8 -*-
# version: 2.2
"""
pg2sql.binary
二进制基础工具：小端读取、varlena 解析（PG 真实磁盘格式）、TOAST 压缩解压。

v2.0 重写（对照 PostgreSQL varatt.h / pg_lzcompress.c 与 pg_filedump/PDU）：
  - 4B varlena 头改为小端 + 低 2 位 tag 分派（原先误用大端 + bit7 分派）
  - 外联指针 18B 解析（0x01 0x12 + 16B varatt_external 小端），
    rawsize/extsize 字段顺序自适应（标准 PG 与金仓标注两种顺序）
  - 新增 PGLZ（LSB-first 控制位）与 LZ4 block 解压
  - 删除旧版大端虚构格式死代码（get_varlena_payload/detoast_external）
"""
import struct

# v2.2: 库文本编码（由 main.py 按库探测/--encoding 设置）。
# 默认 UTF-8；非 UTF-8 库（LATIN1/GB18030/GBK/SQL_ASCII 等）按库编码解码，
# 解码失败字节回退 latin-1（逐字节 0x00-0xFF 均可逆，不产生 � 数据损坏）。
_TEXT_ENCODING = "utf-8"


def set_text_encoding(enc: str) -> None:
    """设置全库文本解码编码（main.py 入口调用）。"""
    global _TEXT_ENCODING
    _TEXT_ENCODING = enc or "utf-8"


def get_text_encoding() -> str:
    return _TEXT_ENCODING


def decode_bytes(raw: bytes) -> str:
    """按库编码解码字节；失败时 latin-1 逐字节兜底（字节可逆，绝不丢失）。

    返回的 str 以 UTF-8 语义写入输出文件（main.py 统一 utf-8 写文件），
    因此输出文件永远是合法 UTF-8；无法解码的源字节（如非法 UTF-8 残留）
    映射为对应 U+0080-U+00FF 字符（UTF-8 编码 0xC2 0x80-0xC3 0xBF），
    PostgreSQL/金仓 UTF8 库可导入（实测 U+009B 等 C1 控制字符被接受）。
    """
    enc = _TEXT_ENCODING
    try:
        return raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1")


def u16(b: bytes, off: int = 0) -> int:
    """小端 uint16"""
    return struct.unpack_from("<H", b, off)[0]


def u32(b: bytes, off: int = 0) -> int:
    """小端 uint32"""
    return struct.unpack_from("<I", b, off)[0]


def i32(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<i", b, off)[0]


def u64(b: bytes, off: int = 0) -> int:
    """小端 uint64"""
    return struct.unpack_from("<Q", b, off)[0]


def i64(b: bytes, off: int = 0) -> int:
    return struct.unpack_from("<q", b, off)[0]


def cstring(b: bytes, off: int = 0) -> str:
    """读取 C 风格以 NUL 结尾的字符串（按库编码解码，失败时 latin-1 逐字节兜底）"""
    end = b.find(b"\x00", off)
    if end < 0:
        end = len(b)
    return decode_bytes(b[off:end])


def hexstr(b: bytes) -> str:
    return b.hex()


def align4(n: int) -> int:
    return (n + 3) & ~3


# ======================================================================
# varlena 核心（PG 真实磁盘格式，小端主机布局）
# 参考 PostgreSQL src/include/postgres.h / varatt.h：
#   VARATT_IS_1B_E:  首字节 == 0x01（外联指针 tag，第二个字节 0x12=VARTAG_ONDISK）
#   VARATT_IS_1B:    首字节 & 0x01（短头，VARSIZE = first >> 1，含头最大 127）
#   VARATT_IS_4B_U:  首字节 & 0x03 == 0x00（4B 头未压缩）
#   VARATT_IS_4B_C:  首字节 & 0x03 == 0x02（4B 头内联压缩）
#   VARSIZE_4B:      (小端 u32 >> 2) & 0x3FFFFFFF
# ======================================================================

VARTAG_ONDISK = 18  # 0x12

# varlena 类型标记
VARLENA_EXTERNAL = "ext"      # 18B 外联指针
VARLENA_1B = "1b"             # 1 字节短头
VARLENA_4B = "4b"             # 4 字节头未压缩
VARLENA_4B_COMPRESSED = "4bc" # 4 字节头内联压缩


def varlena_parse(b: bytes, off: int = 0):
    """解析 varlena 头。

    返回 (kind, total_size, payload_off, payload_len)。
    - kind: 'ext' / '1b' / '4b' / '4bc'；无法识别返回 (None, 0, 0, 0)
    - total_size: 含头总长度（ext 为 18）
    - payload_off: 数据起始偏移（ext 为 off+2）
    """
    if off >= len(b):
        return (None, 0, 0, 0)
    first = b[off]
    # 外联指针优先判定（0x01 也是奇数，必须先判）
    if first == 0x01:
        if off + 2 <= len(b) and b[off + 1] == VARTAG_ONDISK:
            return (VARLENA_EXTERNAL, 18, off + 2, 16)
        return (None, 0, 0, 0)
    if first & 0x01:
        # 1B 短头: VARSIZE = first >> 1（含头）
        total = first >> 1
        if total == 0:
            return (None, 0, 0, 0)
        return (VARLENA_1B, total, off + 1, total - 1)
    if off + 4 > len(b):
        return (None, 0, 0, 0)
    word = u32(b, off)
    total = (word >> 2) & 0x3FFFFFFF
    if total < 4:
        return (None, 0, 0, 0)
    # PG varatt.h：4B 头低 3 位 010=未压缩(U)、110=内联压缩(C)、100=外联(X)。
    # 外联指针恒用 1B 头（first==0x01 已在上方判定），此处 U/C 二选一。
    if (first & 0x06) == 0x06:
        return (VARLENA_4B_COMPRESSED, total, off + 4, total - 4)
    return (VARLENA_4B, total, off + 4, total - 4)


def parse_external_pointer(b: bytes, off: int = 0):
    """解析 18B TOAST 外联指针（0x01 0x12 + 16B varatt_external，小端）。

    真实布局 (varatt.h):
        [va_rawsize 4B][va_extinfo 4B][va_valueid 4B][va_toastrelid 4B]
      - va_rawsize: 原始 varlena 总长（含 4B 头）
      - va_extinfo: PG14+ 低 30 位 = extsize（外部存储长度，压缩后），
        高 2 位 = 压缩方法（0=pglz, 1=lz4）；PG12 前直接是 extsize
      - 压缩判定: extsize < rawsize - 4

    金仓实测数据此前按 [extsize][rawsize] 标注（未压缩时两值相等无法区分）。
    本函数按掩码后大小自适应两种顺序，两种格式均正确。

    返回 dict 或 None（字段不合理）。
    """
    if off + 18 > len(b):
        return None
    if b[off] != 0x01 or b[off + 1] != VARTAG_ONDISK:
        return None
    f1 = u32(b, off + 2)
    f2 = u32(b, off + 6)
    valueid = u32(b, off + 10)
    toastrelid = u32(b, off + 14)
    # 合理性校验
    if not (100 <= valueid <= 100000000):
        return None
    if not (100 <= toastrelid <= 100000000):
        return None
    f1v = f1 & 0x3FFFFFFF
    f2v = f2 & 0x3FFFFFFF
    if f1v >= f2v:
        # 标准 PG 顺序: [rawsize][extinfo]
        rawsize, extinfo = f1, f2
    else:
        # 金仓标注顺序: [extsize][rawsize]（无 method 位）
        rawsize, extinfo = f2, f1
    if not (1 <= rawsize <= 100000000):
        return None
    extsize = extinfo & 0x3FFFFFFF
    method = (extinfo >> 30) & 0x03
    if not (1 <= extsize <= 100000000):
        return None
    return {
        "rawsize": rawsize,
        "extsize": extsize,
        "valueid": valueid,
        "toastrelid": toastrelid,
        "method": method,               # 0=pglz, 1=lz4
        "compressed": extsize < rawsize - 4,
    }


# ======================================================================
# TOAST 压缩解压
# 参考 PostgreSQL src/common/pg_lzcompress.c 与 LZ4 block format
# ======================================================================

TOAST_COMPRESS_METHOD_PGLZ = 0
TOAST_COMPRESS_METHOD_LZ4 = 1


def pglz_decompress(data: bytes, expected_size: int):
    """PGLZ 解压（PG pg_lzcompress.c pglz_decompress 的忠实移植）。

    格式：
      - 控制字节 8 个 tag 位，LSB-first；1=匹配(2B)，0=字面(1B)
      - 匹配: len = (b1 & 0x0F) + 3（b1 高 nibble 是 offset 高 4 位）
              off = ((b1 & 0xF0) << 4) | b2（范围 1..4095，0 = 损坏）
              len == 18 时再读 1 个扩展字节
      - 8 位用完读下一个控制字节

    返回解压 bytes；长度不符或数据损坏返回 None。
    """
    out = bytearray()
    sp, n = 0, len(data)
    destend = expected_size
    while sp < n and len(out) < destend:
        ctrl = data[sp]
        sp += 1
        for _ in range(8):
            if sp >= n or len(out) >= destend:
                break
            if ctrl & 1:
                # 匹配
                if sp + 2 > n:
                    return None
                b1 = data[sp]
                b2 = data[sp + 1]
                sp += 2
                length = (b1 & 0x0F) + 3
                off = ((b1 & 0xF0) << 4) | b2
                if length == 18:
                    if sp >= n:
                        return None
                    length += data[sp]
                    sp += 1
                if off == 0:
                    return None  # 损坏（防死循环）
                if len(out) < off:
                    return None  # 回引超出已输出范围
                remaining = min(length, destend - len(out))
                src = len(out) - off
                # 重叠安全的逐字节复制（PG 用倍增 memcpy，语义等价）
                for i in range(remaining):
                    out.append(out[src + i])
            else:
                # 字面字节
                out.append(data[sp])
                sp += 1
            ctrl >>= 1
    if len(out) != expected_size:
        return None
    return bytes(out)


def lz4_block_decompress(data: bytes, expected_size: int):
    """LZ4 block 格式解压（纯 Python，参照 LZ4 block format spec）。

    格式：
      - token: 高 4 位 literal 长度，低 4 位 match 长度 - 4
      - 长度 15 时读扩展字节序列（每字节 0-255，<255 停止）
      - match: 2 字节小端 offset（1..65535，0 非法）
      - 最后一段只有 literal（无 match 部分）

    返回解压 bytes；长度不符或数据损坏返回 None。
    """
    out = bytearray()
    sp, n = 0, len(data)
    while sp < n:
        token = data[sp]
        sp += 1
        # literal 长度
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if sp >= n:
                    return None
                b = data[sp]
                sp += 1
                lit_len += b
                if b != 255:
                    break
        if sp + lit_len > n:
            return None
        out += data[sp:sp + lit_len]
        sp += lit_len
        if sp >= n:
            break  # 最后一段：只有 literal
        # match
        if sp + 2 > n:
            return None
        offset = data[sp] | (data[sp + 1] << 8)
        sp += 2
        if offset == 0:
            return None
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if sp >= n:
                    return None
                b = data[sp]
                sp += 1
                match_len += b
                if b != 255:
                    break
        if len(out) < offset:
            return None  # 回引超出已输出范围
        src = len(out) - offset
        for i in range(match_len):
            out.append(out[src + i])
    if len(out) != expected_size:
        return None
    return bytes(out)


def toast_decompress(payload: bytes, expected_size: int, method: int = TOAST_COMPRESS_METHOD_PGLZ):
    """解压 TOAST 压缩数据。

    payload 为 chunk_data 重组结果（已去 varlena 头）：
      前 4 字节小端 tcinfo: 低 30 位 = 原始数据长度（不含 varlena 头），
      高 2 位 = 压缩方法（0=pglz, 1=lz4）。
    expected_size 为外联指针中的 rawsize - 4（两处应一致）。

    返回解压 bytes 或 None。
    """
    if len(payload) < 5:
        return None
    tcinfo = u32(payload, 0)
    rawsize = tcinfo & 0x3FFFFFFF
    tc_method = (tcinfo >> 30) & 0x03
    if rawsize != expected_size:
        return None  # 长度校验失败（指针与数据不一致）
    body = payload[4:]
    if tc_method == TOAST_COMPRESS_METHOD_LZ4:
        return lz4_block_decompress(body, rawsize)
    if tc_method == TOAST_COMPRESS_METHOD_PGLZ:
        return pglz_decompress(body, rawsize)
    return None


def rebuild_varlena(payload: bytes) -> bytes:
    """将纯 payload 重建为合法 varlena（含头），供类型解码层使用。

    小端 4B 头: word = (total << 2) | 0x00，total = 4 + len(payload)。
    （旧版误用大端 0x80000000 头，与磁盘真实格式不符。）
    """
    total = 4 + len(payload)
    if total < 128:
        # 1B 短头也合法: header = (total << 1) | 1
        return bytes([(total << 1) | 1]) + payload
    return struct.pack("<I", total << 2) + payload
