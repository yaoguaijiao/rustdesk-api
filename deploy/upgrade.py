#!/usr/bin/env python3
"""现有单容器 S6 部署的镜像升级；Python 标准库，Linux 本地 engine。

pull 会写入服务器镜像层；停服后仅备份已存在的两个 bind 数据目录和配置。
不安装、不构建、不清库，不覆盖原 Compose，不删除密钥或数据目录。
失败仅退回固定旧镜像并保留当前卷；数据异常保留给操作者处理。
要求原 Compose/.env 与运行配置对应，关键业务配置写在 Compose 或 .env 中；
不支持远程 engine、外部 env_file/include 或额外数据服务共享这些目录。
"""
import argparse
import base64
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows 可查看 --help；实际部署在 Linux 上执行。
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

NAME = "rustdesk-server"
DATA = {"/data": "server_data", "/app/data": "api_data"}
HEALTH = " && ".join(
    '[ "$(/package/admin/s6/command/s6-svstat -u /run/s6-rc/servicedirs/%s)" = true ]' % s
    for s in ("hbbs", "hbbr", "api")
) + " && wget -q -T 5 -O /dev/null http://127.0.0.1:21114/api/version"
MUTABLE = {"peer": {"info"}, "user_tokens": {"expired_at", "updated_at"},
           "peers": {"last_online_time", "last_online_ip", "updated_at", "cpu",
                     "hostname", "memory", "os", "username", "version"},
           "address_books": {"online", "updated_at"}}


class GuardError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise GuardError(message)


def fixed_image(value):
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:@-]*", value)), "镜像引用格式无效")
    name, sep, digest = value.partition("@")
    if sep:
        require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", digest)), "镜像 digest 无效")
    tail = name.rsplit("/", 1)[-1]
    tag = tail.split(":", 1)[1] if ":" in tail else ""
    require(bool(sep or tag), "必须提供固定版本标签或 digest")
    require(not re.match(r"(?i)^(latest|stable|edge|nightly|main|master|dev|test)(?:$|[-_.])", tag),
            "拒绝浮动镜像标签")
    return value


def plain(path, directory=None):
    path = Path(os.path.abspath(path))
    for item in tuple(reversed(path.parents)) + (path,):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode), "路径不得含符号链接")
    require(path.resolve(strict=True) == path, "路径解析不一致")
    info = path.stat()
    if directory is True:
        require(stat.S_ISDIR(info.st_mode), "预期目录不存在")
    elif directory is False:
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "文件必须为无硬链接的普通文件")
    return path


def private_dir(path):
    path = Path(path)
    if not path.exists():
        plain(path.parent, True)
        path.mkdir(mode=0o700)
    path = plain(path, True)
    require(path.stat().st_uid == os.geteuid(), "私有目录必须属于当前用户")
    require(path.stat().st_mode & 0o077 == 0, "备份目录权限必须为 0700")
    return path


def save(path, value):
    # 所有新元数据文件均私有；不写入原配置或数据目录。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)


def digest(path):
    h = hashlib.sha256()
    with plain(path, False).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hashes(path):
    path = plain(path, True)
    result = {}
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            plain(Path(root) / name, True)
        for name in files:
            file = plain(Path(root) / name, False)
            result[str(file.relative_to(path))] = digest(file)
    return result


def key_hashes(directory):
    pub, secret = Path(directory) / "id_ed25519.pub", Path(directory) / "id_ed25519"
    require(plain(pub, False).stat().st_size <= 1024 and plain(secret, False).stat().st_size <= 4096,
            "密钥文件大小异常")
    try:
        pk = base64.b64decode(pub.read_bytes().strip(), validate=True)
        sk = base64.b64decode(secret.read_bytes().strip(), validate=True)
    except ValueError:
        raise GuardError("密钥文件格式异常") from None
    require(len(pk) == 32 and len(sk) == 64 and sk[32:] == pk, "现有密钥对格式不匹配")
    return {"public": digest(pub), "private": digest(secret)}


def database(path):
    path = plain(path, False)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        plain(sidecar, False)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "数据库完整性异常")
    except BaseException:
        db.close()
        raise
    return db


