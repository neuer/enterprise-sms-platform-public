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

- 发送：verify / notice / market，直接内容或模板（ID + 参数），可选签名、
  `biz_id`（幂等键）与定时时间；
- 批次：按 `batch_no` 查询、明细、取消、改期、重发失败；
- 最近 8 个批次保存在页面内存，点击即可回查；
- 手机号逐行输入并本地校验 `^1\d{10}$`，不发明文到任何持久层。

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
