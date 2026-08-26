# 生产宿主磁盘置备工具操作手册

## 1. 定位与不可替代的人工边界

`initialize_production_storage.py` 是一次性的生产宿主管理员工具，不是应用初始化脚本，也不是
Compose、代码发布、快速更新或开机自动任务。它只允许在已审批的 VMware 生产专用 VM 上由
root 人工执行。

脚本尽可能失败关闭，但不能证明 vCenter 管理员没有在格式化瞬间热替换 VMDK，也不能从 guest
内部绝对证明“无已知签名且抽样为零”的磁盘从未存过未知数据。执行前仍必须由变更单证明四块
数据 VMDK 是新建盘，并完成 vCenter 资产 ID、controller/unit、datastore、容量、guest by-id 和
guest serial 的逐盘双向核对。vCenter 必须按公司模板启用 VMDK UUID 向 guest 暴露；常见配置是
`disk.EnableUUID=TRUE`，若公司模板使用等价机制，则以公司实现为准，但必须实证产生唯一、重启
稳定的 by-id 与 serial。维护窗内禁止热插拔或调整 SCSI unit。

脚本不提供 `--force`、`--yes`、`--device`、清签名、分区、LVM、自动停止服务、自动卸载、
回滚格式化或非交互执行入口。格式化不可回滚；中断后只能 `resume` 同一计划，或停止并走新的
人工恢复变更。

## 2. 固定合同

| 角色 | 原始 VMDK 精确容量 | 布局 | 文件系统 | 挂载点 |
|---|---:|---|---|---|
| OS | 不少于 107374182400 B | GPT：512 MiB EFI + 其余单一根分区；无 LVM/swap/独立 `/boot` | ext4 | `/` |
| Docker | 268435456000 B | 整盘，无分区、无 LVM | XFS，`ftype=1` | `/var/lib/docker` |
| PostgreSQL | 429496729600 B | 整盘，无分区、无 LVM | XFS，`ftype=1` | `/var/lib/sms-platform/postgres` |
| Redis | 107374182400 B | 整盘，无分区、无 LVM | XFS，`ftype=1` | `/var/lib/sms-platform/redis` |
| Runtime | 214748364800 B | 整盘，无分区、无 LVM | XFS，`ftype=1` | `/var/lib/sms-platform/runtime` |

数据盘在首次格式化时要求原始字节数精确匹配，不能把格式化后文件系统允许的 2% 元数据容差
用于识别 VMDK。脚本要求 guest 中恰好有五块 `TYPE=disk`：一块明确的 OS 盘和清单点名的四块
数据盘；存在额外未知磁盘时失败。

OS 盘绝不写入，但 plan 会验证 GPT、512 MiB EFI System Partition、单一直接 ext4 根分区、根
UUID 与 fstab 一致、无 LVM/独立 `/boot`/活动 swap，且根文件系统已扩到 OS VMDK 的至少 98%。
`/var/lib/sms-platform` 若预先存在，必须已经是 `root:root 0750`；四个挂载点必须不存在或为空且
精确满足固定 owner/mode。

四个数据盘统一执行等价于以下固定参数的 XFS 创建；实际设备和 UUID只能来自封存计划：

```text
mkfs.xfs -q -L <固定短标签> -m uuid=<计划UUIDv4> -n ftype=1 <稳定-by-id>
```

命令没有 `-f`。计划提前固定 UUID，是为了覆盖“`mkfs` 已成功但 checkpoint 尚未写回”的断电
窗口；`resume` 只能接管 UUID、label、XFS 类型和稳定设备身份全部精确匹配的文件系统。

## 3. 执行前准备

### 3.1 主机前提

- Ubuntu Server 24.04 LTS（`VERSION_CODENAME=noble`）、固定 `/usr/bin/python3` 3.12、PID 1
  为 systemd，且当前 `/` 与 `/proc/1/root` 必须是同一目录对象；`systemd-detect-virt` 必须明确
  报告 `vmware` 且不属于 container；chroot/container/private mount namespace 一律拒绝。
