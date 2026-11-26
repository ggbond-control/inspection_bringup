# 导航参数来源与 D1M-B 比对

这些参数最初取自 D1M-B 的子模块 Git HEAD，来源记录见下表。
nav_bridge 的完整参数位于 models/<model>/navigation/param_<model>_nav_bridge.yaml，
没有公共 default。型号 navigate.yaml 选择 robot_type（x30/d1m/cyvet）及 config；
单机 parameters 覆盖后生成快照。Cyvet 参数来自 nav_bridge/config/cyvet_params.yaml。
Livox 同样没有公共 default；livox.config 选择型号目录中的完整 JSON。
X30 保留四雷达 MID360 网络及外参，Cyvet 默认使用 MID360s，另存 MID360 备选。
D1M 不配置 JSON、不启动 Livox。bringup 不再使用 livox.model；子 launch 接收 user_config_path。
faster_lio 已整理为 `param_default_faster_lio.yaml`，以 X30/Mid360 值为准，
同时包含 Airy 独有的完整 RTK 和 IMU 加速度参数；RTK 默认关闭。
加载时叠加 `models/<model>/navigation/param_<model>_faster_lio.yaml`，
最后叠加 robot.yaml 的 navigation.slam.parameters，再生成参数快照。
D1M 的型号文件只开启 RTK 并覆盖 Airy 差异；X30/Cyvet 继承 Mid360 默认。
所有型号的初始配准 score_threshold 统一为 0.5，position 统一为 [0, 0, 0]。
原始两份完整配置移至 test/fixtures/faster_lio，仅作为迁移回归基准。
gridmapper 使用 param_default_gridmapper_local/global.yaml 和可选型号差异。
multi_map_nav 使用 param_default_multi_map_nav.yaml：X30/Cyvet 叠加各自的
minimum_turning_radius 参数，D1M 叠加插件、膨胀参数和专属 straightening 配置。
各型号合并结果以原 new_local/straight_local 为基准，并将普通目标的 XY/yaw
容差覆盖为 0.25；global_costmap 的 footprint 和这两个容差在各型号文件中显式填写。
其他尚未迁移模块仍沿用原文件名和同相对路径的型号覆盖规则。

全局重规划默认 `periodic`，沿用原 Nav2 流程。可在 robot.yaml 中只覆盖一个模式字段：

```yaml
navigation:
  global_planner:
    parameters:
      multi_map_nav_node:
        ros__parameters:
          global_replanning_mode: event_driven
```

新模式正常只在新目标/新导航段规划，持续局部阻塞或明显偏离路径时有限重规划。
同处 `event_replan_*` 参数控制每段重试次数、冷却、规划超时、持续恢复超时和偏离判定。
`event_replan_server_timeout_ms` 默认 1000 毫秒，控制事件行为树中规划和跟随 action 的
请求应答等待。该值必须为正数且小于 `event_replan_planning_timeout` 换算后的毫秒数，
整体规划期限默认 10 秒。

持续封堵重规划默认关闭。可在同一 `ros__parameters` 下设
`global_blockage_replan: true`，并用 `global_blockage_confirm_sec` 设置路线持续封堵且局部
无路的确认时间（默认 20.0 秒，须为有限正数）。开关适用于 `periodic` 和
`event_driven`。开启后，子 launch 加载事件行为树、专用进展检测器和全局代价地图的
`TerrainObstacleLayer`。局部规划器按导航段接收参数并上报确认的封堵。

`global_blockage_max_wait_sec` 默认 60.0 秒，须大于 `global_blockage_confirm_sec`。
它是封堵确认阶段的独立总等待上限，防止观测变化或路径闪烁造成无限等待；
代表障碍点变化不再重置确认计时。超时仍未处理则失败并交给任务层。
此参数与模式2入图后的 `global_blockage_clear_sec` 是两个不同阶段。

同处配置 `global_blockage_clear_mode`（默认 1）：1 保留至连续三次观测畅通；
2 从每批封堵实际入图起计时，达到 `global_blockage_clear_sec`（默认 60.0 秒）后清除，
若这批封堵的首次重规划未结束则暂缓；3 在当前任务航点导航成功且局部到达交接完成后清除。
算出路径、局部预瞄点到达、失败、超时、取消和普通周期规划均不触发模式3清除。
三种模式都支持畅通观测提前清除，换图和定位重置时清除旧记录。
清除只撤销对应临时封堵，保留其他重叠封堵和静态地图障碍。
封堵重规划接受后保持绕行路线至当前任务航点结束；新封堵仍可触发替换。
净移动0.25m只结束持续恢复计时，不恢复普通周期换路。已删除障碍远侧的几何判断。
需同时部署新版 `multi_map_nav` 与 `local_planner`。
参数在重启导航后生效；`global_blockage_replan: false` 时不加载该策略。

在机器狗端的 `nav` 模式使用时，需配套部署支持该功能的 `multi_map_nav` 和
`local_planner`，由 terrain 模块提供地形数据。修改 `robot.yaml` 后重启
`navigation` 服务以重新生成参数快照。

local_planner 使用 `param_default_local_planner.yaml` 和各型号的
`param_<model>_local_planner.yaml`。公共参数以 X30 部署值为基准，
odomTimestampCheckEn 默认 true，D1M 型号层设为 false；
initialModel、flat、offroad 仅在型号文件中维护。
X30/Cyvet 默认跟随模式为 omni。D1M / D1_MAX 型号层默认 turn_feasible，
tracking_mode 和底盘能力表只维护在型号文件的 `/**.ros__parameters` 一处，
localPlanner 与 pathFollower 各自读取同一份参数：

