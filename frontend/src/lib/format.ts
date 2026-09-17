/**
 * 数值展示格式化单点。统计口径的计算仍在 services 后端与各 lib 算法模块，
 * 这里只收敛「 ratio → 百分比文本」的展示形式，视图不得再手写 (x*100).toFixed。
 */

/** ratio（0–1）格式化为百分比文本，默认一位小数：`0.836 → "83.6%"`。 */
export function formatPercent(ratio: number, digits = 1): string {
  return `${(ratio * 100).toFixed(digits)}%`
}