- 时区必须是 `Asia/Shanghai`，`timedatectl` 的 `NTPSynchronized=yes`；不满足时 plan 失败。
- `/` 满足第 2 节 OS 拓扑合同，实测文件系统容量不少于 OS VMDK 的 98%，`root:root 0755`。
- 五块 VMDK 已在 vCenter 完成复核；四块数据盘是新建空盘。
- 每块 VMDK 必须有唯一的直接 `/dev/disk/by-id/...` 和非空 serial；关机重启并调整一次非生产
  SCSI unit 后仍能按同一 VMDK 身份读回。正式维护窗内冻结磁盘拓扑。
- `docker.service`、`docker.socket`、`containerd.service` 和 `sms-platform.service` 必须是
  `masked` 或不存在，并且不处于 active。脚本不会代停或代 mask。
- 主机已有 `xfsprogs`、`util-linux`、`udev`、`systemd` 提供的固定工具：`mkfs.xfs`、
  `xfs_info`、`lsblk`、`blkid`、`wipefs`、`findmnt`、`mount`、`udevadm`、`systemctl`、
  `systemd-detect-virt`、`timedatectl`。
- Ubuntu 24.04 的 glibc/kernel 必须提供 `renameat2(RENAME_NOREPLACE)`；plan 会先验证该原子
  “仅不存在时发布”能力，缺失时在任何格式化前失败。
- 执行人有控制 TTY；SSH 使用正常的交互式 TTY。管道、CI 和无 TTY sudo 不允许 apply。

### 3.2 只读采集

由 VMware 管理员与执行人共同记录以下输出；不要从 `/dev/sdX` 顺序推断角色：

```bash
sudo /usr/bin/lsblk --bytes --paths \
  --output NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,SERIAL,WWN,RO,RM,MOUNTPOINTS,PTTYPE,PARTTYPE
sudo /usr/bin/findmnt --target /
sudo /usr/sbin/blkid
sudo /usr/sbin/wipefs --no-act --json /dev/disk/by-id/<逐盘核对的设备>
sudo /usr/bin/timedatectl status
sudo /usr/bin/timedatectl show --property=Timezone --property=NTPSynchronized
```

必须使用 `/dev/disk/by-id/` 的直接子项；拒绝 `/dev/sdX`、`*-partN`、相对路径、多级 symlink、
loop、dm、md、multipath、只读盘、可移动盘、分区、holder、挂载、swap 或任何已知签名。
必须将 vCenter 五盘映射、上述输出、关机重启后的 by-id/serial 读回和 `disk.EnableUUID` 或公司
等价配置的证据编号写入变更单；缺任一项不得进入 plan。

### 3.3 安装固定工具和清单

先从已经评审的精确 commit 安装脚本到 root 控制的位置。后续 plan/apply/status 都运行安装后的
文件，不直接 sudo 执行 Git checkout 内的副本：

```bash
git -C /opt/sms-platform rev-parse --verify HEAD
git -C /opt/sms-platform status --short
git -C /opt/sms-platform diff --quiet
git -C /opt/sms-platform diff --cached --quiet
sha256sum /opt/sms-platform/deploy/scripts/initialize_production_storage.py \
  /opt/sms-platform/deploy/production-storage-manifest.example.json
sudo /usr/bin/install -d -o root -g root -m 0700 /etc/sms-platform
sudo /usr/bin/install -o root -g root -m 0755 \
  /opt/sms-platform/deploy/scripts/initialize_production_storage.py \
  /usr/local/sbin/sms-production-storage-init
sudo /usr/bin/install -o root -g root -m 0600 \
  /opt/sms-platform/deploy/production-storage-manifest.example.json \
  /etc/sms-platform/production-storage-manifest.json
sudo /usr/bin/sha256sum /usr/local/sbin/sms-production-storage-init
```

`HEAD` 必须是变更单已批准的完整 40 位 commit，`status --short` 无输出，两个 `diff --quiet`
均退出 0；源脚本 SHA-256 必须与同脚本在 disposable 预生产 VMware 演练的证据一致。任一读回
不符就停止，不得从其他 checkout 临时复制。

