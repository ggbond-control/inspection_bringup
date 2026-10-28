# 配置与多机器人部署

采用固定三层配置，不需要 `extends`、初始化脚本或同步生成文件：

```text
config/
├── system.yaml                  # 公共默认，保持旧配置可直接使用
├── navigate.yaml
├── services.yaml
├── navigation/                  # 子模块完整参数，按真实包名分类
├── models/
│   ├── x30/
│   │   ├── system.yaml           # 只写机型差异
│   │   └── navigate.yaml
│   └── d1m/
│       ├── system.yaml
│       ├── navigate.yaml
│       ├── services.yaml         # D1M 的 Zenoh 服务环境与 workspace 列表
│       └── navigation/           # 同名子模块文件，只写型号参数差异
├── robot.example.yaml           # 可提交的填写示例
├── robot.yaml                   # 本机文件，Git 忽略
└── calibration/                 # 可选本机标定文件，Git 忽略
```

## 首次使用

在 bringup 源码目录中，将示例复制一次：

```bash
cp config/robot.example.yaml config/robot.yaml
```

编辑 `config/robot.yaml`，最少只需要：

```yaml
model: d1m
robot_id: D1M-A
```

第二台 D1M 只修改 `robot_id`；X30 使用 `model: x30`。`robot_id` 覆盖公共配置
中的 `mqtt.sn` 和 `mission_execution_agent.robot_id`。有意使用不同身份时，
可以在本机 `system` 字段中分别覆盖。

Cyvet 使用以下本机身份配置（另一台改为 CYVET-B）：

```yaml
model: cyvet
robot_id: CYVET-A
```

`models/cyvet/system.yaml` 关闭未安装的传感器及充电模块，开启任务执行和集群选主，
集群列表包含 X30-A、X30-B、CYVET-A、CYVET-B。任务中枢的充电退出地址保留
部署值 `/inspection_charge_executor/exit_charge`，但 charge 模块关闭，不会由本启动
提供该服务；Cyvet 导航也关闭充电预检查。没有外部实现时，不应执行充电任务。
`auto_execute: true` 继承公共值，表示自动执行任务。
`models/cyvet/services.yaml` 选择 ROS_DOMAIN_ID=81、RUSTICL_ENABLE=panfrost，
并关闭 hardware 的开机自启；现有已运行的 hardware 服务不会仅因该字段立即停止。
公共服务配置中的 workspace、日志、动态库路径及其他环境值继续继承。
不要在型号层重新填写固定 navigate_config_path/system_config_path，否则会绕过型号加载。
Cyvet 定位调用超时按部署值设为 60 秒；若初始化超过该时间，调用方可能先超时，
导航启动不会因此自动取消。

需要单机差异时，直接写在同一个文件中：

```yaml
model: d1m
robot_id: D1M-A

system:
  mqtt:
    host: mqtt.example.com
    username: "<本机账号>"
    password: "<本机密码>"
  modules:
    gas: false

# 这里使用 gridmapper 包内已有的默认文件，可省略以继续使用默认。
navigation:
  terrain:
    config: gridmapper_local.yaml
```

首次安装本次代码后，在 task_ws 中构建并 source：

```bash
colcon build --packages-select inspection_bringup --symlink-install
source install/setup.bash
ros2 launch inspection_bringup inspection_system.launch.py
ros2 launch inspection_bringup navigation.launch.py
```

公共参数直接修改公共 YAML；机型参数直接修改 `models/<model>/`；单机参数直接
修改 `robot.yaml`。不需要重新复制或生成 `robot.yaml`。修改后重启对应进程，
导航 supervisor 在启动时固定读取一次有效配置。

没有 `robot.yaml` 时不叠加机型/身份，继续使用原来的默认配置。已存在但内容
错误的 `robot.yaml` 会报错，不会悄悄退回其他机器人身份。`model`、`robot_id`
必须为非空字符串，机型目录必须存在；本机模块字段必须是字典，未知顶层字段
会报错。增加机型时新建对应目录即可，各机型文件可以只包含差异，未提供的
模块文件直接继承公共默认。`store/` 中的历史现场配置不自动加载。

