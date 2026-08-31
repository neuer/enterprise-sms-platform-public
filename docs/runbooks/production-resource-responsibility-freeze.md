# 生产资源与责任冻结及宿主初始化手册

> **V1 目标架构替代说明（优先于本文后续旧离线图和文字）：**
> V1 目标正常发布路径为 GitHub Release Gate 经受控桥接机向 Ops VM 的内部 Git
> 镜像、私有 Registry 和发布证据库双通道推进，生产联合绑定精确 Git
> commit、root-owned `current -> versions/<commit>` 完整 tracked-tree snapshot 与四个
> `image@sha256:RepoDigest`。Ops VM 同时承载监控、脱敏日志与加密备份；临时离线通道
> 仅保留兼容，不扩展、不在 Registry 故障时自动回退。本文第 2 节及其他把
> Registry 写为“未来”、把离线包写为正常首发权威的内容仅作历史兼容记录。
> 生产采用前必须单独批准 OS 变更，预建
> `/etc/sms-platform/platform.env` (`root:root 0600`)、
> `/etc/sms-platform/secrets` (`root:root 0700`，规范子项 `0600`)、
> `/etc/sms-platform/production-control-approved/<commit>`（`root:root 0444`，精确 41 字节
> `<commit>\n`）与 `/var/lib/sms-platform/security-report`的审核后所有权/模式；
> manager/snapshot/`current` 先就绪，stable launcher 最后原子替换，重跑幂等。
> 本补充不改变本文 `draft / external-verification-required` 状态。详见
> [P0-A 生产控制加固](../security-hardening/p0a-production-control/hardening.md)。

## 1. 文档状态、适用范围与完成定义

**当前状态：`draft / external-verification-required`。** 本仓库已经给出申请规格、责任边界、
失败关闭预检和初始化顺序，但尚未取得任何真实 VMware、网络、TLS、制品、Git、PKI、备份或
人员读回，因此不得把本文件的存在表述为“资源冻结已完成”，更不得据此开始生产安装。

本文把 Phase 0 已确认的单生产 VM 方案转换为可交给 VMware、网络、安全、制品、DBA、监控和
业务团队执行的资源申请、责任矩阵与初始化顺序。它不包含任何真实 IP、域名、账号、证书、
secret 或手机号；这些值只允许写入公司的受限资产系统、密钥系统和生产变更单，不得提交 Git。

本文新增并建议批准的宿主操作系统决策是 **Ubuntu Server 24.04.4 LTS amd64 最小化安装**。
Phase 0 之前只冻结了 `linux/amd64`、宿主 `/usr/bin/python3` 精确为 3.12 和必要宿主工具，
没有冻结 Linux 发行版；因此只有审批、置备和读回都完成后，才能把本决策标记为 `verified`。

“资源与责任冻结完成”必须同时满足：

1. 本文第 11 节每一项都有唯一负责人、完成期限、内部证据编号和状态；
2. `PLAT` 与 `REV` 是两名不同的自然人，且使用两个独立受控身份；
3. 生产、预生产、一次性隔离恢复配额、受控离线制品生成/签名/传输资源和内网只读 Git 镜像均已分配；
4. OS、CPU、内存、五块 VMDK、网络、TLS、固定出口、Redis PKI、25 件 secret、备份材料和
   双渠道告警的真实读回均通过；
5. 所有 `No-Go` 均为 `closed`，或者明确属于 Phase 0 已接受但不阻断首发的残余风险。

文档、脚本或工单中的 `planned`/`requested` 不等于资源存在；只有目标系统的只读读回加独立
复核才是 `verified`。

## 2. 冻结的目标架构

```text
GitHub Actions / 受控构建环境
       │ 候选 commit、四镜像 image ID、SBOM、Trivy、attestation
       ▼
受控联网签名节点 ──签名封闭包──▶ 应用预生产 ──同一包──▶ 生产单 VM
       │
       ├──▶ 内网只读 Git ────────────────────────────────┘
       └──▶ 未来内部 Registry（建成并演练后退出离线通道）
                                      ├─ Web/API/worker/beat/outbox
VPN ─▶ 企业 TLS 终结器 ────────────────┤
                                      ├─ PostgreSQL 16
                                      └─ Redis 7 broker/auth/control
                                             三实例、同 VM、非 HA

生产单 VM ──固定 SNAT IPv4──▶ 新短信服务商
旧短信系统 ──独立旧服务商，长期并行并可受控切回
```

生产不得直连 GitHub、在现场构建镜像、人工 `docker load` 或以裸上传代替受控 driver。内部
Registry 尚未建设期间，只允许签名的生产离线 Docker image archive 发布包（镜像 OCI-compatible，不是 OCI Image Layout）；内网 Git 仍是硬要求。生产、PostgreSQL 和
三 Redis 位于同一 VM 是已接受的共同故障域，只能标记为 `isolated-standalone`，不得描述为 HA。

当前容量规划输入为：2022/2023/2024/2025 年月均约 14/23/35/50 万条，以单号码请求为主，
少量群发不超过 20 个号码，业务应用不超过 20 个；按 30 天计算，50 万条/月只对应约
1.67 万条/日和 0.2 条/秒的长期平均值。厂商当前只提供账户每秒最多 200 次调用、单次 Send
最多 100000 个号码的基础口径，统计窗口、最小间隔、并发、峰值和回调量仍未知，因此这些
历史均值不能作为容量验收。RF-13 必须取得厂商书面 QPS/单次上限，RF-20 必须让首批应用提供
峰值与12小时积压规模；预生产容量冒烟据此生成并归档，不能用 VM 规格或平均值推断已通过。

## 3. 操作系统与宿主软件基线

### 3.1 单一推荐

| 项目 | 冻结值 | 验收方式 |
|---|---|---|
| 发行版 | Ubuntu Server 24.04.4 LTS，Noble，最小化、无 GUI | `/etc/os-release` 的 `ID=ubuntu`、`VERSION_ID=24.04`；记录安装介质点版本 |
| 架构 | amd64 / x86_64 | `uname -m` 必须为 `x86_64` |
| 内核轨道 | Ubuntu GA `linux-generic` 6.8，持续安装同轨安全补丁；首发不用 HWE/edge | 记录 `uname -r` 和 `linux-image` 包；轨道变化须重新做 VMware/存储/恢复演练 |
| Python | `/usr/bin/python3` 精确 Python 3.12 | `host_python_preflight.py lifecycle` 和统一宿主预检均退出 0 |
| init | systemd 255 系列 | unit 校验及 timer 由 systemd 承载；精确补丁版本进入包锁 |
| VMware 工具 | Ubuntu 仓库的 `open-vm-tools`，不装 GUI tools | `systemctl is-active open-vm-tools` 与包版本读回 |
| 时区与时钟 | 宿主与应用均使用 `Asia/Shanghai`；只使用公司 NTP | `timedatectl` 显示同步且时区为 `Asia/Shanghai` |
| 支持期 | 按较保守口径，标准安全维护至 2029-05 | 2028 年前完成升级项目立项，或采购 Ubuntu Pro |

选择理由与版本依据：Ubuntu 24.04 默认 Python 3.12，Docker 官方支持 Ubuntu 24.04 amd64，且
Ubuntu 原生提供 systemd、open-vm-tools、XFS/ext4 工具。ISO/生命周期、Python 版本和 Docker
支持矩阵必须从 Canonical 与 Docker 官方页面读回，并把查询日期和受限证据编号写入变更单；
本文不把外部网页可达性作为生产运行依赖。

不得把 `/usr/bin/python3` 改成自建 symlink，也不得以项目 venv 替代宿主 3.12。不要使用
`get.docker.com` convenience script；Docker Engine/Compose 从公司批准的签名内部 APT 镜像安装，
精确包版本在预生产通过后冻结，生产安装同一版本，禁止脚本永久安装 `latest`。

仓库现有 timer 的 `OnCalendar` 都按宿主本地时区解释，安全日报采集器也按上海自然日聚合；因此
宿主必须冻结为 `Asia/Shanghai`，不能改成 UTC 后仍沿用现有 timer。置备后要对备份、分区维护、
恢复演练和安全日报四个 calendar 分别执行 `systemd-analyze calendar`，把下一次触发时间读回为
上海时间；时区变化属于需重新验收 timer 和日报日期归属的宿主变更。

### 3.2 必装包与版本冻结

