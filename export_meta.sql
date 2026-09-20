-- ============================================================
-- pg2sql 元数据导出脚本
-- 在线 PostgreSQL 实例上执行，导出表结构 JSON 供 pg2sql 离线解析使用
--
-- 用法:
--   psql -d yourdb -At -f export_meta.sql > meta.json
--
-- 或指定 schema:
--   psql -d yourdb -At -v schema=public -f export_meta.sql > meta.json
-- ============================================================

\set schema '''public'''
\if :{?schema}
\else
  \set schema '''public'''
\endif

WITH meta AS (
  SELECT
    current_database() AS database,
    COALESCE(json_agg(
      json_build_object(
        'schema', n.nspname,
        'table', c.relname,
        'relfilenode', c.relfilenode,
        'toastrelid', c.reltoastrelid,
        'primary_key', (
          SELECT json_agg(a.attname)
          FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
          WHERE i.indrelid = c.oid AND i.indisprimary
        ),
        'columns', (
          SELECT json_agg(
            json_build_object(
              'name', a.attname,
              -- P1-1: 数值字段显式 ::int，避免 JSON 中变成字符串
              'type_oid', a.atttypid::int,
              'len', a.attlen::int,
              'attnum', a.attnum::int,
              'typmod', a.atttypmod::int,
              'notnull', a.attnotnull,
              'dropped', a.attisdropped,
              -- P1-3: 导出对齐/传值/存储属性，离线解析按 attalign 逐列对齐
              'attalign', a.attalign::text,
              'attbyval', a.attbyval,
              'attstorage', a.attstorage::text
            )
            ORDER BY a.attnum
          )
          FROM pg_attribute a
          WHERE a.attrelid = c.oid AND a.attnum > 0
        )
      )
      ORDER BY n.nspname, c.relname
    ), '[]') AS tables
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind = 'r'
    AND n.nspname = :schema
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
)
-- P1-2: jsonb_pretty 自 PG14 可用；json_pretty 仅 PG17+。
-- 同时导出服务器版本号（P2-1 NULL 位图语义按版本门控）。
SELECT jsonb_pretty(jsonb_build_object(
  'database', database,
  'pg_version', (SELECT current_setting('server_version_num')::int),
  'tables', tables::jsonb
)) FROM meta;