## 读取与覆盖顺序

首先读取 `robot.yaml` 确定机型；随后按以下顺序合并，后者覆盖前者：

| 模块 | 公共 | 机型 | 本机 |
| --- | --- | --- | --- |
| system、sensors、platform_bridge、tmux | `system.yaml` | `models/<model>/system.yaml` | `robot.yaml` 的身份及 `system` |
| navigation | `navigate.yaml` | `models/<model>/navigate.yaml` | `robot.yaml` 的 `navigation` |
| service 管理脚本 | `services.yaml` | 可选 `models/<model>/services.yaml` | `robot.yaml` 的 `services` |

字典递归合并；列表整体替换；`false`、`0`、空字符串和空列表均保留，不会因为
是假值而跳过覆盖。原有消费端针对空值的默认回退语义保持不变。

显式 launch 参数覆盖合并结果。导航 service 模式也会先应用 launch 参数，
再交给 supervisor；运行时的地图、模式等请求继续在当前有效配置上覆盖。
supervisor 生成的 `/tmp/.../resolved.yaml` 是运行时快照，无需人工维护。
模块开关也保留到 supervisor 的启动序列中；关闭必需模块但仍保留依赖动作时，
原有序列/就绪校验仍可能拒绝启动，需要相应调整机型启动顺序。

以下旧入口明确表示“加载独立完整配置”，会跳过 `robot.yaml` 和机型层：

```bash
ros2 launch inspection_bringup inspection_system.launch.py \
  system_config_path:="$PWD/config/system.yaml"
ros2 launch inspection_bringup navigation.launch.py \
  navigate_config_path:="$PWD/config/navigate.yaml"
scripts/start_inspection_tmux.sh --config "$PWD/config/system.yaml" --dry-run
scripts/manage_inspection_services.sh --config "$PWD/config/services.yaml" render
```

即使显式传入的是公共默认 YAML 路径，也按独立配置处理。

## 导航子模块参数分层

### faster_lio 公共基线与型号差异

faster_lio 使用一份完整公共基线，包含原 Mid360 和 Airy 配置的全部字段。
共有参数以 X30/Mid360 为准；RTK 的全部配置保留在 default 中，但默认关闭，
D1M 型号层启用 RTK，并覆盖 Airy 的输入话题、雷达类型。
所有型号的 initial_alignment.score_threshold 统一为 0.5，position 统一为 [0, 0, 0]。
各型号文件均显式列出 common.lid_topic、common.imu_topic 和
preprocess.self_box_half_size，即使与 default 一致，便于按型号检查和调整。
X30、Cyvet 当前使用 Mid360 话题；三个型号的过滤盒半尺寸均为 [0.65, 0.35, 0.4] 米。

```text
config/navigation/param_default_faster_lio.yaml
  → config/models/<model>/navigation/param_<model>_faster_lio.yaml
  → robot.yaml 的 navigation.slam.parameters
  → /tmp/inspection-parameters-<uid>/<内容摘要>.yaml
  → faster_lio 子 launch 的 config 参数
```

公共 navigate.yaml 选择参数入口，型号 navigate.yaml 自动继承，无需重复声明：

```yaml
slam:
  config: "${INSPECTION_CONFIG_DIR}/navigation/param_default_faster_lio.yaml"
```

解析器对 `param_default_<用途>.yaml` 自动查找当前型号目录内的
`param_<model>_<用途>.yaml`。型号文件可省略；存在但格式错误时报错。
本机 inline 参数优先级最高。D1M 的初始配准位置保持 `[0, 0, 0]`，
现在直接继承公共默认值，不再重复填写。

