# 导航参数来源与 D1M-B 比对

这些完整参数基线取自 D1M-B 的子模块 Git HEAD，不包含现场未提交调参。
文件名和节点参数结构沿用原模块；加载时按相同相对位置叠加型号文件和
robot.yaml 的 navigation.<模块>.parameters。

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
| faster_lio 的 airy.yaml 初始配准 position 从 [0, 0, 1] 改为 [0, 0, 0] | models/d1m/navigation/faster_lio/airy.yaml |
| local_planner 的 motionBlockNoPathTimeout 从 3.0 改为 8.0 | models/d1m/navigation/local_planner/d1m.yaml |
| gridmapper_global.yaml 地图宽高从 100.0 改为 500.0 | models/d1m/navigation/gridmapper/gridmapper_global.yaml；当前导航仍选 gridmapper_local.yaml，全局配置仅在显式选择时应用 |
| nav_bridge 的 robot_type 从 x30 改为 d1_max | models/d1m/navigate.yaml 的 robot_type 及 d1_max_params.yaml 选择 |
| SLAM launch 默认配置从 mid360.yaml 改为 airy.yaml | models/d1m/navigate.yaml 显式选择 airy.yaml，不依赖修改子模块 launch |
| local_planner launch 默认配置从 x30.yaml 改为 d1m.yaml | models/d1m/navigate.yaml 显式选择 d1m.yaml |
| multi_map_nav 的 straight_local.yaml 及其他文件仅增加空行 | 参数无差异，不创建型号覆盖文件 |

所有默认导航 YAML 与对应实机文件按解析后的完整字典比对；注释、缩进和空行
不参与参数比较。Livox 未出现在 D1M 启动序列中，公共 JSON 作为可选完整基线
保留，不会因此启用驱动。

2026-10-19 在 D1M-B 临时目录验证：11 份公共基线与对应 Git HEAD 一致，
叠加 D1M 后与对应实机工作区文件完整匹配；6 个已安装子模块的实际 launch
均能接收生成文件。板卡配置/launch 检查 59 项通过；本地另通过 6 项充电交接
回归检查，共 65 项。验证不启动真实导航节点，生产源码和服务不参与替换。

子模块包仍可独立运行，包内默认文件保留；使用 bringup 启动时，以这里选择并
合并的参数为准。今后部署参数在 bringup 内维护，升级子模块时核对其参数定义
与这些基线的兼容性，不在两处同步修改同一参数。
