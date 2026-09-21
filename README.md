
# pg2sql

> 离线解析 PostgreSQL / 金仓数据库（KingbaseES）堆数据文件并导出为 SQL

> README 版本：v2.0（2026-09-21 重写，对齐当前精简包结构，补充全版本实测矩阵）

## 简介

pg2sql 是一个纯 Python 编写的数据库数据文件解析工具，**无需实例运行，无需任何第三方依赖**。直接读取堆数据文件（`base/{db_oid}/{relfilenode}`），解析 8KB 堆页面中的 HeapTuple，输出 DDL 和 INSERT 语句（或 CSV），支持已删除行的审计恢复。

## 兼容性（实测矩阵，2026-09 闭环验证）

**PostgreSQL（全部"导出 SQL/CSV + 可导入"闭环通过）**

| 版本 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
|---|---|---|---|---|---|---|---|
| 实测 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

覆盖：基础类型全列、dropped 列、TOAST 跨块大字段（30K–120K 字符）、枚举、interval、float 特殊值（NaN/±Infinity）、时间 ±infinity、声明式分区表（RANGE/LIST 子表）、序列依赖、ACL、20000 行批量。

**金仓 KingbaseES（"导出 SQL/CSV + 可导入"闭环通过）**

| 版本 | V8R6C8B14 | V8R6C8B20 | V8R6C9B14 | V9R1C10 |
|---|---|---|---|---|
| 兼容模式 | MySQL | Oracle | Oracle | MySQL |
| 内核代际 | 早期（PG8.4 代际） | 同左 | 同左 | PG12 代际 |
| 系统表命名 | `_` 短名（`_rel`/`_att`/`_typ`） | 同左 | 同左 | `sys_*` |
| 实测 | ✅ 10 张用户表 | ✅ 5 张用户表 | ✅ 16 张用户表 | ✅ 早期轮次闭环 |

金仓 V8 系列系统表 oid 仍为标准值（sys_class=1259 / sys_attribute=1249 / sys_type=1247），catalog 自动发现按 oid 定位、与表名前缀无关，同一代码库同时兼容 V8/V9 双代际。

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
| `--page-size N` | 页面大小（默认 8192） |
| `--parallel N` | 并发进程数 |
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

Python >= 3.6，无第三方依赖。GPL-3.0。

## 变更记录

- **2026-09-21（v2.0 README）**：对齐精简包结构（tests/diag 移出）；新增兼容性实测矩阵（PG12-18 + 金仓 V8R6C8B14/B20/V8R6C9B14/V9R1C10）；补充枚举/interval/float 特殊值支持与已知限制。
- **2026-09-20（修复版）**：修复《源码评审报告》6 个 P1 + 4 个 P2 缺陷并在 PG16.4 双路径回归通过。关键变更：export_meta.sql 重写（OID 显式 ::int、jsonb_pretty）；catalog.py 全版本感知布局 + OID 首列公式；heapfile.py 列级 attalign 对齐 + dropped 列占位；tuple.py 版本门控；types.py jsonb/数组/短 numeric/枚举解码。
- **2026-09-21（扩展类型轮）**：枚举列读 pg_enum 映射 + CREATE TYPE 前置（catalog.py 2.4）；interval 对齐 PG EncodeInterval 输出（types.py 1.7）；float NaN/±Infinity 带引号字面量（heapfile.py 1.8 / main.py 1.18）。
- **2026-09-21（全版本轮）**：PG12/13 attstorage/attalign 顺序 + attinhcount int4；PG18 pg_class 新增 relallfrozen（用户列偏移 115→119）；PG18 pg_attribute 删 attcacheoff 且 typmod/ndims 换位；PG17 stattarg/collation 交换。