配置继续在 bringup 内递归合并为快照，未修改子包源码或引入设备自动覆盖层。
仅源 YAML 保留注释，生成快照不保留注释；重启导航入口后重新解析。
原公共 mid360.yaml、airy.yaml 及 D1M 的旧 airy 覆盖已被新文件替代；
测试目录保留迁移前参数作为回归基准。普通 `config: airy.yaml` 仍引用
faster_lio 包自己的配置文件，不受本次 bringup 文件整理影响。

### 其他导航模块

gridmapper 的 local/global 参数分别采用平铺命名；两者用途不同，保留各自的完整默认配置：

```text
config/navigation/param_default_gridmapper_global.yaml
  → config/models/<model>/navigation/param_<model>_gridmapper_global.yaml
  → robot.yaml 的 navigation.terrain.parameters
  → 合并参数快照

config/navigation/param_default_gridmapper_local.yaml
  → config/models/<model>/navigation/param_<model>_gridmapper_local.yaml（可选）
  → robot.yaml 的 navigation.terrain.parameters
  → 合并参数快照
```

公共 global 地图宽高统一为 500.0 米，各型号直接继承。
目前没有 global 型号差异，不创建空型号文件。
local 的 X30 型号层将 min_point_height 覆盖为 -0.7；Cyvet 型号层将
min_point_height 覆盖为 -0.7、enable_x_passthrough 设为 true、
x_passthrough_min 设为 0.5、sensor_height_origin 设为 0.4。
D1M 的 local 参数继续继承公共 default。
需要选择 global 配置时显式设置：

```yaml
navigation:
  terrain:
    config: "${INSPECTION_CONFIG_DIR}/navigation/param_default_gridmapper_global.yaml"
```

上述片段用于 robot.yaml；公共或型号 navigate.yaml 中从 terrain 字段开始填写。
默认 terrain 选择 param_default_gridmapper_local.yaml，参数值及 local 模式保持不变。

### multi_map_nav 公共参数与插件差异

公共入口为 `config/navigation/param_default_multi_map_nav.yaml`，按以下顺序合并：

```text
param_default_multi_map_nav.yaml
  → models/<model>/navigation/param_<model>_multi_map_nav.yaml
  → robot.yaml 的 navigation.global_planner.parameters
  → 参数快照，通过 params_path 传给子 launch
```

公共参数以原 new_local 为基准；X30/Cyvet 最终参数与原 new_local 一致，
D1M 与原 straight_local 一致，但各型号的 normal_xy_tolerance 和
normal_yaw_tolerance 均按当前要求覆盖为 0.25（分别为米和弧度）。
各型号文件均显式维护 global_costmap 的 footprint 及上述两个容差字段，
即使 footprint 与 default 相同也保留。local_costmap 的 footprint 仍继承 default。
型号文件按字段覆盖，其他节点及 planner_server
未被覆盖的通用参数继续继承。仅 GridBased.plugin 这个字段切换插件类型，
不会增加第二个 planner_server，也不在运行中直接替换插件。

| 参数 | 公共默认 / X30 / Cyvet | D1M 覆盖 |
| --- | --- | --- |
| inflation_layer.cost_scaling_factor | 5.0 | 3.0 |
| inflation_layer.inflation_radius | 0.25 | 0.8 |
| GridBased.cost_travel_multiplier | 10.0 | 2.0 |
| GridBased.plugin | nav2_smac_planner::SmacPlanner2D | smac_straight_planner::SmacStraightPlanner |

D1M 独有的完整 `straightening` 配置只保存在 D1M 型号文件中，不放进 default。
原 new_local 独有的 `minimum_turning_radius: 0.0` 放在 X30/Cyvet 型号文件中，
避免带入 D1M。没有 robot.yaml 时使用公共配置，不额外添加型号专属参数。
原始两份配置移至 `test/fixtures/multi_map_nav/` 用于完整参数回归比对。

