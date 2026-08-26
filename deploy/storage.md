# 生产存储与磁盘失败关闭手册

## 适用范围

本手册只适用于生产专用 VMware 虚拟机。目标是让操作系统、Docker 层、PostgreSQL、
三个 Redis 实例和应用持久态落到已确认的独立 VMDK，并在挂载、容量、权限或映射漂移时
阻止平台启动。`storage_preflight.py` 只读检查，不创建目录、不挂载磁盘，也不执行
`chmod`、`chown`、扩容或任何自动修复。

预生产必须先按同一合同完成一次正常启动和一次缺盘失败演练。共享 Docker 宿主不得安装
本手册的 `docker.service` drop-in。

## 固定 VMDK、挂载点和容量门禁

| 角色 | 固定挂载点 | 标称 VMDK | 预检接受的文件系统下限 | 故障域 |
|---|---|---:|---:|---|
| OS 根 | `/` | 100 GiB OS VMDK | 94 GiB，且不少于根 LV 的 97% | OS VMDK |
| OS `/boot` | `/boot` | 2 GiB 分区 | 1.8 GiB | OS VMDK |
| OS EFI | `/boot/efi` | 1127219200 B 分区 | 0.98 GiB | OS VMDK |
| Docker 层 | `/var/lib/docker` | 250 GiB | 245 GiB | Docker VMDK |
| PostgreSQL | `/var/lib/sms-platform/postgres` | 400 GiB | 392 GiB | PostgreSQL VMDK |
| Redis | `/var/lib/sms-platform/redis` | 100 GiB | 98 GiB | Redis VMDK |
| 应用持久态 | `/var/lib/sms-platform/runtime` | 200 GiB | 196 GiB | Runtime VMDK |

五块 VMDK 中，四个数据挂载点必须对应四块不同的数据盘；`/`、`/boot`、`/boot/efi` 是同一
OS VMDK 上三个不同层级的设备。根与 `/boot` 只允许 `ext4`，EFI 只允许 `vfat`，四块数据盘
只允许 `xfs` 且 `ftype=1`。
这只能证明 guest 内磁盘分离；VMDK 所在 datastore、PVSCSI controller 和 vSphere 放置仍须
在变更单中另附 vCenter 证据。

100 GiB OS VMDK 保留现有 Ubuntu 24.04 GPT/LVM 布局：512 B 逻辑扇区；分区 1 从 sector 2048
开始、1127219200 B、ESP/vfat、挂载 `/boot/efi`；分区 2 从 sector 2203648 开始、2 GiB/ext4、
挂载 `/boot`；分区 3 从 sector 6397952 开始、`LVM2_member`，覆盖余下空间且盘尾差不超过
8 MiB。第三分区只能承载一个 `/dev/mapper/ubuntu--vg-ubuntu--lv` 根 LV，PV/LV 容量差不超过
8 MiB，根 ext4 唯一挂载 `/`。`/swap.img` 固定为根盘上的 8 GiB、`root:root 0600`、非稀疏
普通文件，并且是唯一活动 swap。初始化器只验证这些 OS 对象，绝不改写它们。

表中的 100/250/400/100/200 GiB 是冻结的标称 VMDK 规格，不是格式化后
`statvfs` 必须逐字节相等的容量。四块数据盘保留 2% 文件系统元数据容差；OS 根、`/boot` 与
EFI 使用表中按真实 Ubuntu 布局冻结的独立下限。JSON 用 `capacity_kind` 区分 `vmdk` 与
`partition`，通用标称值是 `nominal_capacity_gib`；兼容字段 `nominal_vmdk_gib` 只在 VMDK
记录中为数值，在 `/boot` 与 EFI 记录中为 `null`，避免容量看板把同一 OS VMDK 重复相加。
`filesystem_capacity_gib` 才是 guest 实测值；guest 无法单独证明 vCenter 中的 VMDK 标称规格，
仍须读回 vCenter 证据。容差不能用于下调 vSphere 中的 VMDK 规格。

生产 Compose override 保留现有命名卷，但必须用 local driver 的 bind 选项映射到下列
固定源目录；不得把服务改回 DockerRootDir 内的匿名目录：

