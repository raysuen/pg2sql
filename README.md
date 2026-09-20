---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '40e3ab79-2595-4bdb-9265-f3c810447b8d'
  PropagateID: '40e3ab79-2595-4bdb-9265-f3c810447b8d'
  ReservedCode1: '8c2f554a-ea6b-4c17-8bd3-aed1ad6080e7'
  ReservedCode2: '8c2f554a-ea6b-4c17-8bd3-aed1ad6080e7'
---

# pg2sql

> 离线解析 PostgreSQL / 金仓数据库（KingbaseES）堆数据文件并导出为 SQL

## 简介

pg2sql 是一个纯 Python 编写的 PostgreSQL 数据文件解析工具，**无需数据库实例运行，无需任何第三方依赖包**。直接读取堆数据文件（`base/{db_oid}/{table_oid}`），解析 8KB 堆页面中的 HeapTuple，输出 DDL 和 INSERT 语句。

兼容 PostgreSQL 和金仓数据库（KingbaseES），已针对金仓的以下非标准格式做适配：

- **page version 5** 非标准页头布局（`pd_pagesize_version` 在偏移 44/48，页头 48~52 字节）
- **无偏移表**元组格式（列数据从 `t_hoff` 开始连续排列，无 ItemId 偏移表）
- **varlena 1 字节头**编码差异（`VARSIZE = header >> 1`，`data_len = VARSIZE - 1`）
- **pg_attribute 多出 attcollation + 额外字段**，atttypmod 偏移后移 8 字节
- **删除元组 xmax hint bits 未设置**，通过 `t_xmax != 0` 判定已删除行
- **数据区扫描模式**定位元组（当 ItemId 格式不兼容时自动回退）

系统目录文件（pg_class / pg_attribute / pg_database 等）的 OID 自动探测，不依赖硬编码，适配不同 PG 分支版本。

适用于以下场景：
- 数据库损坏无法启动，但数据文件完好
- 误删 / TRUNCATE 表后需要从磁盘残留数据恢复
- 审计已删除但未被 VACUUM 清理的行
- 离线数据迁移

## 快速开始

### 方式一：自动发现表结构（推荐，无需导出 JSON）

```bash
# 直接指定数据文件，工具自动解析同目录下的系统目录表
python3 main.py /var/lib/postgresql/data/base/16384/16387 --ddl --sql
```

工具会自动：
1. 从数据文件路径反推数据库目录（`base/16384/`）
2. 解析 `pg_namespace`（2615）获取 schema 名称
3. 解析 `pg_class`（1259）按 relfilenode 匹配表名
4. 解析 `pg_attribute`（1249）获取列定义
5. 构建表结构后解析数据

### 方式二：离线导出 JSON 元数据（无需在线实例）

```bash
# 1. 从数据库目录离线导出元数据 JSON（自动解析系统目录表）
python3 main.py /var/lib/postgresql/data/base/16384 --export-meta -o meta.json

# 2. 用导出的 JSON 解析数据文件
python3 main.py /var/lib/postgresql/data/base/16384/16387 \
  --catalog-json meta.json --ddl --sql
```

### 方式三：在线导出 JSON 元数据（有可用实例时）

```bash
# 1. 在在线实例上执行 SQL 导出（格式与 --export-meta 兼容）
psql -d yourdb -At -f export_meta.sql > meta.json

# 2. 离线解析数据文件
python3 main.py /var/lib/postgresql/data/base/16384/16387 \
  --catalog-json meta.json --ddl --sql
```

## 使用方法

```
python3 main.py <data_file> [options]
python3 main.py -h    # 查看完整帮助
```

### 选项说明

| 选项 | 说明 |
|------|------|
| `-h`, `--help` | 显示帮助信息 |
| `--version` | 显示版本号 |
| **元数据（可选）** | |
| `--catalog-json FILE` | 从 JSON 文件加载表结构（由 export_meta.sql 或 --export-meta 导出） |
| `--datadir DIR` | 从 PG 数据目录离线解析全部表结构 |
| `--db-oid OID` | 目标数据库 OID（配合 --datadir） |
| `--table-name NAME` | 指定表名（多表时筛选） |
| > 不指定时自动发现 | 从数据文件路径反推数据库目录，自动解析系统表 |
| **输出模式** | |
| `--ddl` | 输出 CREATE TABLE DDL |
| `--sql` | 输出 INSERT 语句 |
| `--data` | 输出 CSV 格式（可用 LOAD DATA 导入） |
| `--deleted` | 输出已删除和未删除的行 |
| `--only-deleted` | 只输出已删除的行（不含未删除行） |
| `--count` | 仅统计行数 |
| `--list-tables` | 列出元数据 JSON 中的所有表 |
| `--list-db` | 列出数据目录中的所有数据库（OID + 名称 + 目录路径） |
| `--list-tables-db` | 列出指定数据库目录中的所有表（OID + 表名 + 类型 + 文件路径） |
| `--export-meta` | 从数据库目录离线导出元数据 JSON（无需在线实例） |
| **输出选项** | |
| `--output PATH`, `-o PATH` | 输出到文件或目录（目录则自动命名 `schema.table.type`） |
| `--limit N` | 限制输出行数 |
| `--complete-insert` | INSERT 包含字段名（默认开启） |
| `--no-complete-insert` | INSERT 不含字段名 |
| `--replace` | 用 REPLACE INTO 代替 INSERT INTO |
| `--delimiter CHAR` | CSV 分隔符（默认逗号） |
| `--force` | 强制遍历损坏文件 |
| **高级选项** | |
| `--toast FILE` | TOAST 表文件路径（重组大字段） |
| `--toast-cache MODE` | TOAST 缓存模式：light（默认）/ full（全内存，慢磁盘提速） |
| `--page-size N` | 页面大小（默认 8192） |
| `--parallel N` | 并发进程数 |
| `--verbose` | 输出详细日志 |