所有型号的 `global_planner.config` 使用同一公共入口（Cyvet 直接继承公共选择）。
bringup 合并 default、型号及单机参数后，将生成文件通过子 launch 的 params_path 传入。
bringup 不再提供 params_file 或 params_path 配置字段；完整外部文件也使用 config 指定。

### local_planner 公共参数与运动模型

公共入口为 `config/navigation/param_default_local_planner.yaml`，通用默认值
以当前 X30 通用参数为基准，默认跟随模式为 omni，保留原始注释。
`odomTimestampCheckEn` 默认 true，D1M 型号层覆盖为 false；
X30/Cyvet 继承 true。`motionBlockNoPathTimeout` 默认 3.0 秒，D1M 覆盖为 8.0 秒。
`initialModel`、`flat`、`offroad` 不放入 default，各型号文件完整独立配置。
因此 default 本身不表示一套完整的机器人运动模型；自动型号部署应设置 robot.yaml。

| 型号 | 型号文件 | 跟随模式 | lookAheadDis | motionBlockNoPathTimeout |
| --- | --- | --- | --- | --- |
| X30 | param_x30_local_planner.yaml | omni | 0.5 | 3.0 |
| D1M | param_d1m_local_planner.yaml | turn_preferred | 1.5 | 8.0 |
| Cyvet | param_cyvet_local_planner.yaml | omni | 0.5 | 3.0 |

D1M/Cyvet 型号文件覆盖与 default 不同的规划/跟随参数；X30 直接继承公共值。
D1M 在型号文件中选择 turn_preferred，全部 turn_* 参数仅在 D1M 文件中定义。
X30/Cyvet 使用 omni，合并结果不包含 turn_* 参数。
Cyvet 按提供的实机配置维护：sensorOffsetX 为 0.35，车体长宽为 0.7/0.4，
使用 correspondences_cyvet.txt 并关闭 gaitSwitchEn；offroad 速度/加减速及
slowdownDisThre=1.0、curvatureDecelGain=1.25 均按实机值配置。

原配置移至 `test/fixtures/local_planner/`，仅用于回归比对，不参与启动。

### 安全停车参数

安全停车沿用上述三层合并机制，不新增模块别名或配置映射。
`param_default_local_planner.yaml` 包含 `localPlanner` 的 `terrainTimeout`、
`localizationHealthTimeout`，以及原名节点 `cmd_vel_guard` 的全部六项参数：
`frequency`、`health_timeout`、`data_timeout`、`command_timeout`、
`rtk_max_speed`、`rtk_max_yaw_rate`。原有里程计检查参数仍在 `/**` 段维护。
`param_default_faster_lio.yaml` 包含 `laser_mapping.ros__parameters.navigation_health`
下的 `data_timeout`、`rtk_max_age`、`max_position_sigma`、`max_attitude_sigma_deg`。
默认值与接入前节点实际采用的默认值一致，不调整停车、恢复或定位算法。

local_planner 子 launch 的 `config` 同时传给规划器、跟随器和门控。
门控先读取包内 `navigation_safety.yaml`，再读取 `config`，后者覆盖前者；
旧机型文件没有 `cmd_vel_guard` 段时仍使用包内默认值。
使用 bringup 部署时在这里维护参数，无需同步修改导航包内默认文件。

单机覆盖示例（数值仅示范配置位置，不是调参建议）：

```yaml
navigation:
  local_planner:
    parameters:
      localPlanner:
        ros__parameters:
          terrainTimeout: 0.4
      cmd_vel_guard:
        ros__parameters:
          data_timeout: 0.4
  slam:
    parameters:
      laser_mapping:
        ros__parameters:
          navigation_health:
            data_timeout: 0.3
```

这些超时检查的对象不同，仍分别配置：规划器 `terrainTimeout` 约束地形；
门控 `data_timeout` 同时约束里程计、地形和路径接收间隔，路径不检查时间戳年龄；
SLAM `navigation_health.data_timeout` 约束雷达数据和处理结果更新。
不能只改其中一项就认为其他检查也被放宽。
修改 `robot.yaml` 后需重启相关导航进程，使 bringup 重新生成参数快照；
运行中的节点不随文件编辑自动更新。可用 `ros2 param get /cmd_vel_guard data_timeout`、
`ros2 param get /localPlanner terrainTimeout` 和
`ros2 param get /laser_mapping navigation_health.data_timeout` 核对实际值。