| 类别 | 包/能力 | 冻结要求 |
|---|---|---|
| 基础 | `ca-certificates curl gnupg jq util-linux tzdata sudo` | 来自公司批准的 Ubuntu 24.04 镜像；`tzdata` 必须含 `Asia/Shanghai` |
| 存储 | `xfsprogs e2fsprogs` | 与实际文件系统匹配；两者均安装用于诊断/恢复；置备 UUID 使用固定 Python 3.12，不额外依赖 `uuid-runtime` |
| 时间 | systemd 自带 `systemd-timesyncd` | 只允许公司批准的内部 NTP，`FallbackNTP=` 置空；实际地址只进受限变更证据，不提交公开仓库；不得同时运行多个时间客户端 |
| VMware | `open-vm-tools` | 发行版包，不安装第三方 ISO tools |
| 远程运维 | `openssh-server openssh-client rsync` | 本阶段保留现状：禁止 root 直接 SSH，普通运维账号可通过管理 VPN/堡垒机使用密码或密钥，再按公司流程提权；SSH 加固不夹带在磁盘置备中 |
| 宿主安全日志 | `rsyslog logrotate` | `rsyslog` 必须实际生成 `/var/log/auth.log`；规则与轮转进入受限宿主基线；`fail2ban` 暂不作为首发硬前置 |
| 网络与维护 | `iptables needrestart` | Docker 使用 iptables backend；补丁窗读回重启需求，不允许工具自动重启生产 |
| 密码学 | `openssl` | 版本写入包基线，不用于打印 secret 派生值 |
| 数据库客户端 | `postgresql-client-16` | `psql --version` 主版本必须为 16 |
| 容器 | `docker-ce docker-ce-cli containerd.io docker-compose-plugin` | Engine 首个预生产候选为 29.7.2，Noble 包 `5:29.7.2-1~ubuntu.24.04~noble`；其余完整版本同窗冻结；只使用 Compose v2，生产不安装 buildx |
| 源码只读安装 | `git` | `origin` 只能指向内网只读 Git 镜像 |

正式包锁由 `dpkg-query -W` 生成并存入受限变更证据。它应记录包名、完整版本、内部 APT
snapshot/仓库 ID、ISO SHA-256、模板 ID、内核版本、Docker Engine/Compose 版本，不记录内部
仓库凭据。安全补丁先在预生产验证，再用新包锁进入生产维护窗；不允许无人值守自动重启生产 VM。

Docker Engine 29 及以后在全新安装时默认把 image/container snapshot 写入
`/var/lib/containerd`，而现有五盘合同只隔离 `/var/lib/docker`。Phase 0 因此冻结为 classic
`overlay2`，显式关闭 containerd image store；不得用 symlink/bind 临时搬迁 `/var/lib/containerd`。
29.7.2 仍须先通过同规格预生产，才能从 `candidate` 变为 `approved`。最终冻结记录必须明确
Engine/Compose/containerd 完整版本、日志上限、cgroup、ulimit 和 `docker info` 读回。
当前候选 29.7.2 在 daemon 初始化 data-root 时会将 `/var/lib/docker` 收敛为
`root:root 0710`。Phase 0 因此把 `0710` 作为初启前后全生命周期唯一合同；它比旧
`0711` 去掉了 other 执行权限，是更严格的模式。禁止为了让旧预检通过而手工改回
`0711`；该权限合同的技术验收不能代替上述预生产、RF-07 基线证据或正式批准。

公司网络团队提供不与办公网、VPN、双机房、厂商以及 `172.31.250.0/24` 重叠的 Docker 地址池
后，生产 `/etc/docker/daemon.json` 必须按下列基线生成；尖括号是阻断占位符，未替换前文件无效，
不得安装或启动 Docker：

```json
{
  "data-root": "/var/lib/docker",
  "features": {"containerd-snapshotter": false},
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": {"max-size": "20m", "max-file": "5", "compress": "true"},
  "exec-opts": ["native.cgroupdriver=systemd"],
  "default-cgroupns-mode": "private",
  "default-ulimits": {
    "nofile": {"Name": "nofile", "Soft": 65536, "Hard": 65536}
  },
  "firewall-backend": "iptables",
  "iptables": true,
  "allow-direct-routing": false,
  "default-address-pools": [
    {"base": "<公司批准、至少可切分四个/24的非重叠私网段>", "size": 24}
  ]
}
```

安装前必须逐字段复核上述文件，并用
`dockerd --validate --config-file=/etc/docker/daemon.json` 验证 Docker 能接受该配置；启动后宿主
预检 `runtime` 必须读到 DockerRootDir=`/var/lib/docker`、classic `overlay2`、cgroup v2/systemd
和 `json-file`。Docker 没有通过该只读接口稳定暴露 address pool、iptables 与 default ulimit 的
全部配置事实，因此三者仍须保留 daemon 文件逐字段复核、`dockerd --validate` 证据，并在预生产
用批准镜像以固定格式只读 `inspect`/容器内 `ulimit -n` 证明 `nofile=65536`；不能把 runtime
退出 0 外推为这三项已验收。Docker 内部 JSON 日志只由上述 `max-size/max-file` 轮转，禁止宿主
logrotate 移动或截断。安全日报独立 access log 必须在首发前另有日轮转、保留 `.1/.1.gz` 和
Nginx reopen 演练；未完成时 RF-18 不得批准。

`default-address-pools.base` 不能填写单个 `/24` 却同时要求 `size=24`；它必须是至少可分配四个
`/24` 的更大获批私网池（建议至少 `/22`，或网络团队给出的等价多池方案），并与公司网络及固定
ingress `172.31.250.0/24` 全部不重叠。具体地址仍只能进入受限变更单。

### 3.3 公司宿主安全基线与补丁 SOP

仓库脚本不应猜测公司的 APT 签名、堡垒机、NTP、DNS、VLAN 或防火墙地址。`VMW/SEC` 必须在
RF-07 交付一个已签名、可复核的 **Ubuntu 24.04 生产宿主基线 ID 和版本**；它可以是公司受控的
镜像/Ansible/配置管理资产，但不能只是口头承诺或一段含占位符的 shell。基线必须形成以下闭集：

- APT：精确内部 Ubuntu 与 Docker snapshot、`Signed-By` keyring、签名/有效期验证、移除公共
  source、精确 `apt install package=version` 清单、预生产通过后的 `apt-mark hold` 和包锁读回；
- 运维身份：固定生产 operator 用户/UID/GID、仅管理 VPN/堡垒机来源；保留当前
  `PermitRootLogin no`，普通运维账号继续允许密码或密钥认证，其他团队先登录普通账号再提权；
  禁止 operator 加入 `docker` 组，sudoers 只安装经复核的固定入口。由于当前
  `/usr/local/sbin/sms-compose` 指向 operator 可写 checkout，`PLAT`
  operator 在本版实际上是 **root-equivalent 的受信管理员**，不能宣传成低权限账号；还要冻结
  SSH key 指纹/轮换撤销、主机 host-key 指纹、发布端 `known_hosts` 发放与非交互 sudo 验收；
- 时间与日志：唯一 `systemd-timesyncd` 实现、公司内部 NTP、空 `FallbackNTP=`、
  `Asia/Shanghai`、rsyslog 真实生成 `/var/log/auth.log`、journald/rsyslog 保留和轮转、日志转发
  及磁盘上限；
- SSH 与入侵阻断：首发按上述现状冻结并记录风险接受，不在存储变更中改密码认证、转发、sudo、
  防火墙或安装 fail2ban；紧急解锁与堡垒机回退仍须由公司运维流程负责。未来若扩大 SSH 来源或
  取消 VPN 强制边界，必须先独立完成 SSH hardening 变更；
- 内核与网络：与 Docker cgroup v2/iptables/转发兼容的 sysctl，禁止 ICMP redirect/source route，
  host firewall 与持久化 `DOCKER-USER` 规则，精确允许/拒绝和重启后读回；不得套用会关闭 Docker
  forwarding 或让已发布容器绕过策略的通用模板；至少明确并验证 `net.ipv4.ip_forward=1`、
  `vm.overcommit_memory=1`、Redis THP 关闭，以及 `rp_filter`/IPv6 的公司决策；
- 文件与日志：OS/journal/auth 日志轮转；若后续独立 SSH hardening 变更安装 fail2ban，再纳入其
  日志轮转；安全日报 access log 使用日轮转、`.1/.1.gz` 保留和经验证的 Nginx reopen；不得用
  logrotate 操作 Docker 内部 JSON 日志；
