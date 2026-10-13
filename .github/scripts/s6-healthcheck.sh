#!/bin/sh
# 只读取 S6 状态并请求容器内 API，不修改数据库。
set -eu
for service in hbbs hbbr api; do
    test "$(/command/s6-svstat -u "/run/service/$service")" = true || exit 1
done
wget -q -T 3 -O /dev/null http://127.0.0.1:21114/api/version
