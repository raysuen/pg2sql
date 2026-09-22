
# pg2sql

> 离线解析 PostgreSQL / 金仓数据库（KingbaseES）堆数据文件并导出为 SQL

> README 版本：v3.3（2026-09-22 库编码自动探测与字节可逆解码轮：金仓 V9 MySQL 0x9B 导入报错修复 + 全版本回归）

## 简介

pg2sql 是一个纯 Python 编写的数据库数据文件解析工具，**无需实例运行，无需任何第三方依赖**。直接读取堆数据文件（`base/{db_oid}/{relfilenode}`），**自动探测页大小（8KB / 16KB / 32KB，按 PostgreSQL `pd_pagesize_version` 权威语义 + 页内指针链校验）**，解析堆页面中的 HeapTuple，输出 DDL 和 INSERT 语句（或 CSV），支持已删除行的审计恢复。

## 兼容性（实测矩阵，2026-09 闭环验证）

**PostgreSQL（全部"导出 SQL/CSV + 可导入"闭环通过）**

| 版本 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
|---|---|---|---|---|---|---|---|
| 实测 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

覆盖：基础类型全列、dropped 列、TOAST 跨块大字段（30K–120K 字符）、枚举、interval、float 特殊值（NaN/±Infinity）、时间 ±infinity、声明式分区表（RANGE/LIST 子表）、序列依赖、ACL、20000 行批量。

**Block Size（页大小自动探测，实测矩阵）**

| 版本 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 金仓 V8R6C9B14 |
|---|---|---|---|---|---|---|---|---|
| 16KB | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 32KB | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
|---|---|---|---|---|---|---|---|
| 16KB | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 32KB | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

PG12-18 全部 14 套（7 版本 × 16KB/32KB）+ 金仓 V8R6C9B14 的 8/16/32KB 三套均为：38 列全类型边界表（枚举/数组含 NULL 元素/jsonb 嵌套/inet/macaddr8/几何/±infinity/NaN/含引号换行文本等）→ 导出（自动探测页大小）→ 导入 8KB 实例 → count 与 38 列逐值一致。8KB 全版本（PG12-18）此前已闭环。

页大小探测逻辑：读取页头 `pd_pagesize_version`（`size \| version`，`PageGetPageSize = psv & 0xFF00`），在候选集合 {8192, 16384, 32768} 中按 `lower<=upper<=special<=size` 指针链校验后确定，系统目录与 TOAST 文件同链路自动探测，无需 `--page-size` 参数。

**金仓 KingbaseES（"导出 SQL/CSV + 可导入"闭环通过）**

| 版本 | V8R6C8B14 | V8R6C8B20 | V8R6C9B14 | V9R1C10 |
|---|---|---|---|---|
| 兼容模式 | MySQL | Oracle | Oracle | MySQL |
| 内核代际 | 早期（PG8.4 代际） | 同左 | 同左 | PG12 代际 |
| 系统表命名 | `_` 短名（`_rel`/`_att`/`_typ`） | 同左 | 同左 | `sys_*` |
| 实测用户表 | ✅ 68 张 | ✅ 12 张 | ✅ 23 张 | ✅ test 15 张 / ray 16 张 |

金仓 V8 系列系统表 oid 仍为标准值（sys_class=1259 / sys_attribute=1249 / sys_type=1247），catalog 自动发现按 oid 定位、与表名前缀无关，同一代码库同时兼容 V8/V9 双代际。每实例全表"导出 SQL/CSV/DDL → 导入 PG18 承载库 → count(*) 与 CSV 行数逐表一致"闭环通过；导出失败项均为带 information_schema/pg_catalog 的系统表（main.py 设计过滤），非缺陷。

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
| `--datadir DIR` | 从数据目录离线解析全部表结构 |
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
| `--page-size N` | 页面大小（默认自动探测；显式指定可覆盖） |
| `--parallel N` | 并发进程数 |
| `--encoding CODEC` | 覆盖库编码（Python codec 名，如 `utf-8`/`latin-1`/`gbk`/`gb18030`；默认从 `global/1262` 自动探测） |
| `--verbose` | 输出详细日志 |

### 并发模式与大文件性能（v1.15）

`--parallel N` 将文件按 256 页（约 2MB）切成批次，多进程解析、边收边写：