### 参数合并规则与本机覆盖

`navigate.yaml` 管理启动行为和文件选择；子模块自身的参数另外按三层合并。
公共层保留完整参数文件，型号层只写相同节点/字段的差异，单机层写在
`robot.yaml` 的 `navigation.<模块>.parameters` 中，不需要复制完整文件。

例如当前 D1M 的局部规划：

```text
config/navigation/param_default_local_planner.yaml         # X30 公共默认，超时 3.0
  → config/models/d1m/navigation/param_d1m_local_planner.yaml # D1M 运动模型与差异，超时 8.0
  → robot.yaml 的 navigation.local_planner.parameters   # 可选单机差异
  → /tmp/inspection-parameters-<uid>/<内容摘要>.yaml       # 自动生成，交给子模块
```

型号文件沿用原始参数结构，例如 D1M 的运动模型片段：

```yaml
/**:
  ros__parameters:
    initialModel: offroad
    flat:
      maxSpeed: 1.2
    offroad:
      maxSpeed: 2.2
```

若某台机器需要把该参数从型号默认的 8.0 改为 9.0（基于现有字段的增量示例）：

```yaml
model: d1m
robot_id: D1M-A
navigation:
  local_planner:
    parameters:
      localPlanner:
        ros__parameters:
          motionBlockNoPathTimeout: 9.0
```

公共 `config/navigate.yaml` 选择参数基线，D1M 等型号自动继承：

```yaml
local_planner:
  config: "${INSPECTION_CONFIG_DIR}/navigation/param_default_local_planner.yaml"
```

只有显式的 `${INSPECTION_CONFIG_DIR}/navigation/` 引用启用参数分层。解析器
使用本次实际选中的配置根目录展开它，不要求手动 export，也不受旧 shell
变量影响；除上述 `param_default_` 命名入口外，型号文件按公共文件在
`navigation/` 下的相同相对位置查找。
型号文件缺省时继承公共文件，公共文件不存在则报错，不搜索子模块目录回退。
型号 navigate.yaml 可省略，但需要选择不同 nav_bridge 实现时应保留。
本机 `.parameters` 字典递归覆盖，数组整体替换，false、0、空字符串均保留。

普通相对文件路径仍然相对所属子模块包的 config/ 或 params/；普通绝对路径
直接读取完整文件，两者都不叠加型号参数。完整文件路径不能同时填写
`.parameters`，避免单机增量被忽略。修改参数后重启 navigation；supervisor
及工作进程共用已解析快照，运行过程中不会因源文件改变而混用参数。

nav_bridge 没有公共 default 参数文件。每个型号在 navigate.yaml 中选择
robot_type（x30/d1m/cyvet）和完整的型号参数文件：

```yaml
nav_bridge:
  robot_type: cyvet
  config: "${INSPECTION_CONFIG_DIR}/models/cyvet/navigation/param_cyvet_nav_bridge.yaml"
```

加载顺序为型号完整文件 → robot.yaml 的 navigation.nav_bridge.parameters → 参数快照。
device_id、IP 等单机差异可在 parameters 中覆盖。公共 nav_bridge.config 和 livox.config
留空，由型号层选择完整文件；未指定文件时子 launch 仍使用包内默认配置。
X30 的充电退出服务在型号层指定为 /inspection_charge_executor/exit_charge；
其他型号继承公共 /inspection_charge_executor/undock，Cyvet 关闭 charge_precheck。
nav_bridge_config 是 launch 文件覆盖参数，运行时服务字段为 nav_bridge.config。
内部传给 nav_bridge 子 launch 的参数仍叫 params_file。子 launch 接受 d1m，
并保留 d1_max 旧别名；可执行程序名仍为 d1_max_nav_bridge_node。
cyclonedds_config 是独立 XML 路径，不参与 YAML 合并。

