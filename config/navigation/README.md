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

local_planner 使用 `param_default_local_planner.yaml` 和各型号的
`param_<model>_local_planner.yaml`。公共参数以 X30 部署值为基准，
odomTimestampCheckEn 默认 true，D1M 型号层设为 false；
initialModel、flat、offroad 仅在型号文件中维护。
默认跟随模式为 omni，turn_preferred 和全部 turn_* 参数只放在 D1M 型号文件中。
Cyvet 的传感器偏移、运动模型、车体尺寸、路径对应文件及跟随参数按提供的实机配置维护。
原始完整参数移至 test/fixtures/local_planner 用于回归比对。

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