- 更新：常规补丁周期、CVE 紧急 SLA、维护窗、APT snapshot 保留/回退期、预生产验证、停平台、
  短期 VMware snapshot（若批准）、OS 重启、存储/runtime preflight、业务验收和 snapshot 删除读回。

宿主包更新与代码 release 是两套状态机：`sms-compose release` 不安装、回退或证明 OS/Docker
包更新。正式上线前必须同时取得基线资产和独立“宿主补丁 SOP”；以后每次 Docker daemon、
Engine、Compose、containerd、内核或 systemd 变化，都要按新包锁在预生产重跑缺盘失败、重启、
发布、备份恢复和回退演练。无人值守自动重启、直接运行公共安装脚本以及无 snapshot 版本的
`apt upgrade` 均禁止。

`production_host_preflight.py base/runtime` 只证明脚本明确列出的 OS/架构/资源/Python/systemd/
时间/工具和 Docker runtime 子集；退出 0 **不证明** APT 来源、精确 package lock、sshd、NTP
来源、fail2ban、sysctl、firewall、日志轮转或补丁 SOP 已合格。这些必须由公司基线自身的只读
readback 与另一名自然人复核。

## 4. VMware 资源冻结

### 4.1 资源清单

| 资源 | CPU/内存 | 磁盘 | 网络/用途 | 首发状态 |
|---|---:|---:|---|---|
| 生产 VM | 12 vCPU / 48 GiB | 5 VMDK，共 1050 GiB | 单 NIC，生产 VLAN，固定厂商出口 | 必须置备并验收 |
| 应用预生产 VM | 建议 8 vCPU / 24 GiB | 仍保留 5 个独立 guest VMDK；为完整存储合同演练建议使用同标称容量，可 thin provision | 预生产 VLAN、独立数据/secret、可访问受控离线包/Git | 必须置备并验收 |
| 一次性隔离恢复机配额 | 建议按生产同规格 12 vCPU / 48 GiB | 独立 5 VMDK，PostgreSQL/Runtime 不得复用生产或共享预生产 VMDK | 无生产厂商 Send 出站；用完整 VM 退役 | 必须预留并验证申请时长 |
| 受控联网签名节点 | 建议 4 vCPU / 8 GiB / 200 GiB | 独立制品暂存盘 | 下载 Release Gate 制品、核验 GitHub attestation 并签名；持有私钥但不能访问生产 secret | 必须置备或指定既有受控节点 |
| 后续冷备 | 未配置 | 未配置 | 首发非硬门禁 | 记录残余风险 |

预生产缩小的是计算资源，不得缩减服务、volume、secret 名、TLS、三 Redis 域或发布合同。若
因资源原因缩小预生产 VMDK，必须额外创建一次生产标称容量的临时演练机运行完整
`storage_preflight` 正常/缺盘失败演练；不得把未覆盖的容量合同写成已验证。

### 4.2 vSphere 参数

- 固件、Secure Boot、虚拟硬件版本和 EVC 按公司 Ubuntu 24.04 认证模板冻结；不在应用脚本中修改。
- 网卡使用公司认证的 VMXNET3；数据盘控制器使用公司认证的 PVSCSI。总 vCPU 必须为 12，
  socket/core 布局由 VMware 管理员按物理 NUMA 读回后冻结，不能只看控制台配置值。
- 生产数据库型 VM 建议完整内存预留；若公司不允许，必须记录 balloon/swap/宿主超分风险和告警。
- 每块 VMDK 单独记录 vCenter 资产 ID、controller/unit、datastore、storage policy、thin/thick、
  标称容量和 guest 设备序列号。guest 内磁盘分离不能证明 datastore 或控制器分离。
- vCenter 按公司模板启用 VMDK UUID 向 guest 暴露；常见配置是 `disk.EnableUUID=TRUE`，公司采用
  等价机制时记录其基线 ID。五盘都必须产生唯一且重启稳定的 `/dev/disk/by-id/...` 和 serial，
  并在预生产调整 SCSI unit 后仍按 VMDK 身份读回；否则磁盘置备为 No-Go。
- VMware snapshot 只允许短期、经审批的维护用途，不得代替 PostgreSQL 加密备份；必须有创建人、
  到期时间和删除读回，默认不跨过 24 小时。

## 5. 磁盘与目录的精确处理

### 5.1 冻结布局

| 角色 | VMDK | 文件系统 | 挂载点 | 挂载点 owner/mode | 预检容量下限 |
|---|---:|---|---|---|---:|
| OS 根 | 100 GiB OS VMDK | LVM 上 ext4 | `/` | `root:root 0755` | 94 GiB 且不少于根 LV 的 97% |
| OS `/boot` | 同一 OS VMDK | 2 GiB ext4 | `/boot` | `root:root 0755` | 1.8 GiB |
| OS EFI | 同一 OS VMDK | 1127219200 B vfat | `/boot/efi` | `root:root 0755` | 0.98 GiB |
| Docker | 250 GiB | XFS，`ftype=1` | `/var/lib/docker` | `root:root 0710` | 245 GiB |
| PostgreSQL | 400 GiB | XFS，`ftype=1` | `/var/lib/sms-platform/postgres` | `root:root 0750` | 392 GiB |
| Redis | 100 GiB | XFS，`ftype=1` | `/var/lib/sms-platform/redis` | `root:root 0750` | 98 GiB |
| Runtime | 200 GiB | XFS，`ftype=1` | `/var/lib/sms-platform/runtime` | `root:root 0750` | 196 GiB |

OS 盘保留已置备的 Ubuntu 24.04 GPT/LVM 结构：512 B 逻辑扇区；分区 1 从 sector 2048 开始、
1127219200 B、ESP/vfat、挂载 `/boot/efi`；分区 2 从 sector 2203648 开始、2 GiB/ext4、挂载
`/boot`；分区 3 从 sector 6397952 开始、`LVM2_member`，覆盖余下空间且距盘尾不超过 8 MiB。
第三分区只允许一个 `/dev/mapper/ubuntu--vg-ubuntu--lv` 根 LV，PV/LV 容量差不超过 8 MiB，
根 ext4 唯一挂载 `/`。三个 PARTUUID、PV UUID、根 UUID 和固定起点纳入身份校验。

`/swap.img` 固定为根 ext4 上 `root:root 0600`、单链接、非稀疏且已分配的 8 GiB 普通文件，
并且是唯一活动 swap；fstab 精确为 `/swap.img none swap sw 0 0`。初始化工具只读验证全部 OS
对象，绝不分区、格式化、扩容或修复它们。后续 OS 扩容只能保持三个 PARTUUID/起点不变，依次
扩大最后分区、同一 PV、同一根 LV 和同一 ext4；不得新增 PV/LV 或只完成其中一层。

四块数据 VMDK 固定采用“一盘一个 XFS、无 LVM、无额外分区”的简单布局。公司标准若强制
LVM 或 ext4，当前工具会失败关闭；必须先变更本决策、实现和预生产证据，不得在生产临时决定。

### 5.2 破坏性步骤的人工边界

磁盘识别、`mkfs`、fstab 写入和首次 mount 是 VMware/宿主管理员的审批变更，**不由任何应用
初始化脚本自动执行，也不由代码发布或开机任务执行**。经审批可使用
[生产宿主磁盘置备工具](../../deploy/production-storage-initialization.md) 作为 root 人工入口；该
工具不属于应用初始化，必须绑定 root-only 清单、实时计划摘要和控制 TTY 逐盘确认。固定顺序
如下：

1. 在 vCenter 记录四块新数据 VMDK 的资产 ID、容量、controller/unit 与 datastore；guest 执行
   `lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,SERIAL,MODEL,MOUNTPOINTS`、`blkid` 和
   `wipefs --no-act <已核对的-by-id设备>`。vCenter 与 guest 必须逐盘双向对应。
   变更证据目录与 plan/apply/status JSON 必须是 `root:root 0600`，目录 `root:root 0700`；具体
   留档命令见生产宿主磁盘置备工具手册，禁止让普通 `tee` 按默认 umask 生成 0644 证据。
2. 只有同时满足“新建、无文件系统签名、无分区、未挂载、变更单明确点名、执行人和复核人已
   确认”的设备才允许格式化。不得按 `/dev/sdX` 顺序猜盘，不使用 `mkfs -f` 覆盖已有签名。
