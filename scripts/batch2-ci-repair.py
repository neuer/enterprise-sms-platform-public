"""Temporary exact-input repair; removed before final CI and merge."""
from pathlib import Path
import subprocess

root = Path.cwd()
expected = {
    "backend/app/core/broker_authorization.py": "19acb525129c66b968e6098168e9d971d9b211b2",
    "backend/app/tasks/__init__.py": "c7d982269464315b121bc3812a21e0bfadaa9e1a",
    "backend/tests/test_sql_service_repositories.py": "74b64789f597426f4e121b1e9f04ce9b0f171cf3",
    "deploy/scripts/release_manager.py": "0d17c3782892867fd7439d479acb965817aa52a8",
    "backend/tests/test_release_manager.py": "5c08bf4e59b787d8cd24b43257792973bd696787",
    "deploy/redis-ha.md": "b9f530480b6108d33cbffacf53f30d1a9c52b785",
}
for path, sha in expected.items():
    assert subprocess.check_output(["git", "hash-object", path], text=True).strip() == sha, path

def replace(path, old, new):
    p = root / path
    text = p.read_text()
    assert text.count(old) == 1, path
    p.write_text(text.replace(old, new))

p = root / "backend/tests/test_sql_service_repositories.py"
s = p.read_text()
a = s.index("async def test_recovery_repository_selects_only_recoverable_work(")
b = s.index("\n\n@pytest.mark.asyncio", a)
t = s[a:b]
t = t.replace("            FakeResult(),\n        ]", "            FakeResult(),\n            FakeResult(rowcount=1),  # re-arm batch.ready\n            FakeResult(rowcount=1),  # re-arm chunk.ready\n        ]")
assert t != s[a:b]
t = t.replace("    assert engine.disposed", '''    assert not connection.results
    rearmed = [
        (sql, params) for sql, params in connection.calls
        if "UPDATE outbox_event SET" in sql
    ]
    assert [params["dedup_key"] for _, params in rearmed] == [
        "batch.ready:batch-1", "chunk.ready:8",
    ]
    assert all("state IN ('completed','dead')" in sql for sql, _ in rearmed)
    assert all("lease_id=NULL" in sql and "lease_expires_at=NULL" in sql for sql, _ in rearmed)
    assert engine.disposed''')