使用 `sudoedit /etc/sms-platform/production-storage-manifest.json` 填入变更单号、复核人、最多
24 小时后的带时区过期时间、五盘 by-id 与 serial。为四块数据盘分别执行一次以下固定宿主
Python 命令，把四个互不相同的小写 UUIDv4 固定写入清单；无需额外安装 `uuid-runtime`：

```bash
/usr/bin/python3 -I -c 'import uuid; print(uuid.uuid4())'
```

OS 盘没有 `filesystem_uuid` 字段。示例中的
`REPLACE-*` 占位符故意不可通过校验。

清单必须保持 `root:root 0600`。脚本的计划摘要绑定清单原始 SHA-256、脚本 SHA-256、
machine-id 摘要、VM product UUID 摘要、当前 boot-id、`fstab` inode/摘要和五盘身份。

## 4. 固定执行流程

### 4.1 Plan：只读

先创建受限证据目录；下面的 `CHG-20260825-001` 必须替换为已经通过清单字符集校验的真实变更
单号，不得使用变量、通配符或命令替换：

```bash
sudo /usr/bin/install -d -o root -g root -m 0700 \
  /var/log/sms-platform/storage-init/CHG-20260825-001
sudo /bin/sh -c 'umask 077; exec /usr/local/sbin/sms-production-storage-init --manifest /etc/sms-platform/production-storage-manifest.json plan > /var/log/sms-platform/storage-init/CHG-20260825-001/plan.json'
sudo /usr/bin/stat -c '%U:%G %a %n' \
  /var/log/sms-platform/storage-init/CHG-20260825-001/plan.json
sudo /usr/bin/jq . \
  /var/log/sms-platform/storage-init/CHG-20260825-001/plan.json
```

`plan` 不创建锁、状态、目录、文件系统、fstab 条目或挂载。它会一次性检查完全部五盘、服务、
工具、宿主边界、挂载点、fstab 和数据盘签名，并对每块空数据盘读取十个分布式 1 MiB 样本；
任何一个条件失败都不会执行第一个 `mkfs`。

证据文件必须读回为 `root:root 600`。逐项复核 `role`、`by_id`、`serial`、`major_minor`、容量、
目标 UUID、挂载点和 `plan_sha256`。计划未通过或证据未复核，不得执行 apply。
plan、apply 和最终 initialized status 必须退出 0；恢复前的 status 按第 4.3 节可能预期退出 1，
必须以 JSON 状态和表格共同判定。失败命令可能留下 0600 的空文件；无论退出码如何，空文件都
不构成证据，也不得据此继续下一步。

### 4.2 Apply：不可逆

将下面摘要替换为 plan 文件中读回的完整 64 位小写摘要；命令仍直接从 `/dev/tty` 逐盘确认：

```bash
sudo /bin/sh -c 'umask 077; exec /usr/local/sbin/sms-production-storage-init --manifest /etc/sms-platform/production-storage-manifest.json apply --plan-sha256 REPLACE_WITH_PLAN_SHA256 > /var/log/sms-platform/storage-init/CHG-20260825-001/apply.json'
sudo /usr/bin/stat -c '%U:%G %a %n' \
  /var/log/sms-platform/storage-init/CHG-20260825-001/apply.json
sudo /usr/bin/jq . \
  /var/log/sms-platform/storage-init/CHG-20260825-001/apply.json
```

apply 先完整重跑 plan；摘要不同就退出。随后只从 `/dev/tty` 依次要求重新输入 Docker、
PostgreSQL、Redis、Runtime 四盘完整 serial，以及 plan 输出的最终 `ERASE-4-DATA-DISKS-*`
令牌。确认完成后再获取排他锁；锁内第三次读回完整计划，仍一致才写 durable intent 并进入
不可逆阶段。

