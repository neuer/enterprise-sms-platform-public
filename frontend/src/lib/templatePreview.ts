export const PLACEHOLDER_TOKEN = /\{[^{}]*\}/g

export const PLACEHOLDER_POSITION = /^\{([1-9]\d*)\}$/

export interface ContentPart {
  text: string
  pos?: number
  maxLen?: number
}

/** 提取内容中的 {n} 占位位置；非法 {} 片段单独计数用于内联提示。 */
export function contentPlaceholders(content: string): { positions: number[]; invalidTokens: number } {
  const tokens = content.match(PLACEHOLDER_TOKEN) ?? []
  const positions: number[] = []
  let invalidTokens = 0
  for (const token of tokens) {
    const matched = PLACEHOLDER_POSITION.exec(token)
    if (matched) positions.push(Number(matched[1]))
    else invalidTokens += 1
  }
  return { positions, invalidTokens }
}

/** 把平台内容拆成正文片段与变量片，供列表/详情内联渲染；非法 {} 片段保持原文。 */
export function contentParts(content: string, specs: { pos: number; max_len: number }[]): ContentPart[] {
  const maxLenByPos = new Map(specs.map((spec) => [spec.pos, spec.max_len]))
  const parts: ContentPart[] = []
  let cursor = 0
  for (const match of content.matchAll(PLACEHOLDER_TOKEN)) {
    const matched = PLACEHOLDER_POSITION.exec(match[0])
    if (!matched) continue
    const index = match.index ?? 0
    if (index > cursor) parts.push({ text: content.slice(cursor, index) })
    const pos = Number(matched[1])
    parts.push({ text: match[0], pos, maxLen: maxLenByPos.get(pos) })
    cursor = index + match[0].length
  }
  if (cursor < content.length) parts.push({ text: content.slice(cursor) })
  return parts
}

/** 厂商格式预览：与服务端 to_vendor_template 同一规则，平台 {n} 按声明最大长度转 {s<max_len>}。 */
export function vendorPreviewOf(content: string, specs: { pos: number; max_len: number }[]): string {
  const maxLenByPos = new Map(specs.map((spec) => [spec.pos, spec.max_len]))
  return content.replace(PLACEHOLDER_TOKEN, (token) => {
    const matched = PLACEHOLDER_POSITION.exec(token)
    if (!matched) return token
    const maxLen = maxLenByPos.get(Number(matched[1]))
    return maxLen === undefined ? token : `{s${maxLen}}`
  })
}

/** 发送前展示预览；空参数保留占位符，服务端 template.py 仍执行最终渲染与长度校验。 */
export function renderPreview(content: string, params: string[]): string {
  return splitPreviewParts(content, params)
    .map((part) => part.text)
    .join("")
}

/** 展示已填参数的高亮片段，保留未填与非法占位的原文。 */
export function splitPreviewParts(content: string, params: string[]): { text: string; highlight: boolean }[] {
  const parts: { text: string; highlight: boolean }[] = []
  let cursor = 0
  for (const match of content.matchAll(/\{(\d+)\}/g)) {
    const index = match.index ?? 0
    if (index > cursor) parts.push({ text: content.slice(cursor, index), highlight: false })
    const value = params[Number(match[1]) - 1]?.trim()
    parts.push({ text: value || match[0], highlight: Boolean(value) })
    cursor = index + match[0].length
  }
  if (cursor < content.length) parts.push({ text: content.slice(cursor), highlight: false })
  return parts
}