| Compose volume | 固定源目录 | owner | mode |
|---|---|---:|---:|
| `pgdata` | `/var/lib/sms-platform/postgres/pgdata` | `70:70` | `0700` |
| `redisdata` | `/var/lib/sms-platform/redis/broker` | `999:1000` | `0700` |
| `redisauthdata` | `/var/lib/sms-platform/redis/auth` | `999:1000` | `0700` |
| `rediscontroldata` | `/var/lib/sms-platform/redis/control` | `999:1000` | `0700` |
| `importdata` | `/var/lib/sms-platform/runtime/imports` | `10001:10001` | `0700` |
| `exportdata` | `/var/lib/sms-platform/runtime/exports` | `10001:10001` | `0700` |
| `rawspill` | `/var/lib/sms-platform/runtime/raw-spill` | `10001:10001` | `0700` |

生产加密备份根固定为 `/var/lib/sms-platform/runtime/backups`，必须是 `root:root 0700`，
并与 import/export/raw-spill 一样由 Runtime VMDK 承载。它不是 Compose volume，三个
lifecycle systemd service 通过固定 `ReadWritePaths` 使用；不得回退到旧的
`/var/lib/sms-platform/backups`。

挂载点本身固定为 `root:root`：`/`、`/boot`、`/boot/efi` 是 `0755`，`/var/lib/docker` 全生命周期
唯一允许 `0710`，其余三个数据挂载点是 `0750`。预检要求上述 owner/mode 精确匹配；
宽松权限同样失败。当前候选 Docker Engine 29.7.2 会在 daemon 初始化 data-root 时将该目录
收敛为 `0710`；该模式比旧 `0711` 去掉了 other 用户的执行权限，是更严格而不是放宽。
因此初启前后不做分阶段权限切换，也不得为通过预检手工改回 `0711`。这一技术合同
不代表 29.7.2 已获生产批准；候选版本仍须完成同规格预生产与受限变更证据。

## 首次置备

首次置备是有审批的宿主变更，不由应用发布流程代办。执行前停止 Docker，使用 vCenter 与
`lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS` 双向核对每块 VMDK，记录设备序列号、
UUID 和用途。不得根据易漂移的 `/dev/sdX` 名称猜测目标，更不得在含数据设备上执行格式化。

本仓库提供的 [生产宿主磁盘置备工具操作手册](production-storage-initialization.md) 是上述人工
审批变更的专用 root 操作入口，不是应用初始化或发布自动化。其 `plan`/`status` 只读，`apply`
必须绑定 root-only 清单、实时计划摘要和控制 TTY 逐盘确认，并为断电恢复预先固定 XFS UUID；
不得把该工具接入 `sms-compose`、测试服务器更新、systemd 开机任务或任何无人值守流程。

空生产主机的顺序固定为：

1. 确认 `/var/lib/sms-platform` 不存在或为 `root:root 0750`，持久化创建并验空四个固定挂载点；
   四块数据盘分别按专用工具封存的 UUID 格式化为整盘 XFS，不允许分区、LVM 或 ext4。
2. 以 `UUID=` 写入 `/etc/fstab`；禁止 `nofail`、`noauto` 和
   `x-systemd.automount`。以下只是字段模板，尖括号必须替换为已核对 UUID：

   ```fstab
   /dev/disk/by-id/dm-uuid-LVM-<vg+lv-id> /                ext4 defaults                  0 1
   /dev/disk/by-uuid/<boot-uuid> /boot                     ext4 defaults                  0 1
   /dev/disk/by-uuid/<efi-fat-uuid> /boot/efi               vfat defaults                  0 1
   /swap.img             none                               swap sw                        0 0
   UUID=<docker-uuid>   /var/lib/docker                    xfs  defaults,nodev,nosuid     0 2
   UUID=<postgres-uuid> /var/lib/sms-platform/postgres     xfs  defaults,nodev,nosuid     0 2
   UUID=<redis-uuid>    /var/lib/sms-platform/redis        xfs  defaults,nodev,nosuid     0 2
   UUID=<runtime-uuid>  /var/lib/sms-platform/runtime      xfs  defaults,nodev,nosuid     0 2
   ```

3. 逐个挂载并用 `findmnt --target <固定挂载点>` 核对 UUID、文件系统、`rw` 和设备号，并对
   四个 XFS 挂载点逐个用 `xfs_info <固定挂载点>` 核对 `ftype=1`；
   四个数据挂载点的 fstab 与实际 mount options 都必须同时含 `nodev,nosuid`。预检会解析根
   LVM by-id、`/boot`/EFI by-uuid 和四个数据 UUID，并要求各自 block device major:minor 与当前
   mountinfo 一致。`findmnt --verify` 必须零错误；零 warning 直接通过，也允许固定 swapfile
   条目产生唯一的 `non-bind mount source /swap.img is a directory or regular file` warning；
   其它 warning 或错误均失败关闭。