固定步骤为：持久化创建并验空固定挂载点 → 逐盘再次验真并打开固定块设备句柄 → 让 `mkfs.xfs`
通过继承的 `/proc/self/fd/N` 处理同一块设备，同时继承初始化锁 → 使用计划 UUID/label 格式化 →
读回 XFS → 原子准备 fstab →
用单次 `RENAME_NOREPLACE` 保留并强制落盘 `root:root 0600` 原 fstab 备份 → 拒绝 fstab 目标
路径 symlink/别名以及非 root 控制的可写路径链 → `findmnt --verify` 必须零错误且零警告 →
原子替换并强制落盘 fstab → 只按四个
固定挂载点逐个 mount → 读回 UUID/major:minor/options/容量 → 设置挂载根权限 → 创建八个固定
目录 → 逐盘验证四个 XFS 均为 `ftype=1` → 最后写 state → 清除 intent。

脚本从不运行 `mount -a`，避免触碰 fstab 中无关文件系统。数据盘固定 fstab 条目为：

```fstab
UUID=<docker-uuid>   /var/lib/docker                    xfs defaults,nodev,nosuid 0 2
UUID=<postgres-uuid> /var/lib/sms-platform/postgres     xfs defaults,nodev,nosuid 0 2
UUID=<redis-uuid>    /var/lib/sms-platform/redis        xfs defaults,nodev,nosuid 0 2
UUID=<runtime-uuid>  /var/lib/sms-platform/runtime      xfs defaults,nodev,nosuid 0 2
```

### 4.3 Status：只读

```bash
sudo /bin/sh -c 'umask 077; exec /usr/local/sbin/sms-production-storage-init status > /var/log/sms-platform/storage-init/CHG-20260825-001/status.json'
sudo /usr/bin/stat -c '%U:%G %a %n' \
  /var/log/sms-platform/storage-init/CHG-20260825-001/status.json
sudo /usr/bin/jq . \
  /var/log/sms-platform/storage-init/CHG-20260825-001/status.json
```

状态含义：

| status | 退出码 | 含义 |
|---|---:|---|
| `initialized` | 0 | state、脚本/VM 身份、设备、UUID、fstab、挂载、容量、权限、目录和 ftype 全部读回通过 |
| `absent` | 1 | 没有 intent/state，尚未执行 |
| `in_progress` | 1 | 存在合法未完成 intent，必须检查后 resume |
| `finalization_required` | 1 | 最终 state 已写，intent 尚未清除，需 resume 完成提交 |
| `unsafe_partial` | 1 | intent 无法可信解释，停止并人工处置 |
| `drifted` | 1 | state 存在但任一实时证据不匹配，不能继续部署 |
| `blocked` | 1 | 当前动作被固定 `safe_code` 阻断 |

只有 `initialized` 才允许继续安装生产宿主资产和启动 Docker。status 不创建锁或做自动修复。
平台启动后且不存在恢复 intent 时，completed status 只把 production override 声明的七个精确
Docker `_data` bind 视为合法，并逐项校验目标、FSROOT、XFS、设备号及 `rw,nodev,nosuid`；
其卷控制目录必须仍在 Docker VMDK 上且不得自身成为嵌套挂载。apply、resume 和
`finalization_required` 状态始终不放宽这一限制。
完成后若按正式扩容工单增大原 VMDK，status 接受同一 by-id/serial/wwn 且当前容量不低于初始
合同，并要求 XFS 当前容量位于块设备的 98%–100%；仅增大 VMDK 而未 grow、或文件系统反而
大于底层设备都会是 `drifted`。工具不写扩容高水位；每次扩容前后容量和“禁止缩容”的历史
约束必须由正式扩容工单、vCenter 事件和监控证据保留。

## 5. 中断恢复

先保持四个服务 masked/stopped，禁止手工再次 mkfs、wipe、改 UUID、改 label、改 fstab 或卸载，
执行只读状态：

