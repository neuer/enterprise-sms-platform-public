"""完整 Python 分母、受控子进程覆盖与分片证据绑定。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SOURCES = ("backend/app", "scripts", "deploy/scripts")
POLICY = {"source": SOURCES, "branch": True, "parallel": True,
          "patch": ["subprocess"], "relative_files": False, "include_namespace_packages": True}


def source_files(root: Path) -> set[str]:
    """三个生产目录全量计入；未导入模块同样属于分母。"""
    return {path.relative_to(root).as_posix()
            for folder in SOURCES for path in (root / folder).rglob("*.py")}


def inputs_hash(root: Path) -> str:
    """绑定源码、测试及门禁配置；不读取运行凭据或生成物。"""
    names = source_files(root) | {
        path.relative_to(root).as_posix()
        for folder in ("backend/tests", "backend/scripts_support")
        for path in (root / folder).rglob("*.py")
    }
    names |= {"backend/pyproject.toml", "backend/uv.lock", "scripts/run_backend_tests.sh",
              "scripts/verify_vendor_postgres_recovery.sh"}
    digest = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode())
    for name in sorted(names):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()


def write_config(path: Path, root: Path) -> None:
    """绝对 source 路径避免子进程切换 cwd 后混淆同名脚本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[run]\nbranch = true\nparallel = true\npatch = subprocess\nrelative_files = false\n"
        + "source =\n" + "".join(f"    {root / name}\n" for name in SOURCES)
        + "[report]\ninclude_namespace_packages = true\n",
    )


def begin(directory: Path, *, root: Path, commit: str, shard: str) -> None:
    """失败重跑不得继续使用旧分片成功回执。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "coverage-manifest.json").write_text(json.dumps({
        "schema": 1, "commit": commit, "shard": shard, "success": False,
        "inputs": inputs_hash(root),
    }) + "\n")


def stamp(directory: Path, *, root: Path, commit: str, shard: str) -> None:
    """测试成功且输入未变化后才封存本次 coverage 数据摘要。"""
    path = directory / "coverage-manifest.json"
    manifest = json.loads(path.read_text())
    tests = json.loads((directory / "tests.json").read_text())
    if (manifest != {"schema": 1, "commit": commit, "shard": shard, "success": False,
                     "inputs": inputs_hash(root)}
            or tests.get("sha") != commit or tests.get("shard") != shard
            or tests.get("exit_code") != 0):
        raise ValueError("coverage shard inputs or test result mismatch")
    manifest.update(success=True, data_sha256=hashlib.sha256(
        (directory / ".coverage").read_bytes()).hexdigest())
    path.write_text(json.dumps(manifest, sort_keys=True) + "\n")


def verify(directory: Path, *, root: Path, commit: str, shard: str) -> None:
    """合并前分别核对数据摘要、分片、源码与 SHA；缺失即失败。"""
    manifest = json.loads((directory / "coverage-manifest.json").read_text())
    expected = {"schema": 1, "commit": commit, "shard": shard, "success": True,
                "inputs": inputs_hash(root), "data_sha256": hashlib.sha256(
                    (directory / ".coverage").read_bytes()).hexdigest()}
    if manifest != expected:
        raise ValueError("coverage shard data or source/SHA mismatch")


def main() -> None:
    """供既有 backend 分片入口调用，不重复执行任何测试。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "begin", "stamp", "verify"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--shard", choices=("unit", "postgres"), default="unit")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.mode == "prepare":
        write_config(args.path, root)
    else:
        {"begin": begin, "stamp": stamp, "verify": verify}[args.mode](
            args.path, root=root, commit=args.commit, shard=args.shard,
        )


if __name__ == "__main__":
    main()
