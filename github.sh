#!/bin/bash
# 1. 删除当前目录一级 .开头文件/目录，保留.git
find . -maxdepth 1 -name ".*" ! -path "." ! -name ".git" -exec rm -rf {} +

# 2. 收集所有变更：新增文件 + 已追踪文件的删除/修改
git add .
git add -u

# 3. 提交
git commit -m "`/Users/sunpeng/raysuen/bin/rdate.py`"

# 4. 推送
git push origin main