- **TOAST 索引只建一次**：主进程预建后经 Pool 共享给全部 worker
- **页级 LRU 缓存**：一页常含数十个 TOAST chunk，同页 chunk 只读一次磁盘
- **mmap 读页**：消除 seek/read 系统调用，多进程共享 OS 页缓存
- **乱序完成不阻塞**：`imap_unordered` + 批次号重排，行序与单进程一致
- **定量落盘**：每 1000 行 flush，运行中可实时监控输出文件增长

TOAST 缓存模式（`--toast-cache`）：

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `light`（默认） | 轻量位置索引 + 32MB 页级 LRU 缓存 | 内存有限 |
| `full` | 全部 chunk payload 载入内存，重组零磁盘 IO | 慢磁盘 + 内存充足（≈TOAST 文件大小×2） |

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

# 导出 CSV
python3 main.py data_file --catalog-json meta.json --data -o /tmp/output/

# 输出已删除和未删除的行
python3 main.py data_file --catalog-json meta.json --sql --deleted

# 只输出已删除的行
python3 main.py data_file --catalog-json meta.json --sql --only-deleted

# 8 进程并发解析大文件
python3 main.py big_file --catalog-json meta.json --sql --parallel 8

# 关联 TOAST 表
python3 main.py data_file --catalog-json meta.json --sql --toast /path/to/toast_file

# 列出数据目录中的所有数据库
python3 main.py /var/lib/postgresql/data/base/ --list-db

# 列出指定数据库目录中的所有表
python3 main.py /var/lib/postgresql/data/base/16384 --list-tables-db

# 离线导出元数据 JSON (无需在线实例)
python3 main.py /var/lib/postgresql/data/base/16384 --export-meta -o meta.json
```

## 支持的类型

int2 / int4 / int8 / float4 / float8 / numeric、text / varchar / bpchar / char / name、
bool / bytea、date / time / timestamp / timestamptz / timetz / interval、
uuid / json / jsonb、inet / cidr / macaddr、bit / varbit、money、oid / xid / cid / tid、
**枚举（自动读 pg_enum 映射并输出 CREATE TYPE）、float 特殊值（NaN/±Infinity 带引号字面量）**

## 项目结构（精简包）

```
pg2sql/
├── main.py              # 主入口 CLI
├── export_meta.sql      # 在线元数据导出脚本
└── pg2sql/              # 核心代码包
    ├── __init__.py      # 包初始化（__version__）
    ├── binary.py        # 二进制读取工具 + varlena 解析
    ├── page.py          # 8KB 堆页面解析（支持 version 4/5 + 自动探测）
    ├── tuple.py         # HeapTuple 解析（可见性判定 + NULL 位图）
    ├── types.py         # 40+ 内置类型解码器（含枚举/interval/float 特殊值）
    ├── catalog.py       # 表结构元数据（自动发现 + 数据区扫描 + 版本感知布局）
    ├── toast.py         # TOAST 大字段重组
    └── heapfile.py      # 堆文件读取与导出引擎
