# Frontend

Vue 3 管理端代码目录。业务 API 统一通过 `src/api`，计费与成功率等口径不得在前端重复计算。

## 两套前端并存

同一个 Web 镜像同时交付两套彼此独立构建的 SPA：

- 经典版：`/`，源码位于 `legacy/`；
- 青鸾版：`/next/`，源码位于 `src/`。

默认入口保持经典版，登录后的顶栏可在同一业务路由间切换版本。两套应用共用同源 `/api/` 和浏览器 `sessionStorage` 会话键，不共享组件、样式或运行时 bundle，也不得改用 `localStorage` 等持久化存储。

根目录只维护一份依赖锁文件。`npm test`、`npm run typecheck`、`npm run build` 会依次覆盖两套应用；产物分别写入 `dist/classic/` 和 `dist/next/`。本地开发使用 `npm run dev` 启动青鸾版，使用 `npm run dev:legacy` 启动经典版。
