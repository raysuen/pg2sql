#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比 pg2sql 导出与第三方工具导出的 SQL 数据完整性。

pg2sql:   每行一条 INSERT INTO "ray"."test_50col" ("id", "col1",...) VALUES (...);  (50 值)
第三方:   批量 INSERT，每行一个元组 '\t (...),(...);'  (49 值，无 id)
"""
import sys, re, json
from collections import Counter

WS = "/Users/raysuen/.local/share/TeleAgent/TeleAgent的工作空间"
PG_FILE = f"{WS}/ray.test_50col.sql"
V_FILE = f"{WS}/test_50col_202609171356.sql"


def parse_values(s):
    """解析 VALUES (...) 内的值列表 → [(kind, value), ...]，kind: 's'字符串 / 'n'字面量(NULL/数字)"""
    vals = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in ", \t":
            i += 1
        if i >= n:
            break
        if s[i] == "'":
            i += 1
            buf = []
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'"); i += 2
                    else:
                        i += 1; break
                else:
                    buf.append(s[i]); i += 1
            vals.append(("s", "".join(buf)))
        else:
            j = i
            while j < n and s[j] != ",":
                j += 1
            vals.append(("n", s[i:j].strip()))
            i = j
    return vals


# ---------- 解析 pg2sql（全量，4172 条）----------
pg_rows = {}   # id -> [50 个值字符串(None 表示 NULL)]
pg_bad = 0
with open(PG_FILE, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line.startswith("INSERT INTO"):
            continue
        m = re.search(r" VALUES \((.*)\);$", line)
        if not m:
            pg_bad += 1
            continue
        vals = parse_values(m.group(1))
        if len(vals) != 50:
            pg_bad += 1
            continue
        id_v = vals[0][1]
        row = [v if k == "s" else (None if v == "NULL" else v) for k, v in vals[1:]]
        pg_rows[id_v] = row

print(f"pg2sql: 解析 {len(pg_rows)} 条 INSERT, 无法解析 {pg_bad} 条")
ids = sorted(pg_rows.keys(), key=lambda x: int(x))
id_ints = [int(x) for x in ids]
print(f"  id 范围: {id_ints[0]} ~ {id_ints[-1]}, 连续: {id_ints == list(range(id_ints[0], id_ints[-1]+1))}")

# ---------- 解析第三方（流式，只取需要的数量）----------
need = max(6000, len(pg_rows) + 2000)
v_rows = []
cur_stmt_cols = None
with open(V_FILE, encoding="utf-8", errors="replace") as f:
    for line in f:
        if line.startswith("INSERT INTO"):
            continue
        s = line.strip()
        if not s.startswith("("):
            continue
        s = s.rstrip(",;")
        if not s.endswith(")"):
            # 值中含换行的元组（罕见，跳过计数）
            continue
        vals = parse_values(s[1:-1])
        if len(vals) != 49:
            continue
        row = [v if k == "s" else (None if v == "NULL" else v) for k, v in vals]
        v_rows.append(row)
        if len(v_rows) >= need:
            break

print(f"第三方: 解析 {len(v_rows)} 个元组")

# ---------- 对齐策略 ----------
# 假设第三方按 id 升序（第 i 个元组 = id=i+1），用 pg2sql 的 id 直接索引
# 校验: 抽 pg2sql 前几条与第三方同位置的指纹比对

def norm(v):
    return v if v is not None else "__NULL__"

# 先验证顺序假设：pg2sql id=1 的 col 值 vs 第三方第 1 个元组
print("\n=== 顺序校验 (id=k ↔ 第三方第 k 个元组) ===")
sample_ok = 0
for k in [1, 2, 3, 100, 1000, 4172]:
    if str(k) in pg_rows and k - 1 < len(v_rows):
        a, b = pg_rows[str(k)], v_rows[k - 1]
        match = sum(1 for x, y in zip(a, b) if norm(x) == norm(y))
        print(f"  id={k}: 49 列中 {match} 列匹配")
        if match == 49:
            sample_ok += 1
print(f"  抽样 {sample_ok}/6 完全匹配")

# ---------- 全量对比 ----------
print("\n=== 全量对比 (pg2sql id=k ↔ 第三方第 k 个元组) ===")
n_cmp = 0
n_full_match = 0
col_diff = Counter()       # 列名 -> 差异次数
diff_kinds = Counter()     # 差异类型
examples = []
max_id = min(max(id_ints), len(v_rows))

for k in range(1, max_id + 1):
    if str(k) not in pg_rows:
        continue
    a, b = pg_rows[str(k)], v_rows[k - 1]
    n_cmp += 1
    diffs = []
    for ci in range(49):
        x, y = a[ci], b[ci]
        if norm(x) == norm(y):
            continue
        colname = f"col{ci+1}"
        col_diff[colname] += 1
        # 分类差异
        if x is None and y is not None:
            kind = "pg2sql为NULL(应为值)"
        elif y is None and x is not None:
            kind = "第三方为NULL"
        elif x is not None and y is not None:
            xs, ys = str(x), str(y)
            if ys.startswith(xs.rstrip()) or xs.rstrip() in ys:
                kind = "截断(前缀匹配)"
            elif "\ufffd" in xs:
                kind = "乱码(U+FFFD)"
            elif xs != xs.strip() and ys.startswith(xs.strip()):
                kind = "带前导空白"
            else:
                kind = "其他"
        else:
            kind = "其他"
        diff_kinds[kind] += 1
        if len(examples) < 12:
            examples.append((k, colname, kind, repr(str(x))[:80], repr(str(y))[:80]))
    if not diffs:
        n_full_match += 1

print(f"对比行数: {n_cmp}")
print(f"完全匹配行: {n_full_match} ({n_full_match/n_cmp*100:.1f}%)")
print(f"\n差异类型分布: {dict(diff_kinds)}")
print(f"差异列 Top10: {col_diff.most_common(10)}")
print(f"\n差异示例 (前 12 个):")
for k, colname, kind, x, y in examples:
    print(f"  id={k} {colname} [{kind}]")
    print(f"    pg2sql: {x}")
    print(f"    第三方: {y}")

# ---------- 值长度分布诊断 ----------
print("\n=== pg2sql 值长度特征诊断 ===")
lens_pg = Counter()
lens_v = Counter()
for k in range(1, min(max_id, 200) + 1):
    if str(k) not in pg_rows:
        continue
    for ci in range(49):
        x, y = pg_rows[str(k)][ci], v_rows[k - 1][ci]
        if x is not None:
            lens_pg[len(str(x))] += 1
        if y is not None:
            lens_v[len(str(y))] += 1
print(f"  pg2sql 值长度 Top8: {lens_pg.most_common(8)}")
print(f"  第三方 值长度 Top8: {lens_v.most_common(8)}")

# 乱码字符统计
fffd = sum(1 for k in range(1, max_id + 1) if str(k) in pg_rows
           for x in pg_rows[str(k)] if x and "\ufffd" in str(x))
print(f"  pg2sql 含 U+FFFD 乱码的值: {fffd}")

# NULL 对比
pg_null = sum(1 for k in range(1, max_id + 1) if str(k) in pg_rows
              for x in pg_rows[str(k)] if x is None)
v_null = sum(1 for k in range(1, max_id + 1) if str(k) in pg_rows
             for x in v_rows[k - 1] if x is None)
print(f"  NULL 值: pg2sql={pg_null}, 第三方={v_null}")

print(f"\n=== 结论 ===")
print(f"  表总行数(第三方): 100000")
print(f"  pg2sql 导出行数: {len(pg_rows)} (中断的部分导出)")
print(f"  已导出部分值完整率: {n_full_match}/{n_cmp} = {n_full_match/n_cmp*100:.2f}%")
