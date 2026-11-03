# FOrcaSlicer MCP

通过标准 MCP stdio 工具在后台读写 3MF 工程、切片、检查 G-code，并连接 Snapmaker U1 的 Moonraker 接口。切片使用独立 Xvfb 显示和每个任务自己的数据目录，可以同时使用桌面上的浏览器或其他应用。

支持的平台是 **x86_64-linux / NixOS**。固定 FOrcaSlicer **2.3.2-fos.8.6.8**，AppImage SHA-256 为 `713686b0b9b9101a9cbdb39dd3f57d99b09fde9eafc0da394b5482e7d2471477`；`flake.lock` 固定 nixpkgs 26.05 的 `4feb8eb8bf30`。Python、MCP SDK、Xvfb 与 FHS 运行环境由 Nix 提供。首次构建需要下载依赖；AppImage 上游目前仍需可访问。

## 在另一台 NixOS 上运行

复制整个目录，保留 `flake.lock`，在目录中执行：

```bash
nix --extra-experimental-features 'nix-command flakes' build .
mkdir -p ~/.config/forcaslicer-mcp
cat > ~/.config/forcaslicer-mcp/config.json <<EOF
{
  "workspace": "$HOME/Documents/FOrcaSlicer",
  "printer_url": "http://PRINTER_IP:7125"
}
EOF
./result/bin/forcaslicer-mcp
```

将 `PRINTER_IP` 换成打印机地址，工程放入 `workspace`。服务以 stdio 等待 MCP 客户端；直接运行时没有交互菜单。可通过 `FORCASLICER_MCP_CONFIG` 指定另一份配置文件。需要 Moonraker API key 时，在配置中添加 `api_key_file`，指向权限为 0600 的私有文件。密钥在运行时读取。

命令行切片入口为 `./result/bin/forcaslicer-headless`，参数与该固定版本的原生命令行一致。MCP 的 `slice_project` 管理独立数据目录、输出目录、日志、工程副本和审阅结果。

## Home Manager

已有 Home Manager flake 时，增加本地包输入，保留本包自己的 nixpkgs 锁定：

```nix
inputs.forcaslicer-mcp.url = "path:/absolute/path/to/forcaslicer-mcp";
```

在 `outputs` 参数中加入 `forcaslicer-mcp`，然后在 `homeManagerConfiguration.modules` 中加入 `forcaslicer-mcp.homeManagerModules.default`。用户配置设置：

```nix
programs.forcaslicer-mcp = {
  enable = true;
  package = forcaslicer-mcp.packages.x86_64-linux.default;
  workspace = "${config.home.homeDirectory}/Documents/FOrcaSlicer";
  printerUrl = "http://PRINTER_IP:7125";
  # apiKeyFile = "${config.home.homeDirectory}/.config/private/moonraker-key";
};
```

`forcaslicer-mcp` 输入需要位于该配置的词法作用域，或通过 `extraSpecialArgs` 传入。应用后，三个命令出现在用户环境，配置文件由 Home Manager 管理。

非 flake 配置可直接在 `imports` 中引用本目录的 `nix/home-manager.nix`。省略 `package` 选项时，模块使用调用方的 `pkgs` 构建；如需严格复现，使用上面的锁定 flake 包。

## 接入 Codex

Home Manager 安装完成后：

```bash
codex mcp add forcaslicer -- "$HOME/.nix-profile/bin/forcaslicer-mcp"
```

等价的用户配置：

```toml
[mcp_servers.forcaslicer]
command = "/absolute/path/to/.nix-profile/bin/forcaslicer-mcp"
startup_timeout_sec = 30
tool_timeout_sec = 1800
```

在新会话或 MCP 重连后检查工具列表。已有会话能否动态发现新工具取决于客户端。接入方式参见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

## 工具与使用顺序

| 工具 | 作用 |
|---|---|
| `inspect_project` | 查看嵌入工程配置，可筛选键名 |
| `update_project` | 保存新 3MF 副本并修改已有参数，保留模型和对象级覆盖 |
| `slice_project` | 在独立显示中切片，保存 `slice.log`、`manifest.json`、3MF、G-code 和审阅 JSON |
| `inspect_gcode` | 读取实际工具号、路径范围、线性挤出、温度、风扇、层数及嵌入参数 |
| `printer_status` | 读取打印进度、喷嘴、热床与设备状态 |
| `printer_camera` | 返回原生 JPEG 和文件时间戳，拒绝超过 15 秒的旧帧 |
| `upload_gcode` | 按审阅哈希上传并逐字节读回核对 |
| `start_print` | 在空闲设备上启动指定、已审阅且哈希一致的文件 |
| `pause_print` | 仅暂停文件名匹配的当前打印 |

典型流程是检查工程 → 修改副本 → 切片 → 审阅实际轨迹 → 检查硬件、耗材和空板 → 上传与读回 → 开印 → 监看。工程与输出路径限制在配置的工作目录内。输出文件和任务目录必须是新的；上传拒绝覆盖内容不同的同名文件。上传与开印分开，物理动作发生不确定结果时先读状态，再决定后续操作。

工具本身拥有用户配置授予的打印机权限；MCP 客户端和操作者负责确认打印授权。`build_plate_clear` 是调用者对实物检查的声明。摄像头可能看不到板面，时间戳不能替代清空与安装检查。打印前还要核对设备实际喷嘴和装料；工程参数无法证明实物一致。

## 版本行为与边界

