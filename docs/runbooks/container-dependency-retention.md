# 固定镜像依赖与扫描证据

## 依赖策略

运行镜像采用固定多架构基础 digest；已由基础层提供的修复不再在线重复升级。
额外必需 APK 按 [保留目录](../../deploy/vendor-apk/README.md) 保存原始签名字节、
双架构 SHA256 清单与来源记录。构建步骤断网，基础镜像原有信任库验证签名。
这结合了受控基础 digest 更新与依赖制品保留；不能以版本下限或漂移 tag 代替。

Git 中的已发布依赖提交及离线发布镜像不得早于对应版本的支持期、回退保留期结束
而清理。回退前按既有离线索引核对归档哈希、image ID、schema 兼容性与镜像可取得性，
不临时从当前 Alpine 仓库重新解析旧依赖。不能取得已验证的回退镜像时失败关闭。
该流程不授权回退数据库 schema，也不改变生产审批或共享测试更新入口。

## CI 与发布证据

G2 在干净源码上给四镜像写入候选 SHA；开发中脏工作树只能标记 development。
G2 完成后 `scripts/verify_image_security.py --scope g2 --output <新绝对目录>`
保存并扫描实际四个 image ID。扫描器固定版本/digest，检查 HIGH/CRITICAL、报告中的
ImageID 及漏洞库更新时间（必须在过去 24 小时内）。任何缺失、身份不匹配、命中或
工具错误均关闭门禁。每日 CI 使用无缓存、pull 的冷构建路径，普通变更可复用依赖缓存。

输出 `image-security.json` 含候选 SHA、实际 ID、平台、扫描器、漏洞库时间、扫描
聚合结果及原始报告/SBOM 的 SHA256；每个镜像另有规范化 CycloneDX SBOM。
原始扫描报告只存在于私有临时目录，公共 CI 制品仅上传白名单聚合结果与 SBOM。
源码检查在扫描前后执行，不能把脏工作树或其他提交的镜像标记为候选通过。

mailer 使用 `--scope mailer` 独立冷构建与扫描，并在无网络、无配置挂载的容器内验证
UID 10001 和 TLS CA。该过程不发送邮件、不读取 Resend 配置。mailer 证据独立上传；
主 release manifest、离线镜像索引及回退集合仍严格为 api/web/postgres/redis。
mailer 同时保留已扫描的 `mailer.tar` 及归档哈希，供独立交付使用。部署者必须先核对
候选 SHA、报告 passed、归档哈希及加载后的 image ID，再将 `SMS_SECURITY_REPORT_IMAGE`
设为报告中的精确 `sha256:...`，使用 Compose `up --no-build`。本地开发的默认 `:local`
构建不继承这些证据。不得扫描一个 mailer 后重新 build 并宣称新产物通过；此流程
仅产生候选制品，不代表已部署或已完成邮件投递验收。

正式发布继续执行 `scripts/verify_release.sh` 的同一次构建/归档/扫描/SBOM 合同，
再执行 `scripts/verify_reproducible_build.sh`。G2 的扫描不能替代正式发布证据；若重新
构建发布镜像，必须对新产物重新执行发布门禁，不能沿用 G2 临时标签的扫描结果。
可重复性校验与依赖可获得性分别记录，允许的元数据差异以既有规范化器为准。

漏洞扫描命中、发行版修复状态和运行时可利用性分别记录。没有找到利用链不允许
忽略 HIGH/CRITICAL。升级必须经 PR 和以上检查；不得添加宽泛 ignore、关闭签名或
降低严重性门槛。基础镜像或包更新还需保持非 root、权限、PostgreSQL scratch/gosu
替换、Redis ACL/secret 入口及浏览器安全头，并完成相同 SHA 的 G2。