```yaml
tracking_mode: turn_feasible
turn_forward_speed_samples: [0.0]
turn_lateral_speed_samples: [0.5]
turn_lateral_zero_above_speed: 1.0
turn_yaw_forward_speed_samples: [0.0, 1.0, 2.0]
turn_yaw_rate_samples: [1.5, 1.0, 0.5]
```

角速度表按前速绝对值分档，1/2m/s 分界点属于下一档，不插值；仍受模型 maxYawRate 限制。
这是高速档能力包络，在低/中速档偏保守；offroad.maxSpeed 保持2.2m/s，适用于高速巡航档，
中速档控制上限应不超过2m/s。加减速度不变。使用前需配套支持该能力表的 local_planner
以及修复了 SDK 速度换算的 nav_bridge。

local_planner 的参数合并保持“公共默认 → 型号 → 单机”的层级优先顺序：
高层公共块会移除低层两个控制节点下的同名覆盖项；同一层显式节点参数仍优先。
因此单机可以在 `navigation.local_planner.parameters./**.ros__parameters` 只修改一次能力表或模式，
无需复制到两个节点下。节点专用参数和其他模块的 ROS 参数合并规则保持原样。
Cyvet 的传感器偏移、运动模型、车体尺寸、路径对应文件及跟随参数按提供的实机配置维护。
原始完整参数移至 test/fixtures/local_planner 用于回归比对。

安全停车参数也在公共默认文件中维护：local_planner 文件包含
localPlanner 的 terrainTimeout/localizationHealthTimeout 和原名节点
cmd_vel_guard 的六项参数；faster_lio 文件包含 navigation_health 的超时、RTK质量、
三维位置速度上限（20m/s）及独立的 lidar 健康检查参数。
lidar 健康门限默认与本文件重定位的匹配质量门限一致（400/200点、0.15比例、
0.25m残差、0.5m/5度修正、连续恢复5帧），但不读取或联动 rtk 参数。
沿用型号层和 robot.yaml 的 parameters 覆盖。
local_planner 子 launch 将合并文件同时传给三个节点；门控包内
navigation_safety.yaml 仅提供后备默认值。编辑单机参数后重启相关导航进程。
不同模块的超时仍各自生效，配置示例及含义见 CONFIGURATION.md 的“安全停车参数”。

| ROS 包 | 子模块提交 | 公共基线来源 |
| --- | --- | --- |
| `nav_bridge` | `e7b00010fb2e3e83f6d4b6886e8e294c53151a64` | `config/x30_params.yaml`、`config/d1_max_params.yaml` |
| `faster_lio` | `da0df49438ed3639eb5f1f912d865cb0070ca1f4` | `config/mid360.yaml`、`config/airy.yaml` |
| `gridmapper` | `8c268b4b578761246612a58a6ee5ce9688ac3a12` | `config/gridmapper_local.yaml`、`config/gridmapper_global.yaml` |
| `local_planner` | `b40bd624e2708b7d50cab14ef44b6f68898c5979` | `config/x30.yaml`、`config/d1m.yaml` |
| `multi_map_nav` | `56a562a296d5b0c47b8b06ad98d55a9928a8f8d8` | `params/new_local.yaml`、`params/straight_local.yaml` |
| `livox_ros_driver2` | `07a9ebe09e76a8a616f19e099c034169cc79a070` | `config/multi_MID360_config.json` |

D1M 型号层按实机未提交差异记录了：

| 实机差异 | bringup 承接位置 / 行为 |
| --- | --- |
| faster_lio 的 airy.yaml 初始配准 position 从 [0, 0, 1] 改为 [0, 0, 0] | 当前直接继承 param_default_faster_lio.yaml 的 [0, 0, 0] |
| local_planner 的 motionBlockNoPathTimeout 从 3.0 改为 8.0 | 公共默认保留 X30 的 3.0，D1M 型号层覆盖为 8.0 |
| gridmapper_global.yaml 地图宽高从 100.0 改为 500.0 | 当前已统一到 param_default_gridmapper_global.yaml，各型号直接继承；导航默认选 param_default_gridmapper_local.yaml，全局配置仅在显式选择时应用 |
| nav_bridge 的 robot_type 从 x30 改为 d1m | models/d1m/navigate.yaml 选择 param_d1m_nav_bridge.yaml；子 launch 映射到 D1M 实现 |
| SLAM launch 默认配置从 mid360.yaml 改为 airy.yaml | 公共 default 叠加 param_d1m_faster_lio.yaml，合并后传给子模块 launch |
| local_planner launch 默认配置从 x30.yaml 改为 d1m.yaml | 各型号选择相同的 param_default_local_planner.yaml，再叠加对应型号运动模型及参数 |
| D1M 使用 straight_local，X30/Cyvet 使用 new_local | param_default_multi_map_nav.yaml 叠加 param_<model>_multi_map_nav.yaml；straightening 只在 D1M 型号层定义 |

历史基线及型号参数与对应实机文件按解析后的完整字典比对；注释、缩进和空行
不参与参数比较。Livox 未出现在 D1M 启动序列中，公共 JSON 作为可选完整基线
保留，不会因此启用驱动。

2026-10-19 在 D1M-B 临时目录验证：11 份公共基线与对应 Git HEAD 一致，
叠加 D1M 后与对应实机工作区文件完整匹配；6 个已安装子模块的实际 launch
均能接收生成文件。板卡配置/launch 检查 59 项通过；本地另通过 6 项充电交接
回归检查，共 65 项。验证不启动真实导航节点，生产源码和服务不参与替换。

子模块包仍可独立运行，包内默认文件保留；使用 bringup 启动时，以这里选择并
合并的参数为准。今后部署参数在 bringup 内维护，升级子模块时核对其参数定义
与这些基线的兼容性，不在两处同步修改同一参数。