### 并发模式与大文件性能（v1.15）

`--parallel N` 将文件按 256 页（约 2MB）切成批次，多进程解析、边收边写：

- **TOAST 索引只建一次**：主进程预建后经 Pool 共享给全部 worker
  （Linux fork 写时复制零开销），不再每 worker 重复扫描大 TOAST 文件
- **页级 LRU 缓存**：一页常含数十个 TOAST chunk（实测 ~42/页），
  同页 chunk 只读一次磁盘、解析一次——IO 次数比逐 chunk 读取降低一个数量级
- **mmap 读页**：消除 seek/read 系统调用，多进程共享 OS 页缓存
- **乱序完成不阻塞**：`imap_unordered` + 批次号重排，行序与单进程一致
- **定量落盘**：每 1000 行 flush，运行中可实时监控输出文件增长

TOAST 缓存模式（`--toast-cache`）：

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `light`（默认） | 轻量位置索引 + 32MB 页级 LRU 缓存 | 内存有限 |
| `full` | 全部 chunk payload 载入内存，重组零磁盘 IO | 慢磁盘 + 内存充足（≈TOAST 文件大小×2） |

```bash
# 慢磁盘大 TOAST 场景（如 Docker overlay 存储）：
python3 main.py big_file --sql --parallel 4 --toast-cache full -o /tmp/out/
```

### 输出文件命名规则

使用 `-o` 参数时：

| 指定方式 | 行为 | 示例 |
|----------|------|------|
| `-o /tmp/output/` | 目录（以 `/` 结尾），自动命名 | `/tmp/output/ray.test02.sql` |
| `-o /tmp/output` | 已存在的目录，自动命名 | `/tmp/output/ray.test02.sql` |
| `-o /tmp/result.sql` | 文件路径，直接使用 | `/tmp/result.sql` |

自动命名格式为 `schema.tablename.ext`，扩展名根据输出类型：
- `--ddl` / `--sql` → `.sql`
- `--data` → `.csv`

DDL 和 INSERT 同时输出时合并到同一个 `.sql` 文件。

### 使用示例

```bash
# 自动发现表结构并导出（无需 --catalog-json）
python3 main.py data_file --ddl --sql

# 使用 JSON 元数据导出
python3 main.py data_file --catalog-json meta.json --ddl --sql

# 导出到目录（自动命名 schema.table.sql）
python3 main.py data_file --ddl --sql -o /tmp/output/

# 导出到指定文件
python3 main.py data_file --ddl --sql -o /tmp/result.sql

# 导出 CSV
python3 main.py data_file --catalog-json meta.json --data -o /tmp/output/

# 输出已删除和未删除的行
python3 main.py data_file --catalog-json meta.json --sql --deleted

# 只输出已删除的行（不含未删除行）
python3 main.py data_file --catalog-json meta.json --sql --only-deleted

# 8 进程并发解析大文件
python3 main.py big_file --catalog-json meta.json --sql --parallel 8

# 强制遍历损坏文件
python3 main.py corrupt_file --catalog-json meta.json --sql --force

# 关联 TOAST 表
python3 main.py data_file --catalog-json meta.json --sql --toast /path/to/toast_file

# 列出元数据中的表
python3 main.py --catalog-json meta.json --list-tables

# 列出数据目录中的所有数据库
python3 main.py /var/lib/postgresql/data/base/ --list-db
# 或通过 --datadir 指定 PG 数据目录
python3 main.py --datadir /var/lib/postgresql/data --list-db

# 列出指定数据库目录中的所有表
python3 main.py /var/lib/postgresql/data/base/16384 --list-tables-db

# 离线导出元数据 JSON (无需在线实例)
python3 main.py /var/lib/postgresql/data/base/16384 --export-meta -o meta.json
```

## 支持的类型

int2 / int4 / int8 / float4 / float8 / numeric、text / varchar / bpchar / char / name、
bool / bytea、date / time / timestamp / timestamptz / timetz / interval、
uuid / json / jsonb、inet / cidr / macaddr、bit / varbit、money、oid / xid / cid / tid

## 项目结构

