import { errorText } from "./error"

export function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && "code" in error && typeof error.code === "string") {
    const status = "status" in error && typeof error.status === "number" ? error.status : 0
    const retry = status >= 500 ? "，请刷新重试" : ""
    return `${error.message}（错误码 ${error.code}）${retry}`
  }
  return errorText(error, fallback)
}