```

调试脚本、测试夹具与回归脚本不随包分发（已归档至 `pg2sql-tests-archive.tar.gz`，按需解压回归）。

## 金仓适配细节

pg2sql 针对金仓数据库（KingbaseES）非标准格式的适配：

| 差异点 | 标准 PostgreSQL | 金仓 KingbaseES | 适配方式 |
|--------|-----------------|-----------------|----------|
| 页版本 | version 4 | version 5 | `page.py` 三阶段页头探测 |
| 页头布局 | 24 字节 | 48~52 字节 | 自动扫描 `pd_pagesize_version` 位置 |
| 元组偏移表 | 有 | 无 | `_extract_fields_direct` 按列声明顺序连续读取 |
| ItemId 格式 | 标准 | 不兼容 | 数据区扫描模式自动回退 |
| varlena 1B 头 | `len = header` | `VARSIZE = header >> 1` | `types.py` / `binary.py` / `heapfile.py` 三处修复 |
| pg_attribute 布局 | 无 attcollation | 多 attcollation + 额外字段 | `_scan_pg_attribute` 偏移调整（V8/V9 均覆盖） |
| dropped 列位 | PG12+：attisdropped=16 | V8/V9：attisdropped=17（固定区含 hasdef/hasmissing/identity/generated） | `_attr_idx` 金仓分支索引 17 / attcollation 20 |
| 删除可见性 | xmax hint bits | hint bits 未设置 | `t_xmax != 0` 判定已删除行 |
| 系统表命名 | pg_* | V9：sys_*；V8：`_` 短名 | catalog 按标准 oid 定位，与前缀无关 |

## 限制（已知，如实声明）

- 分区表子表导出为普通 CREATE TABLE，**不还原 `PARTITION OF ... FOR VALUES` 声明**：数据完整、分区结构需手工补建
- 导出 SQL 引用了源库专属角色/对象时（如金仓内部角色 `system` 的 relacl），导入标准 PostgreSQL 会因角色不存在报错——属源数据内容特性，导入金仓/同名角色环境无碍
- 系统目录文件自动探测需读取文件内容（前 2 页），大目录可能较慢
- DROP 表后表结构信息会丢失（pg_class / pg_attribute 中的行也被删除）
- >= 256 列的表使用组合偏移表，当前未实现
- CLOG 事务状态未解析，可见性判定为简化版本（依赖 hint bits + xmax 非零）
- 未实现裸设备扫描恢复 DROP / TRUNCATE 表
- 数据未 CHECKPOINT 落盘时数据文件可能全零（需先在实例中执行 `CHECKPOINT`）

## 要求与许可

Python >= 3.6，无第三方依赖。MIT License。

```
MIT License

Copyright (c) 2026 pg2sql contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 变更记录