3. 对已批准的四个稳定 `/dev/disk/by-id/...` 路径执行 XFS 格式化；四盘都必须显式
   `-n ftype=1`。具体设备路径只能从当次受限变更单复制，本文不提供可直接运行的设备名。
4. 用新文件系统 UUID 写入 `/etc/fstab`。四个数据挂载点固定使用 `defaults,nodev,nosuid`，禁止
   `nofail`、`noauto`、`x-systemd.automount` 和内部 Docker `_data` 的 bind/mount 条目。
5. 置备工具只对四个固定挂载点逐个 mount；禁止用 `mount -a` 触碰 fstab 中无关文件系统。
   挂载后逐盘用 `findmnt --target`、`lsblk -f`、`df -B1` 和设备 major:minor 读回；四个 XFS
   挂载点都用 `xfs_info <固定挂载点>` 确认 `ftype=1`。
6. 只有挂载验真后才创建第 5.3 节八个子目录，并把 `/var/lib/docker` 固定为
   `root:root 0710`。随后安装存储 preflight 与 Docker systemd
   drop-in；preflight 成功前 Docker 必须保持 masked/stopped。
7. Docker 第一次启动后确认 `DockerRootDir=/var/lib/docker`、storage driver 符合公司基线，
   XFS 时 `Supports d_type: true`，且挂载点仍精确为 `root:root 0710`。任何 Docker 数据曾落到
   OS 盘都必须停机调查，不能由脚本搬迁。

fstab 字段模板以 [生产存储手册](../../deploy/storage.md) 为唯一可执行依据。扩容只允许增加
已核对 VMDK：数据盘执行 `xfs_growfs`；OS 盘必须依次扩大最后分区、同一 PV、同一根 LV 和
ext4。禁止缩容、搬迁 Docker `_data`、删除 WAL/AOF/备份或 prune 来跨过 70/80/90 阈值。

### 5.3 固定目录

| 路径 | owner | mode |
|---|---:|---:|
| `/var/lib/sms-platform/postgres/pgdata` | `70:70` | `0700` |
| `/var/lib/sms-platform/redis/broker` | `999:1000` | `0700` |
| `/var/lib/sms-platform/redis/auth` | `999:1000` | `0700` |
| `/var/lib/sms-platform/redis/control` | `999:1000` | `0700` |
| `/var/lib/sms-platform/runtime/imports` | `10001:10001` | `0700` |
| `/var/lib/sms-platform/runtime/exports` | `10001:10001` | `0700` |
| `/var/lib/sms-platform/runtime/raw-spill` | `10001:10001` | `0700` |
| `/var/lib/sms-platform/runtime/backups` | `root:root` | `0700` |

以上目录必须是普通目录、不是 symlink，且与父挂载点处于相同设备；首次 bootstrap 前均为空。
Runtime 盘每天两份、保留 35 天，约 70 个备份 generation。必须以预生产密文实测大小投影
35 天占用并保持低于 70%；预计越线就在 T0 前扩容，不得缩短保留期。

### 5.4 容量预算和预生产通过线

RF-22 不能只写“12 vCPU/48 GiB/1050 GiB”。必须按首批应用真实峰值和 12 小时积压建立：

- PostgreSQL：业务消息/回复/号码明细 12 个月、审计 36 个月、raw/unmatched 90 天、长期汇总，
  记录单条实测字节、索引/WAL/临时空间、月增长和保留清理证据；
- 三 Redis：分别冻结 `maxmemory`、连接上限、AOF 当前/重写峰值和 `noeviction`，不得把 100 GiB
  VMDK 总量当成三个实例都可使用的独立容量；
- Docker：四个当前 digest、至少一个前向回退候选、容器可写层和 `20m × 5` 日志轮转的预算；
- Runtime：imports 24h、exports 7 天、raw-spill 上限、安全日报目录（ENG-01 闭合后）、release
  状态和约 70 个加密备份；
- 宿主：OS/journal/auth、CPU、内存、网络、数据库连接池和每个 worker RSS；fail2ban 指标仅在
  后续独立安装并启用后纳入。

预生产使用同一签名封闭包、真实配置上界和隔离数据，至少验证：API 受理 P95<2000ms；标准性能冒烟
停止施压后 active batch/三队列 480 秒内清零；首批应用给出的完整 12 小时积压在批准的厂商 QPS
下于 12 小时内排空，同时 realtime 预留仍可用、没有自动重发 uncertain。记录 P50/P90/P95/P99、
吞吐、队列峰值、CPU/RAM/RSS、DB 连接/锁/WAL、三 Redis memory/AOF/latency/blocked clients、
五盘起止字节和 Docker 日志增长。常态峰值 CPU、内存、连接、Redis maxmemory 及各盘投影必须
低于 70%；任何资源达到 70% 先扩容或缩小首批范围，不能上线后再观察。测试前先批准工作负载和
阈值，测试失败后不得通过修改阈值把同一次结果改成通过。

## 6. 网络、DNS、TLS 与防火墙冻结

### 6.1 入站

| 源 | 目标 | 端口 | 规则 |
|---|---|---:|---|
| 管理 VPN/堡垒机精确网段 | 生产 VM | TCP 22 | 禁止 root 直接 SSH；本阶段保留普通账号密码/密钥登录现状，其他团队登录后再按流程提权 |
| 公司 VPN 精确业务网段 | 企业 TLS 终结器 | TCP 18443 | 唯一浏览器/API 入口；手机也必须先接 VPN |
| TLS 终结器精确主机 | 生产 VM 专用静态私网 IP 的 Web 上游 | TCP 18080 | 来源必须是逐主机 `/32` 或 `/128` |
| 本机 | API | TCP 8000 | 只绑定 `127.0.0.1`，不对网络开放 |
| 宿主本地 metrics collector | API metrics ingress | TCP 8000 | 容器看到的来源固定为 `172.31.250.1/32` |

互联网及普通内网必须拒绝 `80/443/8080/8443/8000/9028`；`18443` 也不允许互联网直达。
主机防火墙与 Docker `DOCKER-USER` 链都要有真实读回。若 TLS 终结器端口不是 18443，必须在
变更单中同步更新 DNS、探针、证书和防火墙，不能只临时开放另一端口。

### 6.2 出站

| 目的地 | 端口 | 状态/限制 |
|---|---:|---|
| 公司 DNS | UDP/TCP 53 | 仅批准解析器 |
| 公司 NTP | UDP 123 | 仅批准时间源 |
| 内部 APT 镜像 | TCP 443 | OS 置备/维护窗；生产不访问公共软件源 |
| 内网只读 Git | TCP 22 或 443 | 只冻结一种协议和精确目标 |
| 受控离线包入口 | SSH 22（经堡垒机/VPN） | 只由固定 driver 校验上传；禁止裸 scp/rsync、人工 load |
| 内部 Registry（退出目标） | TCP 443 | 建成后只拉取 digest；不在生产 push/build |
| 新短信服务商生产 API | TCP 443 | TLS 校验；固定 SNAT IPv4；书面白名单 |
| `qyapi.weixin.qq.com` | TCP 443 | 只由既有告警实现访问；不继承宿主代理 |
| 公司 SMTP relay | TCP 25、465 或 587 中的一个 | 冻结精确主机、端口、TLS 模式、发件人 |
| `api.resend.com` | TCP 443 | 仅 security-report mailer 投递脱敏安全日报；收件人为公司邮箱 |
| 已批准 callback CIDR | 默认 TCP 443 | 部署上限与管理员配置取交集；空集合时 fail closed |
| PKI CRL/OCSP、内部监控 | 公司指定 | 逐项列出；不得使用任意互联网放行 |

首发本地认证，`LDAP_ALLOWED_HOSTS` 保持空，AD 出站暂不开放。生产不需要 GitHub、Docker Hub、
PyPI、npm 或公共 Trivy 数据库；这些只在受控构建/提升环境使用。

Docker 发布端口可能绕过 UFW 的常规 INPUT 规则；容器入站限制必须落在
`DOCKER-USER` 链。普通管理员不得加入等价 root 的 `docker` 组，Docker API 只监听 Unix socket，
TCP 2375/2376 在所有接口不可达。防火墙验收必须从批准 VPN/TLS 来源、未批准同网段主机和外部
来源三处实测，不能只保存规则文本。

### 6.3 必填网络与 TLS 证据

- 生产 VM 固定私网 IP、NIC、VLAN、网关、DNS resolver、反向记录，以及 `WEB_BIND_IP` 使用的
  专用接口地址；必须与厂商报备的公网 SNAT 地址明确区分；
