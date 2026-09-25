// gen:api-types 后处理：把 openapi.yaml 的内容哈希钉进生成文件首行，
// 供 scripts/check_spec_consistency.py 在无 node 环境下做本地零漂移比对；
// CI 的 gen + git diff 门禁不受影响（两侧产物都带同一戳）。
import { createHash } from "node:crypto"
import { readFileSync, writeFileSync } from "node:fs"

const specPath = new URL("../../openapi.yaml", import.meta.url)
const targetPath = new URL("../src/api/types.gen.ts", import.meta.url)
const digest = createHash("sha256").update(readFileSync(specPath)).digest("hex")
const stamp = `/* openapi.yaml sha256:${digest} */`

const content = readFileSync(targetPath, "utf8").replace(/^\/\* openapi\.yaml sha256:[0-9a-f]{64} \*\/\n?/, "")
writeFileSync(targetPath, `${stamp}\n${content}`)
console.log(`stamped types.gen.ts with openapi.yaml sha256:${digest.slice(0, 12)}…`)