- 输入应是含完整配置的 3MF。此版本命令行的 `--allow-newer-file` 为布尔开关；导出参数使用文件名，输出目录单独传入。
- 此版本 CLI 在 `normalize_fdm_2` 中可能把只有支撑使用第二材料的工程按单材料处理，关闭已请求的擦料塔。`slice_project` 默认在启用支撑和擦料塔时将切片副本的 `timelapse_type` 设为 `1`，借用原有 smooth 模式保留塔；副作用可能包括额外的塔旁移动和耗时。修改记录在 manifest，原工程保留。可用 `preserve_support_tower=false` 关闭此兼容处理，但仍会检查请求的塔是否进入输出。
- 参数修改保留对象级覆盖，最终生效值须以实际 G-code 为准。工具不重新建模或修改孔位。
- G-code 检查跟踪 G0/G1 实际坐标、相对/绝对挤出、回抽、工具切换与速度倍率，按 U1 的 270 mm 工作区判断。上传和开印都重新检查路径；G2/G3、未知运动、无法解释的坐标重置和非恒等材料映射会被拒绝。已知 U1 宏单独列出供固件与实物复核。
- 打印接口面向 U1 实测的 Moonraker 端点，包括原生 `/server/files/camera/monitor.jpg`。其他机型、鉴权配置、固件版本和网络布局需另行验证。
- 适配层没有常驻自动打印队列或后台恢复逻辑。监看周期由 MCP 客户端或另行授权的调度器负责。

FOrcaSlicer 的上游源码与版本见 [FOrcaSlicer v2.3.2-fos.8.6.8](https://github.com/jiyang1018/FOrcaSlicer/tree/v2.3.2-fos.8.6.8)。适配层代码采用本目录的 MIT 许可证；FOrcaSlicer 和 Nix 依赖保留各自的上游许可证。

## 验证

本机已验证标准 MCP 初始化和工具发现、空白数据目录下的真实后台切片、原生打印机状态与摄像头返回。`tests/test_server.py` 覆盖工程复制、路径越界、哈希不符、设备忙、目标文件不符和覆盖保护。

```bash
nix build .
nix flake check
# flake check 使用锁定 Python/MCP 环境运行单元测试，不访问打印机。
```

验证记录属于各机器的工作目录，复现软件包不需要复制个人项目、打印历史或账号文件。

## 物理路径 CI 与放行检查

`gcode_audit.py` 将实际挤出路径按物理工具、对象、特征和沉积 Z 平面分组。层间距取已有沉积区域的 Z 差，`HEIGHT` 注释只用于交叉检查。0.4 mm 喷嘴的支撑与0.2 mm 喷嘴的擦料塔分别应用各自上限；主体、筏、支撑和擦料塔均进入检查。上限取工程的正值 `max_layer_height` 与 `0.8 × 喷嘴直径` 中较小者；80% 是[常用层高范围](https://help.prusa3d.com/article/layers-and-perimeters_1748)，不是材料粘附的保证。

每组结果包含层间距、挤出量、由 E/路径长度估算的截面积和等效线宽、声明线宽、峰值体积流量、温度目标和风扇指令。体积流量按指令速度计算，允许5%倍率余量及最多5%的输出精度余量。材料体积流量上限缺失、超限、关闭加热挤出及风扇值越界均阻止放行。可变线宽和细缝填充可能偏离常用宽度范围，因此线宽范围告警保留证据供审阅。

```bash
forcaslicer-audit job.gcode --physical-nozzles 0:0.4,3:0.2 \
  --json-output evidence/path.json --junit-output evidence/path.xml
python ci/run.py --output ci-output
```

CLI 成功返回0，路径检查失败返回2。JSON包含精确文件 SHA-256、工具号、特征、证据行号与限制；JUnit供CI收集。`--physical-nozzles` 是操作者提供的测量/设备信息，离线运行本身不会查询打印机。源码目录可用 `python gcode_audit.py` 运行同一命令。

GitHub Actions在推送、PR及手动触发时执行离线测试，验证合格路径可通过以及混合喷嘴错误样本必须拒绝，再打包源代码、校验清单和检查报告。测试中打印机接口全部模拟，CI不配置打印机凭据，也不发送加热、运动或开印指令。Nix构建通过 `nix flake check` 运行同一单元测试集。

本机放行路径为：切片 → 路径检查 → 审阅报告和实物条件 → 上传时重检 → 逐字节读回 → 开印时重检。上传和开印均读取设备实际使用的喷嘴直径；不匹配或缺失时拒绝。开印提交前再次确认空闲状态、喷嘴配置和远端文件。客户端仍须获得这一次打印的明确授权，并核对装料、平台和新材料接触小样。

这些检查提供路径层面的放行门槛。层间距中的 XY 区域使用包围范围匹配，不证明每条线下面都有支撑；等效线宽不是实物测量。E-only清料单独统计，U1固件宏、喷嘴实体避碰、材料含水量、粘附和尺寸需要另外验证。固件宏及材料交界层尚未验证时，不应把 `passed=true` 当作可开印证明。修改喷嘴组合或材料接触工艺后，先验证小样；视野遮挡或旧帧不能用作粘附通过的证据。

真实设备的持续测试可参考 [labgrid](https://labgrid.readthedocs.io/en/latest/) 与 [LAVA](https://docs.lavasoftware.org/lava/)。本适配层将可重复的文件检查与现场工艺验收分别记录，具体任务的实物结果保存在操作者的工作目录。
