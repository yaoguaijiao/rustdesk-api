# 私有 GHCR 镜像部署

镜像 `ghcr.io/yaoguaijiao/rustdesk-server-s6:v2026.10.14.1` 供本人及明确获授权的账户使用，包保持 **Private**。适用 `linux/amd64`，一个 S6 容器包含 hbbs、hbbr、Go API 和 Vue 管理前端。镜像只包含程序，不包含生产数据库、密钥或账号。

使用已有的 Podman + podman-compose，或 Docker Engine + Docker Compose。不需要在部署机器安装项目依赖或编译源码。以下命令在部署机器的私有 Bash 终端、此 `deploy` 目录中执行；一次部署只选择一种容器引擎。

## 先配置数据和地址

```bash
# 仅在还没有 .env 时复制小文本，已有配置不覆盖；写入约 1 KB，不下载依赖。
test -e .env || (umask 077; cp .env.example .env)
```

编辑 `.env`，填写 `PUB_HOST` 和完整的 `PUBLIC_API_URL`。前者派生 ID 服务器 `:21116`、中继 `:21117`；后者允许沿用已有反向代理地址。两项留空时 Compose 会拒绝启动，不依赖嵌套变量插值。

**升级现有服务必须让 `SERVER_DATA_DIR`、`API_DATA_DIR` 指向原两处绝对目录，并保持客户端原地址与公钥。** 123 服务器对应 `/opt/rustdesk/server_data`、`/opt/rustdesk/api_data`。原 `id_ed25519`、`id_ed25519.pub`、`db_v2.sqlite3`、`rustdeskapi.db` 都必须保留；不能清库、删除密钥或用新建空目录替代。先取得一致备份，再核对原环境变量和配置差异；此模板不提供自动迁移或自动回滚。

```bash
# 升级前只读检查原四个文件；全部通过后才继续，失败时不能启动到空库。
# 此处是 123 服务器路径，其他部署应改成自己的原绝对路径；不写入磁盘。
test -s /opt/rustdesk/server_data/id_ed25519 && \
test -s /opt/rustdesk/server_data/id_ed25519.pub && \
test -s /opt/rustdesk/server_data/db_v2.sqlite3 && \
test -s /opt/rustdesk/api_data/rustdeskapi.db
```

首次空目录安装会生成新密钥及数据库。两处 bind mount 的 `:Z` 会在启用 SELinux 的系统上调整数据目录标签，所以只能指向该服务专用目录。既有 `/app/conf` 等定制配置挂载应在原 Compose 中保留。模板保持 `MUST_LOGIN=N`、JWT key 为空，远控无需 API 账号登录，但仍使用原公钥及设备密码；后台账号登录继续生效。

## 登录私有镜像仓库

在 [GitHub Token 设置](https://github.com/settings/tokens/new?scopes=read:packages) 网页自行创建 **personal access token (classic)**，只勾选 `read:packages`，设置合适的到期时间。登录账户还须具有该私有包的读取权限。不要选择写入或删除权限，不把 PAT 保存到 `.env`、源码、命令历史或发给 AI。[GitHub 官方认证说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)

```bash
# 关闭命令回显，交互读取用户名和隐藏 PAT；只占内存，不把 token 写进命令历史。
set +x
read -r -p 'GitHub 用户名：' GHCR_USER
read -r -s -p '只读 GHCR Token：' GHCR_PULL_PAT
printf '\n'
# Podman 登录会在当前部署账户的本机 authfile 保存少量凭据；不拉取镜像。
printf '%s' "$GHCR_PULL_PAT" | podman login ghcr.io --username "$GHCR_USER" --password-stdin
# 清除 shell 中的 PAT，不打印内容；凭据仍由 Podman 的 authfile 管理。
unset GHCR_PULL_PAT
```

Podman 默认 Linux authfile 位于运行时目录，重启后可能需要重新登录；凭据是编码保存，不是加密保险箱，文件只应由部署账户访问，不能上传。后续 pull 使用同一部署账户。[Podman 官方登录说明](https://docs.podman.io/en/latest/markdown/podman-login.1.html)

Docker 用户在上面同一交互流程中，把登录命令替换为下列命令，然后同样清除变量：

```bash
# Docker 替代登录：同样从 stdin 读取 PAT；凭据由本机 credential store/config 管理。
printf '%s' "$GHCR_PULL_PAT" | docker login ghcr.io --username "$GHCR_USER" --password-stdin
# 清除内存中的 PAT，不打印、不保存到源码。
unset GHCR_PULL_PAT
```

## 拉取和启动

以下启动命令用于配置已核对的新安装。已有生产容器升级前，应先完成备份、端口/项目/原挂载核对及切换安排，不能与旧容器同时占用同一数据卷和端口。

```bash
# Podman：只拉取指定固定版本，写入服务器镜像存储；已有层会复用，不编译。
podman-compose -p rustdesk -f compose.yaml --env-file .env pull
# 启动一体容器；会创建容器/bridge 网络，首次安装会写密钥、数据库及日志。
podman-compose -p rustdesk -f compose.yaml --env-file .env up -d
```

```bash
# Docker 替代：拉取固定版本，写入 Docker 镜像存储，已有层复用，不编译。
docker compose -p rustdesk -f compose.yaml --env-file .env pull
# 创建并启动容器/bridge 网络；持久化写入仅在指定数据目录及容器日志中。
docker compose -p rustdesk -f compose.yaml --env-file .env up -d
```

使用固定项目名 `-p rustdesk`。服务为 `rustdesk`、容器名为 `rustdesk-server`，bridge 网络 key 为 `rustdesk-net`；映射 TCP 21114–21119 及 UDP 21116。显式健康检查要求 hbbs、hbbr、API 三个 S6 进程都实际 up，且内部 `/api/version` 请求成功。

管理入口为 `PUBLIC_API_URL/_admin/`。只有首次空库安装才生成随机 admin 密码；在自己的私有终端查看首次容器日志，登录后自行修改密码，不上传日志或密码。沿用旧库时继续使用原账号密码。

```bash
# Podman：只读查看最近启动日志，不写文件；可能含首次密码，只能本人私下查看。
podman logs --tail 200 rustdesk-server
```

```bash
# Docker 替代：只读查看日志，不写文件；不要复制密码或完整日志到 AI/GitHub。
docker logs --tail 200 rustdesk-server
```

`.env`、authfile、数据库、密钥、备份和日志均留在部署机器，不提交到 Git。后续版本只在验证并备份后更换固定 tag/digest，保留原两处数据目录。