p.write_text(s[:a] + t + s[b:])
replace("backend/app/tasks/__init__.py", '    "app.tasks.security_daily",', '    "app.tasks.security_daily",\n    "app.tasks.worker_probe",')
replace("backend/app/core/broker_authorization.py", '    "app.tasks.poll_report": frozenset({"report"}),', '    "app.tasks.worker_probe": frozenset(WORKER_QUEUES),\n    "app.tasks.poll_report": frozenset({"report"}),')
p = root / "deploy/scripts/release_manager.py"
s = p.read_text().replace('_WORKER_PROBE_SERVICE = "worker-realtime"\n', '')
a = s.index("        ping = self.runner.run(", s.index("    def _verify_final_runtime"))
b = s.index("        for service in _RUNTIME_SERVICES:", a)
s = s[:a] + '''        # 远程控制已关闭；逐职责真实入队/消费，不能用容器 running 代替可消费。
        for service, queue in _WORKER_QUEUES.items():
            nonce = uuid.uuid4().hex
            hostname = f"celery@{worker_hostnames[service]}"
            probe = self.runner.run(
                self._compose() + [
                    "exec", "-T", service, "python", "-m", "app.core.worker_probe",
                    "--queue", queue, "--nonce", nonce, "--hostname", hostname,
                ],
                cwd=self.root,
            )
            if probe.returncode != 0:
                fail("worker_probe_command")
            try:
                if len(probe.stdout) > 4096:
                    fail("worker_probe_output", ambiguous=True)
                reply = json.loads(probe.stdout, object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeError, json.JSONDecodeError, ReleaseManagerError):
                fail("worker_probe_output", ambiguous=True)
            expected = {
                "schema_version": 1, "nonce": nonce, "worker": hostname,
                "queue": queue, "exchange": queue, "routing_key": queue,
                "active_queues": [queue],
            }
            if type(reply) is not dict or reply != expected:
                fail("worker_probe_binding")

''' + s[b:]
s = s.replace("post_ping", "post_probe").replace("post-ping", "post-probe")
p.write_text(s)
p = root / "backend/tests/test_release_manager.py"
s = p.read_text()
a = s.index('            if command[-8:] == [\n                "exec",')
b = s.index("            action = next(", a)
s = s[:a] + '''            if "app.core.worker_probe" in command:
                index = command.index("exec")
                service = command[index + 2]
                assert command[index:index + 6] == [
                    "exec", "-T", service, "python", "-m", "app.core.worker_probe",
                ]
                if self.after_probe is not None:
                    self.after_probe()
                if service == self.missing_worker_probe:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="probe failed")
                queue = self.worker_queue_overrides.get(service, WORKER_QUEUES[service])
                reply = {
                    "schema_version": 1,
                    "nonce": command[command.index("--nonce") + 1],
                    "worker": f"celery@{self.service_hostnames[service]}",
                    "queue": queue, "exchange": queue, "routing_key": queue,
                    "active_queues": [queue],
                }
                if self.probe_reply_mutation is not None:
                    self.probe_reply_mutation(reply)
                return self._result(command, json.dumps(reply) + "\\n")
''' + s[b:]
s = s.replace("missing_worker_ping", "missing_worker_probe").replace("after_ping", "after_probe").replace("worker_ping_membership", "worker_probe_command").replace("worker_active_queues_binding", "worker_probe_binding").replace("post_ping", "post_probe").replace("during_worker_ping", "during_worker_probe")
s = s.replace("        self.after_probe: Any = None", "        self.after_probe: Any = None\n        self.probe_reply_mutation: Any = None")
a = s.index('    assert (\n        compose\n        + [\n            "exec",\n            "-T",\n            "worker-realtime",\n            "celery",')
b = s.index("    events = [", a)
s = s[:a] + '''    probes = [command for command in runner.calls if "app.core.worker_probe" in command]
    assert len(probes) == len(WORKER_QUEUES)
    assert {command[command.index("exec") + 2] for command in probes} == set(WORKER_QUEUES)
    nonces = set()
    for command in probes:
        service = command[command.index("exec") + 2]
        assert command[command.index("--queue") + 1] == WORKER_QUEUES[service]
        assert command[command.index("--hostname") + 1] == (
            f"celery@{runner.service_hostnames[service]}"
        )
        nonce = command[command.index("--nonce") + 1]
        assert len(nonce) == 32 and int(nonce, 16) >= 0
        nonces.add(nonce)
    assert len(nonces) == len(WORKER_QUEUES)
    assert not any("celery" in command and "inspect" in command for command in runner.calls)
''' + s[b:]
a = s.index('    assert (\n        manager._compose()\n        + [\n            "exec",\n            "-T",\n            "worker-realtime",\n            "celery",')
b = s.index("    assert not any(", a)
s = s[:a] + '''    assert len([
        command for command in runner.calls if "app.core.worker_probe" in command
    ]) == len(WORKER_QUEUES)
''' + s[b:]
s += '''\n\n@pytest.mark.parametrize("field", ["nonce", "worker", "active_queues", "queue", "exchange", "routing_key"])
def test_forged_worker_probe_binding_rolls_back(tmp_path: Path, field: str) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.probe_reply_mutation = lambda reply: reply.update({field: "wrong-binding"})
    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])
    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
'''
p.write_text(s)
p = root / "deploy/redis-ha.md"
p.write_text(p.read_text() + '''\n\n### 无远程控制的发布健康验证

职责隔离启用后，发布验收不再使用 `celery inspect ping/active_queues`，也不得为了
通过健康检查而开放 pidbox、远程控制或跨职责 ACL。宿主发布控制器逐一进入既有四个
worker 容器，使用各自凭据向其固定队列发送无业务副作用的 `app.tasks.worker_probe`。
响应必须来自实际消费任务进程，并匹配随机挑战、容器主机名及唯一消费队列、exchange、
routing key；探测前后继续核验容器身份、镜像和健康状态。未消费、超时、错误队列或
响应绑定不符均不能发布成功，仍走原有回滚/人工恢复状态机。

探测不读取或修改业务数据库，不调用厂商，不使用 Celery 结果订阅；响应仅包含固定运行
元数据及随机挑战，使用本职责 `result:<role>:` 命名空间，30 秒过期，并在客户端退出时
清理。它是发布时的消费可用性验证，不替代日常任务心跳和外部监控。
''')