- 生产业务 DNS、VPN 源网段、TLS 终结器精确地址、`SMS_TRUSTED_PROXY_CIDRS`；
- 证书 SAN、签发者、序列号或受限证据 ID、到期日、续期负责人和提前告警天数；
- HTTP→HTTPS、TLS≥1.2、证书剩余≥14天、HSTS 至少一年且含 `includeSubDomains`；
- 固定主出口公网 IPv4、NAT 设备、VM/容器重启后出口读回；
- 厂商书面回执：工单号、QPS、单次号码上限、生效时间和 GetBalance 结果；
- 真实 IP/域名只进入受限证据，公开摘要仅写证据编号和 `verified`。

“已有 SSL 证书”不等于已有 HTTPS 入口。RF-11 必须确认公司已经提供可运维的企业 LB/反向代理，
并冻结其资产 ID、软件/服务、终结地址、18443 listener、回源地址、健康探针、证书安装/续期和
告警责任。如果公司没有该终结器，RF-11 保持阻断，必须另行设计受控 TLS ingress；不得把证书
直接塞进当前仅 HTTP 的 Web 容器，也不得在未更新端口、镜像、systemd、监控和发布合同前上线。

## 7. 制品、代码和发布资源

1. CI/G2 在日常合并流程完成；临时 GitHub Release Gate 不再重复运行。它为受保护 `main` 的
   精确候选 commit 单次构建四镜像，生成四个 archive、独立 Trivy、候选 SBOM、离线索引并
   attestation 索引；索引明确记录未证明可重复构建。`REL` 在受控联网签名节点下载并核验后生成 schema v2
   manifest 与 Ed25519 签名。私钥不得进入仓库、预生产、生产或发布包。
2. `REL` 建设内网只读 Git 镜像；生产 `/opt/sms-platform` 的 `origin` 只能指向该镜像。
3. 封闭制品必须是**生产离线 Docker image archive 发布包（镜像 OCI-compatible，不是 OCI Image Layout）**，由 driver 校验 manifest/签名文件闭包、离线索引及四 tar 的 SHA-256/size 后
   上传；release manager 导入后逐镜像读回 image ID、平台和 labels。禁止人工 `docker load`、
   裸上传、现场构建和 raw Compose。
   `SEC/PLAT` 通过获批的一次性宿主信任材料置备流程安装 `/etc/sms-platform/offline-release-signing-public.pem` 与
   `/etc/sms-platform/offline-release-signing-key-id`，均为 `root:root 0644` 普通单链接文件；
   Ed25519 私钥只在受控联网签名节点，严禁进入 Git、发布包、预生产或生产。
4. 预生产使用将要生产激活的同一签名封闭包，完成发布、迁移、适用 UAT、容量冒烟、备份/恢复、
   企微+邮件、最小真实厂商链路和旧系统切回演练。
5. 全新生产主机只执行一次 `release bootstrap --confirm-empty-host`。后续正常更新执行
   `release prepare`→`activate`→`status`；中断后只按状态给出的 `next_step` 执行 `resume`，仅在
   工具允许的未成功状态执行 `rollback`。`succeeded` 版本不能原地回滚；临时离线通道制作新
   commit、四镜像全新 ID、无迁移整包并走普通 prepare/activate，Registry 路径才使用
   prepare-forward-rollback。所有生产 Compose 动作只通过
   `sudo /usr/local/sbin/sms-compose`，完整状态机以部署手册为准。

内网 Git 镜像不可省略。CI 成功不代表离线包已签发，预生产成功也不代表同一包已上传或部署
生产；各类证据必须分别归档。上传、验签或导入失败保留 staging、release 状态和已导入镜像，
禁止无范围 prune。内部 Registry、不可变 namespace、生产只读身份和按 manifest RepoDigest
拉取入口建成，并以同一候选完成预生产演练后，离线通道退出；Registry 门禁及测试不得删除。

宿主资产安装器是一次性、拒绝覆盖的 bootstrap 工具；常规 application release 只推进 Git 和
容器状态，不会同步升级复制到 `/etc`、`/usr/local` 的宿主资产。任何候选只要修改安装器 inventory
中的 wrapper、preflight、systemd unit/drop-in 或配置样例，ENG-03 未闭合前就必须阻断；不能让
Git fast-forward 先改变 wrapper symlink 目标，再继续使用旧的 root-owned unit/脚本。

### 7.1 生产 `.env` 的生成边界

仓库的 `deploy/.env.example` 是开发样例，含本地 tag、Mock URL、`development`、`DEBUG=1`、
`TRUSTED_HOSTS=*`、宽 metrics CIDR 和 `dev` profile，**不得复制后稍改几行就用于生产**。当前也
没有已批准的 production env 生成器。ENG-04 必须交付一个由 RF 实值生成、闭集校验、输出
`root:root 0600` 且不含凭据的受控资产。至少冻结以下非 secret 项：

- 四个 `SMS_*_IMAGE` 必须与获批 manifest 精确一致：Registry 路径为
  `image@sha256:RepoDigest`，临时离线路径由受控 release manager 写入已验签导入的 image ID；
- `POSTGRES_DB/DB_HOST/DB_PORT/DB_NAME`，全部 DB pool/overflow/timeout/statement-timeout 预算；
- `REDIS_HA_MODE=isolated-standalone`、三个不同 Redis host、CA 路径与非 secret 连接上限；
- `VENDOR_BASE_URL` 为真实 HTTPS、`VENDOR_LIVE_TEST_ORIGIN` 与首发模式边界、
  `VENDOR_MOCK=0`；
- `ENVIRONMENT=production`、`DEBUG=0`、`AUTH_MOCK=0`、`TZ=Asia/Shanghai`、
  `COMPOSE_PROFILES` 为空、`JWT_ACCEPT_LEGACY=false`、`LDAP_ALLOWED_HOSTS` 为空；
- 精确 `TRUSTED_HOSTS`、`WEB_HTTP_BASE_URL/WEB_BASE_URL`、`WEB_BIND_IP`、
  `SMS_EXTERNAL_TLS_MODE`、逐主机 `SMS_TRUSTED_PROXY_CIDRS`；
- 固定 `SMS_INGRESS_SUBNET=172.31.250.0/24`、`SMS_API_INGRESS_IPV4=172.31.250.2`、
  `SMS_WEB_INGRESS_IPV4=172.31.250.3`、`METRICS_ALLOWED_CIDRS=172.31.250.1/32`；
- 公司 SMTP allowlist、callback 精确 CIDR/端口上限、readiness/metrics/worker RSS/raw-spill
  有界参数、日志级别；
- 运行 secret、vendor-test/socket 空边界以及安全日报 config/control/access-log 的宿主绝对路径。
  安全日报路径在 ENG-01 闭合前没有可批准值，不能回退到源码目录默认值。

生成后必须人工确认没有 password/key/token，运行 `sms-compose config --quiet` 并由 release
evidence 绑定非镜像 env 摘要；仅 Compose 能展开不代表网络、TLS、目录、secret 或容量已经验证。

## 8. PKI、secrets、备份与监控

### 8.1 Redis PKI

内部 PKI 必须签发 `serverAuth` 证书，SAN 精确包含 `redis`、`redis-auth`、`redis-control`。
宿主 CA 与服务端证书为 `root:root 0644`；无口令 PKCS#8 私钥是第 25 件 canonical secret，
`root:root 0600`，只进入三个 Redis 容器。上线时证书至少剩余 7 天，但资源冻结应设置 30/14/7 天
告警并预留停全栈轮换窗口。

### 8.2 运行 secret 与恢复材料

生产 `deploy/secrets` 必须是 `root:root 0700` 非链接目录，恰好包含 25 个 `root:root 0600`
非空普通文件：

```text
vendor_secret_name vendor_secret_key data_aes_key data_hmac_key
audit_context_key audit_system_api_context_key
audit_system_realtime_context_key audit_system_bulk_context_key
alert_credential_public_key alert_credential_private_key jwt_secret ldap_bind_password
metrics_scrape_token db_owner_password db_auth_password db_accept_password
db_send_password db_callback_password db_export_password db_scheduler_password
db_metrics_password redis_broker_password redis_auth_password
redis_control_password redis_tls_server_key
```

即使 AD 延后，`ldap_bind_password` 仍属于固定 inventory，但 AD Provider 保持禁用。另行提供：

