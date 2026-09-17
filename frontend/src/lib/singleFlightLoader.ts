/** 合并同一资源的并发加载；失败后释放缓存，成功仅初始化一次。 */
export function singleFlightLoader<T>(load: () => Promise<T>): () => Promise<T> {
  let pending: Promise<T> | null = null
  return () => {
    if (pending) return pending
    const current = Promise.resolve()
      .then(load)
      .catch((error: unknown) => {
        if (pending === current) pending = null
        throw error
      })
    pending = current
    return current
  }
}
