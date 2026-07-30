# docs/ui-design.md — 青鸾平台 UI 设计规范 v1.6

> 配套 `sms-ui-prototype.html`（关键业务屏静态原型）与 `frontend/tests/qingluan-*-contract.test.ts`（结构机判）。本文是视觉唯一基准，`frontend/src` 是唯一运行实现；冲突时先修正规范与实现，不得恢复第二套前端。

## 1. 设计立场

深色内部安全运营监视台：**信息密度优先、状态可读优先、克制的装饰**。深色不是装饰主题切换，而是青鸾唯一界面基准。全站记忆点是信道监视条（双队列深度 + QPS 令牌），发送页计费分段块与之呼应；系统机制直接成为界面语言。

## 2. 设计令牌（Design Tokens）

### 2.1 色彩

| Token | 值 | 用途 |
|---|---|---|
| --bg | #101814 | 页面底与工作区 |
| --panel / --panel-2 | #18231E / #141D19 | 卡片、表格与浮层 |
| --sink | #0C1512 | 输入框、深层容器与侧栏 |
| --tx / --tx-2 / --tx-3 / --tx-hi | #C9D2CC / #8B978F / #6D7A72 / #EAF1ED | 正文 / 次要 / 弱化 / 高强调 |
| --hair / --hair-2 | rgba(255,255,255,.08) / rgba(255,255,255,.05) | 控件边框 / 细分隔线 |
| **--verdi / --verdi-l** | **#0E7A63 / #2FA184** | **主色**：主按钮、选中态、success、verify 类 |
| --slate | #4574A3 | notice 类 |
| --amber | #D8A35C | market 类、warning |
| --verm | #E46A4F | danger、失败 |

Element Plus 映射以 `frontend/src/styles/theme.css` 为唯一实现：页面、浮层、输入、禁用态、遮罩和文字层级都必须映射到上述深色令牌；禁止组件回退到默认白色背景。根元素固定 `color-scheme: dark`。

**类别三色是硬约定**：verify=verdi、notice=slate、market=amber，出现在类别标签左竖条、统计分段条、图表系列色，任何页面不得混用其他色相表达类别。

### 2.2 字体

| 角色 | 字体栈 | 规则 |
|---|---|---|
| 界面/正文 | PingFang SC, HarmonyOS Sans SC, Source Han Sans SC, Microsoft YaHei, system-ui | 基准 13px/1.6；H1 19px/600；表格 12.5px |
| **数据字** | IBM Plex Mono, JetBrains Mono, Consolas, monospace | 批次号、customId、手机号、金额/条数/百分比、时间戳；一律 `font-variant-numeric: tabular-nums` |
| 品牌字 | Noto Serif SC, Source Han Serif SC, serif | 仅两处：侧栏「青鸾」logo、登录页大字。其余禁用衬线 |

### 2.3 形状与层次

圆角：卡片 10px / 控件 7px / 标签 5px。以 `hair` 细边框和 panel 层级代替大面积阴影；阴影只允许 Drawer 与 Popover。间距采用 4 基数。品牌印章允许 verdi 深浅渐变，普通按钮禁用装饰性渐变与大圆角胶囊。

## 3. 布局

216px 深墨侧栏（分组：概览/发送/治理/管理/运维，角色控制可见项）+ 56px 顶栏 + 内容区 max 1440px。<960px 时侧栏收起为抽屉；390px 必须无页面整体横向滚动。

**顶栏固定结构**：面包屑 ｜ 信道监视条 ｜ 余额 ｜ 用户。信道监视条数据来自 Prometheus 指标接口，10s 轮询；余额点击跳余额走势。

## 4. 核心组件规约

| 组件 | 规约 |
|---|---|
| `<CategoryTag>` | 左侧 3px 竖色条 + 类别名；三色见 2.1 |
| `<StatusTag>` | 状态→色：queued/sending/在途=info 灰；delivered/completed=verdi；failed/rejected=verm；pending_approval/scheduled=amber；balance_blocked/uncertain 使用更高对比的 danger 组合，表示“需要人来” |
| `<PhoneMask>` | mono 显示 `138****2041` + 眼睛图标；点击→调解密接口→行内替换明文并 toast「已记入审计」；无权限则图标隐藏 |
| `<SegmentBar>` | 计费分段可视化：满段实心 verdi 块、末段斜纹块（title 显示 n/67 字）、恒显 1 个灰色 ghost 块提示下一段边界；数据只来自 /billing 预估接口 |
| `<ChannelStrip>` | 顶栏签名组件：实时(verdi)/批量(amber)两条深度条 + 5 枚 QPS 令牌点（占用=verdi 实心） |
| 统计环 | 批次详情 92px donut：verdi 送达 / verm 失败 / 灰 在途，中心 mono 百分比 |
| 表格 | 行高 40、th 11px 字距 0.08em、panel 背景与 hair 分隔；hover 只提高一层亮度；数字列右对齐 mono；行点击开 Drawer（560px），不整页跳转 |
| 空态 | 两行文案：一行结论粗体 + 一行"这里会出现什么/下一步"，不放插画 |

## 5. 文案基调

按钮写结果：「已充值，恢复队列」「通过并排期」「下载剔除清单」。错误讲原因和出路：「营销发送须先确认用户同意」。危险与留痕如实告知：「勾选行为与操作人将写入审计日志」。全站中文、句号收尾的长提示、不用感叹号。

## 6. 动效与可达性

仅三处过渡：Drawer 滑入 220ms、按钮 hover 120ms、switch 150ms；无入场动画、无环形加载堆叠。`prefers-reduced-motion` 全关。焦点样式 2px verdi 外描边全站保留；表格行可键盘 Enter 开 Drawer。

## 7. 页面基准（与原型逐屏对应）

1. **仪表盘**：4 统计卡（今日消息含三色分段条 / 成功率含口径注释 / 待审批 / 余额消耗预测天数）→ 余额 14 日走势（标 10,000 阈值虚线）+ 今日告警 → uncertain / unmatched / 回调 dead 三张处置卡 →（v1.5）任务健康格：每任务一枚状态点（success=verdi、failed/stalled=verm）+ mono 最近运行时间，点击进 ops 任务健康 tab
2. **人工发送**：左表单（类别三选卡片、号码粘贴/导入+四类剔除摘要、模板+签名、定时/测试开关、market 同意勾选琥珀框）＋ 右侧 sticky「发送预检」（受理数、SegmentBar、`n×s` 消耗大数字、配额条、审批提示、按钮文案随场景切换 立即发送/提交审批）
3. **批次列表**：筛选行 + 密表格（类别竖条/进度微条/计费列），行点击 → 详情 Drawer（KV、统计环、失败重发、明细表 PhoneMask）
3a. **号码时间线（v1.5）**：号码搜索页双视图切换；时间线按日分组、垂直细线串联，下行事件左对齐（类别竖条+摘要+状态 tag），用户回复右缩进 24px 前缀"↩"，页顶号码徽标（黑名单状态 amber/verm tag + 近30日接收量 mono）
4. **审批中心**：卡片流（内容引用块 + 计费与排期元信息 + 同意标记）；本人单右侧只显示「本人提交 · 审批回避」灰字
5. **运维中心**：熔断横幅卡（琥珀底 + 「已充值，恢复队列」主按钮）+ 7 tab（uncertain / unmatched / 回调 / 原始报文 / 告警 / 任务健康 / 队列恢复）；uncertain 只允许查看比对记录和升级人工核查，状态仍只能由 reconcile 迁移
6. **登录**：430px 内深色单卡，品牌印章 + Provider 显式选择 + 审计告知，无背景插画