- `/etc/sms-platform/backup-secrets/sms-backup-passphrase`；
- `/etc/sms-platform/recovery-crypto-generation-id`；
- `/etc/sms-platform/backup-secrets/generation-id`；
- 与两个 ID 对应、位于生产主机之外的不可变 recovery-crypto bundle 与备份口令 escrow。

只能记录文件名、owner、mode、代次 ID 与发放事实；禁止记录值、长度、摘要或哈希。恢复材料的
发放与一次性恢复机安装必须由两名真实人员见证。

现有加密备份位于同一生产 VM 的 Runtime VMDK，只能覆盖“主 PostgreSQL 数据损坏但 Runtime
VMDK 仍可读”等故障，不能覆盖整 VM、全部 VMDK 或 datastore 丢失。RF-21 必须在 T0 前二选一：

1. 建设每日离机加密备份复制，保持 manifest/口令/crypto generation 配对并完成隔离 full restore；
2. 由 `BUS/DBA/SEC/REV` 正式批准：RPO≤24h 只适用于 Runtime VMDK 存活的故障，整 VM/存储丢失
   时新平台数据可能不可恢复，业务仅切回旧系统，并把该范围写成已接受残余风险。

这不是要求首发建立冷备或热备，但不得在只有同机备份时无条件宣称整个新平台 RPO≤24h。

### 8.3 监控与告警

- 生产 VM 宿主部署 Prometheus 或等价 collector，使用独立 token 抓取十二组指标；
- VMware/宿主外监控必须能发现整 VM 不可达，同机 collector 不能替代；
- 宿主 journal、存储阈值、备份/RPO、任务心跳、PostgreSQL、三 Redis 域、TLS 到期、固定出口和
  业务异常均有责任人和升级路径；
- 企业微信与公司邮件各有主接收人和替补，并分别完成一次无 PII 真实告警和收件确认；
- 业务告警邮件固定走公司 SMTP；安全日报仍按既有独立边界走 `api.resend.com:443`，由管理员
  页面配置 Resend Key 和最多 3 个公司邮箱收件人，两条邮件链都必须各自真实验收；
- `log-sink`、Mock、单元测试或只有一个收件渠道均不是生产告警闭环。

当前 `deploy/security-report/docker-compose.yml` 仍要求第五个本地 build 镜像并在独立手册中使用
裸 Compose；config/control/access log 默认还位于源码或 OS 盘，collector unit 也写入 `/opt`，且
仓库没有 access-log rotate/reopen 资产。这与四镜像 digest、生产禁止现场构建、Runtime VMDK 和
受控发布合同冲突。ENG-01 闭合前不得启用 mailer/collector，也不得把安全日报写成已验收；由于
安全日报是当前 PRD 要求，生产 T0 同样被阻断，除非通过正式产品变更明确批准例外。

## 9. 责任矩阵

### 9.1 角色

| 代码 | 角色 | 可兼任边界 |
|---|---|---|
| `PLAT` | 平台技术管理员、变更执行人 | 可以兼任 REL/DBA/MON 的执行工作 |
| `REV` | 第二复核人，业务负责人或公司变更审批人 | 必须是 PLAT 之外的另一名自然人 |
| `VMW` | VMware/宿主管理员 | 可由基础设施团队承担 |
| `NET` | 网络、防火墙、VPN、DNS 管理员 | 负责固定出口和网络读回 |
| `SEC` | 安全、PKI、密钥和 escrow 保管人 | secret 值只在受控系统内处理 |
| `REL` | 构建、attestation 核验、离线包生成/签名、未来 Registry、Git 镜像负责人 | 不获得生产运行 secret；签名私钥不进入生产 |
| `DBA` | PostgreSQL、备份和隔离恢复负责人 | `sms_owner` 只在受控动作使用 |
| `MON` | 监控、值班和告警负责人 | 负责宿主外 VM 心跳 |
| `APP` | 首批应用负责人 | 每个应用单独指定 |
| `LEGACY` | 旧短信系统负责人 | 负责互斥路由和切回验收 |
| `BUS` | 业务上线最终责任人 | 批准首批范围与残余风险 |

### 9.2 RACI

| 工作包 | R | A | C | I |
|---|---|---|---|---|
| OS 模板、生产/预生产/恢复 VM | VMW | PLAT | SEC、DBA | REV、BUS |
| VMDK 识别、格式化、fstab、挂载 | VMW | PLAT | DBA、REV | BUS |
| Ubuntu 包锁、Docker/Compose | VMW、REL | PLAT | SEC | REV |
| 离线包生成/签名、Git 镜像、未来 Registry | REL | REL | SEC、PLAT | REV |
| VPN、TLS、DNS、防火墙 | NET、SEC | NET | PLAT、APP | REV |
| 固定出口与厂商白名单 | NET | NET | PLAT、APP | REV |
| Redis PKI、25 secrets、escrow | SEC | SEC | PLAT、DBA | REV |
| PostgreSQL、备份容量、隔离恢复 | DBA | DBA | VMW、SEC、PLAT | REV |
| metrics、VM 外心跳、企微和邮件 | MON | MON | PLAT、NET、SEC | REV、BUS |
| 候选发布和生产 bootstrap | PLAT、REL | PLAT | DBA、SEC、MON | REV、BUS |
| 首批应用与旧系统切回 | APP、LEGACY | BUS | PLAT、MON | REV |

`PLAT` 可以是当前唯一平台管理员，但 `REV` 不能是同一个人，也不能使用共享凭据。没有第二名
真实复核人时，资源可以继续申请和预生产演练，但生产上线、恢复和解除业务围栏均为 No-Go。

## 10. 初始化与验收顺序

### 当前工程阻断项

这些不是靠填写 RF 表即可消失的外部资源，必须先由独立代码/运维交付闭合：

| ID | 当前缺口 | 关闭条件 | R/A |
|---|---|---|---|
| ENG-01 | 安全日报使用第五个本地构建、裸 Compose、源码/OS 路径且无 access-log rotate/reopen | 纳入受控 digest 发布（或复用四镜像之一）；config/control/access log 固定到 Runtime VMDK并受存储预检；补轮转/reopen、容量与升级验收 | REL/PLAT |
| ENG-02 | 内部 Registry 尚未建设，生产镜像不能靠裸上传或人工 load 进入 | 签名 schema v2 封闭包、固定生产公钥/key ID、driver 校验上传、release-manager 验签/受控导入及失败保留均有测试，并以同一包通过预生产；未来 Registry 建成并演练后退出 | REL/REL |
| ENG-03 | 宿主资产仅可首装，常规更新会推进 symlink checkout 却不升级 root-owned 资产 | 新增受控 host-asset plan/apply/status/rollback 或发布前精确漂移围栏；所有受保护资产、commit、原子切换和失败恢复有测试 | REL/PLAT |
| ENG-04 | 只有危险的开发 `.env.example`，没有生产生成/闭集校验 | 从 RF 实值生成无 secret 的 0600 文件，拒绝开发默认、未知/重复项、相互矛盾设置和未闭合安全日报路径；release 绑定 | REL/PLAT |
| ENG-05 | 没有只读的 canonical 25 secrets 精确 inventory | 验证根目录 owner/mode/非链接、恰好 25 个名称、每项 root-owned 0600 非空普通非链接文件及格式；固定输出不读取/泄漏值 | SEC/SEC |
| ENG-06 | 公司宿主安全基线与独立补丁状态机尚未给出 | 第 3.3 节闭集取得基线 ID/版本、自动化资产、只读 readback、维护/紧急 SLA、预生产与回退证据 | VMW/PLAT |

ENG-02 随本次生产离线包实现进入候选验收，只有定向测试、预生产同包演练和生产信任根读回均
通过后才可标记 `closed`；其余工程项仍按实际证据收口，本文不因文字修改声称已经实现。ENG-03
未闭合前，只有明确不改变宿主资产
inventory 的 application-only release 可进入后续评估。唯一窄例外是 Docker 首次启动前，从固定旧
commit `555fb20b0d630ece9099a88a463eb1ce1121c012` 执行仅限六个存储预检资产的
mountinfo credential 修复；它不适用于已 bootstrap 主机，也不关闭 ENG-03。

### Gate 0：责任和变更治理

1. 指定 `PLAT`、`REV`、`BUS` 以及其余团队负责人；创建生产变更单。
2. 冻结维护窗、回退决策人、旧系统切回负责人、值班联系方式和证据存放位置。
3. 本节未完成前不得申请 T0 激活，但可并行申请资源。

