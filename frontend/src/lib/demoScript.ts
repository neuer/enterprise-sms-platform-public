import type { ManagedApp } from "../api/apps"
import type { VarSpec } from "../api/templates"

export const DEMO_LANGUAGES = ["curl", "python", "node", "java", "go", "php"] as const

export type DemoLanguage = (typeof DEMO_LANGUAGES)[number]

export const DEMO_LABELS: Record<DemoLanguage, string> = {
  curl: "cURL",
  python: "Python",
  node: "Node.js",
  java: "Java",
  go: "Go",
  php: "PHP",
}

export interface DemoContext {
  app: ManagedApp
  templateId: number
  templateName: string
  templateContent: string
  params: string[]
}

export const DEMO_BIZ_ID = "ORDER-20260804-001"

export const DEMO_MOBILES = ["138****8000"]

/** 按 var_specs 生成不超过 max_len 的示例参数值。 */
export function exampleParamsFor(specs: VarSpec[]): string[] {
  const sorted = [...specs].sort((a, b) => a.pos - b.pos)
  return sorted.map((spec) => {
    for (const candidate of [`示例参数${spec.pos}`, `参数${spec.pos}`, "示例", "值"]) {
      if (candidate.length <= spec.max_len) return candidate
    }
    return "a".repeat(spec.max_len)
  })
}

/** 生成目标语言可用的模板参数数组字面量。 */
export function paramsLiteral(language: DemoLanguage, params: string[]): string {
  const quoted = params.map((value) => `"${value}"`)
  if (language === "java") return `new String[]{${quoted.join(", ")}}`
  if (language === "go") return `[]string{${quoted.join(", ")}}`
  if (language === "php") return `[${params.map((value) => `'${value}'`).join(", ")}]`
  return `[${quoted.join(", ")}]`
}

/** 生成与语言无关的请求 JSON（cURL 与 Java 文本块直接内嵌）。 */
export function payloadJson(context: DemoContext): string {
  return JSON.stringify({
    category: "notice",
    mobiles: DEMO_MOBILES,
    template_id: context.templateId,
    template_params: context.params,
    biz_id: DEMO_BIZ_ID,
  })
}

