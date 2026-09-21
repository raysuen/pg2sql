#!/bin/bash
# 1. 删除当前目录一级所有.开头文件/目录，保留.git
find . -maxdepth 1 -name ".*" ! -path "." ! -name ".git" -exec rm -rf {} +

# 2. git添加：新增文件 + 已追踪但本地删除的文件
git add .
git add -u

# 3. 提交
git commit -m "`/Users/raysuen/raysuen/bin/rdate.py -f "%Y%m%d"`"

# 4. 推送到远程
git push origin main