默认已集中管理 nav_bridge、faster_lio、gridmapper、local_planner 和
multi_map_nav 的 YAML。Livox 不设公共 default JSON，也不再使用 livox.model。
livox.config 直接选择型号目录下的完整 JSON，单机 navigation.livox.parameters
可覆盖其中字段，数组整体替换。X30 选择四雷达 MID360 配置；Cyvet 默认选择
param_cyvet_livox_ros_driver2_mid360s.json，MID360 版本保留为可选完整文件。
D1M 将 livox.config 设为空，所有模式的启动序列均不包含 livox。
XML 文件不参与参数字典合并。

global_planner 的文件入口统一为 config，launch 覆盖参数为 global_config，
运行时服务对应 global_planner.config。单独更换 nav_bridge 的 robot_type 时，应
同步选择对应参数文件；本机、launch 或运行时请求显式切换类型且未指定文件时
沿用子模块默认，避免继承另一个型号的参数。

配置来源及 D1M-B Git 差异比对见 [导航参数来源](config/navigation/README.md)。

## 配置根目录与文件路径

所有入口共用 `scripts/bringup_config.py`，根目录按以下规则确定：

1. 设置 `INSPECTION_CONFIG_DIR` 时使用该目录。
2. 否则使用当前 ROS 环境选中的 `inspection_bringup` 的配置目录。
   `--symlink-install` 下解析 `config/system.yaml` 的真实位置，使用源码配置目录，
   因此构建后新建的 `robot.yaml`、机型文件也能立即读取。
3. 未 source ROS 或包不在索引中时，独立脚本使用自身所在 bringup 的 `config/`。

查看实际根目录：

```bash
python3 scripts/bringup_config.py root
```

需要固定到当前源码目录时，可让所有入口统一使用：

```bash
export INSPECTION_CONFIG_DIR="$PWD/config"
```

普通非 symlink 安装使用安装目录内的配置副本；修改源码后应重新安装，或通过
上述变量直接指定源码配置。不要依赖 shell 脚本和 launch 分别猜测工作区路径。

子模块配置文件统一使用两条规则：

1. **相对路径**相对于对应 ROS 包的配置目录。`d1m.yaml` 与 `./d1m.yaml`
   含义相同，子目录也按同一基准解析。
2. **绝对路径**直接使用指定文件，可引用子模块外的单机标定文件。

公共 YAML、机型 YAML、`robot.yaml`、子模块文件选择的 launch 参数和导航
运行时请求均使用同一规则，不相对于 bringup YAML 或当前工作目录查找。
解析使用当前 ROS 环境选中的包 share 目录，不写死源码工作区；先 source 对应
依赖包的工作区。路径展开环境变量和 `~` 后再判断是否为绝对路径。
文件不存在直接报错，不搜索其他目录。合并后转换为绝对路径再交给子模块，
运行时快照不会改变路径含义。空值/未填写继续沿用子模块默认值。

以下均为本地源码中已存在的文件名称，按需填写，不要求全部填写：

```yaml
nav_bridge:
  config: d1_max_params.yaml
slam:
  config: mid360.yaml
terrain:
  config: gridmapper_local.yaml
local_planner:
  config: d1m.yaml
global_planner:
  config: straight_local.yaml
```

例如 `terrain.config` 指向 `gridmapper/config/gridmapper_local.yaml`，
而不是 `config/models/d1m/gridmapper_local.yaml`。`global_planner.config`
指向 `multi_map_nav/params/straight_local.yaml`。

绝对路径示例（该文件在当前本地环境实际存在；板卡上应填写板卡的实际路径）：

```yaml
local_planner:
  config: /home/jazzy/nav_t_ws/src/local_planner/config/d1m.yaml
```

