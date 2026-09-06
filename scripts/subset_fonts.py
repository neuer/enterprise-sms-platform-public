# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools", "brotli"]
# ///
"""Noto Serif SC 品牌文案子集化：合并 fontsource 分片并裁剪为静态字重文件。

用法：``uv run scripts/subset_fonts.py``（PEP 723 内联依赖由 uv 自动解析）。

serif 字体只渲染固定品牌文案（登录/改密品牌名、首次改密标题、侧栏"青鸾"），
字符清单见 frontend/src/assets/fonts/serif-subset-glyphs.txt，漂移由
frontend/tests/serif-subset-contract.test.ts 拦截。

fontTools.merge 不支持可变字体（fvar/gvar 没有 mergeMap，会被静默丢弃），
因此按使用点实际字重把各分片实例化为 400/600 静态字体后再合并子集；
产物经 frontend/src/styles/theme.css 顶部的 @font-face 引入，family 名
沿用 "Noto Serif SC Variable"，--serif 令牌与全部使用点保持不变。
"""

from __future__ import annotations

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
# .brand-mark（App.vue）继承正文 400，其余使用点（login-brand-name/mode-title/
# brand strong）均为 600。
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


def main() -> None:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"fontsource 分片目录不存在（先 npm ci）: {SOURCE_DIR}")
    chars = load_manifest()
    plan = plan_sources(chars)
    with tempfile.TemporaryDirectory() as tmp:
        for weight in WEIGHTS:
            output = build_weight(plan, weight, Path(tmp))
            verify(output, chars, weight)
            size = output.stat().st_size
            print(f"{output.relative_to(ROOT)}: {size} bytes (wght {weight})")


if __name__ == "__main__":
    main()