4. 仅在挂载核对成功后，由 root 按上表创建固定子目录及 owner/mode。不得让应用预检代建。
5. 在 Docker 第一次启动前确认 `/var/lib/docker` 确实是 250 GiB VMDK 的挂载点、
   为空，且已是全生命周期固定的 `root:root 0710`。
6. 安装只读预检及 production-only systemd 资产：

   ```bash
   sudo install -o root -g root -m 0755 \
     deploy/scripts/storage_preflight.py /usr/local/sbin/sms-storage-preflight
   sudo install -o root -g root -m 0644 \
     deploy/systemd/sms-storage-preflight.service \
     /etc/systemd/system/sms-storage-preflight.service
   sudo install -d -o root -g root -m 0755 \
     /etc/systemd/system/docker.service.d \
     /etc/systemd/system/sms-platform.service.d
   sudo install -o root -g root -m 0644 \
     deploy/systemd/docker.service.d/10-sms-platform-storage.conf \
     /etc/systemd/system/docker.service.d/10-sms-platform-storage.conf
   sudo install -o root -g root -m 0644 \
     deploy/systemd/sms-platform.service.d/10-storage-preflight.conf \
     /etc/systemd/system/sms-platform.service.d/10-storage-preflight.conf
   sudo systemctl daemon-reload
   sudo systemctl start sms-storage-preflight.service
   ```

最后一条必须成功后才能启动 Docker 和平台。`docker.service` drop-in 保证 Docker 不会在
`/var/lib/docker` VMDK 缺失时退回 OS 盘写数据；平台 drop-in 会在每次 Compose 启动前再
执行一次检查。该只读预检会按角色强制 `/` 为 ext4、四块数据盘为 XFS，并逐盘通过固定
`/usr/sbin/xfs_info` 验证 `ftype=1`；同时要求 `findmnt --verify` 零错误且至多只有上述已知 warning、根盘
dump/pass 为 `0 1`、数据盘为 `0 2`、固定挂载均暴露文件系统根，且受管 UUID/设备不得出现
第二挂载别名。因为 unit 的 `ProtectSystem=strict` 会建立只读私有 mount namespace，脚本固定
读取 PID 1 的 `/proc/1/mountinfo` 判断宿主真实的 `rw` 状态，并在读取前验证 root、systemd、
非容器、VMware 和宿主根边界；不得改回 `/proc/self/mountinfo`。unit 保留
`DevicePolicy=closed`，只为 PVSCSI 数据盘和 LVM 根卷分别增加 `DeviceAllow=block-sd r`、
`DeviceAllow=block-device-mapper r`，并只读暴露 `/dev/disk/by-uuid` 与 `/dev/disk/by-id`；
没有块设备写入或 `mknod` 权限。备份、恢复演练、生命周期巡检和分区维护这些直接或间接
调用预检的 hardened unit 必须保持同一只读授权；其中分区维护不得再用会隐藏宿主块设备的
`PrivateDevices=yes`。

Docker 启动后，生产 override 的七个 local-driver bind mount 会在宿主 mountinfo 中表现为同一
XFS 设备的附加挂载。预检只允许这七个固定 `_data` target，并逐项核对来源父盘 major:minor、
`FSROOT`、XFS、`rw,nodev,nosuid`、规范目录和单一实例；近似路径、错误盘、第八个 bind 或重复
挂载仍立即失败。`/var/lib/docker/volumes` 及七个 volume 父目录只要已经存在，就必须是
canonical、root 控制且与 `/var/lib/docker` 同一设备；这些控制目录本身不得成为独立 mount，
防止卷元数据或目标层静默落到 OS 盘、临时文件系统或其它 VMDK。预检失败不得用
`ExecStartPre=-...`、临时删除依赖或手工执行 Compose 绕过。

