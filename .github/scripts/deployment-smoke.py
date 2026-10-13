#!/usr/bin/env python3
"""仅 GitHub Actions 临时副本：实测升级、幂等执行和保卷回滚。

依赖 CI 已有 Python3、Docker Compose 或 Podman/podman-compose，不安装工具。
仅构建两个 FROM + LABEL/ENV 配置层，不重新编译；日志和测试密钥留在 0700
RUNNER_TEMP 子目录，不输出密码、令牌或密钥，不访问生产，不 prune / down -v。
"""
import importlib.util
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path


class SmokeError(Exception):
    pass


def require(ok, category):
    if not ok:
        raise SmokeError(category)


def write(path, content):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def load_upgrade(path):
    spec = importlib.util.spec_from_file_location("rustdesk_upgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_diagnostics(text, allowed_guards):
    """只返回源码中固定的守卫字串、异常类或已知参数名，绝不回显任意日志。"""
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        if line in allowed_guards:
            result.append("guard=" + line)
        elif re.fullmatch(r"Failure class: [A-Za-z][A-Za-z0-9_]{0,63}", line):
            result.append("failure-class=" + line.split(": ", 1)[1])
        elif re.fullmatch(r"数据库原静态字段变化：[A-Za-z_][A-Za-z0-9_]{0,63}\.[A-Za-z_][A-Za-z0-9_]{0,63}", line):
            result.append("guard=" + line)
        else:
            # provider 可能在异常 traceback 中回显完整命令；只输出预知的参数名。
            match = re.search(r"(?:unrecognized arguments|unknown flag|no such option):?\s*(--[a-z-]+)", line, re.I)
            if match and match[1] in {"--env-file", "--services", "--no-deps", "--no-build", "--force-recreate"}:
                result.append("cli-unsupported=" + match[1])
            match = re.match(r"^(FileNotFoundError|PermissionError|KeyError|TypeError|ValueError|JSONDecodeError|TimeoutExpired):", line)
            if match:
                result.append("failure-class=" + match[1])
    return list(dict.fromkeys(result))[-8:]


class Fixture:
    def __init__(self, root, source, engine, image, module):
        self.root, self.source, self.engine, self.image, self.up = root, source, engine, image, module
        self.project = "rustdesk-ci-" + uuid.uuid4().hex[:12]
        self.old = "localhost/rustdesk-server-s6:ci-old-" + self.project
        self.bad = "localhost/rustdesk-server-s6:ci-bad-" + self.project
        self.provider = ["docker", "compose"] if engine == "docker" else ["podman-compose"]
        self.log = (root / "smoke.private.log").open("x", encoding="utf-8")
        os.chmod(root / "smoke.private.log", 0o600)
        self.created = False
        self.token, self.late_token = uuid.uuid4().hex, uuid.uuid4().hex
        self.user = None
        self.keys = None
        parsed = ast.parse((source / "deploy" / "upgrade.py").read_text(encoding="utf-8"))
        # 只允许 require(..., 固定字串) 和 GuardError(固定字串)，排除带路径的 print。
        self.allowed_guards = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                arg = node.args[1] if node.func.id == "require" and len(node.args) > 1 else (
                    node.args[0] if node.func.id == "GuardError" and node.args else None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.allowed_guards.add(arg.value)
        self.env = dict(os.environ)
        # CI 外部业务变量不能覆盖这个独立副本；保留 engine auth 和 provider 路径。
        for name in ("PUB_HOST", "PUBLIC_API_URL", "SERVER_DATA_DIR", "API_DATA_DIR",
                     "RUSTDESK_IMAGE", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
            self.env.pop(name, None)

    def run(self, args, capture=False, ok=True, text=None, timeout=600):
        result = subprocess.run(args, cwd=self.root, env=self.env, text=True, input=text,
                                stdout=subprocess.PIPE if capture else self.log,
                                stderr=self.log, timeout=timeout)
        require(not ok or result.returncode == 0, "subprocess")
        return result.stdout if capture else result.returncode

    def inspect(self, kind, ref):
        return json.loads(self.run([self.engine, kind, "inspect", ref], capture=True))[0]

    def container(self):
        return json.loads(self.run([self.engine, "inspect", "rustdesk-server"], capture=True))[0]

    def compose(self, *args):
        return self.run(self.provider + ["-p", self.project, "--env-file", str(self.root / ".env"),
                                        "-f", str(self.root / "compose.yaml")] + list(args))

    def own(self):
        current = self.container()
        labels = current["Config"].get("Labels") or {}
        project = labels.get("com.docker.compose.project") or labels.get("io.podman.compose.project")
        require(project == self.project, "fixture-ownership")
        return current

    def health(self):
        end = time.monotonic() + 120
        while time.monotonic() < end:
            self.own()
            if self.run([self.engine, "exec", "rustdesk-server", "sh", "-c", self.up.HEALTH],
                        ok=False, timeout=12) == 0:
                return
            time.sleep(2)
        raise SmokeError("fixture-health")

    def stop(self):
        self.own()
        self.run([self.engine, "stop", "--time", "30", "rustdesk-server"], timeout=60)
        state = self.own()["State"]
        require(not state["Running"] and state.get("Pid") == 0, "fixture-pid-not-zero")

    def build(self, tag, bad=False):
        context = self.root / "empty-context"
        if not context.exists():
            context.mkdir(mode=0o700)
        definition = "FROM %s\nLABEL io.rustdesk.ci.fixture=%s\n" % (self.image, self.project)
        if bad:
            definition += "ENV CI_BAD_CONFIG=1\n"
        else:
            definition += "LABEL io.rustdesk.ci.baseline=1\n"
        pull = "--pull=false" if self.engine == "docker" else "--pull=never"
        self.run([self.engine, "build", pull, "--network=none", "--platform=linux/amd64",
                  "-t", tag, "-f", "-", str(context)], text=definition)

    def prepare(self):
        # 不碰已有同名容器；只允许空 CI runner 上的新单服务副本。
        require(self.run([self.engine, "container", "inspect", "rustdesk-server"], ok=False) != 0,
                "preexisting-container")
        require(self.run(self.provider + ["version"], ok=False) == 0, "compose-provider")
        target = self.inspect("image", self.image)
        require(target.get("Os") == "linux" and target.get("Architecture") == "amd64",
                "fixture-platform")
        self.target_id = target["Id"]
        self.build(self.old)
        self.old_id = self.inspect("image", self.old)["Id"]
        require(self.old_id != self.target_id, "baseline-image-id")
        for directory in ("server_data", "api_data"):
            (self.root / directory).mkdir(mode=0o700)
        template = (self.source / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        for port, transport in ((21114, "tcp"), (21115, "tcp"), (21116, "tcp"),
                                (21116, "udp"), (21117, "tcp"), (21118, "tcp"), (21119, "tcp")):
            old = '"%d:%d/%s"' % (port, port, transport)
            require(template.count(old) == 1, "fixture-template-ports")
            template = template.replace(old, '"127.0.0.1:%d:%d/%s"' % (port, port, transport))
        require(template.count("    driver: bridge") == 1, "fixture-template-network")
        template = template.replace("    driver: bridge", "    driver: bridge\n    internal: true")
        write(self.root / "compose.yaml", template)
        write(self.root / ".env", "RUSTDESK_IMAGE=%s\nPUB_HOST=127.0.0.1\n"
              "PUBLIC_API_URL=http://127.0.0.1:21114\nSERVER_DATA_DIR=%s\nAPI_DATA_DIR=%s\n" %
              (self.old, self.root / "server_data", self.root / "api_data"))
        # 失败也只清理已确认属于此项目的资源。
        self.created = True
        self.compose("up", "-d", "--no-build")
        self.health()
        self.stop()
        self.seed()
        self.run([self.engine, "start", "rustdesk-server"])
        self.health()
        self.auth()
        self.keys = self.up.key_hashes(self.root / "server_data")
        print("PASS baseline", flush=True)

    def seed(self, late=False):
        with sqlite3.connect(self.root / "api_data" / "rustdeskapi.db") as db:
            self.user = db.execute("SELECT id,username,is_admin,status FROM users ORDER BY id LIMIT 1").fetchone()
            require(self.user and self.user[2] == 1 and self.user[3] == 1, "fixture-admin")
            stamp = "2020-01-01 00:00:00+00:00"
            db.execute("INSERT INTO user_tokens(user_id,device_uuid,device_id,token,expired_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?)", (self.user[0], "ci-late" if late else "ci-existing", "900001",
                                                self.late_token if late else self.token,
                                                int(time.time()) + 604800, stamp, stamp))
            db.execute("INSERT INTO address_books(id,user_id,alias,password,tags,hash,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", ("ci-late" if late else "900001", self.user[0], "ci-fixture",
                                                "ci-only-password", "[]", "ci-only-hash", stamp, stamp))
            if not late:
                db.execute("INSERT INTO peers(id,uuid,user_id,alias,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           ("900001", "ci-existing", self.user[0], "ci-fixture", stamp, stamp))
        if not late:
            with sqlite3.connect(self.root / "server_data" / "db_v2.sqlite3") as db:
                db.execute("INSERT INTO peer(guid,id,uuid,pk,created_at,info) VALUES(?,?,?,?,?,?)",
                           (uuid.uuid4().bytes, "900001", uuid.uuid4().bytes, bytes(range(32)),
                            "2020-01-01 00:00:00", '{"ip":"127.0.0.1"}'))

    def auth(self, late=False):
        self.own()
        payload = self.run([self.engine, "exec", "rustdesk-server", "wget", "-q", "-T", "8", "-O", "-",
                            "--header=Authorization: Bearer " + (self.late_token if late else self.token),
                            "http://127.0.0.1:21114/api/user/info"], capture=True, timeout=15)
        require(len(payload) <= 65536, "existing-token-response-size")
        body = json.loads(payload)
        require(body.get("name") == self.user[1] and body.get("is_admin") is True,
                "existing-token-account")

    def states(self):
        return sorted((self.root / "backups").glob("*/state.json"))

    def assert_preserved(self, backup, image_id, late=False):
        current = self.own()
        require(current["Image"].removeprefix("sha256:") == image_id.removeprefix("sha256:"), "runtime-image")
        self.health()
        require(self.up.key_hashes(self.root / "server_data") == self.keys, "keys-changed")
        for directory, filename in (("server_data", "db_v2.sqlite3"), ("api_data", "rustdeskapi.db")):
            self.up.compare_database(backup / directory / filename, self.root / directory / filename,
                                     directory == "api_data")
        self.auth()
        if late:
            self.auth(late=True)
            with closing(self.up.database(self.root / "api_data" / "rustdeskapi.db")) as db:
                require(db.execute("SELECT COUNT(*) FROM address_books WHERE id='ci-late'").fetchone() == (1,),
                        "rollback-restored-stale-data")

    def manager(self, *args, success=True):
        self.log.flush()
        offset = self.log.tell()
        logs = lambda: set((self.root / "backups").glob("*/*.log"))
        previous = logs()
        rc = self.run([sys.executable, "-B", str(self.source / "deploy" / "upgrade.py")] + list(args), ok=False)
        if rc != 0:
            self.log.flush()
            paths = [(self.root / "smoke.private.log", offset)] + [(p, 0) for p in sorted(logs() - previous)[-2:]]
            summaries = []
            for path, start in paths:
                with path.open("rb") as private:
                    private.seek(0, os.SEEK_END)
                    private.seek(max(start, private.tell() - 65536))
                    summaries.extend(safe_diagnostics(private.read().decode("utf-8", errors="replace"), self.allowed_guards))
            for summary in list(dict.fromkeys(summaries))[-8:]:
                print("DIAG " + summary, flush=True)
            print("DIAG manager-exit=" + str(rc), flush=True)
        require((rc == 0) == success, "upgrade-command-result")

    def upgrade(self, image, success=True):
        self.manager("upgrade", image, "--compose", str(self.root / "compose.yaml"),
                     "--service", "rustdesk", "--engine", self.engine, success=success)

    def cases(self):
        self.upgrade(self.image)
        states = self.states()
        require(len(states) == 1, "upgrade-backup-state")
        backup = states[0].parent
        self.assert_preserved(backup, self.target_id)
        print("PASS upgrade-preserves-data", flush=True)
        current = self.own()
        stable = (current["Id"], current["State"]["Pid"], current["State"]["StartedAt"])
        self.upgrade(self.image)
        current = self.own()
        require(stable == (current["Id"], current["State"]["Pid"], current["State"]["StartedAt"])
                and self.states() == states, "same-image-restarted-or-backed-up")
        self.assert_preserved(backup, self.target_id)
        print("PASS same-image-no-restart", flush=True)
        self.stop()
        # 升级后的新增数据必须在手动回滚后继续存在；故意保持 stopped 状态调用入口。
        self.seed(late=True)
        self.manager("rollback", "--backup", str(backup))
        self.assert_preserved(backup, self.old_id, late=True)
        print("PASS stopped-rollback-keeps-current-data", flush=True)
        self.build(self.bad, bad=True)
        before = len(self.states())
        self.upgrade(self.bad, success=False)
        require(len(self.states()) == before + 1, "bad-image-did-not-reach-complete-backup")
        state = json.loads(self.states()[-1].read_text(encoding="utf-8"))
        require(state["new_id"].removeprefix("sha256:") == self.inspect("image", self.bad)["Id"].removeprefix("sha256:")
                and state["old_id"].removeprefix("sha256:") == self.old_id.removeprefix("sha256:"),
                "bad-image-fallback-state")
        self.assert_preserved(backup, self.old_id, late=True)
        print("PASS bad-config-auto-fallback-keeps-data", flush=True)

    def cleanup(self):
        if not self.created:
            return
        # 对固定同名容器再核项目所有权；不执行全局清理或删除数据目录。
        if self.run([self.engine, "container", "inspect", "rustdesk-server"], ok=False) == 0:
            self.own()
        self.compose("down")
        refs = {self.old, self.bad}
        for path in self.states() if (self.root / "backups").exists() else []:
            state = json.loads(path.read_text(encoding="utf-8"))
            require(state["project"] == self.project and Path(state["deployment"]) == self.root,
                    "cleanup-state-ownership")
            refs.update((state["old_tag"], state["new_tag"]))
        # 同版本检查只留少量元数据；其 rollback 标签也只从本项目快照推导。
        for path in (self.root / "backups").glob("*/container.snapshot.json") if (self.root / "backups").exists() else []:
            ref = "localhost/rustdesk-server-s6:rollback-" + path.parent.name
            refs.add(ref)
        for ref in sorted(refs):
            require(ref.startswith("localhost/rustdesk-server-s6:"), "cleanup-image-scope")
            self.run([self.engine, "image", "rm", ref], ok=False)


def main():
    require(sys.platform.startswith("linux") and os.environ.get("GITHUB_ACTIONS") == "true", "ci-only")
    engine = os.environ.get("DEPLOY_ENGINE", "docker")
    require(engine in ("docker", "podman"), "engine-selection")
    image = os.environ.get("IMMUTABLE_IMAGE", "")
    require(bool(re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image)), "immutable-image")
    require(not os.environ.get("CONTAINER_HOST") and not os.environ.get("CONTAINER_CONNECTION") and
            (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")), "local-engine-only")
    # Docker bind 中 root:0600 的密钥需要 root 读；仅 CI 重入，Podman 始终保留 rootless 身份。
    if engine == "docker" and os.geteuid() != 0:
        env = dict(os.environ)
        env.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
        os.execvpe("sudo", ["sudo", "-n", "-E", "--", sys.executable, "-B", str(Path(__file__).resolve())], env)
    source = Path(os.environ.get("PROJECT_SOURCE", Path(__file__).resolve().parents[2])).resolve(strict=True)
    require((source / "deploy" / "upgrade.py").is_file() and (source / "deploy" / "compose.yaml").is_file(), "project-source")
    module = load_upgrade(source / "deploy" / "upgrade.py")
    runner = module.plain(os.environ.get("RUNNER_TEMP", ""), True)
    require(runner != Path("/") and "RUNNER_TEMP" in os.environ, "runner-temp")
    os.umask(0o077)
    root = runner / ("rustdesk-deployment-smoke-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    fixture = Fixture(root, source, engine, image, module)
    failure = None
    try:
        module.Engine(engine, fixture.log, root)  # 复用本地 Unix socket/provider 守卫。
        fixture.prepare()
        fixture.cases()
    except BaseException as error:
        failure = error
    finally:
        try:
            fixture.cleanup()
        except BaseException as error:
            failure = failure or error
        fixture.log.close()
    if failure:
        print("FAIL " + (str(failure) if isinstance(failure, SmokeError) else type(failure).__name__), flush=True)
        return 1
    print("PASS deployment-smoke-" + engine, flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print("FAIL " + (str(error) if isinstance(error, SmokeError) else type(error).__name__), flush=True)
        sys.exit(1)
