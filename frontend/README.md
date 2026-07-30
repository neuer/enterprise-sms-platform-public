# Frontend

Vue 3 管理端代码目录。业务 API 统一通过 `src/api`，计费与成功率等口径不得在前端重复计算。

## 青鸾单一前端

Web 镜像只交付一套青鸾 SPA：

- 唯一入口：`/`；
- 源码：`src/`；
- 静态产物：`dist/`。

业务 API 继续使用同源 `/api/`。普通 access/refresh 会话只存当前标签页 `sessionStorage`，高风险短期令牌只存在组件局部易失内存，不得改用 `localStorage` 等持久化存储。

`npm test`、`npm run typecheck`、`npm run build` 分别执行唯一前端的测试、类型检查与生产构建；本地开发使用 `npm run dev`。`/next` 是开发阶段退役入口，由 Nginx 返回 `410 Gone`，不得重新承载第二套 SPA。