### Gate 1：OS、VM 与磁盘

1. VMW 用批准 ISO/模板创建生产、预生产，并预留一次性恢复机配额；Docker 保持未启动。
2. 从公司内部 APT snapshot 安装第 3.2 节基础、存储、时间、VMware 和 Git 精确版本包；冻结
   `Asia/Shanghai`、公司 NTP，读回同步状态、VMDK UUID 暴露和五盘稳定 by-id/serial。此时不安装
   或启动 Docker；如模板已有 Docker unit，先 mask `docker.service`、`docker.socket` 和
   `containerd.service`。
3. 建立固定 production operator（不加入 `docker` 组），由该账号从内网只读 Git 镜像把精确
   候选 commit 检出到原本为空的 `/opt/sms-platform`；核对 `origin`、40 位 commit、owner/mode、
   tracked/暂存区干净。该 operator 按第 3.3 节明确作为 root-equivalent 受信发布身份管理。
4. 从这个已核对 commit 按磁盘置备手册把专用脚本安装到 root 控制路径，完成第 5 节人工
   plan/apply/status、UUID fstab、挂载和固定目录创建；正式 apply 前必须已有同脚本 SHA 的
   disposable VMware/PVSCSI 演练证据。
   演练还必须使用仓库原始 hardened unit 验证 PID 1 宿主 mountinfo、只读 `block-sd` 与
   `block-device-mapper` 设备授权、`/dev/disk/by-uuid` 与 `/dev/disk/by-id` 只读可见、
   `findmnt --verify` 零错误且至多出现固定 `/swap.img` 的一个已知 warning，以及七个固定 Docker
   bind 正常通过/任意额外 bind 失败关闭。
5. 从同一 APT snapshot 安装 Docker 精确版本并保存完整包锁，三个容器 unit 继续
   stopped/masked。网络负责人填写并复核 Docker address pool；安装第 3.2 节 daemon 基线并运行
   `dockerd --validate`。再由 operator 执行只读宿主 base preflight 和存储 preflight；任一失败
   不得启动 Docker 或进入 Gate 2。

```bash
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/production_host_preflight.py base
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/storage_preflight.py --mode startup
```

### Gate 2：安装仓库宿主资产

使用 Gate 1 已由 production operator 检出的精确、干净候选。先预览、再安装、最后读回固定
wrapper、preflight、systemd unit 和非 secret 配置样例：

```bash
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py plan \
  --expected-commit <40位候选commit>
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py apply \
  --expected-commit <40位候选commit> \
  --confirm-dedicated-production-host \
  --confirm-vcenter-storage-reviewed
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py status
```

安装中断后只允许用同一候选 commit 恢复或回滚，禁止手工猜测删除已发布目标：

```bash
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py resume \
  --expected-commit <40位候选commit>
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py rollback \
  --expected-commit <40位候选commit> \
  --confirm-rollback-this-install
```

