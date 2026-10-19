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
robot_id: D1M-001
```

第二台 D1M 只修改 `robot_id`；X30 使用 `model: x30`。`robot_id` 覆盖公共配置
中的 `mqtt.sn` 和 `mission_execution_agent.robot_id`。有意使用不同身份时，
可以在本机 `system` 字段中分别覆盖。

需要单机差异时，直接写在同一个文件中：

```yaml
model: d1m
robot_id: D1M-001

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
    params_file: gridmapper_local.yaml
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

`navigate.yaml` 管理启动行为和文件选择；子模块自身的参数另外按三层合并。
公共层保留完整参数文件，型号层只写相同节点/字段的差异，单机层写在
`robot.yaml` 的 `navigation.<模块>.parameters` 中，不需要复制完整文件。

例如当前 D1M 的局部规划：

```text
config/navigation/local_planner/d1m.yaml                  # 完整公共基线，超时 3.0
  → config/models/d1m/navigation/local_planner/d1m.yaml    # 型号差异，超时 8.0
  → robot.yaml 的 navigation.local_planner.parameters   # 可选单机差异
  → /tmp/inspection-parameters-<uid>/<内容摘要>.yaml       # 自动生成，交给子模块
```

型号文件内容沿用原始参数结构：

```yaml
localPlanner:
  ros__parameters:
    motionBlockNoPathTimeout: 8.0
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

`config/models/d1m/navigate.yaml` 选择公共参数基线：

```yaml
local_planner:
  config: "${INSPECTION_CONFIG_DIR}/navigation/local_planner/d1m.yaml"
```

只有显式的 `${INSPECTION_CONFIG_DIR}/navigation/` 引用启用参数分层。解析器
使用本次实际选中的配置根目录展开它，不要求手动 export，也不受旧 shell
变量影响；型号文件按公共文件在 `navigation/` 下的相同相对位置查找。
型号文件缺省时继承公共文件，公共文件不存在则报错，不搜索子模块目录回退。
本机 `.parameters` 字典递归覆盖，数组整体替换，false、0、空字符串均保留。

普通相对文件路径仍然相对所属子模块包的 config/ 或 params/；普通绝对路径
直接读取完整文件，两者都不叠加型号参数。完整文件路径不能同时填写
`.parameters`，避免单机增量被忽略。修改参数后重启 navigation；supervisor
及工作进程共用已解析快照，运行过程中不会因源文件改变而混用参数。

不同传感器或实现保留各自完整基线，例如 mid360.yaml 与 airy.yaml、
x30_params.yaml 与 d1_max_params.yaml。它们是不同参数方案；同一方案的
型号调参写在型号层，不需要给默认文件添加删除字段等额外语法。

默认已集中管理 nav_bridge、faster_lio、gridmapper、local_planner 和
multi_map_nav 的 YAML。Livox 支持相同的 JSON 分层，数组整体替换；未显式
选择 JSON 时，继续由驱动按 mid360/mid360s 选择默认文件，避免固定一个 JSON
破坏原有型号选择。XML 文件不参与参数字典合并。

原 `global_planner.params_file` 文件名入口保留。型号、本机、launch 或运行时
请求显式填写它而没有同时填写 `params_path` 时，清除继承的完整路径；两者
同时指定则 `params_path` 优先。单独更换 nav_bridge 的 robot_type 时，应
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
  params_file: d1_max_params.yaml
slam:
  config: mid360.yaml
terrain:
  params_file: gridmapper_local.yaml
local_planner:
  config: d1m.yaml
global_planner:
  params_path: straight_local.yaml
```

例如 `terrain.params_file` 指向 `gridmapper/config/gridmapper_local.yaml`，
而不是 `config/models/d1m/gridmapper_local.yaml`。`global_planner.params_path`
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
| `nav_bridge.robot_type` | `robot_type` | 如 `x30`、`d1_max` |
| `nav_bridge.params_file` | `params_file` | 相对 `nav_bridge/config/`，或绝对路径 |
| `nav_bridge.cyclonedds_config` | `cyclonedds_config` | 同上，如已有 `cyvet_cyclonedds.xml` |
| `livox.launch_file` | 选择 launch 文件 | 默认仍为 `msg_multi_MID360_launch.py` |
| `livox.user_config_path` | `user_config_path` | 相对 `livox_ros_driver2/config/`，如已有 `MID360_config.json`，或绝对路径 |
| `slam.config` | `config` | 相对 `faster_lio/config/`，或绝对路径 |
| `terrain.params_file` | `params_file` | 相对 `gridmapper/config/`，或绝对路径 |
| `local_planner.config` | `config` | 相对 `local_planner/config/`，或绝对路径 |
| `global_planner.params_file` | `params_file` | 保留旧语义：包内文件名，不含扩展名 |
| `global_planner.params_path` | `params_path` | 相对 `multi_map_nav/params/`，或绝对路径；优先于上述名称 |

`global_planner.params_file: straight_local` 保留原有的“不含扩展名的配置名称”
语义；需要指定文件路径时使用 `params_path`。地图目录等数据路径使用原有语义，
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
robot_id: D1M-001
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
