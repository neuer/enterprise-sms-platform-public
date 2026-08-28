# API 测试台（单 HTML 文件）

`frontend/public/api-test.html` 是一个自包含单文件测试页，用于从浏览器直接调用
平台的应用侧接口（`X-Api-Key`）。它随 Web 构建以 `/api-test.html` 同源托管，
因此浏览器不需要 CORS 就能调用 `/api/v1/messages/*`。

## 为什么不能把文件复制到手机本地打开

平台 API 默认不开启 CORS（安全设计，凭据只走请求头）。从 `file://` 或其他域名
打开本页面时，浏览器会拦截跨域响应。要获得“手机真实出口 IP 被服务端看到”的效果，
请让手机浏览器直接访问服务器上同源托管的地址。

## 在手机上使用（同时验证 IP 白名单）

1. 确认测试服务器 Web 端口（默认 `18080`）可从手机访问：
   `http://<服务器公网IP>:18080/api-test.html`；
2. 在页面填入应用的 API Key（仅保存在页面内存，关闭即消失）；
3. 发送测试短信。服务端 nginx 会把手机出口 IP 写入 `X-Forwarded-For`，
   应用 IP 白名单按该 IP 判定：
   - 白名单包含手机出口 IP → `200`，正常受理；
   - 白名单不含手机出口 IP → `403 IP_NOT_ALLOWED`，且不消耗应用限流与配额；
4. 想验证“拒绝”方向，把应用的 `allowed_ips` 临时改成不包含手机 IP 的网段再发送。

注意：通过 cloudflared Quick Tunnel 访问时，服务端看到的是本机回环地址
（`127.0.0.1`），不是手机 IP；IP 白名单测试请使用服务器直连地址。

## 功能

- 发送：verify / notice / market，直接内容或全局平台模板（平台 ID + 参数，不能填写厂商编号），可选签名、
  `biz_id`（幂等键）与定时时间；
- 受控真实联调 UAT：在 `development-vendor-live` 环境向已登记测试号码发送
  真实短信（仅 notice、单号码、必填 `biz_id`，每日 100 计费条）；
- UAT 模板模式：uat-send 支持已审核的全局平台模板（`template_id` + `template_params`），
  与普通发送同规则（参数个数与 `{1}..{n}` 一致、每参数 ≤ max_len、渲染后 ≤500）；
- 批次：按 `batch_no` 查询、明细、取消、改期、重发失败；
- 最近 8 个批次保存在页面内存，点击即可回查；
- 手机号逐行输入并本地校验 `^1\d{10}$`，不发明文到任何持久层。

注意：普通发送在受控真实联调环境会返回 `VENDOR_TEST_CONSOLE_ONLY`，这是平台
规则 38 的预期行为；如需测试普通发送全流程，请在 Mock 环境（`scripts/local_test.sh`）
使用同一页面。

## 常见问题

- **点“发送”没有任何反应**：先点页面上的“自检连接”。自检期望返回
  `401 UNAUTHORIZED`（不带 Key 属预期），说明同源通路正常；若自检失败，
  请确认是用浏览器访问服务器托管的 `/api-test.html`（如
  `http://<服务器IP>:18080/api-test.html`），而不是把文件复制到手机本地打开——
  平台 API 未开启 CORS，本地文件会被浏览器拦截；
- 页面顶部会显示脚本错误横幅；发送按钮下方有即时状态反馈，失败时自动滚动到结果区；
- 请求 15 秒无响应会提示超时。

## 维护注意（CSP 哈希）

该页面是自包含单文件，平台全局 CSP 默认禁止内联脚本/样式。nginx 为
`/api-test.html` 单独配置了哈希白名单（`deploy/nginx.conf` 中
`location = /api-test.html` 的两个 `sha256-` 值）。修改
`frontend/public/api-test.html` 时必须同步更新这两个哈希，
`backend/tests/test_api_test_playground.py` 会强制二者一致；不要改用
`script-src 'unsafe-inline'` 放宽全局 CSP。

## 安全说明

- API Key 只保存在当前页面 JS 内存变量，不写 `localStorage`、`sessionStorage`、
  IndexedDB 或 Cookie，关闭页面即丢失；
- 页面无任何外部脚本、字体或 CDN 引用；
- 不要在非受控环境粘贴正式厂商环境的 Key；真实联调 UAT 仍走系统配置页控制台。

## 本地开发

```bash
cd frontend
npm run dev        # 访问 http://localhost:5173/api-test.html
npm run build      # 产物 dist/api-test.html 随 Web 镜像发布
```

Vite 开发服务器默认不代理 `/api`，本地联调可在 `vite.config.ts` 配置 server.proxy，
或直接把构建产物放到与 API 同源的静态服务器上。