def compare_database(before, after, api):
    q = lambda s: '"' + s.replace('"', '""') + '"'
    with closing(database(before)) as old, closing(database(after)) as new:
        sql = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        require(set(old.execute(sql)) <= set(new.execute(sql)), "数据库原结构或索引变化")
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"versions", "users", "user_tokens", "peers", "address_books"} if api else {"peer"}
        require(required <= tables, "数据库缺少必要业务表")
        if api:
            for db in (old, new):
                require(db.execute("SELECT version FROM versions ORDER BY id DESC LIMIT 1").fetchone() == (265,),
                        "仅支持已验证的 API schema 265")
        for (table,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            info = old.execute("PRAGMA table_info(%s)" % q(table)).fetchall()
            columns = [r[1] for r in info]
            pk = [columns.index(r[1]) for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
            require(pk, "数据库表缺少主键")
            sql = "SELECT %s FROM %s" % (",".join(map(q, columns)), q(table))
            rows = lambda db: {tuple(r[i] for i in pk): r for r in db.execute(sql)}
            baseline, current = rows(old), rows(new)
            require(baseline.keys() <= current.keys(), "数据库原记录缺失")
            mutable = MUTABLE.get(table, set()) if api else {"info"}
            for key, row in baseline.items():
                for col, a, b in zip(columns, row, current[key]):
                    if col not in mutable:
                        require(a == b, "数据库原静态字段变化：%s.%s" % (table, col))
                    elif table == "user_tokens" and col == "expired_at":
                        require(b >= a, "原令牌有效期倒退")


class Engine:
    def __init__(self, name, log, cwd):
        require(name in ("podman", "docker"), "engine 无效")
        require(not os.environ.get("CONTAINER_HOST") and not os.environ.get("CONTAINER_CONNECTION"), "不支持远程 Podman")
        require(not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://"), "不支持远程 Docker")
        self.name, self.log, self.cwd = name, log, cwd
        require(shutil.which(name), "找不到已安装的 engine")
        self.provider = ["podman-compose"] if name == "podman" else ["docker", "compose"]
        if name == "podman":
            require(shutil.which("podman-compose"), "找不到已安装的 podman-compose")
        else:
            context = self.json(["context", "inspect"])[0]
            require(context["Endpoints"]["docker"]["Host"].startswith("unix://"), "Docker context 必须是本地 Unix socket")

    def run(self, args, capture=False, input=None, check=True, timeout=600):
        result = subprocess.run(args, input=input, stdout=subprocess.PIPE if capture else self.log,
                                stderr=self.log, cwd=self.cwd, timeout=timeout, text=True)
        require(not check or result.returncode == 0, "命令失败，详情仅写入私有日志")
        return result.stdout if capture else result.returncode

    def json(self, args):
        return json.loads(self.run([self.name] + args, capture=True, timeout=30))

    def container(self):
        return self.json(["inspect", NAME])[0]

    def image(self, ref):
        return self.json(["image", "inspect", ref])[0]

    def compose(self, state, override, action, capture=False):
        # stdin 作为首个 Compose 文件，两种 provider 均按原 cwd 解析相对路径。
        args = self.provider + ["-p", state["project"]]
        if self.name == "docker":
            args += ["--project-directory", str(self.cwd)]
        env = Path(state["backup"]) / "dotenv.snapshot"
        if env.exists():
            args += ["--env-file", str(plain(env, False))]
        args += ["-f", "-", "-f", str(override)] + action
        return self.run(args, capture=capture, input=plain(Path(state["backup"]) / "compose.snapshot", False).read_text(encoding="utf-8"))

    def stopped(self):
        state = self.container()["State"]
        require(not state["Running"] and state.get("Pid") == 0, "容器尚未完全停止，不能备份")

    def stop(self):
        if not self.container()["State"]["Running"]:
            self.stopped()
            return
        self.run([self.name, "stop", "--time", "30", NAME], timeout=60)
        self.stopped()

    def assert_no_writers(self, paths, skip_id=None):
        ids = self.run([self.name, "ps", "--quiet"], capture=True, timeout=30).split()
        roots = [Path(p) for p in paths.values()]
        for current in self.json(["inspect"] + ids) if ids else []:
            if current.get("Id") == skip_id or not current.get("State", {}).get("Running"):
                continue
            for mount in current.get("Mounts", []):
                if mount.get("RW") and mount.get("Source"):
                    source = Path(mount["Source"]).resolve()
                    require(not any(source == root or source in root.parents or root in source.parents for root in roots),
                            "另一个运行容器拥有重叠可写挂载；停止备份")

    def healthy(self):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.run([self.name, "exec", NAME, "sh", "-c", HEALTH], check=False, timeout=12) == 0:
                return
            time.sleep(2)
        raise GuardError("三个 S6 服务或 API 未通过健康检查")

    def fingerprint(self, container):
        network = container
        if self.name == "podman" and container.get("Pod"):
            pod = self.json(["pod", "inspect", container["Pod"]])
            pod = pod[0] if isinstance(pod, list) else pod
            if pod.get("InfraContainerID"):
                network = self.json(["inspect", pod["InfraContainerID"]])[0]
        ports = network.get("NetworkSettings", {}).get("Ports") or network.get("HostConfig", {}).get("PortBindings") or {}
        published = sorted((p, b.get("HostIp") or "0.0.0.0", str(b["HostPort"]))
                           for p, bindings in ports.items() for b in (bindings or []))
        env = dict(v.split("=", 1) for v in container["Config"].get("Env", []) if "=" in v)
        for key in ("PATH", "HOME", "HOSTNAME", "TERM"):
            env.pop(key, None)
        return {"ports": published, "env": env,
                "networks": sorted(network.get("NetworkSettings", {}).get("Networks", {})),
                "network_mode": network.get("HostConfig", {}).get("NetworkMode", "").split(":", 1)[0],
                "mounts": sorted((m["Type"], m["Source"], m["Destination"], m.get("RW")) for m in container["Mounts"])}


def deployment(container, service, engine, allow_stopped=False):
    state = container["State"]
    valid_state = state["Running"] and state.get("Pid", 0) > 0
    valid_state = valid_state or (allow_stopped and not state["Running"] and state.get("Pid") == 0)
    require(container["Name"].lstrip("/") == NAME and valid_state,
            "容器必须是当前支持的 rustdesk-server，且 PID 状态有效")
    require(container["Config"].get("Entrypoint") in (["/init"], "/init"), "现有容器不是已支持的 S6 部署")
    labels = container["Config"].get("Labels") or {}
    project = labels.get("com.docker.compose.project") or labels.get("io.podman.compose.project")
    require(project and re.fullmatch(r"[A-Za-z0-9_.-]+", project), "无法确定原 Compose 项目")
    require(labels.get("com.docker.compose.service") == service, "Compose service 与现有容器不匹配")
    paths = {}
    for mount in container["Mounts"]:
        target = mount["Destination"]
        require(mount["Type"] == "bind" and target in (*DATA, "/app/conf"), "不支持额外卷或挂载布局")
        source = plain(mount["Source"], True)
        if target in DATA:
            require(mount.get("RW") is True, "原数据挂载必须可写")
            paths[target] = str(source)
    require(set(paths) == set(DATA), "必须已有 /data 与 /app/data 两个 bind 挂载")
    a, b = map(Path, paths.values())
    require(a != b and a not in b.parents and b not in a.parents, "数据目录不得重叠")
    key_hashes(paths["/data"])
    for target, file in (("/data", "db_v2.sqlite3"), ("/app/data", "rustdeskapi.db")):
        with closing(database(Path(paths[target]) / file)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"versions", "users", "user_tokens", "peers", "address_books"} if target == "/app/data" else {"peer"}
            require(required <= tables, "现有数据库缺少必要业务表")
            if target == "/app/data":
                require(db.execute("SELECT version FROM versions ORDER BY id DESC LIMIT 1").fetchone() == (265,), "仅支持 schema 265")
    return project, paths, engine.fingerprint(container)


def override(backup, service, image, label):
    path = Path(backup) / (label + ".override.json")
    # Podman Compose 1.5 合并 healthcheck.test 数组会追加而不是替换；保留原健康配置。
    # 升级结果仍由 Engine.healthy 的三个 S6 服务 + API 检查独立确认。
    save(path, {"services": {service: {"image": image, "pull_policy": "never"}}})
    return path


def check_runtime(engine, state, image_id):
    current = engine.container()
    require(current["Image"].removeprefix("sha256:") == image_id.removeprefix("sha256:"), "运行镜像 ID 不匹配")
    # JSON 反序列化会将 tuple 转换成 list，统一序列化后比较。
    require(json.dumps(engine.fingerprint(current), sort_keys=True) == json.dumps(state["fingerprint"], sort_keys=True),
            "端口、网络、原挂载或业务环境发生变化")
    engine.healthy()
    require(key_hashes(state["paths"]["/data"]) == state["keys"], "原密钥内容变化；不会自动覆盖密钥")
    for target, filename in (("/data", "db_v2.sqlite3"), ("/app/data", "rustdeskapi.db")):
        source = Path(state["paths"][target]) / filename
        with closing(database(source)):
            pass
        if state.get("complete"):
            compare_database(Path(state["backup"]) / DATA[target] / filename, source, target == "/app/data")


def upgrade(args, compose, backup, log):
    engine = Engine(args.engine, log, compose.parent)
    current = engine.container()
    project, paths, fingerprint = deployment(current, args.service, engine)
    working_dir = current["Config"]["Labels"].get("com.docker.compose.project.working_dir")
    require(working_dir and plain(working_dir, True) == compose.parent, "必须在原 Compose 部署目录升级")
    for source in paths.values():
        require(Path(source) not in backup.parents, "备份目录不得位于数据目录中")
    source_text = compose.read_text(encoding="utf-8")
    require(not re.search(r"(?:^|[\s{,])[\"']?(include|extends|env_file)[\"']?\s*:", source_text),
            "此工具不支持外部 env_file/include/extends")
    save(backup / "compose.snapshot", source_text)
    dotenv = compose.parent / ".env"
    if dotenv.exists():
        shutil.copy2(plain(dotenv, False), backup / "dotenv.snapshot")
        os.chmod(backup / "dotenv.snapshot", 0o600)
    save(backup / "container.snapshot.json", current)
    old_tag = "localhost/rustdesk-server-s6:rollback-" + backup.name
    engine.run([engine.name, "tag", current["Image"], old_tag])
    if args.image.startswith("localhost/rustdesk-server-s6:"):
        print("核对已经存在的本地固定镜像；不访问 registry。", flush=True)
    else:
        print("拉取固定镜像；完成后才停服。", flush=True)
        engine.run([engine.name, "pull", fixed_image(args.image)])
    target, previous = engine.image(args.image), engine.image(current["Image"])
    require((target.get("Architecture"), target.get("Os")) == (previous.get("Architecture"), previous.get("Os"))
            and target.get("Os") == "linux", "新镜像平台与原镜像不匹配")
    require(target.get("Config", {}).get("Entrypoint") in (["/init"], "/init"), "目标镜像必须是 S6 镜像")
    new_id = target["Id"]
    if new_id.removeprefix("sha256:") == current["Image"].removeprefix("sha256:"):
        check_runtime(engine, {"fingerprint": fingerprint, "paths": paths,
                              "keys": key_hashes(paths["/data"]), "complete": False}, new_id)
        print("已运行相同镜像且验证通过；不停服、不复制数据。", flush=True)
        return
    new_tag = "localhost/rustdesk-server-s6:upgrade-" + backup.name
    engine.run([engine.name, "tag", new_id, new_tag])
    state = {"engine": args.engine, "project": project, "service": args.service, "deployment": str(compose.parent),
             "backup": str(backup), "paths": paths, "fingerprint": fingerprint, "keys": key_hashes(paths["/data"]),
             "old_tag": old_tag, "old_id": current["Image"], "new_tag": new_tag, "new_id": new_id, "complete": False}
    old_override = override(backup, args.service, old_tag, "old")
    new_override = override(backup, args.service, new_tag, "new")
    engine.compose(state, old_override, ["config"])
    engine.compose(state, new_override, ["config"])
    services = engine.compose(state, old_override, ["config", "--services"], capture=True).splitlines()
    require(services == [args.service], "此工具仅支持原项目中的单个 S6 服务")
    require(engine.container()["Id"] == current["Id"], "预检期间现有容器发生变化")
    engine.assert_no_writers(paths, skip_id=current["Id"])
    stopped = False
    try:
        print("停止单个服务，完整备份数据及 WAL/SHM；不清理原卷。", flush=True)
        stopped = True
        engine.stop()
        engine.assert_no_writers(paths)
        state["files_sha256"] = {}
        for target, name in DATA.items():
            source = Path(paths[target])
            before = tree_hashes(source)
            state["files_sha256"][target] = before
            shutil.copytree(source, backup / name, symlinks=False)
            require(tree_hashes(backup / name) == before == tree_hashes(source), "完整数据副本哈希不一致")
        for mount in current["Mounts"]:
            if mount["Destination"] == "/app/conf":
                source = plain(mount["Source"], True)
                before = tree_hashes(source)
                shutil.copytree(source, backup / "conf.snapshot", symlinks=False)
                require(tree_hashes(backup / "conf.snapshot") == before == tree_hashes(source), "配置副本哈希不一致")
        engine.stopped()
        state["complete"] = True
        for target, filename in (("/data", "db_v2.sqlite3"), ("/app/data", "rustdeskapi.db")):
            compare_database(backup / DATA[target] / filename, Path(paths[target]) / filename, target == "/app/data")
        require(key_hashes(backup / "server_data") == state["keys"], "备份密钥不一致")
        save(backup / "state.json", state)
        engine.compose(state, new_override, ["up", "-d", "--no-deps", "--no-build", "--force-recreate", args.service])
        check_runtime(engine, state, new_id)
    except BaseException:
        if stopped:
            print("升级未通过，尝试切回固定旧镜像并保留当前卷。", flush=True)
            try:
                engine.compose(state, old_override, ["up", "-d", "--no-deps", "--no-build", "--force-recreate", args.service])
                check_runtime(engine, state, state["old_id"])
            except BaseException:
                raise GuardError("自动回退未完全通过；数据保留，需人工检查私有日志与回滚入口") from None
            raise GuardError("升级失败，已回退旧镜像；原卷未恢复或清空，数据异常需人工检查") from None
        raise
    print("升级通过；备份与固定回滚入口：" + str(backup), flush=True)


def rollback(backup, log):
    state = json.loads(plain(backup / "state.json", False).read_text(encoding="utf-8"))
    require(state.get("complete") and Path(state["backup"]) == backup, "备份状态不完整或路径不匹配")
    root = plain(state["deployment"], True)
    engine = Engine(state["engine"], log, root)
    current = engine.container()
    _, paths, fp = deployment(current, state["service"], engine, allow_stopped=True)
    require(paths == state["paths"] and json.dumps(fp, sort_keys=True) == json.dumps(state["fingerprint"], sort_keys=True),
            "当前布局或业务配置已变化，禁止使用过期回滚配置")
    require(engine.image(fixed_image(state["old_tag"]))["Id"].removeprefix("sha256:") == state["old_id"].removeprefix("sha256:"),
            "固定旧镜像缺失或标签已变化")
    old = plain(backup / "old.override.json", False)
    engine.compose(state, old, ["config"])
    engine.stop()
    engine.compose(state, old, ["up", "-d", "--no-deps", "--no-build", "--force-recreate", state["service"]])
    check_runtime(engine, state, state["old_id"])
    print("已退回固定旧镜像；当前数据卷保留。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    up = sub.add_parser("upgrade")
    up.add_argument("image", type=fixed_image)
    up.add_argument("--compose", type=Path)
    up.add_argument("--service", default="rustdesk")
    up.add_argument("--engine", choices=("podman", "docker"), default="podman")
    back = sub.add_parser("rollback")
    back.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    require(sys.platform.startswith("linux"), "仅支持 Linux 本地部署")
    os.umask(0o077)
    if args.action == "upgrade":
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+", args.service)), "service 名称无效")
        compose = plain(args.compose or (Path("docker-compose.yml") if Path("docker-compose.yml").exists() else Path("compose.yaml")), False)
        backups = private_dir(compose.parent / "backups")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = private_dir(backups / stamp)
    else:
        backup = plain(args.backup, True)
        require(backup.stat().st_uid == os.geteuid() and backup.stat().st_mode & 0o077 == 0, "回滚备份必须为当前用户的私有目录")
        backups = private_dir(backup.parent)
    lock = os.open(backups / ".upgrade.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "w") as hold:
        fcntl.flock(hold, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logpath = backup / ("upgrade.log" if args.action == "upgrade" else "rollback-%d.log" % time.time_ns())
        fd = os.open(logpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            try:
                upgrade(args, compose, backup, log) if args.action == "upgrade" else rollback(backup, log)
            except BaseException as error:
                log.write("\nFailure class: %s\n" % type(error).__name__)
                print("操作未完全通过，数据未自动恢复或删除。私有日志：" + str(logpath), file=sys.stderr)
                if isinstance(error, GuardError):
                    print(str(error), file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GuardError, OSError, ValueError):
        print("预检失败；请检查参数、路径、权限和升级锁。", file=sys.stderr)
        sys.exit(1)