需要单机标定时，可从已存在的文件增量建立本机副本，在 bringup 源码目录执行：

```bash
mkdir -p config/calibration
cp "$(ros2 pkg prefix --share local_planner)/config/d1m.yaml" config/calibration/d1m.yaml
realpath config/calibration/d1m.yaml
```

按需修改副本，将 `realpath` 输出的绝对路径填入 `robot.yaml` 的
`navigation.local_planner.config`；无需将公共配置同步到本机副本。

导航文件入口及含义：

| bringup 字段 | 子模块参数 | 说明 |
| --- | --- | --- |
| `nav_bridge.robot_type` | `robot_type` | `x30`、`d1m`、`cyvet` |
| `nav_bridge.config` | `params_file` | 型号完整文件；普通相对路径相对 `nav_bridge/config/` |
| `nav_bridge.cyclonedds_config` | `cyclonedds_config` | 同上，如已有 `cyvet_cyclonedds.xml` |
| `livox.launch_file` | 选择 launch 文件 | 默认仍为 `msg_multi_MID360_launch.py` |
| `livox.config` | `user_config_path` | 型号完整 JSON；普通相对路径相对 `livox_ros_driver2/config/` |
| `slam.config` | `config` | 相对 `faster_lio/config/`，或绝对路径 |
| `terrain.config` | `params_file` | 相对 `gridmapper/config/`，或绝对路径 |
| `local_planner.config` | `config` | 相对 `local_planner/config/`，或绝对路径 |
| `global_planner.config` | `params_path` | 相对 `multi_map_nav/params/`，或绝对路径 |

`global_planner.config` 使用包含扩展名的文件路径，例如 `straight_local.yaml`。
地图目录等数据路径使用原有语义，
建议写绝对路径；它们不属于本节的子模块配置文件入口。

system 中的 `gimbal.params_file`、`live_stream.config_path`、`gas.params_file`、
`sensor_rs485.params_file` 分别相对 `sensor_gimbal/config/`、
`inspection_platform_bridge/config/`、`gas_monitor/config/`、`sensor_rs485/config/`。
已有文件分别有 `stub_params.yaml`、`live_streams.yaml`、`gas_params_default.yaml`、
`sensor_rs485_params.yaml`。`gimbal_params_file` launch 参数也遵循同一规则。

services 中显式指定的 `navigate_config_path`、`system_config_path` 属于 bringup
自己的配置，相对路径统一从当前 bringup 配置根目录解析；渲染不检查目标板卡
文件是否存在。顶层 launch 的独立配置入口、脚本 `--config` 仍是普通命令行文件
路径，相对当前工作目录；它们用于选择 bringup YAML，不属于子模块参数文件。

迁移提示：之前填写的 `./calibration/...` 等 bringup 相对路径应改为实际绝对路径。
不自动兼容旧查找位置，以免同名文件产生歧义。

本次配套给本地 gridmapper 增加了可选 `params_file`，multi_map_nav 增加了
可选 `params_path`，Livox 的 `msg_MID360_launch.py` 增加了 `user_config_path`。
这些依赖包的改动需要一同部署；bringup 不能使旧子模块自动支持未声明的参数。
本地 Livox checkout 与部署清单版本不同，不含默认的 multi launch；仅在确实
使用单雷达驱动时，才设置 `livox.launch_file: msg_MID360_launch.py`。默认 multi
启动行为保持不变，需要在部署所用的驱动版本上核对对应参数入口。

## systemd 与 tmux

```bash
scripts/start_inspection_tmux.sh --dry-run
scripts/manage_inspection_services.sh render
```

机器人模式下，service 管理脚本移除公共 services 配置中继承的两个固定 YAML
启动路径，改为向 wrapper 写入同一个 `INSPECTION_CONFIG_DIR`。机型和本机
`services` 仍可显式指定完整独立配置。tmux 的窗口开关和实际子进程使用相同
的配置根目录；各窗口只启动所属模块，平台与算法传输合并在 platform 窗口。