如果专用生产主机已在 `109c10865b2aac3989bc4cebf3c60788f44b168c` 完成宿主资产首装并
已技术首启 Docker，从而保留了非空 data-root 元数据，但因旧 preflight 期待 `0711` 而失败，
只能使用 [部署手册](README.md#安装受控包装器与-systemd) 中固定旧 SHA 的一次性、仅
`storage-preflight` 修复流程。该流程不要求 Docker root 为空，但严禁删除或重建其内容；
它也不授权手工 `chmod`、直接覆盖预检文件或开始平台 bootstrap。

## 70/80/90 阈值

预检按 `df` 的非 root 可用空间口径计算每个文件系统使用率。三个 CLI mode 都只读，不会
停止进程、清理文件或扩容：

- `--mode observe`：70/80/90 分别输出 warning/critical/emergency JSON 事件，容量事件本身
  不改变退出码，供外部采集器持续告警。
- `--mode release`：`>=80%` 返回非零，是常规发布 No-Go；生产 `sms-compose release` 的
  变更子命令必须显式使用该模式。
- `--mode startup`：`>=80%` 仍输出 critical 但允许恢复性启动，`>=90%` 返回非零；生产
  systemd 与普通 Compose 启动固定使用该模式。

`>=70%` 当天建立容量工单并确认增长速度；`>=80%` 立即安排在线扩容并暂停批量导入。
不得把删除 WAL、AOF、备份、发布证据或 Docker 数据作为临时腾挪手段。`>=90%` 时现有
进程不会被脚本主动停止，运维需先隔离写入面、扩容并复核，再按正式恢复流程重启。

预检不是常驻监控。上述事件只在脚本被调用时出现；必须由外部监控定时执行
`sms-storage-preflight --mode observe`、采集 JSON/journal 并配置阈值告警。若尚未接入采集，
不得把 systemd 启动时的一次检查声称为持续容量监控。

任何挂载缺失、UUID/fstab 漂移、`nofail`、设备复用、容量低于基线、文件系统/权限错误、
固定子目录越盘或 Docker 内部卷路径被 fstab/symlink 搬迁，也都立即返回非零。

## 在线扩容

只允许扩容，不支持在线或离线缩容。操作前确认最近一次加密备份已通过恢复演练，并记录
当前 `findmnt`、`lsblk -f`、`df -B1` 和预检输出。推荐顺序：

1. 在 vSphere 只增加已核对角色的 VMDK 容量，不新增或替换设备。
2. 在 guest 重新扫描该精确设备，并确认 block device 已看到新容量。
3. 四块整盘 XFS 数据 VMDK 没有 partition/LVM，禁止临时添加。扩 OS VMDK 时必须保持 GPT、
   三个 PARTUUID、三个分区起点、EFI 和 `/boot` 大小不变，只扩大最后一个 LVM 分区；不得照抄
   未核对的 `/dev/sdX`。
4. OS 按同一身份顺序完成最后分区扩展、`pvresize`、根 LV `lvextend`、根 ext4 `resize2fs`；
   不得新增 PV/LV 或只完成其中一层。数据 XFS 对固定挂载点执行 `xfs_growfs <mountpoint>`。
   所有层都只允许扩容，禁止缩容、移动起点或重建 UUID。
5. 重新运行 `/usr/local/sbin/sms-storage-preflight --mode observe`，确认容量、UUID 对应的
   major:minor、owner/mode 和使用率；
   再运行 `/usr/local/sbin/sms-production-storage-init status`，确认同一设备身份、容量没有缩小，
   数据 XFS 已扩到新块设备容量的至少 98%，OS 的 LVM 分区距盘尾、LV 距 PV 均不超过 8 MiB，
   根 ext4 达到 LV 的至少 97%。平台运行期间，status 只接受 production override
   的七个固定 Docker bind mount；其它附加挂载仍判定为 `drifted`；
   再观察 PostgreSQL、三个 Redis AOF、Docker 与应用目录的读写和告警。

扩容期间不得卸载或移动 `/var/lib/docker/volumes`，不得以 VMware snapshot 代替数据库
备份，也不得在未确认文件系统扩展成功时关闭工单。

## 明确禁止

- 禁止对 `/var/lib/docker/volumes` 或其子目录写 fstab mount/bind 条目。
- 禁止用 symlink、`mount --bind`、`mv`、`cp -a` 或 `rsync` 手工/间接搬迁 Docker 内部
  volume 目录；生产 override 只把固定源目录声明给 Docker local volume driver。
- 禁止在独立 VMDK 未挂载时创建同名数据目录并继续启动；这会静默落到 OS 盘。
- 禁止 `nofail`、忽略 preflight 退出码、临时移除 systemd 依赖或绕过 `sms-compose`。
- 禁止自动 prune、删除 PostgreSQL WAL、删除 Redis AOF 或清理恢复/发布证据来跨过阈值。