```
pg2sql/
├── main.py              # 主入口 CLI
├── export_meta.sql      # 在线元数据导出脚本
├── pg2sql/              # 核心代码包
│   ├── __init__.py      # 包初始化
│   ├── binary.py        # 二进制读取工具 + varlena 解析
│   ├── page.py          # 8KB 堆页面解析（支持 version 4/5 + 自动探测）
│   ├── tuple.py         # HeapTuple 解析（可见性判定 + NULL 位图）
│   ├── types.py         # 40+ 内置类型解码器
│   ├── catalog.py       # 表结构元数据（自动发现 + 数据区扫描）
│   ├── toast.py         # TOAST 大字段重组
│   └── heapfile.py      # 堆文件读取与导出引擎
├── tests/               # 测试
│   ├── test_synthetic.py     # 合成数据冒烟测试
│   ├── test_list_db.py       # --list-db 功能测试
│   ├── test_list_tables_db.py # --list-tables-db 功能测试
│   ├── test_auto_discover.py # 自动发现表结构测试
│   ├── test_export_meta.py  # 离线导出元数据 JSON 测试
│   └── test_kb_scan.py      # 金仓格式扫描模式测试
└── diag/                # 诊断脚本
    ├── diag.py               # 通用诊断
    ├── diag_v2.py            # pg_attribute + 数据文件综合诊断
    ├── diag_user_table.py    # 用户表页头布局诊断
    ├── diag_attribute.py     # pg_attribute 列布局诊断
    ├── diag_attr.py          # atttypmod 偏移诊断
    ├── diag_tuple_layout.py  # 元组数据区布局诊断
    └── diag_data_file.py     # 数据文件全零诊断
```

## 金仓兼容性

pg2sql 已针对金仓数据库（KingbaseES）的非标准格式做了全面适配：

| 差异点 | 标准 PostgreSQL | 金仓 KingbaseES | 适配方式 |
|--------|-----------------|-----------------|----------|
| 页版本 | version 4 | version 5 | `page.py` 三阶段页头探测 |
| 页头布局 | 24 字节 | 48~52 字节 | 自动扫描 `pd_pagesize_version` 位置 |
| 元组偏移表 | 有 | 无 | `_extract_fields_direct` 按列声明顺序连续读取 |
| ItemId 格式 | 标准 | 不兼容 | 数据区扫描模式自动回退 |
| varlena 1B 头 | `len = header` | `VARSIZE = header >> 1` | `types.py` / `binary.py` / `heapfile.py` 三处修复 |
| pg_attribute 布局 | 无 attcollation | 多 attcollation + 额外字段 | `_scan_pg_attribute` 偏移调整 |
| 删除可见性 | xmax hint bits | hint bits 未设置 | `t_xmax != 0` 判定已删除 |

## 限制

- 系统目录文件自动探测需读取文件内容（前 2 页），大目录可能较慢
- DROP 表后表结构信息会丢失（pg_class / pg_attribute 中的行也被删除）
- >= 256 列的表使用组合偏移表，当前未实现
- CLOG 事务状态未解析，可见性判定为简化版本（依赖 hint bits + xmax 非零）
- 未实现裸设备扫描恢复 DROP / TRUNCATE 表（需后续开发）
- 数据未 CHECKPOINT 落盘时数据文件可能全零（需先在实例中执行 `CHECKPOINT`）

## 要求

Python >= 3.6，无第三方依赖

## 许可证

GPL-3.0

> AI生成
---

## 修复版说明（2026-09-20）

本目录为修复工作副本（原版见 `../pg2sql/`，保持只读）。已修复《pg2sql_源码评审报告.md》全部
6 个 P1 + 4 个 P2 缺陷，并在真实 PostgreSQL 16.4 实例（tdb 库 9 张边界表）完成双路径回归
（自动发现 / --catalog-json）。详见 `../pg2sql_修复说明.md` 与 `../pg2sql_修复可视化/`。

关键新增/变更：
- `export_meta.sql` 重写：OID/整数显式 ::int（P1-1）、jsonb_pretty(jsonb_build_object)（P1-2）、
  导出 attalign/attbyval/attstorage 与 pg_version。
- `pg2sql/catalog.py`：pg_attribute/pg_class/pg_namespace/pg_database 全版本感知布局 +
  OID 首列公式 + bool C-bool 读取。
- `pg2sql/heapfile.py`：列级 attalign 对齐（P1-3）、dropped 列占位推进（P1-5）、pg_version 链路。
- `pg2sql/tuple.py`：get_oid/get_nulls/is_header_consistent 版本门控（P1-4/P2-1/P2-2）。
- `pg2sql/types.py`：jsonb（官方 getJsonbOffset）与数组（att_align_nominal）解码（P1-6）、
  short numeric 负 weight 补码修正、bool C-bool。
- `tests/fixtures/pg16/`：真实 PG16.4 堆页 fixture；`tests/test_pg16_fixture.py` 无服务器回归；
  `scripts/regress_live.sh` 实时双路径回归；`tests/test_realfmt.py` 测试脚手架 3 处 bug 修复。

验证：`python3 tests/test_pg16_fixture.py`（12/12 脚本全绿）；实时回归见 scripts/regress_live.sh。