尖括号必须替换为将要安装的 40 位候选 commit，不能原样执行。
若该主机已在固定旧 commit 完成首装，但仍未首次启动 Docker，上述 mountinfo
credential 窄修复必须使用 [部署手册](../../deploy/README.md#安装受控包装器与-systemd)
中固定的 `555fb20b0d630ece9099a88a463eb1ce1121c012` →
`109c10865b2aac3989bc4cebf3c60788f44b168c`、`apply/resume/rollback/upgrade-accept`
流程；禁止把 NEW 指向其它 commit、手工覆盖六个目标或直接改写 canonical state。该历史路径
只接受空 data-root 的 `0711`；到达 `109c108...` 后须在该版本完成一次受控 Docker 技术首启，
随后停止并重新 mask Docker/containerd、保持平台未 bootstrap，再使用下述第二 profile 收敛到
当前全生命周期 `0710` 合同和新合并 SHA。
若主机已在 `109c10865b2aac3989bc4cebf3c60788f44b168c` 完成宿主资产首装并已技术
首启 Docker，但尚未 bootstrap，旧 preflight 会把 Docker 稳态 `root:root 0710` 报为错误。
此时只能按部署手册中第二个固定 repair profile，使用下列同一 CLI 参数从该旧 SHA
升级到已审核、干净的新合并 SHA：

```bash
OLD_COMMIT='109c10865b2aac3989bc4cebf3c60788f44b168c'
NEW_COMMIT='<40位新合并commit>'
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py plan \
  --from-commit "$OLD_COMMIT" --expected-commit "$NEW_COMMIT"
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py apply \
  --from-commit "$OLD_COMMIT" --expected-commit "$NEW_COMMIT" \
  --confirm-dedicated-production-host --confirm-vcenter-storage-reviewed
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py upgrade-accept \
  --from-commit "$OLD_COMMIT" --expected-commit "$NEW_COMMIT"
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/install_production_host_assets.py status
```

该 profile 只允许 `storage-preflight` 一项宿主资产变化；Docker data-root 可非空但必须原样
保留，Docker/containerd 三个 unit 必须 masked/inactive，平台、vendor、四个维护 service、
四个 timer 和 release root 必须仍符合 pre-bootstrap 边界。候选 preflight 必须与旧 payload
逐字节相同，唯一允许的差异是将唯一的 Docker MountRequirement `0o711` 替换为
`0o710`；同一文件内任何其它字节变化都失败关闭。`apply/resume` 只写 root-only intent
并原子替换该一项资产；替换前必须先用绑定候选 commit 的字节通过内联只读 preflight。
`upgrade-accept` 必须新启动正式 preflight，读到
`Result=success`/`ExecMainStatus=0` 且再次验真全部边界后才最后提交 canonical state。
accept 前可用同一 OLD/NEW 和 `--confirm-rollback-this-install` 执行 `rollback`；它只恢复
旧 preflight，因其会继续将 `0710` 报错，所以 rollback 是安全 No-Go，不是恢复启动。
该一次性修复不关闭 ENG-03，也不得将 29.7.2 改标为 `approved`；RF-07 与 RF-12 仍只能
依各自受限证据和责任人批准收口。
该脚本只安装固定仓库资产，不执行 APT、Git、`mkfs`、mount、fstab、Docker、systemctl enable、
secret、`.env` 或 release；唯一例外是窄升级的 `upgrade-accept` 在 installer 锁内同步启动一次
`sms-storage-preflight.service`，绝不启动 Docker、平台、vendor 或维护服务。该命令失败或超时会
保留旧 canonical state 与 upgrade intent；`active/activating/deactivating` 或 `Job` 非空时，
accept/resume/rollback 均失败关闭。排障并确认 preflight 静止且 PID 1 无 pending job 后，只能用
相同 OLD/NEW 重试，禁止手工改写状态。安装后人工核对 `/etc/sms-platform/compose.env` 与 lifecycle 配置，运行
`systemd-analyze verify`、`sms-storage-preflight` 和宿主 Python preflight；首个 bootstrap 前平台与
四个 timer 保持 disabled/inactive。

仅常规首次安装路径在完成上述读回后，人工 `daemon-reload` 并先启动一次
storage preflight，再解除 containerd/Docker 的 mask 并启动二者；两种窄修复路径已由
`upgrade-accept` 同步完成该次预检，禁止手工重复启动。随后立即执行：

```bash
sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/production_host_preflight.py runtime
```

runtime 失败时停止 Docker并调查存储/daemon 配置；不得继续上传/导入离线包或用 raw Compose 验证。

### Gate 3：网络、制品和机密材料

1. 完成 VPN/TLS/防火墙、固定出口、内网 Git、Redis PKI、双渠道告警，以及受控离线包
   受控联网签名/传输链路；内部 Registry 记录为离线通道的退出项目。
2. 在生产边界独立安装 25 件 secret、备份口令、两个 generation ID。ENG-05 闭合后执行其只读
   inventory；再执行
   `sudo /usr/bin/python3 /opt/sms-platform/deploy/scripts/redis_tls_preflight.py`。两者均不得打印
   secret 值、长度、摘要或派生信息；ENG-05 未闭合不得用 `find` 无输出代替通过。
3. ENG-04 闭合后生成并校验第 7.1 节生产 `.env`；不含凭据。ENG-02 闭合后由正式 driver
   `--stage-only` 校验并上传签名封闭包；该动作不执行 Git、prepare、bootstrap 或 secrets 准备。
   任一步失败不得进入 Gate 4。

### Gate 4：预生产同签名包演练

完成发布/迁移、适用 UAT、容量冒烟、存储缺盘失败、三 Redis ACL/TLS/AOF、备份/隔离恢复、
企微+邮件、最小真实厂商链路、12 小时上游积压和旧系统切回。失败必须先修复并生成新候选证据。

### Gate 5：生产首次引导

1. 双人复核 manifest/签名、manifest SHA-256 与闭集逐文件 SHA-256/size、固定公钥/key ID、同包预生产证据、空主机、挂载、secret、固定出口和 No-Go。
2. 只通过 `sms-compose release bootstrap --manifest ... --confirm-empty-host` 建立首个基线。
3. 成功后启用平台、分区、备份和 lifecycle-status timer；生产不启用 restore-drill timer。
4. 仅在 ENG-01 闭合后安装并验证 security-report mailer/collector；collector 固定使用宿主
   `/usr/bin/python3` 和 `/var/log/auth.log`，只有 Runtime 路径、rsyslog、轮转/reopen、脱敏输出
   与受控目录读回通过后才启用 timer。当前仓库字节禁止在生产启用它们。
5. 立即生成首份生产加密备份，在一次性空白隔离恢复机 full restore；恢复证据通过后才
   `init-admin`、首次改密和创建生产 API Key。
6. 只接入 1–2 个低风险 notice 应用，至少观察 3 天。AD、verify、market、更多应用另行变更。

完整 release 命令与唯一边界以 [部署手册](../../deploy/README.md) 为准；本文不授权 raw Git、
raw Compose、通用 `exec`、手工 SQL 恢复、管理员初始化或真实发送。

## 11. 受限生产变更单填写表

下面每行必须在公司受限系统填写真实值；公开仓库只保留状态和不透明证据编号。

| ID | 冻结对象 | 必填字段 | R/A | 状态 | 内部证据编号 | 截止时间 |
|---|---|---|---|---|---|---|
| RF-01 | 第二复核人 | 两名自然人、独立身份、职责和替补 | PLAT/REV、BUS | `unassigned` | 待填 | 待填 |
| RF-02 | OS 模板 | ISO SHA、模板 ID、补丁日期、内核、open-vm-tools、EFI/root 布局、swap 状态 | VMW/PLAT | `requested` | 待填 | 待填 |
| RF-03 | 生产 VM | 资产 ID、cluster/host、12 vCPU、48 GiB、NUMA/内存策略 | VMW/PLAT | `requested` | 待填 | 待填 |
| RF-04 | 五 VMDK | 每盘 asset/controller/datastore/policy/UUID/容量/文件系统、执行人与独立复核人 | VMW/PLAT | `requested` | 待填 | 待填 |
| RF-05 | 预生产 | VM、同拓扑、独立数据/secret、差异清单 | VMW/PLAT | `requested` | 待填 | 待填 |
| RF-06 | 隔离恢复配额 | 申请 SLA、独立 VMDK、无 Send 出站、退役流程 | VMW/DBA | `requested` | 待填 | 待填 |
| RF-07 | 宿主包锁与 Docker daemon | 内部 APT snapshot/签名、公司基线 ID/版本、完整包锁、overlay2、日志、cgroup、ulimit、补丁 SOP | VMW/PLAT | `requested` | 待填 | 待填 |
| RF-08 | 生产制品交付 | 临时离线包生成/签名/固定公钥与 key ID/受控上传及同包预生产证据；未来 Registry namespace、不可变策略、四仓库、只读身份和退出演练 | REL/REL | `unassigned` | 待填 | 待填 |
| RF-09 | 内网 Git | mirror、同步 SLA、只读生产身份、候选 commit | REL/REL | `unassigned` | 待填 | 待填 |
| RF-10 | 受控联网签名节点 | attestation 核验、私钥保管、签名审计和证据保留 | REL/REL | `unassigned` | 待填 | 待填 |
| RF-11 | VPN/TLS/DNS | VM 静态私网 IP/NIC/VLAN/网关/DNS、VPN CIDR、终结器资产/主机/回源/探针、FQDN、证书与续期 | NET/NET | `unassigned` | 待填 | 待填 |
| RF-12 | 防火墙与容器网段 | 入/出站矩阵、DOCKER-USER、可分配至少四个 `/24` 的非重叠 address pool、2375/2376 拒绝、真实读回 | NET/NET | `unassigned` | 待填 | 待填 |
| RF-13 | 厂商出口 | 固定公网 IPv4、书面工单、QPS、号码上限、GetBalance | NET/NET | `unassigned` | 待填 | 待填 |
| RF-14 | Redis PKI | CA、SAN、EKU、到期、轮换人 | SEC/SEC | `unassigned` | 待填 | 待填 |
| RF-15 | 25 secrets | generation、inventory、owner/mode、发放记录 | SEC/SEC | `unassigned` | 待填 | 待填 |
| RF-16 | 恢复材料 | 两 ID、不可变 escrow、双人见证、35 天保留 | SEC/DBA | `unassigned` | 待填 | 待填 |
| RF-17 | 备份容量 | 单份实测、70 generation 投影、扩容结论 | DBA/DBA | `unassigned` | 待填 | 待填 |
| RF-18 | 监控与日志容量 | metrics、VM 外心跳、journal、Docker JSON 轮转、ENG-01 后的 access log 轮转/reopen、值班升级 | MON/MON | `unassigned` | 待填 | 待填 |
| RF-19 | 双渠道告警 | 企微/邮件主收件人、替补、真实测试 | MON/MON | `unassigned` | 待填 | 待填 |
| RF-20 | 首批应用 | 1–2 notice、12h 积压、biz_id、互斥路由、LEGACY 切回卡片 | APP+LEGACY/BUS | `unassigned` | 待填 | 待填 |
| RF-21 | RPO 故障范围 | 每日离机加密备份+隔离恢复，或四方批准同机备份范围和整 VM 数据丢失风险 | DBA/BUS | `unassigned` | 待填 | 待填 |
| RF-22 | 全面容量预算 | PG/三 Redis/Docker/Runtime/OS 增长模型、12h 排空、性能与资源实测、<70% 结论 | DBA+MON/PLAT | `unassigned` | 待填 | 待填 |

状态只允许 `unassigned/requested/delivered/verified/approved/not-applicable`。只有 `verified` 后经
对应 A 批准才能成为 `approved`；不得用口头确认、截图文字或脚本退出 0替代外部资源读回。

## 12. No-Go 与已接受残余风险

以下任一项未闭合即不得生产激活：RF-01 至 RF-22 未全部 `approved`；第二名真实复核人；生产
VM/五 VMDK；签名内部 APT snapshot、宿主安全基线或补丁 SOP；内网 Git，或既无已批准的签名
离线包通道也无已批准的内部 Registry；
VPN/真实 TLS 终结器/VM 静态私网 IP/固定出口/厂商书面白名单；Redis TLS/三域/25 secrets；企微
或公司邮件；同签名包（Registry 路径则同 digest）预生产；首份备份隔离恢复；RPO 故障范围决策；全面容量预算与 12 小时
积压演练；首批应用互斥路由和旧系统切回；任何正式 release 门禁失败。

ENG-01、ENG-02、ENG-04、ENG-05、ENG-06 必须 `closed`。ENG-03 必须 `closed`，或者 T0 候选与
首次安装 commit 的宿主资产 inventory 精确无差异且由 PLAT/REV 明确批准 application-only 例外；
例外不允许任何后续候选改变 wrapper、preflight、unit、drop-in 或宿主配置样例。安全日报尚属
ENG-01 时不能以业务告警已通代替。所有真实 RF 即使文档默认写 `requested/unassigned` 也不得
推断已交付。

首发已接受但必须记录的残余风险：

- 冷备、备出口和跨机房备份尚未建设；RF-21 若选择同机 RPO 范围，整 VM/存储丢失风险另行接受；
- Core、PostgreSQL、三 Redis 共享单 VM、内核、VMDK 和维护窗；
- 当前没有 KMS，使用 root-owned 文件和离机 escrow 补偿；
- dedicated live production restore 尚未封口；事故时围栏新平台并切回旧系统，新平台保持停服；
- AD 延后，首发只使用本地管理员；
- 24 小时/10 万条长稳压测延期，扩大首批范围前补做。

两机房裸光纤、VMware snapshot、Redis AOF、旧系统可切回或文档测试通过，都不能单独证明新平台
高可用、跨机房灾备或生产恢复能力。
