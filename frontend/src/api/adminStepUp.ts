import { PASSWORD_AUTH_REQUEST_TIMEOUT_MS } from "./auth"
import { apiRequest } from "./client"

export type AdminOperation =
  | "user_create_admin"
  | "user_role_change"
  | "user_password_reset"
  | "user_status_change"
  | "provider_role_mapping_change"
  | "provider_save_draft"
  | "provider_enable_disable"
export interface AdminIntent {
  operation: AdminOperation
  target_id: string
  parameters: Record<string, unknown>
}
export async function issueAdminStepUp(intent: AdminIntent, password: string): Promise<string> {
  const result = await apiRequest<{ token: string; expires_in: number }>(
    "/admin/step-up",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...intent, password }),
    },
    PASSWORD_AUTH_REQUEST_TIMEOUT_MS,
  )
  return result.token
}
export function adminStepUpHeaders(token?: string): Record<string, string> {
  return token ? { "X-Admin-Step-Up": token } : {}
}
