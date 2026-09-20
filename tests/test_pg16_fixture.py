#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-3 回归：真实 PG16.4 堆页 fixture。

fixtures/pg16/*.dat 为真实 PG16.4 实例（tdb 库）各测试表的堆文件第 0 页原始字节
（CHECKPOINT 落盘后拷贝），meta.json 为该实例 export_meta.sql（修复版）导出的
权威系统目录元数据。期望 INSERT 行由同一实例服务端真值（SET TIME ZONE 'UTC'）逐行核对固化。

运行：python3 tests/test_pg16_fixture.py
无服务器依赖；只验证 catalog-json 路径（自动发现路径见 scripts/regress_live.sh）。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pg16"
MAIN = ROOT / "main.py"

# 期望输出 = pg2sql --sql 的 INSERT 行（逐字，含列名列表；服务端真值核对后固化）
EXPECTED = {
    "t_name": [
        'INSERT INTO "public"."t_name" ("id", "name", "num", "ts", "amt") VALUES (1, \'alice\', 100, \'2024-01-02 03:04:05+00\', 123.45);',
        'INSERT INTO "public"."t_name" ("id", "name", "num", "ts", "amt") VALUES (2, \'bob\', 200, \'2025-06-07 08:09:10+00\', -0.01);',
    ],
    "t_json": [
        "INSERT INTO \"public\".\"t_json\" (\"id\", \"j\", \"t\") VALUES (1, '{\"a\": 1, \"b\": [1, 2, 3], \"c\": \"x\", \"d\": null, \"e\": true, \"f\": 3.14, \"g\": {\"h\": [10, 20]}}', 'plain text');",
    ],
    "t_drop": [  # b 列已 DROP：输出中不得出现
        'INSERT INTO "public"."t_drop" ("id", "a", "c") VALUES (1, 10, 30);',
        'INSERT INTO "public"."t_drop" ("id", "a", "c") VALUES (2, 40, 60);',
    ],
    "t_arr": [
        'INSERT INTO "public"."t_arr" ("id", "arr", "txt") VALUES (1, \'{1,2,3}\', \'{x,y}\');',
    ],
    "t_nn": [
        'INSERT INTO "public"."t_nn" ("id", "nm") VALUES (1, \'hello\');',
    ],
    "t_null": [
        'INSERT INTO "public"."t_null" ("a", "b", "c", "d") VALUES (1, NULL, 2, NULL);',
        'INSERT INTO "public"."t_null" ("a", "b", "c", "d") VALUES (3, \'x\', NULL, \'y\');',
    ],
    "t_j": [
        'INSERT INTO "public"."t_j" ("id", "j") VALUES (1, \'{"a": 1, "b": 2}\');',
        "INSERT INTO \"public\".\"t_j\" (\"id\", \"j\") VALUES (2, '123');",
        'INSERT INTO "public"."t_j" ("id", "j") VALUES (3, \'"abc"\');',
        "INSERT INTO \"public\".\"t_j\" (\"id\", \"j\") VALUES (4, 'true');",
        'INSERT INTO "public"."t_j" ("id", "j") VALUES (5, \'[1, 2]\');',
        'INSERT INTO "public"."t_j" ("id", "j") VALUES (6, \'{"s": "hello world", "x": {"y": [1]}}\');',
    ],
    "t_n": [
        'INSERT INTO "public"."t_n" ("id", "n") VALUES (1, 123);',
    ],
    "t_jn": [
        "INSERT INTO \"public\".\"t_jn\" (\"id\", \"j\") VALUES (1, '1');",
        "INSERT INTO \"public\".\"t_jn\" (\"id\", \"j\") VALUES (2, '2');",
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (3, \'3.14\');',
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (4, \'-0.01\');',
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (5, \'100000000\');',
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (6, \'0.001\');',
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (7, \'123.45\');',
        'INSERT INTO "public"."t_jn" ("id", "j") VALUES (8, \'-123\');',
    ],
}

DDL_EXPECT = {  # DDL 关键断言：dropped 列过滤 + 列齐全
    "t_drop": ['"id" integer', '"a" integer', '"c" integer'],
}


def run_pg2sql(table: str) -> str:
    dat = FIXTURES / f"{table}.dat"
    meta = FIXTURES / "meta.json"
    cmd = [
        sys.executable, str(MAIN), str(dat),
        "--catalog-json", str(meta),
        "--table-name", f"public.{table}",
        "--sql", "--ddl",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pg2sql failed on {table}: {r.stderr}")
    return r.stdout


def main() -> int:
    fails = 0
    for table, expect_rows in EXPECTED.items():
        out = run_pg2sql(table)
        insert_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("INSERT INTO")]
        ok = insert_lines == expect_rows
        # DDL 断言
        ddl_ok = True
        if table in DDL_EXPECT:
            ddl = out.split("INSERT INTO")[0]
            for col in DDL_EXPECT[table]:
                if col not in ddl:
                    ddl_ok = False
                    print(f"  {table}: DDL 缺 {col}")
            if '"b" integer' in ddl:
                ddl_ok = False
                print(f"  {table}: DDL 错误出现已删除列 b")
        if not ok:
            fails += 1
            print(f"[FAIL] {table}")
            print("  期望:")
            for ln in expect_rows:
                print(f"    {ln}")
            print("  实际:")
            for ln in insert_lines:
                print(f"    {ln}")
        else:
            print(f"[PASS] {table} ({len(expect_rows)} 行{'，DDL 校验通过' if table in DDL_EXPECT else ''})")
    print("=" * 60)
    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