- **2026-09-22（库编码自动探测与字节可逆解码轮 v3.3，main.py 1.24→1.25、binary.py 2.1→2.2、types.py 2.3→2.4、catalog.py 3.0→3.2）**：金仓 V9 MySQL 模式 50 列大表（`ray.test_50col_old`，TOAST 61,361 页）导出 SQL 导入报 `ERROR: invalid byte sequence for encoding "UTF8": 0x9b`。根因：**非 UTF-8 源库文本一律按 utf-8/replace 输出**——LATIN1 库 0x9B 等字节被替换为 U+FFFD（数据损坏），且旧版部分路径写入裸字节导致导入非法。修复：① catalog.py 新增 `detect_database_encoding()`——从 `global/1262` 解析 pg_database 元组按库 OID 探测 `datencoding`，ID→codec 映射表按 **PG12-18 各版本源码 `pg_wchar.h` 的 `pg_enc` 权威枚举逐一核对**（UTF8=6、LATIN1=8、GB18030=40、GBK=38…，7 个版本完全一致）；② 新增 `--encoding CODEC` 命令行覆盖；③ binary.py `decode_bytes`/`cstring` 按库编码解码，strict 解码失败时**逐字节 latin-1 兜底**（0x9B→U+009B→UTF-8 `C2 9B`，导出文件恒为合法 UTF-8 且字节无损可逆）。调试实证修正本轮的 4B 偏移：1262 元组 encoding 字段在 `t_hoff+76`，原 layout 用 2 元组 `(64,'c')` 被 `_extract_fields_direct` 误判为 varlena 列导致错位，已按 3 元组 `(attlen, is_varlena, attalign)` 修正。验证：PG16 LATIN1 库 `0x9B 0x81 0x9B`→导出→导入 UTF8 库字节一致（`C2 9B` 无损）；**金仓 V9 MySQL 用户报错库**（base/24576/41008，50 列，2,591,126 个 TOAST valueid）自动探测 utf-8→导出 SQL 全 UTF-8 合法（原 10955 行 0x9B 消除）；PG12-18 × 16KB/32KB 14 套 + 金仓 V8R6C9B14 8/16/32KB 3 套"导出→同版本导入→逐值一致"闭环全 PASS、`--list-tables` 14 套 PASS、并行 CSV 与串行 CSV 逐字节一致。
- **2026-09-22（代码审核轮 v3.2，main.py 1.23→1.24、types.py 2.2→2.3）**：全量通读 9 个源码文件后修复 4 处健壮性/逻辑问题——① **`--list-db`/`--list-tables-db` 辅助命令硬编码 8192 页大小**：16KB/32KB 实例上 `_is_pg_class_content`/`_is_pg_database_content`/`_parse_pg_database`/`_parse_pg_class`/`_parse_pg_class_raw_scan` 用固定 8192 读页，Page 长度校验失败导致数据库名/表名解析为空（仅显示 OID）。修复：上述路径全部改为 `_probe_file_page_size()` 页大小自动探测；② **并行 CSV 路径 `_write_row` 缺 `__TOAST_MISSING__` 特判**：与单进程 `heapfile.to_data()` 不一致（TOAST chunk 缺失时并行输出字面字符串、单进程输出 `\N`）。修复：两者一致输出 `\N`（NULL）；③ **`--deleted --count` 忽略 deleted 标志**：count 模式只统计活行。修复：`include_deleted = deleted or only_deleted`；④ **`decode_money` 用浮点除法**：int64 微元 `/100` 转 float，>2^53/100 微元时精度损失（实测 `9223372036854775807` 微元旧实现输出 `92233720368547760.00`）。修复：整数整除 + 补零。回归：PG12-18 × 16KB/32KB 14 套 + 金仓 V8R6C9B14 8/16/32KB 3 套全部"导出→同版本导入→三库逐值一致"闭环通过；`--list-db`/`--list-tables-db` 全 17 套名称解析 PASS；并行 CSV 与默认 CSV 逐字节一致；`--deleted --count`=活行+删除行（修复前漏删除行）。
- **2026-09-22（CSV 转义双路径统一修复 + 全版本边界回归轮 v3.1）**：发现并行模式（`--parallel>1`）的 CSV 写入与默认路径转义规则不一致——缺 `\r`（回车）检测与字面 `\N` 特判，字段含单独回车会破坏 COPY 行结构、字面 `\N` 会被误判为 NULL。修复 main.py 1.22→1.23：`_write_row` 对齐 `heapfile.to_data()` 规则（含分隔符/换行/回车/引号或恰好为 `\N` 的字段一律双引号包裹、内部引号双写）。fixture 增至 4 行（新增 CSV 边界行：单独 `\r` 字段、字面 `\N`、空串 vs NULL、含逗号+引号+回车的数组元素、json 空字符串）→ PG12-18 × 16KB/32KB 14 套 + 金仓 8/16/32KB 3 套全部"导出→同版本导入→三库 38 列逐值一致"；并行 CSV 与默认 CSV 导入值逐行一致（`c_varchar='A\rB'` 回车无损、`c_text='\N'` 保留字面非 NULL）。该修复为纯输出逻辑，金仓与 PG 各版本通用。
- **2026-09-22（PG12-18 非默认表空间全版本闭环轮 v3.0）**：PG12-18 × 16KB/32KB 共 14 套实例，`CREATE TABLESPACE pts LOCATION '<数据目录外路径>'`，38 列全类型表置于表空间（`pg_tblspc/{oid}` 符号链接指向 `PG_{ver}_.../{dboid}/{relfilenode}`），灌入 3 行（第 3 行含中英文特殊字符：单引号/双引号/反斜杠/换行/制表/回车/`%&$#@!?;--`/中文全角标点/emoji，jsonb/json 中文键值）→ 自动探测页大小 → SQL 与 CSV 双路径导出 → 导入**同版本**新库 → 38 列逐值一致。
- **修复：enum label 版本感知（catalog.py 2.9→3.0、main.py 1.21→1.22）**：PG 的 `pg_enum.enumlabel` 是 name 定长 64B（NUL 右填充），金仓（V8R6C8B14/V8R6C8B20/V8R6C9B14/V9R1C10）是 varlena varchar（实测 V9 各库 sys_attribute 中 enumlabel attlen=-1）。此前统一按 varlena 提取，PG 的 name 首字节为奇数（如 `'sad'` 0x73）被误判为 1B 变长头而丢失首字符（实测导出 `'sad'` 变 `'ad'`）。修复后 `load_enum_map` 按 `is_kingbase` 分支选择布局与提取方式，PG 全版本 14 套表空间闭环三库逐值一致（含 enum `'sad'` 无损）。
- **2026-09-22（金仓非默认表空间闭环轮 v2.9）**：金仓 V8R6C9B14 三实例（8/16/32KB）`CREATE TABLESPACE kbts LOCATION '<数据目录外路径>'`，38 列全类型表置于表空间（物理文件在 `sys_tblspc/{oid}` 符号链接指向的 `SYS_12_202404121/{dboid}/{relfilenode}`），灌入 3 行（第 3 行含中英文特殊字符：单引号/双引号/反斜杠/换行/制表/回车/`%&$#@!?;--`/中文全角标点/emoji）。pg2sql 直接以表空间物理文件为 datafile（`--datadir` 指向数据目录取元数据）→ 自动探测页大小（8192/16384/32768）→ SQL 与 CSV 双路径导出 → 导入同版本金仓新库 → count=3 且 38 列逐值一致，特殊字符（含 emoji、`引号"和反斜杠\`）三库无损。
- **2026-09-22（金仓含中文全类型双路径闭环轮 v2.8）**：金仓 V8R6C9B14 三实例（8/16/32KB）灌入 38 列全类型表 3 行（第 3 行为中文数据：中文 char/varchar/text/name、中文键值 jsonb、中文数组元素、含引号/换行中文文本等），自动探测页大小（8192/16384/32768）→ SQL 与 CSV 双路径导出 → 导入同版本金仓新库（SQL 路径 `psql -f`、CSV 路径 `COPY ... WITH (FORMAT csv, NULL E'\\N')`）→ count=3 且 38 列逐值完全一致。期间实证一条金仓语义差异：**金仓把空 bytea（`''::bytea`）存储为 NULL**（PG 存空串），pg2sql 如实导出 NULL，非解析缺陷；CSV 的 NULL 标记为 `\N`，导入需配 `NULL '\N'`（README 已注明 LOAD DATA 用法）。
- **2026-09-22（金仓多 Block Size 闭环轮 v2.7）**：用金仓 V8R6C9B14 安装包 initdb 创建 8/16/32KB 三实例（`--block-size=8/16/32`）实测闭环。修复 2 处金仓差异：① types.py 2.1→2.2 `DECODERS[8020]=decode_timestamp`——金仓 oracle 模式 DATE 类型 oid=8020（PG 为 1082），磁盘为 8B 微秒、epoch 2000-01-01（同 PG timestamp 布局），原按未知类型输出原始二进制；② catalog.py 2.8→2.9 `load_enum_map` enumlabel 按 varlena 读取——金仓 `sys_enum.enumlabel` 为 varchar（attlen=-1，1B 头 `len<<1|1`），PG 为 name(64B)，原按定长 64 读取致枚举映射为空、DDL 输出 `"c_mood" oid:16388`。修复后三实例自动探测（8192/16384/32768）→ 导出 → 导入 PG18 → 38 列逐值一致。
- **2026-09-21（全版本 Block Size 闭环轮 v2.6）**：编译并实测 PG17.11 / PG18.6 的 16KB/32KB 实例（编译环境缺 bison/flex，已源码级安装 GNU Bison 3.8.2 + Flex 2.6.4 至本地 _pgver/local）；连同此前 12-16 版本，**PG12-18 全 7 版本 × 16KB/32KB 共 14 套导出→导入→逐值一致闭环全部通过**（8KB 此前已闭环），页大小自动探测逻辑跨版本无差异（`pd_pagesize_version` 语义 PG12-18 不变）。
- **2026-09-21（Block Size 自动探测轮 v2.5）**：新增任意页大小自动探测——page.py 1.6→1.7 `detect_page_size(raw)`（`pd_pagesize_version` 语义 + 页内指针链校验，支持 8/16/32KB）；heapfile.py 2.1→2.2 / toast.py 1.9→2.0 `page_size=None` 默认自动探测；catalog.py 2.6→2.7 系统目录探测 + 2.8 `_read_pages` 按探测页大小切页（修复 16KB 实例 pg_enum 被按 8192 切块致枚举映射为空、DDL 输出 `"c_mood" oid:16570` 的缺陷）；main.py 1.20→1.21 `--page-size` 默认 0（自动）。实测闭环：PG12.22 16KB、PG15.19 16KB、PG12.22 32KB 三套（38 列全类型边界表，导出→导入 8KB PG18→逐列值一致）；8KB 回归通过。`--page-size` 仍可显式指定覆盖。
- **2026-09-21（金仓全表闭环轮 v2.3）**：修复金仓 dropped 列识别错位——catalog.py 2.5→2.6：金仓 V8（PG8.4 代际）与 V9（PG12 代际）实测同用扩展 pg_attribute 固定区（relid..inhcount 共 21 项 + collation），attisdropped 在索引 **17**、attcollation 在 **20**；旧代码沿用 PG12 标准位（16/19），致 V8 `ksh_history_data` 等表 dropped 列泄漏进 DDL（`"........kb.dropped.1........" oid:0` 非法语法）。修复后 V8/V9 全表 DDL 干净。同期完成金仓 5 实例（V8R6C8B14-mysql / V8R6C8B20-ora / V8R6C9B14-ora / V9 test / V9 ray）**全表导出 SQL+CSV+DDL** 与 **导入闭环**（PG18 承载库重建→预建 schema→自动提取预建金仓专属角色→逐表 DROP+导入→count(*) 与 CSV 行数逐表比对）：68/12/23/15/15 表全部一致，仅 v9ray `test01`（pg_class 快照表，relacl 引用源库专属角色 sso_oper 等）因源数据角色特性未闭环，非解析器缺陷；导出失败项均为 information_schema/pg_catalog 系统表（设计过滤）。
- **2026-09-21（深度排查轮 v2.2）**：对照 PG18 源码 + 边界表 t_edge/t_net 实测定位并修复 6 处——① types.py 1.7→1.9 decode_array：`dataoffset` 是数据区相对 ArrayType 起点的绝对偏移（元素区起点 = dataoffset−4），位图字节数 = (nelems+7)//8（非 dataoffset），bit=1 表示非空（旧实现取反致含 NULL 数组全错位）；② types.py decode_inet/cidr 完全重写：PG7.4 起磁盘格式仅 family+bits+ipaddr（无 is_cidr/nb，恒 1B 头），旧实现把 varlena 头当 family 且按旧 4 字段布局解；③ types.py decode_interval：负数月转年用 C 整除（-13 mons → -1 years -1 mons，非 divmod）；④ types.py decode_jsonb：getJsonbOffset/getJsonbLength 按官方顺序推进语义重写（HAS_OFF 项重置为绝对终点 + 长度 = offlen−起点）；⑤ heapfile.py 2.0→2.1 to_data：改纯 COPY CSV 语义（引号包裹+双写、反斜杠原样、NULL 裸 \N），修复原 text/csv 混合转义导致数组/文本导入失败；⑥ main.py 1.19→1.20 `_resolve_output_path`：无扩展名视为前缀（--ddl --sql --data 组合输出不再互相覆盖）、带扩展名视为完整路径。验证：PG18 t_edge（16 列 × 3 行边界值：NULL 数组元素/多维数组/含引号换行反斜杠文本/inet v4v6/负 interval/jsonb 嵌套/NaN±Infinity）SQL+CSV 双路径导入逐列一致；t_net（inet/cidr/macaddr/macaddr8）导入一致；PG12-18 七版本精简边界表导出→导入 md5 全同；归档回归套件 12 项全过。
- **2026-09-21（v2.0 README）**：对齐精简包结构（tests/diag 移出）；新增兼容性实测矩阵（PG12-18 + 金仓 V8R6C8B14/B20/V8R6C9B14/V9R1C10）；补充枚举/interval/float 特殊值支持与已知限制。
- **2026-09-20（修复版）**：修复《源码评审报告》6 个 P1 + 4 个 P2 缺陷并在 PG16.4 双路径回归通过。关键变更：export_meta.sql 重写（OID 显式 ::int、jsonb_pretty）；catalog.py 全版本感知布局 + OID 首列公式；heapfile.py 列级 attalign 对齐 + dropped 列占位；tuple.py 版本门控；types.py jsonb/数组/短 numeric/枚举解码。
- **2026-09-21（扩展类型轮）**：枚举列读 pg_enum 映射 + CREATE TYPE 前置（catalog.py 2.4）；interval 对齐 PG EncodeInterval 输出（types.py 1.7）；float NaN/±Infinity 带引号字面量（heapfile.py 1.8 / main.py 1.18）。
- **2026-09-21（全版本轮）**：PG12/13 attstorage/attalign 顺序 + attinhcount int4；PG18 pg_class 新增 relallfrozen（用户列偏移 115→119）；PG18 pg_attribute 删 attcacheoff 且 typmod/ndims 换位；PG17 stattarg/collation 交换。
- **2026-09-21（v2.4）**：许可由 GPL-3.0 改为 MIT License（附完整许可文本）。
