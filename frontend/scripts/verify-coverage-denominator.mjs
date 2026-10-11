// 复用生产 Vite coverage 配置验证未导入源码；仅创建并删除本进程的临时目录。
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const root = realpathSync(mkdtempSync(join(tmpdir(), "sms-v8-denominator-")))
try {
  mkdirSync(join(root, "src/api"), { recursive: true })
  mkdirSync(join(root, "tests"))
  symlinkSync(join(frontend, "node_modules"), join(root, "node_modules"), "dir")
  writeFileSync(join(root, "package.json"), '{"type":"module"}\n')
  writeFileSync(join(root, "src/api/covered.ts"), "export const value = 1\n")
  writeFileSync(
    join(root, "src/api/unimported.ts"),
    "export function unused(value: number) {\n" +
      Array.from({ length: 40 }, (_, i) => `if (value === ${i}) return ${i + 1}\n`).join("") +
      "return 0\n}\n",
  )
  writeFileSync(
    join(root, "tests/covered.test.ts"),
    'import { value } from "../src/api/covered"\ntest("synthetic", () => expect(value).toBe(1))\n',
  )
  const config = join(root, "vite.config.mts")
  writeFileSync(
    config,
    `import base from ${JSON.stringify(join(frontend, "vite.config.ts"))}\n` +
      `export default {...base, root: ${JSON.stringify(root)}, test: {...base.test, setupFiles: [], ` +
      `coverage: {...base.test.coverage, reportsDirectory: ${JSON.stringify(join(root, "report"))}}}}\n`,
  )
  const result = spawnSync(
    process.execPath,
    [join(frontend, "node_modules/vitest/vitest.mjs"), "run", "--config", config, "--coverage"],
    { cwd: root, encoding: "utf8", timeout: 60000 },
  )
  assert.equal(result.error, undefined, "coverage fixture must execute")
  assert.equal(result.signal, null, "coverage fixture must finish normally")
  assert.notEqual(result.status, 0, "uncovered source must fail the real configured thresholds")
  const summary = JSON.parse(readFileSync(join(root, "report/coverage-summary.json"), "utf8"))
  const unimported = summary[join(root, "src/api/unimported.ts")]
  assert.ok(unimported, "unimported source must appear in the coverage denominator")
  assert.ok(unimported.statements.total > 0)
  assert.equal(unimported.statements.covered, 0)
  assert.ok(unimported.branches.total > 0)
  assert.equal(unimported.branches.covered, 0)
  assert.ok(summary[join(root, "src/api/covered.ts")].statements.covered > 0, result.stdout + result.stderr)
  console.log("coverage denominator fixture: unimported source counted and gate rejected")
} finally {
  rmSync(root, { recursive: true, force: true })
}