/** 生成指定语言的模板发送 demo；API Key 一律使用环境变量占位，不嵌入明文。 */
export function buildDemoScript(language: DemoLanguage, context: DemoContext): string {
  const base = "https://sms.example.com/api/v1"
  const head = `// 应用：${context.app.name}（id=${context.app.id}）· 模板：${context.templateName}（id=${context.templateId}）`
  const warning = "// 正式接入必须使用已审核模板，直接内容会进入服务商人工审核"
  const baseHint = "// 请把 base 替换为平台地址（测试环境 http://<服务器IP>:18080/api/v1）"
  const contentLine = context.templateContent ? `// 模板内容：${context.templateContent}` : ""
  const params = paramsLiteral(language, context.params)
  if (language === "curl") {
    return [
      `# 应用：${context.app.name}（id=${context.app.id}）· 模板：${context.templateName}（id=${context.templateId}）`,
      contentLine,
      `# 正式接入必须使用已审核模板，直接内容会进入服务商人工审核`,
      `# 请把 base 替换为平台地址（测试环境 http://<服务器IP>:18080/api/v1）`,
      `curl -X POST '${base}/messages/send' \\`,
      `  -H "X-Api-Key: $SMS_API_KEY" \\`,
      `  -H 'Content-Type: application/json' \\`,
      `  -d '${payloadJson(context)}'`,
    ]
      .filter(Boolean)
      .join("\n")
  }
  const common: string[] = [head, warning, baseHint]
  if (contentLine) common.splice(1, 0, contentLine)
  if (language === "python") {
    return [
      ...common,
      `import os`,
      `import requests`,
      ``,
      `URL = "${base}/messages/send"`,
      ``,
      `def send_template(mobiles, template_id, template_params, biz_id):`,
      `    resp = requests.post(`,
      `        URL,`,
      `        json={`,
      `            "category": "notice",`,
      `            "mobiles": mobiles,`,
      `            "template_id": template_id,`,
      `            "template_params": template_params,`,
      `            "biz_id": biz_id,`,
      `        },`,
      `        headers={"X-Api-Key": os.environ["SMS_API_KEY"]},`,
      `        timeout=15,`,
      `    )`,
      `    resp.raise_for_status()`,
      `    return resp.json()`,
      ``,
      `print(send_template(${JSON.stringify(DEMO_MOBILES)}, ${context.templateId}, ${params}, "${DEMO_BIZ_ID}"))`,
    ].join("\n")
  }
  if (language === "node") {
    return [
      ...common,
      `const SMS_API_KEY = process.env.SMS_API_KEY;`,
      `const TEMPLATE_ID = ${context.templateId};`,
      `const URL = "${base}/messages/send";`,
      ``,
      `const response = await fetch(URL, {`,
      `  method: "POST",`,
      `  headers: {`,
      `    "X-Api-Key": SMS_API_KEY,`,
      `    "Content-Type": "application/json",`,
      `  },`,
      `  body: JSON.stringify({`,
      `    category: "notice",`,
      `    mobiles: ${JSON.stringify(DEMO_MOBILES)},`,
      `    template_id: TEMPLATE_ID,`,
      `    template_params: ${params},`,
      `    biz_id: "${DEMO_BIZ_ID}",`,
      `  }),`,
      `});`,
      `const data = await response.json();`,
      `console.log(data.batch_no, data.status, data.quota_cost);`,
    ].join("\n")
  }
  if (language === "java") {
    return [
      ...common,
      `import java.net.URI;`,
      `import java.net.http.HttpClient;`,
      `import java.net.http.HttpRequest;`,
      `import java.net.http.HttpResponse;`,
      ``,
      `public class SmsDemo {`,
      `    public static void main(String[] args) throws Exception {`,
      `        String body = """`,
      `            ${payloadJson(context)}`,
      `            """;`,
      `        HttpRequest request = HttpRequest.newBuilder()`,
      `            .uri(URI.create("${base}/messages/send"))`,
      `            .header("X-Api-Key", System.getenv("SMS_API_KEY"))`,
      `            .header("Content-Type", "application/json")`,
      `            .POST(HttpRequest.BodyPublishers.ofString(body))`,
      `            .build();`,
      `        HttpResponse<String> response = HttpClient.newHttpClient()`,
      `            .send(request, HttpResponse.BodyHandlers.ofString());`,
      `        System.out.println(response.statusCode());`,
      `        System.out.println(response.body());`,
      `    }`,
      `}`,
    ].join("\n")
  }
  if (language === "go") {
    return [
      ...common,
      `package main`,
      ``,
      `import (`,
      `    "bytes"`,
      `    "encoding/json"`,
      `    "fmt"`,
      `    "io"`,
      `    "net/http"`,
      `    "os"`,
      `)`,
      ``,
      `func main() {`,
      `    payload, _ := json.Marshal(map[string]interface{}{`,
      `        "category":        "notice",`,
      `        "mobiles":         []string{"138****8000"},`,
      `        "template_id":     ${context.templateId},`,
      `        "template_params": ${params},`,
      `        "biz_id":          "${DEMO_BIZ_ID}",`,
      `    })`,
      `    req, _ := http.NewRequest("POST", "${base}/messages/send", bytes.NewReader(payload))`,
      `    req.Header.Set("X-Api-Key", os.Getenv("SMS_API_KEY"))`,
      `    req.Header.Set("Content-Type", "application/json")`,
      `    resp, _ := http.DefaultClient.Do(req)`,
      `    defer resp.Body.Close()`,
      `    body, _ := io.ReadAll(resp.Body)`,
      `    fmt.Println(resp.StatusCode, string(body))`,
      `}`,
    ].join("\n")
  }
  return [
    ...common,
    `<?php`,
    `$url = '${base}/messages/send';`,
    `$payload = json_encode([`,
    `    'category' => 'notice',`,
    `    'mobiles' => ['138****8000'],`,
    `    'template_id' => ${context.templateId},`,
    `    'template_params' => ${params},`,
    `    'biz_id' => '${DEMO_BIZ_ID}',`,
    `]);`,
    `$ch = curl_init($url);`,
    `curl_setopt_array($ch, [`,
    `    CURLOPT_POST => true,`,
    `    CURLOPT_POSTFIELDS => $payload,`,
    `    CURLOPT_HTTPHEADER => [`,
    `        'X-Api-Key: ' . getenv('SMS_API_KEY'),`,
    `        'Content-Type: application/json',`,
    `    ],`,
    `    CURLOPT_RETURNTRANSFER => true,`,
    `    CURLOPT_TIMEOUT => 15,`,
    `]);`,
    `$response = curl_exec($ch);`,
    `$status = curl_getinfo($ch, CURLINFO_HTTP_CODE);`,
    `curl_close($ch);`,
    `echo $status, "\\n", $response, "\\n";`,
  ].join("\n")
}
