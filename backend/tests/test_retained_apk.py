from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "deploy/vendor-apk"
sys.path.insert(0, str(ROOT / "scripts"))


def test_retained_packages_match_complete_dual_architecture_manifest() -> None:
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    expected: set[Path] = set()
    components: dict[str, dict[str, set[tuple[str, str]]]] = {}
    checksums: dict[Path, list[str]] = {}
    for record in manifest["packages"]:
        component, architecture = record["component"], record["architecture"]
        assert component in {"web", "postgres", "api"}
        assert architecture in {"x86_64", "aarch64"}
        assert record["origin"] == "dl-cdn.alpinelinux.org/alpine"
        assert record["repository"] == "v3.24/main"
        assert record["license"]
        filename = f"{record['package']}-{record['version']}.apk"
        assert filename == record["filename"]
        path = ASSETS / component / architecture / filename
        assert path not in expected
        expected.add(path)
        assert not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        checksums.setdefault(path.parent, []).append(f"{record['sha256']}  {filename}")
        components.setdefault(component, {}).setdefault(architecture, set()).add(
            (record["package"], record["version"])
        )
    assert expected == set(ASSETS.glob("**/*.apk"))
    assert set(components) == {"web", "postgres", "api"}
    for architectures in components.values():
        assert set(architectures) == {"x86_64", "aarch64"}
        assert architectures["x86_64"] == architectures["aarch64"]
    for directory, lines in checksums.items():
        assert sorted((directory / "SHA256SUMS").read_text().splitlines()) == sorted(lines)


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
def test_web_retains_fixed_brotli_decoder_for_each_architecture(architecture: str) -> None:
    """固定 Nginx 基底的 Brotli 解码器必须由双架构安全修复包覆盖。"""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    versions = [
        record["version"]
        for record in manifest["packages"]
        if record["component"] == "web"
        and record["architecture"] == architecture
        and record["package"] == "brotli-libs"
    ]
    assert versions == ["1.2.1-r0"]


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
@pytest.mark.parametrize("package", ["curl", "libcurl"])
def test_web_retains_fixed_curl_for_each_architecture(architecture: str, package: str) -> None:
    """两架构均覆盖 HTTP/2 upload UAF（CVE-2026-92392）的命令行和共享库。"""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    versions = [
        record["version"]
        for record in manifest["packages"]
        if record["component"] == "web"
        and record["architecture"] == architecture
        and record["package"] == package
    ]
    assert versions == ["8.23.0-r0"]


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
def test_web_retains_fixed_expat_for_each_architecture(architecture: str) -> None:
    """两架构均覆盖 Expat 2.10.0 修复的四项 XML 解析安全缺陷。"""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    versions = [
        record["version"]
        for record in manifest["packages"]
        if record["component"] == "web"
        and record["architecture"] == architecture
        and record["package"] == "libexpat"
    ]
    assert versions == ["2.10.0-r0"]


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
@pytest.mark.parametrize("component", ["api", "postgres"])
def test_krb5_libs_retained_for_images_that_ship_it(architecture: str, component: str) -> None:
    """api 与 postgres 基础层自带的 krb5-libs 1.22.2-r1 有未修复缺陷，需 1.22.3-r0 覆盖。"""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    versions = [
        record["version"]
        for record in manifest["packages"]
        if record["component"] == component
        and record["architecture"] == architecture
        and record["package"] == "krb5-libs"
    ]
    assert versions == ["1.22.3-r0"]


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
@pytest.mark.parametrize("component", ["web", "postgres"])
def test_libxslt_retained_for_images_that_ship_it(architecture: str, component: str) -> None:
    """web 与 postgres 基础层自带的 libxslt 1.1.43-r3 需 1.1.43-r4 覆盖。"""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    versions = [
        record["version"]
        for record in manifest["packages"]
        if record["component"] == component
        and record["architecture"] == architecture
        and record["package"] == "libxslt"
    ]
    assert versions == ["1.1.43-r4"]


@pytest.mark.parametrize(
    "path",
    ["deploy/vendor-apk/web/x86_64/libexpat-2.10.0-r0.apk", "deploy/install-retained-apk.sh"],
)
def test_retained_package_changes_require_release_control(path: str) -> None:
    from classify_ci_changes import classify_paths

    classification = classify_paths([path])
    assert classification.g2
    assert classification.security
    assert classification.release_control
