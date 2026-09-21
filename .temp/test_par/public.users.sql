CREATE TABLE "public"."users" (
  "id" integer NOT NULL,
  "name" text,
  PRIMARY KEY ("id")
);

INSERT INTO "public"."users" ("id", "name") VALUES (1, 'alice');
INSERT INTO "public"."users" ("id", "name") VALUES (2, 'bob');
INSERT INTO "public"."users" ("id", "name") VALUES (3, 'charlie');
INSERT INTO "public"."users" ("id", "name") VALUES (4, '中文测试');
INSERT INTO "public"."users" ("id", "name") VALUES (5, 'hello world');
