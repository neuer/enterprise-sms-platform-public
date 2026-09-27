# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools", "brotli"]
# ///
"""Noto Serif SC / Noto Sans SC 子集化：只保留界面实际渲染的字符。

用法：``uv run scripts/subset_fonts.py [all|sans|serif]``（PEP 723 内联依赖由 uv
自动解析）。新增或修改界面文案后运行 ``sans``，字符清单与产物随源码重新生成。

Noto Sans SC（正文 --sans）：从 frontend/src、frontend/index.html 与后端
字符串常量（错误文案会原样透出到界面）提取字符，写入
frontend/src/assets/fonts/sans-subset-glyphs.txt；按 fontsource 官方
unicode-range 把字符分配到原分片，逐片裁剪为保留 wght 轴的可变子集，并生成
frontend/src/styles/fonts-sans.css。清单外的字符（用户数据中的生僻字等）
回退到 --sans 后续的系统字体。漂移由 frontend/tests/sans-subset-contract.test.ts 拦截。

serif 字体只渲染固定品牌文案（登录会话名片与侧栏的"青鸾"），
字符清单见 frontend/src/assets/fonts/serif-subset-glyphs.txt，漂移由
frontend/tests/serif-subset-contract.test.ts 拦截。

fontTools.merge 不支持可变字体（fvar/gvar 没有 mergeMap，会被静默丢弃），
因此按使用点实际字重把各分片实例化为 400/600 静态字体后再合并子集；
产物经 frontend/src/styles/theme.css 顶部的 @font-face 引入，family 名
沿用 "Noto Serif SC Variable"，--serif 令牌与全部使用点保持不变。
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path
from typing import Final

from fontTools.merge import Merger
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR: Final = ROOT / "frontend" / "src" / "assets" / "fonts"
GLYPH_MANIFEST: Final = FONT_DIR / "serif-subset-glyphs.txt"
SOURCE_DIR: Final = (
    ROOT / "frontend" / "node_modules" / "@fontsource-variable" / "noto-serif-sc" / "files"
)
# .brand-mark（App.vue）继承正文 400，其余使用点（login-intro-name/brand strong）
# 均为 600。
WEIGHTS: Final = (400, 600)
# fontTools.merge 无法处理含 VarStore 的表；品牌文案全是表意文字，无标点
# 压缩（halt）与基线对齐需求，丢弃 GPOS/BASE 不影响渲染。
DROP_TABLES: Final = ["HVAR", "VVAR", "MVAR", "GPOS", "BASE", "FFTM"]


def load_manifest() -> set[str]:
    """读取字符清单：`#` 开头为注释行，其余行的非空白字符全部计入。"""

    chars: set[str] = set()
    for line in GLYPH_MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        chars.update(c for c in line if not c.isspace())
    if not chars:
        raise SystemExit(f"字符清单为空: {GLYPH_MANIFEST}")
    return chars


def plan_sources(chars: set[str]) -> dict[Path, set[str]]:
    """按 cmap 覆盖把字符分配到 fontsource 分片，不硬编码分片编号。"""

    remaining = set(chars)
    plan: dict[Path, set[str]] = {}
    for path in sorted(SOURCE_DIR.glob("noto-serif-sc-*-wght-normal.woff2")):
        if not remaining:
            break
        with TTFont(path, lazy=True) as font:
            cmap = font.getBestCmap()
        hit = {c for c in remaining if ord(c) in cmap}
        if hit:
            plan[path] = hit
            remaining -= hit
    if remaining:
        raise SystemExit(f"以下字符在任何 fontsource 分片中都不存在: {sorted(remaining)}")
    return plan


def build_weight(plan: dict[Path, set[str]], weight: int, workdir: Path) -> Path:
    """把覆盖清单字符的各分片实例化为指定字重、子集化后合并为单个 woff2。"""

    parts: list[str] = []
    for index, (source, chars) in enumerate(sorted(plan.items())):
        source_font = TTFont(source)
        # instantiateVariableFont 返回新对象，不原地修改
        font = instantiateVariableFont(source_font, {"wght": weight}, updateFontNames=True)
        options = Options(notdef_outline=True, recalc_bounds=True)
        options.drop_tables = [*options.drop_tables, *DROP_TABLES]
        subsetter = Subsetter(options=options)
        subsetter.populate(text="".join(chars))
        subsetter.subset(font)
        part = workdir / f"part-{weight}-{index}.ttf"
        font.save(part)
        parts.append(str(part))
    merged = Merger().merge(parts)
    merged.flavor = "woff2"
    output = FONT_DIR / f"noto-serif-sc-subset-{weight}.woff2"
    merged.save(output)
    return output


def verify(path: Path, chars: set[str], weight: int) -> None:
    """产物必须覆盖清单全部字符，且字重已按目标实例化。"""

    with TTFont(path, lazy=True) as font:
        cmap = font.getBestCmap()
        actual_weight = font["OS/2"].usWeightClass
    missing = sorted(c for c in chars if ord(c) not in cmap)
    if missing:
        raise SystemExit(f"{path.name} 缺少字符: {missing}")
    if actual_weight != weight:
        raise SystemExit(f"{path.name} 字重错误: 期望 {weight}，实际 {actual_weight}")


SANS_PACKAGE: Final = ROOT / "frontend" / "node_modules" / "@fontsource-variable" / "noto-sans-sc"
SANS_MANIFEST: Final = FONT_DIR / "sans-subset-glyphs.txt"
SANS_OUT_DIR: Final = FONT_DIR / "sans"
SANS_CSS: Final = ROOT / "frontend" / "src" / "styles" / "fonts-sans.css"
FRONTEND_TEXT_GLOBS: Final = ("src/**/*.vue", "src/**/*.ts", "src/**/*.css", "index.html")
SANS_MANIFEST_HEADER: Final = """\
# Noto Sans SC 子集字符清单 —— 由 scripts/subset_fonts.py 从源码自动生成，勿手改。
# 来源：frontend/src（vue/ts/css）、frontend/index.html、backend/app 字符串常量与可打印 ASCII。
# 清单外字符回退到 --sans 后续系统字体；frontend/tests/sans-subset-contract.test.ts
# 要求前端源码字符全部在清单内，新增文案后重跑脚本。
"""
FACE_RE: Final = re.compile(
    r"url\(\./files/(noto-sans-sc-([\w-]+?)-wght-normal)\.woff2\).*?unicode-range:\s*([^;]+);",
    re.S,
)


def frontend_chars() -> set[str]:
    """前端源码中出现的全部非空白字符（含注释，宁多勿漏）。"""

    frontend = ROOT / "frontend"
    chars: set[str] = set()
    for pattern in FRONTEND_TEXT_GLOBS:
        for path in frontend.glob(pattern):
            chars.update(c for c in path.read_text(encoding="utf-8") if not c.isspace())
    return chars


def backend_chars() -> set[str]:
    """后端字符串常量中的非 ASCII 字符；docstring 不会透出到界面，跳过。"""

    chars: set[str] = set()
    for path in (ROOT / "backend" / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                chars.update(c for c in node.value if ord(c) > 0x7E and not c.isspace())
    return chars


def write_sans_manifest(chars: set[str]) -> None:
    """按码位排序，每行 64 字写出清单。"""

    ordered = "".join(sorted(chars))
    lines = [ordered[i : i + 64] for i in range(0, len(ordered), 64)]
    SANS_MANIFEST.write_text(SANS_MANIFEST_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def parse_ranges(value: str) -> list[tuple[int, int]]:
    """解析 CSS unicode-range（U+4e00 / U+4e00-4e0f）。"""

    ranges: list[tuple[int, int]] = []
    for part in value.split(","):
        start, _, end = part.strip().removeprefix("U+").removeprefix("u+").partition("-")
        ranges.append((int(start, 16), int(end or start, 16)))
    return ranges


def format_ranges(codepoints: list[int]) -> str:
    """把升序码位压缩为 unicode-range 文本。"""

    parts: list[str] = []
    start = prev = codepoints[0]
    for cp in [*codepoints[1:], -1]:
        if cp == prev + 1:
            prev = cp
            continue
        parts.append(f"U+{start:x}" if start == prev else f"U+{start:x}-{prev:x}")
        start = prev = cp
    return ", ".join(parts)


def build_sans() -> None:
    """按官方分片裁剪 Noto Sans SC 可变字体并生成 @font-face。"""

    index_css = (SANS_PACKAGE / "index.css").read_text(encoding="utf-8")
    faces = [
        (name, slice_id, parse_ranges(rng)) for name, slice_id, rng in FACE_RE.findall(index_css)
    ]
    if not faces:
        raise SystemExit(f"未能解析 fontsource 分片声明: {SANS_PACKAGE / 'index.css'}")
    chars = frontend_chars() | backend_chars() | {chr(c) for c in range(0x20, 0x7F)}
    write_sans_manifest(chars)

    SANS_OUT_DIR.mkdir(exist_ok=True)
    for stale in SANS_OUT_DIR.glob("*.woff2"):
        stale.unlink()
    remaining = {ord(c) for c in chars}
    blocks: list[str] = []
    total = 0
    for name, slice_id, ranges in faces:
        hit = sorted(cp for cp in remaining if any(lo <= cp <= hi for lo, hi in ranges))
        if not hit:
            continue
        remaining -= set(hit)
        # 保留源字体 head.modified，同一清单重跑产物逐字节一致
        font = TTFont(SANS_PACKAGE / "files" / f"{name}.woff2", recalcTimestamp=False)
        cmap = font.getBestCmap()
        present = [cp for cp in hit if cp in cmap]
        if not present:
            continue
        subsetter = Subsetter(options=Options(flavor="woff2"))
        subsetter.populate(unicodes=present)
        subsetter.subset(font)
        font.flavor = "woff2"
        output = SANS_OUT_DIR / f"noto-sans-sc-{slice_id}-subset.woff2"
        font.save(output)
        total += output.stat().st_size
        blocks.append(
            "@font-face {\n"
            '  font-family: "Noto Sans SC Variable";\n'
            "  font-style: normal;\n"
            "  font-display: swap;\n"
            "  font-weight: 100 900;\n"
            f'  src: url("../assets/fonts/sans/{output.name}") format("woff2-variations");\n'
            f"  unicode-range: {format_ranges(present)};\n"
            "}\n"
        )
    SANS_CSS.write_text(
        "/* 由 scripts/subset_fonts.py 生成，勿手改：Noto Sans SC 可变字体按界面字符裁剪的分片，\n"
        "   清单外字符回退到 --sans 后续系统字体。 */\n" + "\n".join(blocks),
        encoding="utf-8",
    )
    uncovered = "".join(sorted(chr(cp) for cp in remaining))
    print(f"{SANS_CSS.relative_to(ROOT)}: {len(blocks)} faces, {total} bytes, {len(chars)} chars")
    if uncovered:
        print(f"Noto Sans SC 不含以下字符，回退系统字体: {uncovered}")


def build_serif() -> None:
    """按 serif 清单生成 400/600 静态子集。"""

    chars = load_manifest()
    plan = plan_sources(chars)
    with tempfile.TemporaryDirectory() as tmp:
        for weight in WEIGHTS:
            output = build_weight(plan, weight, Path(tmp))
            verify(output, chars, weight)
            size = output.stat().st_size
            print(f"{output.relative_to(ROOT)}: {size} bytes (wght {weight})")


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target not in {"all", "sans", "serif"}:
        raise SystemExit("用法: uv run scripts/subset_fonts.py [all|sans|serif]")
    if not SOURCE_DIR.is_dir() or not SANS_PACKAGE.is_dir():
        raise SystemExit("fontsource 字体包不存在（先在 frontend 执行 npm ci）")
    # 合并产物带生成时间戳，重跑即产生二进制差异；只重建清单实际变化的一侧。
    if target in {"all", "serif"}:
        build_serif()
    if target in {"all", "sans"}:
        build_sans()


if __name__ == "__main__":
    main()
