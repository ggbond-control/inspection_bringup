# D1M-A 导航构建改造与验证

日期：2026-10-22。对应改造前问题见 [评估文档](D1M_A_NAVIGATION_BUILD_ASSESSMENT.md)。

## 改造

- `build_navigation.sh` 保留为统一入口，参数、环境、包发现和阶段编排集中到 `navigation_build.py`。
- 构建配置使用 `config/build/navigation.yaml` 和由 `robot.yaml` 选择的机型配置；D1M 显式加载 Zenoh，开启 D1 Max 后端。
- 默认顺序为系统/输入预检、公共接口、第三方库、驱动、算法。通过 `local_setup.bash` 显式组织 underlay。
- 补齐 inspection_interfaces、robots_dog_msgs、gnss_driver 和 smac_straight_planner 的清单。
- 使用 colcon 发现 managed 仓库内的包，遵守 COLCON_IGNORE，并检查空列表/缺失包/失败状态。
- 新增 `--plan`、`--check`、`--output-root`；保留原有路径、更新、jobs 和阶段跳过选项。
- 成功构建记录源码 commit、dirty 状态、RMW、包列表和第三方文件哈希。

GTSAM 已安装时默认不执行其 configure/build/install；Livox-SDK2 同样复用。首次缺少安装时初始化，GTSAM 安装不完整则明确要求修复。只有 `--rebuild-gtsam` / `--rebuild-third-party` 会显式进入重编流程。第三方前缀有独立锁，防止不同 ROS 输出目录同时初始化同一份库。

### nav_bridge 机型支持边界

默认驱动阶段包含 `nav_bridge`。当前只有 `config/build/d1m.yaml` 显式选择后端：D1 Max ON、X30 OFF、Cyvet OFF。`robot.yaml` 的 `model`（或本次命令的 `--model`）选择构建配置；`config/models/<model>/navigate.yaml` 中的 `nav_bridge.robot_type` 则用于运行时，两者不能相互替代。

X30、Cyvet 尚无专属构建配置。全新 nav_bridge CMake 配置默认 X30 ON、其余 OFF；从 D1M 切换到无构建配置的型号时，旧缓存开关可能继续生效，`--cmake-force-configure` 不会自动重置它们。Cyvet 还依赖 `uniubi` 和 `uniubi_motion_client`，当前 vendored UniUbi 目录的 `COLCON_IGNORE` 会排除这些包。因此本次通过的是 D1M 构建，不代表三机型均已支持。

后续完善应为每个型号显式设置全部三个开关、对缺失/未知机型配置提前报错，并补齐 Cyvet SDK/消息依赖的构建与加载验证。跨型号最好使用独立输出目录，避免已安装的旧型号可执行文件残留造成误判。机型切换不应触发 GTSAM 重编。

## 本地验证

```bash
python3 test/build_scripts_test.py
bash -n scripts/build_navigation.sh
python3 -m py_compile scripts/navigation_build.py scripts/update_repositories.py
git diff --check
```

构建/更新集成测试 21 项通过，覆盖默认接口编排、jobs 策略、普通 Git 更新、GTSAM 首次初始化/默认复用/显式重编/不完整安装、失败前置检查、环境清理，以及真实 colcon 的 COLCON_IGNORE 行为。

额外执行 `test/config_test.py`：51 项中存在 8 个失败条目。将未修改的 `git archive HEAD` 解压到临时目录复测，出现完全相同的 8 个失败条目，属于已有配置与固定参考样本不一致。本次未修改这些导航/传感器运行参数及其参考样本。

## 实机验证设置

设备 Ubuntu 24.04.5 / aarch64 / ROS 2 Jazzy。保留设备当前源码分支和本地配置，不执行 pull/checkout。验证使用独立 ROS 输出目录，复用设备已安装的 GTSAM/Livox-SDK2：

```bash
env -i HOME=/home/cat USER=cat PATH=/usr/bin:/bin \
  bash --noprofile --norc \
  /home/cat/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh \
  --output-root /home/cat/tmp/navigation_build_validation_20261022
```

`--output-root` 只用于隔离本次验证产物，日常在默认目录构建不需要此参数。

- 原文件备份：`/home/cat/tmp/navigation_build_backup_20261022/original_files.tar.gz`。
- GTSAM 验证前哈希与 mtime：同目录 `gtsam_before.json`。
- 首次构建日志：`/home/cat/tmp/navigation_build_validation_20261022/build.log`。
- 完成状态：同目录 `exit_code`。

首次预检在无 shell 配置的环境中通过。已核对独立 nav_bridge CMake cache：`rmw_zenoh_cpp`、D1 Max ON、X30/Cyvet OFF，搜索路径没有旧 task/algor/driver ROS install。

相同的 21 项构建/更新测试也在 D1M-A 上通过，耗时约 33 秒。首次编译 `faster_lio` 时，观测到单个编译进程 RSS 约 4.3 GiB，因此该 8 GiB 设备保留默认 `-j1`，不建议盲目增加并发。

### 最终结果

| 验证项 | 结果 |
| --- | --- |
| 首次独立 ROS 输出构建 | 返回 0，2208.7 秒（约 36 分 49 秒）；接口 1 包、驱动 6 包、算法 5 包 |
| 相同命令增量重跑 | 返回 0，94.2 秒；GTSAM、Livox-SDK2 均打印 reuse |
| ROS 包路径 | 12 个包全部解析至本次隔离输出目录 |
| 动态链接检查 | 81 个 ELF 文件的 `ldd` 全部成功，无 `not found` |
| D1M 后端 | `d1_max_nav_bridge_node` 存在，CMake 三个后端开关符合 D1M 配置 |
| 规划器插件 | XML 包含 `smac_straight_planner::SmacStraightPlanner`，插件共享库 `dlopen` 成功 |
| 缓存环境 | 隔离 CMake cache 不含旧 task/driver/algor install；记录的 RMW 均为 Zenoh |
| GTSAM 默认复用 | `libgtsam.so.4.2.0`、`libgtsam_unstable.so.4.2.0` 和原第三方 `CMakeCache.txt` 的 SHA-256、mtime_ns 全部与验证前一致 |
| 原服务 | `inspection-navigation`、`inspection-system` 均保持 active |

增量日志为同目录 `incremental.log`，返回码为 `incremental_exit_code`；产物检查日志为 `artifact_validation.log`，详细证据为 `artifact_validation.json`，返回码为 `artifact_exit_code`（均为 0）。首次/增量构建中的 stderr 提示未导致构建失败，产物链接与插件加载检查也已通过。

本次未重启服务、未把服务切换到隔离产物、未执行机器人运动；上述结果验证构建及静态产物的依赖/加载，不代表完成导航运行与硬件验收。日常默认目录构建的原有产物也未被本次隔离验证覆盖。

## 使用

```bash
# 日常构建，无必填附加参数
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh

# 查看计划 / 预检
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh --plan
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh --check

# 仅在明确需要重编 GTSAM 时使用
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh --rebuild-gtsam
```

本次实机构建基于 D1M-A 当前各仓库源码，包含已有 dev 分支和 GNSS 配置修改。独立输出构建验证的是不依赖旧 ROS 安装产物；不等同于已验证所有远端 jazzy 分支的全新 clone 组合。发布前应以成功构建记录的 commit 集合建立固定版本清单。
