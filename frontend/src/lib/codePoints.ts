/** 与 Python len 对齐的 Unicode 码点长度；超限时在 max+1 停止，避免超大粘贴构造完整数组。 */

export const CONTENT_CODE_POINT_MAX = 500

export function codePointLength(value: string, limit = Number.POSITIVE_INFINITY): number {
  let count = 0
  for (const _character of value) {
    count += 1
    if (count > limit) return count
  }
  return count
}

export function exceedsCodePoints(value: string, maxCodePoints: number): boolean {
  return codePointLength(value, maxCodePoints) > maxCodePoints
}
