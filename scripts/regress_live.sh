#!/usr/bin/env bash
# 实时实例双路径回归（PG16.x，需可连接实例与 pageinspect）
#
# 用法: ./scripts/regress_live.sh <PGDATA> <PGPORT> <PGSOCK> [DB=tdb]
#   PGDATA: 数据目录（读 base/<dboid>/<relfilenode> 原始文件）
#   PGPORT: 端口;  PGSOCK: socket 目录
#
# 覆盖：9 张边界表 ×（自动发现 / --catalog-json）两路径，逐表输出 INSERT 行。
# 期望行由服务端真值（SET TIME ZONE 'UTC'）比对固化，与 tests/test_pg16_fixture.py 一致。
set -u
PGDATA="${1:?PGDATA}"; PGPORT="${2:?PGPORT}"; PGSOCK="${3:?PGSOCK}"; DB="${4:-tdb}"
PGBIN="${PGBIN:-}"
if [ -z "$PGBIN" ] && command -v pg_config >/dev/null 2>&1; then
    PGBIN="$(pg_config --bindir)"
fi
[ -n "$PGBIN" ] && export PATH="$PGBIN:$PATH"
PSQL="psql -h $PGSOCK -p $PGPORT -U postgres -d $DB -tA"
cd "$(dirname "$0")/.." || exit 1

DBOID=$($PSQL -c "SELECT oid FROM pg_database WHERE datname='$DB'")
$PSQL -c "CREATE EXTENSION IF NOT EXISTS pageinspect;" >/dev/null 2>&1
$PSQL -At -f export_meta.sql > /tmp/regress_meta.json 2>/dev/null || { echo "export_meta.sql 失败"; exit 1; }

fail=0
for t in t_name t_json t_drop t_arr t_nn t_null t_j t_n t_jn; do
    rfn=$($PSQL -c "SELECT c.relfilenode FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname='$t' AND c.relkind='r'")
    [ -z "$rfn" ] && { echo "SKIP $t (不存在)"; continue; }
    f="$PGDATA/base/$DBOID/$rfn"
    for mode in auto json; do
        if [ "$mode" = auto ]; then
            out=$(python3 main.py "$f" --sql 2>/dev/null)
        else
            out=$(python3 main.py "$f" --catalog-json /tmp/regress_meta.json --table-name "public.$t" --sql 2>/dev/null)
        fi
        n=$(echo "$out" | grep -c "^INSERT" )
        echo "[$mode] $t: $n 行"
        [ "$n" -eq 0 ] && fail=$((fail+1))
    done
done
echo "=== 完成，失败计数: $fail ==="
exit $fail