```bash
sudo /bin/sh -c 'umask 077; exec /usr/local/sbin/sms-production-storage-init status > /var/log/sms-platform/storage-init/CHG-20260825-001/recovery-status.json'
sudo /usr/bin/stat -c '%U:%G %a %n' \
  /var/log/sms-platform/storage-init/CHG-20260825-001/recovery-status.json
```

若为 `in_progress` 或 `finalization_required`，在确认 by-id、serial、变更单和 plan 摘要仍是原值后：

```bash
sudo /bin/sh -c 'umask 077; exec /usr/local/sbin/sms-production-storage-init --manifest /etc/sms-platform/production-storage-manifest.json resume --plan-sha256 REPLACE_WITH_ORIGINAL_PLAN_SHA256 > /var/log/sms-platform/storage-init/CHG-20260825-001/resume.json'
sudo /usr/bin/stat -c '%U:%G %a %n' \
  /var/log/sms-platform/storage-init/CHG-20260825-001/resume.json
sudo /usr/bin/jq . \
  /var/log/sms-platform/storage-init/CHG-20260825-001/resume.json
```

resume 仍要求控制 TTY 和四盘 serial。空白盘可以继续；已经具有计划 UUID、label 和 XFS 类型的
盘只会读回，不会第二次格式化；任何部分签名或不完全匹配都会返回 `unsafe_partial_filesystem`，
必须停止并建立新的人工恢复变更。脚本不自动删除 fstab、不卸载、不 wipe，也不伪造“格式化
回滚”。

resume 在交互确认前和取得排他锁后都会重新比较安装脚本 SHA-256、machine-id 摘要与 VMware
product UUID 摘要；三者任一变化就拒绝。正常断电重启会改变 boot-id，只有这一项允许变化，
并继续由当前实时计划和设备读回约束。

初次 plan/apply 严格拒绝过期清单。若 durable intent 已在清单有效期内建立，恢复跨过 24 小时，
resume 只允许继续使用原始字节完全不变且 SHA-256 与 intent 精确匹配的清单；不得修改
`not_after`、重新生成 UUID 或另存新清单。这个例外只用于同一不可逆 intent 的收敛，不允许开始
新的格式化计划。

## 6. 完成后证据与后续步骤

至少归档：manifest 摘要、脚本摘要、plan JSON、apply JSON、最终 status JSON、vCenter 五盘映射、
fstab 备份路径、`lsblk -f`、逐盘 `findmnt`、`df -B1`、四个固定挂载点各自的 `xfs_info` 和
执行/复核人。
实际 manifest 含内部资产标识，不提交 Git。

随后按 [生产存储手册](storage.md) 安装只读 `storage_preflight` 和 production-only systemd 资产；
其 startup 模式通过前，Docker 与平台继续保持 masked/stopped。

正式生产执行前，必须在一次性预生产 VMware VM 和四块 disposable VMDK 上至少完成：首次
plan/apply/status、首盘 mkfs 后强制中断并 resume、fstab replace 后强制中断并 resume、调整
SCSI unit 后 by-id/UUID 读回、移除一块盘后的启动失败关闭、重新接回后的恢复、确认
`/proc/self/fd/N` 方式可被 Noble 的 `mkfs.xfs` 接受、在 mkfs 期间强制终止父进程后第二个
resume 仍被继承锁阻断、在目录创建/backup/state 原子发布边界分别断电并成功 resume，以及确认
第五块 decoy 盘从未发生写入。还必须按仓库 unit 原样安装并实际启动
`sms-storage-preflight.service`，证明 `ProtectSystem=strict` 下读取的是 PID 1 宿主 mountinfo、
`DeviceAllow=block-sd r` 足以让 `findmnt --verify` 零警告通过且没有块设备写权限；生产 override
创建七个固定 Docker bind 后预检仍通过，增加第八个/错误设备 bind 或把宿主数据盘改为只读时
必须失败关闭；备份、恢复演练、生命周期巡检和分区维护 unit 中的同一预检也必须实际通过。
普通 pytest 和 loop device 不能替代这项 VMware/PVSCSI 演练；
该演练未签字通过前，禁止在正式 VM 执行 apply。