单机 service 差异示例：

```yaml
model: d1m
robot_id: D1M-A
services:
  service:
    environment:
      ROS_DOMAIN_ID: "24"
```

更换账户、工作区或中间件时，按实际情况修改 `service.user`、`workspace_root`、
`setup_paths`、`library_paths` 和 `environment` 等字段；这些字段不相互推导。

D1M 的 `models/d1m/services.yaml` 已包含 Zenoh workspace、中间件、session
配置和 Domain 24，公共 `services.yaml` 保留原有默认值。字典合并会继承公共
环境变量，因此 D1M 将 `CYCLONEDDS_URI` 显式设为空字符串。单机需要其他
Domain 时，用上述 `robot.yaml` 的 `services` 字段覆盖即可。

现场迁移时，先备份公共配置中的修改，确认差异已归入机型或本机层，再恢复
公共文件。导航使用 `control_action`；`start_control_action` 是无效字段。
D1M 的 nav 模式继承公共 `control_action: acquire_control`，add 模式通过
`control_action: ""` 显式取消热切换动作；不应靠删除或注释字段取消继承。

修改 system/navigation 配置后重启对应服务；修改 services 中的环境、进程或
启动参数后，需要重新执行 service `install` 再重启。仓库更新不会覆盖
Git 忽略的本机文件；重新克隆或替换整个源码目录前，需要备份本机配置和标定。

服务退出由各自的主进程负责转发信号，systemd 使用 `KillMode=mixed` 在超时后
清理整个服务进程组。navigation 停止时同时通知各 worker，按统一的时间预算
等待退出；停止期间的重复信号不会打断清理。hardware 的 agent 退出等待也有
上限。节点就绪检查直接读取 ROS 图，不会启动 ROS CLI daemon。
hardware 与 system/navigation 一样按 `service.setup_paths` 顺序加载工作区，
确保 agent 能使用配置指定的 ROS 中间件（例如 Zenoh）。

使用 `rmw_zenoh_cpp 0.2.9` 时，还需修复该仓库
`src/detail/rmw_context_impl_s.cpp` 中 `Data::shutdown()` 的锁顺序：锁内标记
关闭并取出图订阅，锁外注销订阅和关闭 session。否则图回调与订阅注销可能
互相等待，表现为不同节点偶发退出超时。该修复需要单独编译安装
`rmw_zenoh_cpp`；bringup 的 service `install` 不会更新中间件。

管理脚本会显示每个服务动作的耗时。`restart navigation` 返回表示 systemd
已经重新启动服务；service 模式下还需要后续导航启动请求，才会加载地图和启动
导航子模块。本次退出策略更新需要重新执行 `scripts/manage_inspection_services.sh install`。

## 本地验证

```bash
source /opt/ros/jazzy/setup.bash
python3 test/config_test.py
python3 test/config_launch_test.py
python3 test/navigation_charge_handoff_test.py
python3 test/service_shutdown_test.py
```

前两组只检查配置、生成的 shell 和 launch 描述，不启动硬件或导航；最后一组
使用独立 ROS domain 和本机发现范围的充电服务 mock，不连接机器人。
退出回归测试使用独立 ROS domain 和模拟进程，覆盖重复信号、超时升级、残留
子进程清理与节点就绪查询。
# 障碍取证开关

`system.yaml` 的 `task_hub.obstacle_snapshot_enabled` 默认 `false`。
也可在 `robot.yaml` 的 `system.task_hub.obstacle_snapshot_enabled` 覆盖。
设为 `true` 后，TaskHub 对正在巡检中的 planner 避障开始事件异步抓拍一张可见光照片，
随障碍文字记录存入现有端侧巡检报告；关闭时仍记录文字。启动时读取，修改后需重启。
需启用现有云台拍照节点、巡检报告及照片落盘；不改变导航行为或自动转向云台。
