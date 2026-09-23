#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# version: 1.28
"""
pg2sql - 离线解析 PostgreSQL 堆数据文件并导出为 SQL
用法: python3 main.py <data_file> [options]
帮助: python3 main.py -h
"""
import sys
import os
import json
import struct
import argparse
import multiprocessing
from multiprocessing import Pool

# 确保能 import 同级 pg2sql 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pg2sql import __version__
from pg2sql.page import PAGE_SIZE
from pg2sql.heapfile import HeapFile, _extract_fields_direct
from pg2sql.catalog import (
    load_meta_json, parse_catalog_offline,
    auto_discover_meta, auto_discover_all_tables,
    export_meta_to_json, is_kingbase_datadir, detect_pg_version,
    load_enum_map,
    TableMeta, Column,
)
from pg2sql.toast import ToastFile
from pg2sql.types import sql_string_literal


# ======================================================================
# 帮助文本
# ======================================================================

EPILOG = """
示例:
  # 查看帮助
  python3 main.py -h

  # 自动发现表结构并导出 DDL + INSERT (无需 --catalog-json)
  python3 main.py /var/lib/postgresql/data/base/16384/16387 --ddl --sql

  # 使用 JSON 元数据导出 (更精确, 需先运行 export_meta.sql)
  python3 main.py /var/lib/postgresql/data/base/16384/16387 --catalog-json meta.json --ddl --sql

  # 导出为 CSV 格式 (LOAD DATA)
  python3 main.py /var/lib/pg/data/base/16384/16387 --data

  # 恢复已删除的行
  python3 main.py /var/lib/pg/data/base/16384/16387 --sql --deleted

  # 限制输出 100 行
  python3 main.py data_file --catalog-json meta.json --sql --limit 100

  # 指定输出目录
  python3 main.py data_file --catalog-json meta.json --sql --output /tmp/result

  # 8 进程并发解析大文件
  python3 main.py big_data_file --catalog-json meta.json --sql --parallel 8

  # 强制遍历损坏文件
  python3 main.py corrupt_file --catalog-json meta.json --sql --force

  # 关联 TOAST 表文件解析大字段
  python3 main.py data_file --catalog-json meta.json --sql --toast /path/to/toast_table_file

  # 列出元数据中的所有表
  python3 main.py --catalog-json meta.json --list-tables

  # 列出数据目录中的所有数据库 (OID + 名称 + 目录路径)
  python3 main.py /var/lib/postgresql/data/base/ --list-db
  # 或通过 --datadir 指定 PG 数据目录
  python3 main.py --datadir /var/lib/postgresql/data --list-db

  # 列出指定数据库目录中的所有表 (OID + 表名 + 文件路径)
  python3 main.py /var/lib/postgresql/data/base/16384 --list-tables-db

  # 离线导出元数据 JSON (无需在线实例, 替代 export_meta.sql)
  python3 main.py /var/lib/postgresql/data/base/16384 --export-meta -o /tmp/meta.json

  # 从数据目录离线解析元数据 (无需 JSON)
  python3 main.py /var/lib/pg/data/base/16384/16387 --datadir /var/lib/pg/data --db-oid 16384 --sql --ddl

  # 只导出指定字段 (SQL/CSV 均生效)
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --sql --fields id,name,remark

  # CSV 首行输出字段名 (配合 COPY ... WITH (FORMAT csv, HEADER true) 导入)
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --data --header -o out.csv

  # 非 UTF-8 源库指定编码 (LATIN1/GB18030/GBK/SQL_ASCII 等, 默认自动探测)
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --sql --encoding gbk

  # 显式指定页面大小 (默认自动探测 8/16/32KB)
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --data --page-size 32768

  # 恢复已删除的行 (t_xmax 已设置但未被 vacuum)
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --sql --deleted
  python3 main.py data_file --datadir /var/lib/pg/data --db-oid 16384 --sql --only-deleted

更多详情: pg2sql - 离线解析 PostgreSQL/KingbaseES 堆数据文件工具
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pg2sql",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "pg2sql v%s - 离线解析 PostgreSQL/KingbaseES 堆数据文件并导出为 SQL\n"
            "\n"
            "无需实例运行，直接读取数据文件(base/{db_oid}/{table_oid})，\n"
            "解析堆页面（8/16/32KB 自动探测）中的 HeapTuple，输出 DDL 和 INSERT 语句。\n"
            "支持自动发现表结构（无需 --catalog-json）、恢复已删除行、\n"
            "TOAST 大字段重组、坏页容错、并发解析。\n"
            "\n"
            "pg2sql 之于 PostgreSQL Heap。"
            % __version__
        ),
        epilog=EPILOG,
        add_help=False,
    )

    # --- 位置参数 ---
    parser.add_argument("datafile", nargs="?", default=None,
                        help="PostgreSQL 堆数据文件路径 (如 base/16384/16387)")

    # --- 帮助 ---
    parser.add_argument("-h", "--help", action="help",
                        help="显示此帮助信息并退出")
    parser.add_argument("--version", action="version",
                        version="pg2sql v%s" % __version__,
                        help="显示版本号并退出")

    # --- 元数据 ---
    meta_group = parser.add_argument_group("元数据", "表结构来源 (可选, 不指定则自动发现)")
    meta_group.add_argument("--catalog-json", metavar="FILE", default=None,
                            help="从 JSON 文件加载元数据 (由 export_meta.sql 导出)")
    meta_group.add_argument("--datadir", metavar="DIR", default=None,
                            help="PostgreSQL 数据目录 (离线解析全部表结构)")
    meta_group.add_argument("--db-oid", type=int, default=5, metavar="OID",
                            help="目标数据库 OID (配合 --datadir 使用, 默认 5)")
    meta_group.add_argument("--table-name", default=None, metavar="NAME",
                            help="指定表名 (当元数据中有多个表时筛选)")

    # --- 输出模式 ---
    out_group = parser.add_argument_group("输出模式")
    out_group.add_argument("--ddl", action="store_true", default=False,
                           help="输出 CREATE TABLE DDL 语句")
    out_group.add_argument("--sql", action="store_true", default=False,
                           help="输出 INSERT 语句")
    out_group.add_argument("--data", action="store_true", default=False,
                           help="输出 CSV 格式 (可用 LOAD DATA 导入)")
    out_group.add_argument("--deleted", action="store_true", default=False,
                           help="输出已删除和未删除的行 (t_xmax 已设置但未被 vacuum 清理)")
    out_group.add_argument("--only-deleted", action="store_true", default=False,
                           help="只输出已删除的行 (不含未删除行)")
    out_group.add_argument("--count", action="store_true", default=False,
                           help="仅统计行数, 不输出数据")
    out_group.add_argument("--list-tables", action="store_true", default=False,
                           help="列出元数据 JSON 中的所有表, 然后退出")
    out_group.add_argument("--list-db", action="store_true", default=False,
                           help="列出数据目录中的所有数据库 (OID + 名称 + 目录路径)")
    out_group.add_argument("--list-tables-db", action="store_true", default=False,
                           help="列出指定数据库目录中的所有表 (扫描文件 + 解析 pg_class)")
    out_group.add_argument("--export-meta", action="store_true", default=False,
                           help="从数据库目录离线导出元数据 JSON (替代 export_meta.sql)")

    # --- 输出选项 ---
    opt_group = parser.add_argument_group("输出选项")
    opt_group.add_argument("--output", "-o", default=None, metavar="PATH",
                           help="输出到文件或目录 (目录则自动命名 schema.table.type)")
    opt_group.add_argument("--limit", type=int, default=0, metavar="N",
                           help="限制输出行数 (0=不限制)")
    opt_group.add_argument("--fields", default=None, metavar="COL1,COL2",
                           help="只导出指定字段 (逗号分隔, 如 --fields id,name; 默认全部)")
    opt_group.add_argument("--header", action="store_true", default=False,
                           help="CSV 首行输出字段名 (配合 --data, 与 COPY HEADER true 兼容)")
    opt_group.add_argument("--complete-insert", action="store_true", default=True,
                           help="INSERT 语句包含字段名 (默认开启)")
    opt_group.add_argument("--no-complete-insert", action="store_false", dest="complete_insert",
                           help="INSERT 语句不含字段名")
    opt_group.add_argument("--replace", action="store_true", default=False,
                           help="使用 REPLACE INTO 代替 INSERT INTO")
    opt_group.add_argument("--delimiter", default=",", metavar="CHAR",
                           help="CSV 模式分隔符 (配合 --data, 默认逗号)")
    opt_group.add_argument("--force", action="store_true", default=False,
                           help="强制遍历整个文件 (用于损坏/不完整文件)")

    # --- 高级选项 ---
    adv_group = parser.add_argument_group("高级选项")
    adv_group.add_argument("--toast", default=None, metavar="FILE",
                            help="TOAST 表文件路径 (用于重组大字段)")
    adv_group.add_argument("--toast-cache", default="light", choices=["light", "full"],
                            help="TOAST 缓存模式: light=轻量索引+页级LRU缓存(默认, 内存小); "
                                 "full=全量 payload 入内存(慢磁盘提速明显, 内存≈TOAST文件大小×2)")
    adv_group.add_argument("--page-size", type=int, default=0, metavar="N",
                           help="页面大小 (默认 0=自动探测页头编码; 可显式指定 8192/16384/32768)")
    adv_group.add_argument("--parallel", type=int, default=0, metavar="N",
                           help="并发进程数 (大文件加速, 0=单进程)")
    adv_group.add_argument("--encoding", default="auto", metavar="CODEC",
                           help="源库字符编码 (默认 auto=从 global/pg_database 自动探测; "
                                "非 UTF-8 库如 LATIN1/GB18030/GBK/SQL_ASCII 可显式指定, "
                                "如 --encoding gbk)")
    adv_group.add_argument("--verbose", action="store_true", default=False,
                           help="输出详细日志到 stderr")

    return parser


# ======================================================================
# 核心逻辑
# ======================================================================

def _configure_encoding(args) -> None:
    """设置库文本解码编码（v1.25）。

    优先级：用户显式 --encoding > 自动探测（global/pg_database 的 datencoding）> UTF-8。
    探测失败或未知编码 ID 时保持默认 UTF-8（写文件始终 UTF-8，不会产生非法字节）。
    """
    from pg2sql.binary import set_text_encoding
    if getattr(args, "encoding", "auto") and args.encoding != "auto":
        try:
            set_text_encoding(args.encoding)
            return
        except Exception:
            pass
    datadir = getattr(args, "datadir", None)
    db_oid = getattr(args, "db_oid", None)
    if not datadir and getattr(args, "datafile", None):
        # 从数据文件路径反推数据目录（base/{dboid}/file 或表空间外文件）
        db_dir = os.path.dirname(os.path.abspath(args.datafile))
        cand = os.path.dirname(db_dir)
        if os.path.isfile(os.path.join(cand, "global", "1262")):
            datadir = cand
    if datadir and db_oid:
        from pg2sql.catalog import detect_database_encoding
        enc = detect_database_encoding(datadir, int(db_oid))
        if enc:
            set_text_encoding(enc)

def load_metadata(args) -> dict:
    """加载元数据, 返回 {"database": str, "tables": {name: TableMeta}}。

    优先级:
      1. --catalog-json: 从 JSON 文件加载
      2. --datadir: 从数据目录离线解析全部表
      3. 自动发现: 从数据文件路径反推数据库目录，自动解析系统表
    """
    # 1. JSON 元数据
    if args.catalog_json:
        if not os.path.exists(args.catalog_json):
            error_exit(f"元数据文件不存在: {args.catalog_json}")
        meta = load_meta_json(args.catalog_json)
        if args.verbose:
            log(f"从 JSON 加载元数据: {len(meta['tables'])} 个表")
        return meta

    # 2. --datadir 指定数据目录
    if args.datadir:
        if not os.path.isdir(args.datadir):
            error_exit(f"数据目录不存在: {args.datadir}")
        base = os.path.join(args.datadir, "base", str(args.db_oid))
        if not os.path.isdir(base):
            error_exit(f"数据库目录不存在: {base}")
        load_enum_map(base, detect_pg_version(args.datadir) or 0,
                      is_kingbase_datadir(args.datadir))
        meta = auto_discover_all_tables(base, page_size=args.page_size)
        if args.verbose:
            log(f"从数据目录自动发现元数据: {len(meta['tables'])} 个表")
        return meta

    # 3. 自动发现: 从数据文件路径反推
    if args.datafile:
        data_file = os.path.abspath(args.datafile)
        if os.path.isfile(data_file):
            try:
                load_enum_map(os.path.dirname(data_file), detect_pg_version(
                    os.path.dirname(os.path.dirname(data_file))) or 0,
                    is_kingbase_datadir(os.path.dirname(data_file)))
                meta = auto_discover_meta(data_file, page_size=args.page_size)
                if args.verbose:
                    tm = list(meta["tables"].values())[0]
                    log(f"自动发现表结构: {tm.schema}.{tm.relname} ({len(tm.columns)} 列)")
                return meta
            except Exception as e:
                if args.verbose:
                    log(f"自动发现失败: {e}")
                    import traceback
                    traceback.print_exc()
                error_exit(f"无法自动发现表结构: {e}\n请使用 --catalog-json 提供元数据, 或确保数据文件同目录下有 pg_class(1259) 和 pg_attribute(1249)")
        elif os.path.isdir(data_file):
            # datafile 是数据库目录 (如 base/16384/)
            load_enum_map(data_file, detect_pg_version(
                os.path.dirname(os.path.dirname(data_file))) or 0,
                is_kingbase_datadir(data_file))
            meta = auto_discover_all_tables(data_file, page_size=args.page_size)
            if args.verbose:
                log(f"从数据库目录自动发现元数据: {len(meta['tables'])} 个表")
            return meta

    error_exit("无法获取表结构元数据。请指定 --catalog-json, 或确保数据文件同目录下有系统目录表 (pg_class/pg_attribute)")


def select_table(meta: dict, args) -> TableMeta:
    """从元数据中选定目标表。"""
    tables = meta["tables"]
    # 优先用 table_name 筛选
    if args.table_name:
        # 尝试 schema.table 或 table（支持非 public 默认 schema，如金仓 ray）
        for key in [args.table_name, f"public.{args.table_name}"]:
            if key in tables:
                return tables[key]
        if "." not in args.table_name:
            # 数字键（relfilenode）与 full_name 指向同一 TableMeta，按 (schema, relname) 去重
            hits = {}
            for v in tables.values():
                if v.relname == args.table_name:
                    hits[f"{v.schema}.{v.relname}"] = v
            if len(hits) == 1:
                return list(hits.values())[0]
            if len(hits) > 1:
                error_exit(f"表名 {args.table_name} 存在多个 schema，请使用 schema.table 指定")
        error_exit(f"未找到表: {args.table_name}")

    # 去重：按 schema.relname 唯一化（排除 relfilenode 数字键）
    user_tables = {}
    for k, v in tables.items():
        if k.startswith("pg_") or v.schema in ("pg_catalog", "information_schema"):
            continue
        unique_key = f"{v.schema}.{v.relname}"
        user_tables[unique_key] = v

    if len(user_tables) == 1:
        return list(user_tables.values())[0]

    if len(user_tables) == 0:
        error_exit("元数据中未找到用户表")

    # 多张表时提示用户选择
    log("找到多张表, 请用 --table-name 指定:")
    for name, tm in sorted(user_tables.items()):
        log(f"  {tm.schema}.{tm.relname}  (relfilenode={tm.relfilenode})")
    error_exit("未指定表名")


def _find_toast_path(args):
    """查找 TOAST 表文件路径。

    优先级:
    1. --toast 显式指定
    2. 扫描主表数据文件，从外联指针中提取 toastrelid，
       在同目录查找对应文件（金仓: [01 12] 开头 18B 小端指针）

    返回 toast 文件路径或 None。
    """
    if args.toast:
        if not os.path.exists(args.toast):
            error_exit(f"TOAST 文件不存在: {args.toast}")
        return args.toast

    # 扫描主表数据区，寻找金仓外联指针中的 toastrelid
    toastrelids = set()
    try:
        from pg2sql.page import Page
        from pg2sql.heapfile import _is_kb_external, KB_EXTERNAL_SIZE

        page_size = args.page_size or _probe_file_page_size(args.datafile)
        # 外联指针通常在前几页就出现，限制扫描页数避免大文件全扫
        MAX_SCAN_PAGES = 8
        file_size = os.path.getsize(args.datafile)
        npages = min(file_size // page_size, MAX_SCAN_PAGES)
        with open(args.datafile, "rb") as f:
            for pageno in range(npages):
                raw = f.read(page_size)
                if len(raw) < page_size:
                    break
                page = Page(pageno, raw, page_size=page_size)
                if not page.has_valid_layout:
                    continue
                # 在数据区扫描外联指针
                pd_upper = page.header.get("upper", 0)
                pd_special = page.header.get("special", page_size)
                if pd_upper < page.header_size or pd_upper >= pd_special:
                    continue
                # 快速预判: 页面数据区含 0x01 0x12 才逐字节细扫
                region = raw[pd_upper:pd_special]
                if b"\x01\x12" not in region:
                    continue
                pos = pd_upper
                while pos + KB_EXTERNAL_SIZE <= pd_special:
                    if _is_kb_external(raw, pos):
                        relid = struct.unpack_from("<I", raw, pos + 14)[0]
                        toastrelids.add(relid)
                        pos += KB_EXTERNAL_SIZE
                    else:
                        pos += 1  # 外联指针可在任意字节位置（跟随 varlena 数据流）
                if toastrelids:
                    break  # 找到就停止（通常一个表只有一个 TOAST 表）
    except Exception:
        pass

    if not toastrelids:
        return None

    # 在同目录找 TOAST 表文件（轻量验证：只读前 2 页确认有有效 chunk）
    db_dir = os.path.dirname(os.path.abspath(args.datafile))
    for relid in sorted(toastrelids):
        toast_path = os.path.join(db_dir, str(relid))
        if os.path.exists(toast_path):
            try:
                toast = ToastFile(toast_path, page_size=args.page_size)
                toast.load(max_pages=2)  # 只读前 2 页快速验证
                n_chunks = len(toast.get_all_chunk_ids())
                if n_chunks > 0:
                    return toast_path
            except Exception:
                continue

    return None


def _probe_file_page_size(path: str) -> int:
    """自动探测数据文件页大小（页头编码）；失败回退 8192。"""
    from pg2sql.page import detect_page_size
    try:
        with open(path, "rb") as f:
            ps = detect_page_size(f.read(130))
        if ps:
            return ps
    except OSError:
        pass
    return PAGE_SIZE


def _auto_detect_toast(args, hf, table_meta):
    """自动发现并关联 TOAST 表到 HeapFile。

    --toast-cache light: 轻量位置索引 + 页级 LRU 缓存（默认）
    --toast-cache full:  全量 payload 入内存（慢磁盘零 IO 重组）
    """
    toast_path = _find_toast_path(args)
    if not toast_path:
        if args.verbose:
            log("未发现 TOAST 外联或未找到 TOAST 表文件")
        return
    try:
        import os as _os
        toast = ToastFile(toast_path, page_size=args.page_size)
        fsize = _os.path.getsize(toast_path)
        use_full = (getattr(args, "toast_cache", "light") == "full") or fsize <= 64 * 1024 * 1024
        if use_full:
            if args.verbose:
                log(f"TOAST 全量加载 ({fsize // 1048576}MB → 内存)...")
            toast.load()
        else:
            if args.verbose:
                log(f"TOAST 表较大 ({fsize // 1048576}MB)，使用轻量位置索引...")
            toast.build_index(verbose=args.verbose, log=log)
        n_chunks = len(toast.get_all_chunk_ids())
        hf.set_toast(toast)
        if args.verbose:
            log(f"自动关联 TOAST 表: {toast_path} ({n_chunks} 个 valueid)")
    except Exception as e:
        if args.verbose:
            log(f"加载 TOAST 表 {toast_path} 失败: {e}")


def _normalize_fields(args, table_meta):
    """规范化并校验 --fields（v1.26）：字符串→字段名列表，校验存在性。

    串行 (run) 与并行 (run_parallel) 两条路径都必须调用——并行入口
    直接走 run_parallel，不经过 run()，若不在此规范化会导致字段名
    子串误匹配（'c_json' 命中 'c_jsonb'）。
    """
    if args.fields:
        if isinstance(args.fields, str):
            args.fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        valid = {c.name for c in table_meta.columns if not c.attdropped}
        missing = [f for f in args.fields if f not in valid]
        if missing:
            error_exit(f"--fields 中不存在的字段: {', '.join(missing)}\n"
                       f"可用字段: {', '.join(sorted(valid))}")


def run(args):
    # --- 加载元数据 ---
    meta = load_metadata(args)

    # --- --list-tables 模式 ---
    if args.list_tables:
        print(f"数据库: {meta['database']}")
        print(f"{'Schema':<20} {'表名':<30} {'relfilenode':<12} {'列数':<6} {'主键'}")
        print("-" * 90)
        seen = set()
        for name, tm in sorted(meta["tables"].items()):
            key = f"{tm.schema}.{tm.relname}"
            if key in seen:
                continue
            seen.add(key)
            pk = ", ".join(tm.primary_key) if tm.primary_key else ""
            ncol = len([c for c in tm.columns if not c.attdropped])
            print(f"{tm.schema:<20} {tm.relname:<30} {tm.relfilenode:<12} {ncol:<6} {pk}")
        return

    # --- 需要数据文件 ---
    if not args.datafile:
        error_exit("请指定数据文件路径, 使用 -h 查看帮助")

    if not os.path.exists(args.datafile):
        error_exit(f"数据文件不存在: {args.datafile}")

    table_meta = select_table(meta, args)

    # --- --fields 字段名校验 (v1.26) ---
    _normalize_fields(args, table_meta)

    # --- 创建 HeapFile（P2-1: 带 PG 主版本，NULL 位图语义随版本；金仓布局差异穿透）---
    hf = HeapFile(args.datafile, page_size=args.page_size,
                  pg_version=_pg_major(meta.get("pg_version", 160000)),
                  is_kingbase=is_kingbase_datadir(os.path.dirname(os.path.abspath(args.datafile))))

    # --- 自动关联 TOAST ---
    _auto_detect_toast(args, hf, table_meta)

    # --- 输出准备 ---
    out_files = {}  # {type: file_handle}
    out_dir = None
    FLUSH_INTERVAL = 1000  # 每写 1000 行 flush 一次

    def _get_out_file(file_type):
        """获取或创建输出文件。file_type: 'sql', 'csv'"""
        if file_type in out_files:
            return out_files[file_type]
        if not args.output:
            return sys.stdout
        out_path = _resolve_output_path(args.output, table_meta, file_type)
        os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
        # buffering=1 行缓冲；大文件场景改为手动定量 flush
        fp = open(out_path, "w", encoding="utf-8", buffering=8192 * 16)
        out_files[file_type] = fp
        if args.verbose:
            log(f"输出到: {out_path}")
        return fp

    def _flush_out_files():
        """flush 所有输出文件（定量刷新，数据落盘）"""
        for fp in out_files.values():
            if fp is not sys.stdout:
                fp.flush()

    # --- DDL ---
    if args.ddl:
        ddl = table_meta.generate_ddl()
        # DDL 写入 sql 文件（与 INSERT 合并）
        fp = _get_out_file("sql")
        fp.write(ddl + "\n\n")
        if args.verbose:
            log("DDL 输出完成")

    # --- 仅 DDL 模式 ---
    if args.ddl and not args.sql and not args.data and not args.count and not args.deleted and not args.only_deleted:
        for fp in out_files.values():
            if fp is not sys.stdout:
                fp.close()
        return

    # --- --count 模式 ---
    if args.count:
        # v1.24: --deleted --count 组合应统计删除行（原实现只认 only_deleted）
        count = 0
        for row in hf.dump_rows(table_meta,
                                include_deleted=args.deleted or args.only_deleted,
                                only_deleted=args.only_deleted, limit=0):
            count += 1
        fp = _get_out_file("sql")
        fp.write(f"-- 总行数: {count}\n")
        if args.verbose:
            log(f"统计完成: {count} 行")
        for fp in out_files.values():
            if fp is not sys.stdout:
                fp.close()
        return

    # --- SQL / DATA 输出 ---
    include_deleted = args.deleted or args.only_deleted
    only_deleted = args.only_deleted
    n_written = 0

    if args.data:
        fp = _get_out_file("csv")
        for line in hf.to_data(table_meta, include_deleted=include_deleted,
                               only_deleted=only_deleted,
                               limit=args.limit, delimiter=args.delimiter,
                               force=args.force, fields=args.fields,
                               header=args.header):
            fp.write(line + "\n")
            n_written += 1
            if n_written % FLUSH_INTERVAL == 0:
                _flush_out_files()
                if args.verbose:
                    log(f"已输出 {n_written} 行...")
    elif args.sql or not args.ddl:
        # INSERT 语句 (默认行为)
        # DDL 和 SQL 写同一文件
        fp = _get_out_file("sql")
        for stmt in hf.to_sql(table_meta, include_deleted=include_deleted,
                              only_deleted=only_deleted,
                              limit=args.limit, complete_insert=args.complete_insert,
                              replace=args.replace, force=args.force,
                              fields=args.fields):
            fp.write(stmt + "\n")
            n_written += 1
            if n_written % FLUSH_INTERVAL == 0:
                _flush_out_files()
                if args.verbose:
                    log(f"已输出 {n_written} 行...")

    # --- 坏页报告 ---
    if hf.bad_pages and args.verbose:
        log(f"跳过 {len(hf.bad_pages)} 个坏页: {hf.bad_pages[:10]}...")

    for fp in out_files.values():
        if fp is not sys.stdout:
            fp.close()
    if args.verbose:
        log("完成")


# ======================================================================
# 并发支持
# ======================================================================

# Pool 共享状态（fork 模式下 COW 零拷贝共享；spawn 模式经 pickle 传递）
_TOAST_INDEX = None  # 轻量: {valueid: [(seq, (pageno, off)), ...]} / 全量: {valueid: [(seq, payload), ...]}
_TOAST_MODE = "light"  # 'light' | 'full'


def _pool_init(toast_index, toast_mode):
    """Pool initializer: 把主进程预建的 TOAST 索引注入每个 worker。"""
    global _TOAST_INDEX, _TOAST_MODE
    _TOAST_INDEX = toast_index
    _TOAST_MODE = toast_mode


def _pg_major(version) -> int:
    """归一化为 PG 主版本号：server_version_num(如 160004) → 16；16 → 16。"""
    try:
        v = int(version or 0)
    except (TypeError, ValueError):
        v = 0
    if v >= 10000:
        return v // 10000
    return v or 12


def _parallel_worker(args_tuple):
    """并发 worker: 只读取并解析分配给自己的页面范围（seek 定位，不读整个文件）。

    TOAST 索引由主进程预建一次、经 Pool initializer 共享（fork COW），
    worker 不再各自扫描 480MB TOAST 文件。

    返回 (batch_idx, [(values, is_deleted), ...])（可 pickle）。
    """
    batch_idx, path, page_size, pageno_start, pageno_end, table_meta_dict, toast_path, del_mode, pg_version, is_kingbase = args_tuple
    # del_mode: 'live' 只活行 | 'both' 活+删 | 'only' 只删行
    hf = HeapFile(path, page_size=page_size, pg_version=pg_version, is_kingbase=is_kingbase)
    if toast_path:
        toast = ToastFile(toast_path, page_size=page_size)
        if _TOAST_INDEX is not None:
            # 直接挂接主进程预建的索引（无需重新扫描）
            if _TOAST_MODE == "full":
                toast._chunk_index = _TOAST_INDEX
            else:
                toast._pos_index = _TOAST_INDEX
        else:
            # 兼容路径: 无共享索引时 worker 自行加载
            toast.build_index()
        hf.set_toast(toast)

    # 重建 TableMeta
    tm = _rebuild_table_meta(table_meta_dict)

    from pg2sql.page import Page
    from pg2sql.tuple import HeapTuple
    from pg2sql.types import decode_value, VARLENA_TYPES
    from pg2sql.heapfile import _check_external, _rebuild_varlena, _extract_fields_direct, _build_col_lengths, _decode_fields

    col_lengths = _build_col_lengths(tm)
    # P1-5: 元组 nattrs = 全部列（含 dropped），扫描模式校验必须用全列数
    n_expected = len(tm.columns)

    # 检查标准模式结果是否有效（至少有一个非空值）
    def _has_valid_data(values_list):
        for vals in values_list:
            for v in vals:
                if v is not None and v != "" and v != "__TOAST_MISSING__":
                    return True
        return False

    results = []
    # 只读自己的页面范围（seek 直接定位，避免读整个文件）
    with open(path, "rb") as f:
        f.seek(pageno_start * page_size)
        for pageno in range(pageno_start, pageno_end):
            raw = f.read(page_size)
            if len(raw) < page_size:
                break
            page = Page(pageno, raw)
            if not page.has_valid_layout:
                continue

            def _process_tuple(tup):
                """解析单个元组，返回 (values, is_deleted) 或 None"""
                is_deleted = tup.is_deleted  # 真删除（排除 abort/锁/multi）
                if not tup.is_live and not is_deleted:
                    return None  # 插入已回滚等死行，任何模式都不输出
                if del_mode == "only" and not is_deleted:
                    return None
                if del_mode == "live" and is_deleted:
                    return None
                nulls = tup.get_nulls()
                # 统一使用直接字段提取（兼容金仓无偏移表格式）
                fields = _extract_fields_direct(tup.raw, tup.t_hoff, nulls, col_lengths)
                values = _decode_fields(fields, nulls, tm, hf._toast)
                return (values, is_deleted)

            # 方式 1: 标准 ItemId
            page_results = []
            for item in page.items:
                if item.flags != 1:
                    continue
                data = page.raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data, pg_version=pg_version)
                except Exception:
                    continue
                values = _process_tuple(tup)
                if values is not None:
                    page_results.append(values)

            # 方式 2: 标准 ItemId 无有效结果时回退到数据区扫描（仅当前页）
            if page_results and not _has_valid_data(page_results):
                page_results = []
            if not page_results:
                pd_upper = page.header.get("upper", 0)
                pd_special = page.header.get("special", page_size)
                if pd_upper >= page.header_size and pd_upper < pd_special:
                    pos = pd_upper
                    while pos + 23 <= pd_special:
                        import struct as _s
                        t_xmin = _s.unpack_from("<I", raw, pos)[0]
                        t_infomask2 = _s.unpack_from("<H", raw, pos + 18)[0]
                        t_infomask = _s.unpack_from("<H", raw, pos + 20)[0]
                        t_hoff = raw[pos + 22]
                        nattrs = t_infomask2 & 0x07FF
                        if (t_hoff < 23 or t_hoff > 256 or nattrs == 0 or nattrs > 1600
                                or (n_expected and nattrs != n_expected)
                                or t_xmin == 0 or t_xmin > 0x7FFFFFFF
                                or (t_infomask & 0xFF00) == 0):
                            pos += 4
                            continue
                        try:
                            tup = HeapTuple(raw[pos:pd_special], pg_version=pg_version)
                        except Exception:
                            pos += 4
                            continue
                        if tup.t_hoff != t_hoff or tup.nattrs != nattrs:
                            pos += 4
                            continue
                        # P2-2: t_hoff 一致性校验（标准 ItemId 与扫描模式统一接通）
                        if not tup.is_header_consistent():
                            pos += 4
                            continue
                        values = _process_tuple(tup)
                        if values is not None:
                            page_results.append(values)
                        # 计算元组大小精确跳过（MAXALIGN 8）
                        from pg2sql.heapfile import _calculate_tuple_size
                        actual_size = _calculate_tuple_size(tup, col_lengths)
                        next_pos = (actual_size + 7) & ~7
                        pos += max(next_pos, 8)

            results.extend(page_results)

    return (batch_idx, results)


def _rebuild_table_meta(d: dict) -> TableMeta:
    cols = [Column(c["name"], c["atttypid"], c["attlen"], c["attnum"], c["typmod"],
                   c.get("notnull", False), c.get("dropped", False),
                   c.get("attalign") or None, c.get("attbyval", True),
                   c.get("attstorage", "x"))
            for c in d["columns"]]
    return TableMeta(d["dbname"], d["schema"], d["relname"], d["relfilenode"],
                     cols, primary_key=d.get("primary_key", []),
                     type_names=d.get("type_names"),
                     role_map=d.get("role_map"))


def _table_meta_to_dict(tm: TableMeta) -> dict:
    return {
        "dbname": tm.dbname, "schema": tm.schema, "relname": tm.relname,
        "relfilenode": tm.relfilenode, "primary_key": tm.primary_key,
        "type_names": tm.type_names,
        "role_map": tm.role_map,
        "columns": [{"name": c.name, "atttypid": c.atttypid, "attlen": c.attlen,
                      "attnum": c.attnum, "typmod": c.typmod, "notnull": c.notnull,
                      "dropped": c.attdropped, "attalign": c.attalign,
                      "attbyval": c.attbyval, "attstorage": c.attstorage}
                     for c in tm.columns],
    }


def run_parallel(args, meta, table_meta):
    """并发模式: 将文件按页面范围切成小批次分片, 多进程解析, 边收边写。

    - TOAST 索引: 主进程预建一次（轻量位置索引），经 Pool initializer 共享给全部
      worker（Linux fork 写时复制零开销），避免每个 worker 重复扫描大 TOAST 文件。
    - imap_unordered + 批次号重排: worker 乱序完成先缓冲，主进程按批次号顺序写出，
      既保证行序稳定，又不会因某批次慢而阻塞后续输出。
    """
    import time as _time

    # v1.26: 并行入口与串行共享 --fields 规范化/校验（绕过 run() 时必须显式调用）
    _normalize_fields(args, table_meta)

    page_size = args.page_size or _probe_file_page_size(args.datafile)
    file_size = os.path.getsize(args.datafile)
    npages = file_size // page_size
    n_workers = args.parallel

    BATCH_PAGES = 256  # 每批次页面数（约 2MB/批，保证内存占用小且吞吐稳定）

    tm_dict = _table_meta_to_dict(table_meta)

    # --- 主进程预建 TOAST 索引（一次扫描，全部 worker 共享）---
    toast_path = _find_toast_path(args)
    toast_index = None
    toast_mode = getattr(args, "toast_cache", "light")
    if toast_path:
        t0 = _time.time()
        toast_probe = ToastFile(toast_path, page_size=page_size)
        if toast_mode == "full":
            toast_probe.load()
            toast_index = toast_probe._chunk_index
        else:
            toast_probe.build_index(verbose=args.verbose, log=log)
            toast_index = toast_probe._pos_index
        toast_probe.close()
        toast_mb = os.path.getsize(toast_path) // 1048576
        if args.verbose:
            log(f"并发模式 TOAST: {toast_path} ({toast_mb}MB, {toast_mode} 模式)")
            log(f"TOAST 索引预建完成: {len(toast_index)} 个 valueid, "
                f"耗时 {_time.time() - t0:.1f}s (worker 共享, 免重复扫描)")
            if toast_mode == "full":
                log("提示: full 模式 payload 全部在内存, 重组零磁盘 IO")

    # 把全部页面切成固定大小的批次（带批次号，乱序完成后重排用）
    del_mode = "only" if args.only_deleted else ("both" if args.deleted else "live")
    pg_version = _pg_major(meta.get("pg_version", 160000))
    is_kb = is_kingbase_datadir(os.path.dirname(os.path.abspath(args.datafile)))
    ranges = []
    for batch_idx, start in enumerate(range(0, npages, BATCH_PAGES)):
        end = min(start + BATCH_PAGES, npages)
        ranges.append((batch_idx, args.datafile, page_size, start, end,
                       tm_dict, toast_path, del_mode, pg_version, is_kb))

    if not ranges:
        return

    out_fp = sys.stdout
    if args.output:
        # --data 模式输出 csv，否则 sql
        file_type = "csv" if args.data else "sql"
        out_path = _resolve_output_path(args.output, table_meta, file_type)
        os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
        out_fp = open(out_path, "w", encoding="utf-8")
        if args.verbose:
            log(f"输出到: {out_path}")

    if args.ddl and not args.data:
        out_fp.write(table_meta.generate_ddl() + "\n\n")

    # 预生成 SQL 模板（避免每行重复构建）
    live_cols = [c for c in table_meta.columns if not c.attdropped]
    # v1.26: --fields 只输出指定列（out_idx 对齐 live_cols；worker 返回全列 values，
    #         _write_row 按 out_idx 过滤后再写出）
    if args.fields:
        out_idx = [i for i, c in enumerate(live_cols) if c.name in args.fields]
    else:
        out_idx = list(range(len(live_cols)))
    col_names = [live_cols[i].name for i in out_idx]
    col_str = f"({', '.join(chr(34)+c+chr(34) for c in col_names)})" if args.complete_insert else ""
    target = f'"{table_meta.schema}"."{table_meta.relname}"'
    verb = "REPLACE INTO" if args.replace else "INSERT INTO"

    # v1.27: --header CSV 首行输出字段名（与 COPY HEADER true 兼容；--fields 时只输出选定列名）
    if args.header and args.data:
        hdr = args.delimiter.join(
            '"' + c.replace('"', '""') + '"' if (args.delimiter in c or '"' in c) else c
            for c in col_names)
        out_fp.write(hdr + "\n")

    FLUSH_INTERVAL = 1000  # 每写 1000 行 flush 一次
    total = 0
    limit_reached = False

    def _write_row(values, is_deleted=False):
        """写一行（CSV 或 INSERT），返回是否达到 --limit。"""
        nonlocal total
        vals = [values[i] for i in out_idx]
        if args.data:
            parts = []
            for v in vals:
                # v1.24: __TOAST_MISSING__ 与单进程 to_data 一致输出 \N（NULL）
                if v is None or v == "__TOAST_MISSING__":
                    parts.append("\\N")
                else:
                    s = str(v)
                    # 与 heapfile.to_data 的 CSV 转义规则保持一致：
                    # 含分隔符/换行/回车/引号或字面量恰好为 "\N" 的字段必须加引号包裹
                    # （缺 \r 检测会导致 COPY 导入把回车当行分隔符；缺 \N 特判会把
                    #  字面 "\N" 误判为 NULL——均会造成数据损坏）
                    if args.delimiter in s or "\n" in s or "\r" in s or '"' in s \
                            or s == "\\N":
                        s = '"' + s.replace('"', '""') + '"'
                    parts.append(s)
            out_fp.write(args.delimiter.join(parts) + "\n")
        else:
            sql_vals = []
            for i, v in enumerate(vals):
                if v is None:
                    sql_vals.append("NULL")
                elif v == "__TOAST_MISSING__":
                    sql_vals.append("NULL")
                else:
                    # 注意: values 只含未 dropped 列（_decode_fields 已跳过），
                    # 索引必须对齐 live_cols，不能直接用 table_meta.columns
                    col = live_cols[out_idx[i]] if i < len(out_idx) else None
                    # 数字类型不加引号（xid/cid 除外：PG 无 int→xid 隐式转换，需文本输入）
                    if col is not None and col.atttypid in (16, 20, 21, 23, 26, 700, 701, 1700):
                        sv = str(v)
                        # float 特殊值（NaN/±Infinity）PG 需要文本字面量，裸标识符非法
                        if sv in ("NaN", "Infinity", "-Infinity"):
                            sql_vals.append(sql_string_literal(sv))
                        else:
                            sql_vals.append(sv)
                    else:
                        sql_vals.append(sql_string_literal(str(v)))
            stmt = f"{verb} {target} {col_str} VALUES ({', '.join(sql_vals)});\n"
            if is_deleted:
                out_fp.write(f"-- DELETED\n{stmt}")
            else:
                out_fp.write(stmt)
        total += 1
        if total % FLUSH_INTERVAL == 0:
            if out_fp is not sys.stdout:
                out_fp.flush()
            if args.verbose:
                log(f"已输出 {total} 行...")
        return bool(args.limit) and total >= args.limit

    with Pool(n_workers, initializer=_pool_init, initargs=(toast_index, toast_mode)) as pool:
        # imap_unordered: 谁先完成先返回（不因某批次慢阻塞管道）
        # 按批次号重排缓冲: 乱序完成先缓冲，按序写出保证行顺序稳定
        next_out = 0
        pending = {}  # batch_idx -> rows
        for batch_idx, result_list in pool.imap_unordered(_parallel_worker, ranges):
            pending[batch_idx] = result_list
            while next_out in pending:
                rows = pending.pop(next_out)
                next_out += 1
                for values, is_deleted in rows:
                    if _write_row(values, is_deleted):
                        limit_reached = True
                        break
                if limit_reached:
                    break
            if limit_reached:
                break

    if args.verbose:
        log(f"并发解析完成: {total} 行")

    if out_fp is not sys.stdout:
        out_fp.close()


# ======================================================================
# 工具函数
# ======================================================================

def log(msg: str):
    print(f"[pg2sql] {msg}", file=sys.stderr)

def error_exit(msg: str, code: int = 1):
    print(f"[pg2sql] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def _resolve_output_path(output, table_meta, file_type):
    """根据 --output 参数解析输出文件路径。

    - 如果 output 是已存在的目录 → 自动命名: schema.table.type
    - 如果 output 以 / 结尾 → 视为目录，自动命名
    - 否则 → 视为文件前缀，追加 .sql/.csv 扩展名
      （避免 --ddl/--sql 与 --data 组合输出互相覆盖同一文件）

    file_type: 'sql', 'ddl', 'csv'
    """
    ext = {"sql": "sql", "csv": "csv"}.get(file_type, "txt")
    basename = f"{table_meta.schema}.{table_meta.relname}.{ext}"

    if output.endswith("/"):
        return os.path.join(output, basename)
    if os.path.isdir(output):
        return os.path.join(output, basename)
    # 带任意扩展名 → 视为完整文件路径直接使用；否则视为文件前缀追加扩展名
    if os.path.splitext(output)[1]:
        return output
    return output + "." + ext


# ======================================================================
# 列出数据库 (--list-db)
# ======================================================================

def list_databases(args):
    """扫描 PG 数据目录 base/ 子目录，解析 global/pg_database 获取数据库名称。

    用法: python3 main.py /path/to/pgdata/base/ --list-db
    或者: python3 main.py --datadir /path/to/pgdata --list-db
    """
    import struct as _struct

    # 确定 base/ 目录路径
    if args.datafile:
        base_dir = args.datafile
    elif args.datadir:
        base_dir = os.path.join(args.datadir, "base")
    else:
        error_exit("--list-db 需要指定数据目录路径 (作为位置参数或 --datadir)")

    if not os.path.isdir(base_dir):
        error_exit(f"目录不存在: {base_dir}")

    # 拆分出数据根目录与 base 目录
    base_dir = os.path.abspath(base_dir)
    pgdata = os.path.dirname(base_dir)  # base 的父目录就是 PGDATA

    # 1. 扫描 base/ 下所有数字命名的子目录（= 数据库 OID）
    db_oids = []
    for name in sorted(os.listdir(base_dir)):
        if name.isdigit():
            full = os.path.join(base_dir, name)
            if os.path.isdir(full):
                db_oids.append(int(name))

    if not db_oids:
        log("未在 base/ 下找到数据库目录")
        return

    # 2. 尝试解析 global/pg_database 获取数据库名称
    #    先试标准 OID 1262，找不到则自动探测
    db_names = {}  # oid -> name
    global_dir = os.path.join(pgdata, "global")
    pg_database_path = _detect_sys_catalog_file(
        global_dir, "1262", _is_pg_database_content, "pg_database", args.verbose
    )

    if pg_database_path:
        if args.verbose:
            log(f"解析 pg_database: {pg_database_path}")
        try:
            db_names = _parse_pg_database(pg_database_path, args.verbose)
        except Exception as e:
            if args.verbose:
                log(f"解析 pg_database 失败: {e}")
    else:
        if args.verbose:
            log(f"未在 {global_dir} 中找到 pg_database 文件，仅显示 OID 目录")

    # 3. 输出结果
    if db_names:
        print(f"{'OID':<12} {'数据库名称':<25} {'目录路径'}")
        print("-" * 70)
        for oid in sorted(db_oids):
            name = db_names.get(oid, "(未知)")
            path = os.path.join(base_dir, str(oid))
            print(f"{oid:<12} {name:<25} {path}")
        # 也列出 pg_database 中有但 base/ 下没有目录的（理论上不应出现）
        for oid, name in sorted(db_names.items()):
            if oid not in db_oids:
                print(f"{oid:<12} {name:<25} (base/ 下无对应目录)")
    else:
        print(f"{'OID':<12} {'目录路径'}")
        print("-" * 50)
        for oid in sorted(db_oids):
            path = os.path.join(base_dir, str(oid))
            print(f"{oid:<12} {path}")
        print()
        log("未能解析数据库名称（需要 pg_database 文件），仅显示 OID 目录")
        log("提示: 确保指定的是完整的 PG 数据目录（包含 global/ 子目录）")


def _parse_pg_database(path: str, verbose: bool = False) -> dict:
    """解析 global/pg_database 堆文件，返回 {oid: datname}。

    P1-4: 版本感知——PG12+ 布局 = [OID 4B] + datname(64B)（OID 位于数据区首
    字段，旧 get_oid 从 t_hoff-4 读已失效）；PG<=11 OID 在 t_hoff-4。
    兼容金仓（无偏移表）回退保留。
    """
    from pg2sql.page import Page, PAGE_SIZE, ITEMID_NORMAL
    from pg2sql.tuple import HeapTuple
    from pg2sql.binary import cstring
    from pg2sql.catalog import detect_pg_version
    import struct as _s

    version = detect_pg_version(os.path.dirname(os.path.abspath(path))) or 16
    result = {}
    # v1.24: 页大小自动探测（16KB/32KB 实例的 global/pg_database 用页头编码，
    # 硬编码 8192 会导致 Page 长度校验失败、数据库名解析为空）
    page_size = _probe_file_page_size(path)
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
            for item in page.items:
                if item.flags != ITEMID_NORMAL:
                    continue
                data = raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data, pg_version=version)
                except Exception:
                    continue
                if not tup.is_live:
                    continue

                oid = -1
                datname = ""
                t_hoff = tup.t_hoff
                nulls = tup.get_nulls()

                if version >= 12:
                    # 方案 A（PG12+）: [OID 4B][datname 64B]
                    if t_hoff + 68 <= len(data):
                        oid_candidate = _s.unpack_from("<I", data, t_hoff)[0]
                        name_candidate = cstring(data[t_hoff + 4 : t_hoff + 68])
                        if 1 <= oid_candidate <= 1000000 and name_candidate and name_candidate[0].isalpha():
                            oid = oid_candidate
                            datname = name_candidate
                else:
                    # 方案 A'（PG<=11）: OID 在 t_hoff-4，datname 从 t_hoff 起
                    oid_a = tup.get_oid()
                    fields_a = tup.get_fields([(64, False)])
                    if fields_a and fields_a[0] is not None and len(fields_a[0]) >= 1:
                        name_a = cstring(fields_a[0])
                        if name_a and name_a[0].isalpha():
                            oid = oid_a
                            datname = name_a

                # 方案 B: 金仓无偏移表，OID 在数据区开头（或 datname 直接开头）
                if not datname and t_hoff + 68 <= len(data):
                    oid_candidate = _s.unpack_from("<I", data, t_hoff)[0]
                    name_candidate = cstring(data[t_hoff + 4 : t_hoff + 68])
                    if 1 <= oid_candidate <= 1000000 and name_candidate and name_candidate[0].isalpha():
                        oid = oid_candidate
                        datname = name_candidate
                    else:
                        name_candidate = cstring(data[t_hoff : t_hoff + 64])
                        if name_candidate and name_candidate[0].isalpha():
                            datname = name_candidate
                            oid = tup.get_oid() if tup.get_oid() > 0 else -1

                if not datname:
                    continue

                if oid > 0:
                    result[oid] = datname
                if verbose:
                    log(f"  pg_database: oid={oid} datname={datname}")
    return result


# ======================================================================
# 列出表 (--list-tables-db)
# ======================================================================

def list_tables_in_db(args):
    """扫描数据库目录，列出所有表文件并通过 pg_class 映射表名。

    用法: python3 main.py /path/to/base/16384 --list-tables-db
    """
    if not args.datafile:
        error_exit("--list-tables-db 需要指定数据库目录路径 (如 base/16384)")

    db_dir = os.path.abspath(args.datafile)
    if not os.path.isdir(db_dir):
        error_exit(f"目录不存在: {db_dir}")

    # 目录名就是数据库 OID
    db_oid = os.path.basename(db_dir)
    if not db_oid.isdigit():
        log(f"警告: 目录名 '{db_oid}' 不是数字 OID")

    # 1. 扫描目录下所有数字命名的文件（= 表 relfilenode）
    #    排除 *_fsm、*_vm、*_init 后缀的辅助文件
    table_files = {}  # relfilenode -> filepath
    for name in sorted(os.listdir(db_dir)):
        # 跳过辅助文件
        if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
            continue
        # 主文件是纯数字名
        if name.isdigit():
            table_files[int(name)] = os.path.join(db_dir, name)

    if not table_files:
        log("未找到表数据文件")
        return

    # 2. 解析 pg_class 获取表名映射
    #    先试标准 OID 1259，找不到则自动探测
    pg_class_path = _detect_sys_catalog_file(
        db_dir, "1259", _is_pg_class_content, "pg_class", args.verbose
    )
    table_names = {}  # oid -> (relname, relkind)
    table_relfilenodes = {}  # oid -> relfilenode

    if pg_class_path:
        if args.verbose:
            log(f"解析 pg_class: {pg_class_path}")
        try:
            table_names, table_relfilenodes = _parse_pg_class(pg_class_path, args.verbose)
        except Exception as e:
            if args.verbose:
                log(f"解析 pg_class 失败: {e}")
    else:
        if args.verbose:
            log(f"未在 {db_dir} 中找到 pg_class 文件，仅显示文件列表")

    # 3. 建立 relfilenode -> (relname, relkind) 的映射
    #    pg_class 中 relfilenode 列记录了实际文件名
    #    对于大多数表 relfilenode == oid，但 CLUSTER/TRUNCATE 后可能不同
    filenode_to_info = {}  # relfilenode -> (relname, relkind)
    for oid, (relname, relkind) in table_names.items():
        rfn = table_relfilenodes.get(oid, oid)
        filenode_to_info[rfn] = (relname, relkind)

    # 4. 输出结果
    relkind_names = {
        "r": "普通表", "v": "视图", "m": "物化视图", "i": "索引",
        "S": "序列", "c": "复合类型", "t": "TOAST表",
    }

    if filenode_to_info:
        print(f"数据库 OID: {db_oid}")
        print(f"{'relfilenode':<14} {'表名':<30} {'类型':<10} {'文件路径'}")
        print("-" * 90)
        for rfn in sorted(table_files.keys()):
            info = filenode_to_info.get(rfn)
            if info:
                relname, relkind = info
                kind_str = relkind_names.get(relkind, relkind)
            else:
                relname = "(未知/系统表)"
                kind_str = "-"
            path = table_files[rfn]
            print(f"{rfn:<14} {relname:<30} {kind_str:<10} {path}")
    else:
        print(f"数据库 OID: {db_oid}")
        print(f"{'relfilenode':<14} {'文件路径'}")
        print("-" * 70)
        for rfn in sorted(table_files.keys()):
            path = table_files[rfn]
            print(f"{rfn:<14} {path}")
        print()
        log("未能解析表名称（需要 pg_class 文件 1259），仅显示文件列表")


def _parse_pg_class(path: str, verbose: bool = False) -> tuple:
    """解析 pg_class 堆文件，返回 ({oid: (relname, relkind)}, {oid: relfilenode})。

    P1-4: 版本感知——PG12+ 系统目录布局 = [OID 4B] + 用户列（OID 在数据区
    首字段，relkind 为第 16 个用户列）；PG<=11 OID 在 t_hoff-4。
    最终回退: 原始扫描模式（不依赖页头，直接搜索已知系统表名）。
    """
    from pg2sql.page import Page, PAGE_SIZE, ITEMID_NORMAL
    from pg2sql.tuple import HeapTuple
    from pg2sql.catalog import detect_pg_version, _class_fields
    import struct as _s

    version = detect_pg_version(os.path.dirname(os.path.abspath(path))) or 16

    names = {}
    relfilenodes = {}

    # v1.24: 页大小自动探测（16KB/32KB 实例 pg_class）
    page_size = _probe_file_page_size(path)
    filesize = os.path.getsize(path)
    npages = filesize // page_size

    # ===== 阶段 1: 通过页头 + ItemId 解析 =====
    page_ok = 0
    item_ok = 0
    with open(path, "rb") as f:
        for pageno in range(npages):
            raw = f.read(page_size)
            if len(raw) < page_size:
                break
            page = Page(pageno, raw, page_size=page_size)
            if not page.has_valid_layout:
                if verbose and pageno == 0:
                    log(f"  page 0 解析失败: {page.error}")
                continue
            page_ok += 1
            for item in page.items:
                if item.flags != ITEMID_NORMAL:
                    continue
                data = raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data, pg_version=version)
                except Exception:
                    continue
                if not tup.is_live:
                    continue
                row = _class_fields(tup, version)
                if row is None:
                    continue
                oid, relname, relnamespace, relfilenode, relkind = row
                if oid > 0 and relname:
                    names[oid] = (relname, relkind)
                    relfilenodes[oid] = relfilenode if relfilenode else oid
                    item_ok += 1

    if verbose:
        log(f"  阶段1(页头): {page_ok}/{npages} 页有效, {item_ok} 个元组解析成功")

    if names:
        return names, relfilenodes

    # ===== 阶段 2: 原始扫描模式（不依赖页头）=====
    if verbose:
        log(f"  阶段1无结果，回退到原始扫描模式...")
    return _parse_pg_class_raw_scan(path, version, verbose)


def _parse_pg_class_raw_scan(path: str, version: int = 0, verbose: bool = False) -> tuple:
    """数据区扫描模式: 不依赖 ItemId，直接在每页的 pd_upper~pd_special 区域
    按 [OID 4B][relname 64B] 模式定位元组数据。

    适用于金仓等非标准 PG 分支的 ItemId 格式。
    version: PG 主版本（0=未知，按 PG11 布局 115；>=18 用 119）。
    """
    from pg2sql.page import Page, PAGE_SIZE
    from pg2sql.binary import cstring
    import struct as _s

    names = {}
    relfilenodes = {}

    # v1.24: 页大小自动探测（16KB/32KB 实例 pg_class 扫描路径）
    page_size = _probe_file_page_size(path)
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

            # 在 pd_upper~pd_special 区域按 4 字节对齐扫描
            # 寻找模式: [OID 4B][relname 64B (NUL 结尾)]
            pos = pd_upper
            while pos + 68 <= pd_special:
                # 检查 4 字节是否像有效 OID
                oid_candidate = _s.unpack_from("<I", raw, pos)[0]
                if oid_candidate < 1 or oid_candidate > 10000000:
                    pos += 4
                    continue

                # 检查接下来 64 字节是否是合法标识符
                name_bytes = raw[pos + 4 : pos + 68]
                name_str = cstring(name_bytes)
                if not name_str or len(name_str) < 1:
                    pos += 4
                    continue

                # 验证: 首字符必须是字母或下划线
                first = name_str[0]
                if not (first.isalpha() or first == '_'):
                    pos += 4
                    continue

                # 验证: 全部字符是合法的 PG 标识符字符
                valid = True
                for ch in name_str:
                    if not (ch.isalnum() or ch in ('_', '.', '-', '$')):
                        valid = False
                        break
                if not valid:
                    pos += 4
                    continue

                # 验证: 名字后面应有 NUL 填充（name 类型固定 64 字节）
                name_end = name_str.encode("utf-8")
                if len(name_end) < 64:
                    if name_bytes[len(name_end)] != 0:
                        pos += 4
                        continue

                # 提取 relfilenode: 第 7 列
                # 布局: [OID 4B][relname 64B][relns 4B][reltype 4B][reloftype 4B]
                #        [relowner 4B][relam 4B][relfilenode 4B]
                # relfilenode 偏移 = 4 + 64 + 5*4 = 88
                rfn_off = pos + 88
                rfn = 0
                if rfn_off + 4 <= pd_special:
                    rfn = _s.unpack_from("<I", raw, rfn_off)[0]

                # relkind: 第 16 列（PG11 布局含 reloftype/relallvisible）
                # 布局继续: ...[reltablespace 4B][relpages 4B][reltuples 4B]
                #           [relallvisible 4B][reltoastrelid 4B][relhasindex 1B]
                #           [relisshared 1B][relpersistence 1B][relkind 1B]
                # relkind 偏移 = 88 + 5*4 + 3*1 = 115（PG12-17）；
                # PG18 在 relallvisible 后新增 relallfrozen(int4) → 119
                relkind = "r"
                rk_off = pos + (119 if version >= 18 else 115)
                if rk_off + 1 <= pd_special:
                    relkind = chr(raw[rk_off])

                oid = oid_candidate
                if oid not in names:
                    names[oid] = (name_str, relkind)
                    relfilenodes[oid] = rfn if rfn else oid
                    if verbose:
                        log(f"  数据区扫描: oid={oid} relname={name_str} relfilenode={rfn} relkind={relkind}")

                # 跳过这个元组的数据
                # 估算元组大小: 88(relfilenode) + 20(rest) = ~108+ bytes
                # 但更安全的是按对齐跳到下一个可能的位置
                pos += 4
                continue

    return names, relfilenodes


# ======================================================================
# 系统目录文件自动探测（兼容金仓等非标准 OID）
# ======================================================================

# pg_class 中必然出现的已知系统表名
_KNOWN_PG_CLASS_NAMES = {
    "pg_class", "pg_attribute", "pg_type", "pg_namespace",
    "pg_proc", "pg_index", "pg_constraint", "pg_authid",
    "pg_database", "pg_tablespace", "pg_depend",
}

# pg_database 中必然出现的已知数据库名
_KNOWN_DB_NAMES = {
    "template1", "template0", "postgres", "kingbase", "security",
    "test", "sys", "esrep", "info", "system",
}


def _detect_sys_catalog_file(directory, standard_oid, content_checker, label, verbose=False):
    """在目录中查找系统目录文件。

    策略:
      1. 先试标准 OID 文件名 (如 "1259")
      2. 找不到则扫描所有数字命名文件，用内容特征匹配

    返回文件路径，找不到返回 None。
    """
    # 1. 先试标准 OID
    std_path = os.path.join(directory, standard_oid)
    if os.path.exists(std_path):
        return std_path

    if not os.path.isdir(directory):
        return None

    if verbose:
        log(f"标准 {label} 文件 ({standard_oid}) 不存在，开始自动探测...")

    # 2. 扫描所有数字命名文件（排除 _fsm/_vm/_init）
    candidates = []
    for name in sorted(os.listdir(directory)):
        if name.endswith("_fsm") or name.endswith("_vm") or name.endswith("_init"):
            continue
        if name.isdigit():
            fpath = os.path.join(directory, name)
            if os.path.isfile(fpath) and os.path.getsize(fpath) >= 8192:
                candidates.append(fpath)

    if verbose:
        log(f"扫描 {len(candidates)} 个候选文件...")

    # 3. 逐个检查内容
    for fpath in candidates:
        try:
            if content_checker(fpath, verbose):
                if verbose:
                    log(f"检测到 {label}: {fpath}")
                return fpath
        except Exception:
            continue

    return None


def _is_pg_class_content(path, verbose=False):
    """检查文件内容是否像 pg_class：解析元组，看第一列(name)是否含已知系统表名。"""
    from pg2sql.page import Page, PAGE_SIZE, ITEMID_NORMAL
    from pg2sql.tuple import HeapTuple
    from pg2sql.binary import cstring

    match_count = 0
    total_checked = 0

    # v1.24: 页大小自动探测（16KB/32KB 实例）
    page_size = _probe_file_page_size(path)

    with open(path, "rb") as f:
        # 只读前 2 页即可判断
        for pageno in range(2):
            raw = f.read(page_size)
            if len(raw) < page_size:
                break
            page = Page(pageno, raw, page_size=page_size)
            if not page.has_valid_layout:
                continue
            for item in page.items:
                if item.flags != ITEMID_NORMAL:
                    continue
                data = raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data)
                except Exception:
                    continue
                if not tup.is_live:
                    continue
                fields = tup.get_fields([(64, False)])
                if not fields or fields[0] is None:
                    continue
                relname = cstring(fields[0])
                total_checked += 1
                if relname in _KNOWN_PG_CLASS_NAMES:
                    match_count += 1
                    if verbose:
                        log(f"  {path}: 匹配 '{relname}'")
                # 命中 2 个以上即可确定
                if match_count >= 2:
                    return True
                # 检查超过 50 个仍无匹配则放弃
                if total_checked > 50:
                    return False

    return match_count >= 2


def _is_pg_database_content(path, verbose=False):
    """检查文件内容是否像 pg_database：解析元组，看第一列(name)是否含已知数据库名。"""
    from pg2sql.page import Page, PAGE_SIZE, ITEMID_NORMAL
    from pg2sql.tuple import HeapTuple
    from pg2sql.binary import cstring

    match_count = 0
    total_checked = 0

    # v1.24: 页大小自动探测（16KB/32KB 实例）
    page_size = _probe_file_page_size(path)

    with open(path, "rb") as f:
        for pageno in range(2):
            raw = f.read(page_size)
            if len(raw) < page_size:
                break
            page = Page(pageno, raw, page_size=page_size)
            if not page.has_valid_layout:
                continue
            for item in page.items:
                if item.flags != ITEMID_NORMAL:
                    continue
                data = raw[item.off : item.off + item.len]
                try:
                    tup = HeapTuple(data)
                except Exception:
                    continue
                if not tup.is_live:
                    continue
                fields = tup.get_fields([(64, False)])
                if not fields or fields[0] is None:
                    continue
                datname = cstring(fields[0])
                total_checked += 1
                if datname in _KNOWN_DB_NAMES:
                    match_count += 1
                    if verbose:
                        log(f"  {path}: 匹配 '{datname}'")
                if match_count >= 1:
                    return True
                if total_checked > 20:
                    return False

    return match_count >= 1


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --page-size 0 = 自动探测（页头 pd_pagesize_version 编码）
    if args.page_size == 0:
        args.page_size = None

    # v1.25: 库文本编码设置（auto=自动探测, 或用户显式 --encoding）
    _configure_encoding(args)

    # 无任何参数时显示帮助
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # --list-tables 不需要 datafile
    if args.list_tables:
        if not args.catalog_json and not args.datadir:
            error_exit("--list-tables 需要 --catalog-json 或 --datadir")
        meta = load_metadata(args)
        print(f"数据库: {meta['database']}")
        print(f"{'Schema':<20} {'表名':<30} {'relfilenode':<12} {'列数':<6} {'主键'}")
        print("-" * 90)
        seen = set()
        for name, tm in sorted(meta["tables"].items()):
            key = f"{tm.schema}.{tm.relname}"
            if key in seen:
                continue
            seen.add(key)
            pk = ", ".join(tm.primary_key) if tm.primary_key else ""
            ncol = len([c for c in tm.columns if not c.attdropped])
            print(f"{tm.schema:<20} {tm.relname:<30} {tm.relfilenode:<12} {ncol:<6} {pk}")
        return

    # --list-db: 列出数据目录中的所有数据库
    if args.list_db:
        list_databases(args)
        return

    # --list-tables-db: 列出指定数据库目录中的所有表
    if args.list_tables_db:
        list_tables_in_db(args)
        return

    # --export-meta: 离线导出元数据 JSON
    if args.export_meta:
        if not args.datafile:
            error_exit("--export-meta 需要指定数据库目录 (如 base/16384/)")
        db_dir = os.path.abspath(args.datafile)
        if not os.path.isdir(db_dir):
            error_exit(f"目录不存在: {db_dir}")
        import json as _json
        if args.verbose:
            log(f"从数据库目录离线导出元数据: {db_dir}")
        try:
            meta_json = export_meta_to_json(db_dir, page_size=args.page_size)
        except Exception as e:
            error_exit(f"导出元数据失败: {e}")
        output = _json.dumps(meta_json, indent=2, ensure_ascii=False)
        if args.output:
            out_path = args.output
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(output)
            log(f"元数据已导出到: {out_path}")
        else:
            print(output)
        if args.verbose:
            log(f"共导出 {len(meta_json['tables'])} 张表")
        return

    try:
        if args.parallel and args.parallel > 1:
            meta = load_metadata(args)
            table_meta = select_table(meta, args)
            run_parallel(args, meta, table_meta)
        else:
            run(args)
    except KeyboardInterrupt:
        log("已中断")
        sys.exit(130)
    except FileNotFoundError as e:
        error_exit(str(e))
    except Exception as e:
        if args.verbose:
            import traceback
            traceback.print_exc()
        error_exit(str(e))


if __name__ == "__main__":
    main()
