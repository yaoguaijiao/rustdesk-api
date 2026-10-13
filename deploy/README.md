# 私有 GHCR 镜像部署

私有发布目标为 `ghcr.io/yaoguaijiao/rustdesk-server-s6-private:v2026.10.14.1`，供本人及明确获授权的账户使用，包必须保持 **Private**。适用 `linux/amd64`，一个 S6 容器包含 hbbs、hbbr、Go API 和 Vue 管理前端。镜像只包含程序，不包含生产数据库、密钥或账号；具体可用版本和 digest 以私有发布 Actions 的成功结果为准。

使用已有的 Podman + **podman-compose 1.5 或更新版本**，或 Docker Engine + Docker Compose。123 服务器现有 podman-compose 为 1.5；升级工具使用的 Compose stdin 方式已在该版本核对。工具只需要 Linux Python 3.9+ 标准库，不需要在部署机器安装项目依赖或编译源码。以下命令在部署机器的私有 Bash 终端、此 `deploy` 目录中执行；一次部署只选择一种容器引擎。

## 先配置数据和地址

```bash
# 仅在还没有 .env 时复制小文本，已有配置不覆盖；写入约 1 KB，不下载依赖。
test -e .env || (umask 077; cp .env.example .env)
```

编辑 `.env`，填写 `PUB_HOST` 和完整的 `PUBLIC_API_URL`。前者派生 ID 服务器 `:21116`、中继 `:21117`；后者允许沿用已有反向代理地址。两项留空时 Compose 会拒绝启动，不依赖嵌套变量插值。

**升级现有服务必须沿用原两处绝对数据目录，并保持客户端原地址与公钥。** 123 服务器对应 `/opt/rustdesk/server_data`、`/opt/rustdesk/api_data`。原 `id_ed25519`、`id_ed25519.pub`、`db_v2.sqlite3`、`rustdeskapi.db` 都必须保留；不能清库、删除密钥或用新建空目录替代。模板本身不会自动执行迁移；配套 `upgrade.py` 可以为已支持的现有部署取得停服一致备份、切换镜像和回退旧镜像。

对于已有的 123 服务，使用原 `/opt/rustdesk/docker-compose.yml` 和其同目录现有 `.env`，**不要用新安装模板覆盖原 Compose/.env**。新安装才填写本目录 `.env` 的 `SERVER_DATA_DIR`、`API_DATA_DIR`；既有升级直接从运行容器与原配置核对挂载、端口、网络和项目名。

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

## 升级已有服务及回滚

`upgrade.py` 只支持 Linux 本地 Docker/Podman、现有单个 S6 容器 `rustdesk-server`、两处原 bind 数据卷和已验证的 API schema **265**。123 的原 service 是 `rustdesk`；项目名、原网络和挂载从现有部署读取，原 Compose 和现有 `.env` 会形成私有快照，不改写原配置。原 `MUST_LOGIN=N` 和 JWT 配置保持，免登录远控方式继续生效。

先在部署账户的私有终端完成前述 GHCR 登录，再执行固定版本升级：

```bash
# 在本 deploy 目录运行：使用原 Compose，先拉取私有固定镜像，拉取完成才停服。
# 会短暂中断服务；完整复制当时原数据目录（含 WAL/SHM）、配置及密钥到私有备份。
# 磁盘写入为新增镜像层、约等于数据目录大小的备份和少量日志；不安装、不编译、不清库。
python3 -B ./upgrade.py upgrade \
  ghcr.io/yaoguaijiao/rustdesk-server-s6-private:v2026.10.14.1 \
  --compose /opt/rustdesk/docker-compose.yml --service rustdesk --engine podman
```

Docker 部署将上述 `--engine podman` 改为 `--engine docker`，使用原 Docker Compose 路径。后续版本把镜像参数替换为已验证的新固定 tag，或直接粘贴 Actions 输出的完整 `ghcr.io/yaoguaijiao/rustdesk-server-s6-private@sha256:...` 引用。重复指定当前相同镜像 ID 时只核对现有健康、密钥和布局，不停服、不重复复制业务数据备份；可能有少量日志与镜像元数据写入。

工具将私有备份目录打印为“升级通过；备份与固定回滚入口：...”，目录位于原 Compose 同级的 `backups` 下。需要回滚时使用**这次工具实际输出的完整目录**，不使用旧手工切换的备份目录：

```bash
# 只在需要回滚时读取本次实际备份路径；变量只在内存，不复制任何数据库。
read -r -p '本次升级输出的完整备份目录：' RUSTDESK_UPGRADE_BACKUP
# 会短暂停服并重建旧镜像容器，沿用当前原卷；只写少量日志和容器元数据，不覆盖原库。
python3 -B ./upgrade.py rollback --backup "$RUSTDESK_UPGRADE_BACKUP"
```

升级后会检查三个 S6 服务、API、原密钥、端口/网络/业务环境及原数据库内容；验证失败时会尝试自动切回固定旧镜像，仍使用当前原数据卷。**回滚不恢复旧数据库、不清空数据、不重置密钥**；仅支持 schema 265。若数据库损坏或自动回退未完全通过，保留数据和私有日志供人工处理，不能自动用旧备份覆盖切换后的合法数据。

## 后续版本私有发布

唯一发布入口为[私有仓库 rustdesk-deploy 的 Actions](https://github.com/yaoguaijiao/rustdesk-deploy/actions/workflows/publish-ghcr.yaml)。在 **Publish private RustDesk S6 image → Run workflow** 中选择默认分支，填写：

| 输入 | 要求 |
| --- | --- |
| `version` | 新的固定发布版本，例如下一版 `v2026.10.14.2`；同时作为镜像 tag 和 API 显示版本，不能重复已发布 tag |
| `api_ref` | `yaoguaijiao/rustdesk-api` 已审阅源码的完整 40 位提交 SHA |
| `server_ref` | `yaoguaijiao/rustdesk-server` 已审阅源码的完整 40 位提交 SHA |
| `frontend_ref` | `yaoguaijiao/rustdesk-api-web` 已审阅源码的完整 40 位提交 SHA |

三项源码输入不能填写 `master`、`main`、分支名或缩短 SHA；未改动的部分沿用已审阅的固定 SHA。点击 Run workflow 后，依赖下载和前端/Go/Rust 编译只发生在 GitHub CI，用户电脑和生产服务器不参与构建；发布不会自动升级生产服务。

只有工作流成功、包保持 Private、匿名访问被拒绝，且认证 Docker/Podman 拉取及启动验证通过后，才使用该版本。到该次 Actions 的 Summary 获取固定 tag/digest，再按上面的登录和升级步骤使用；不将其他仓库或旧公开构建当作私有发布结果。
