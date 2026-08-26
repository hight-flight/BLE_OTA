# WCH BLE OTA 桌面工具

这是一个面向 Windows 10/11 的 WCH BLE 固件升级工具。程序使用 Python 3.11、PySide6 与 qasync；Windows 默认通过沁恒 `WCHBLEDLL.dll` 访问 BLE，并保留 Bleak/WinRT 备用后端，实现 BLE 扫描、连接、设备信息读取、BIN/Intel HEX 固件升级和日志导出。

## 系统与设备要求

- Windows 10 或 Windows 11（64 位）。
- 一个可正常工作的 Bluetooth Low Energy 适配器；请在 Windows 设置中开启蓝牙并安装适配器厂商驱动。
- 目标设备须提供 WCH OTA 服务 `FEE0` 与特征 `FEE1`。
- 从源码运行需要 Python 3.11 或更高版本；发布目录版不需要预装 Python。

## 安装与开发运行

在 PowerShell 中进入 `pc_ota` 目录后执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m wch_ota
```

也可在已激活的虚拟环境中运行命令行入口：

```powershell
wch-ota
```

默认使用 WCH DLL 后端。如需临时切回 Bleak，可在当前 PowerShell 会话中执行：

```powershell
$env:WCH_OTA_BLE_BACKEND = "bleak"
python -m wch_ota
```

运行自动化测试：

```powershell
python -m pytest -v
```

使用当前 Python 环境生成 Windows 发布包：

```powershell
python build_exe.py
```

默认生成单文件 `dist/WCH-BLE-OTA.exe`。如需目录型发布包，执行：

```powershell
python build_exe.py --directory
```

目录型产物位于 `dist/WCH-BLE-OTA/`。单文件模式启动时会解压到系统临时目录；如果其他电脑被杀毒软件、AppLocker 或企业策略阻止，可改用目录型发布包进行兼容性验证。

## 使用步骤

1. 开启电脑蓝牙和目标设备，使目标设备进入可升级状态。
2. 点击设备列表中的“开始扫描”；程序会持续扫描并更新同一设备的名称和 RSSI，直到手动停止或连接设备。扫描期间按钮显示为“停止扫描”，也可按设备名或地址过滤结果；点击设备名、地址或 RSSI 表头可切换排序。
3. 点击目标设备所在行的“连接”；连接成功后程序会读取芯片、当前 Image、偏移和擦除块大小。
4. 选择 `IMAGEA`、`IMAGEB` 或 `IMAGE_IAP`，再点击固件“浏览”选择 `.bin` 或 `.hex` 文件。`IMAGE_IAP` 仅允许 BIN；所有 BIN 都需要手动填写擦除起始地址，HEX 自动解析地址。
5. 使用非 CH579 的 BIN 时填写十六进制擦除起始地址，例如 `0x00004000`；程序按 Android demo 的规则，从 BIN 文件相同偏移处截取待升级数据。CH579 根据当前 Image 自动选择地址；HEX 地址从文件记录解析。
6. 点击“开始升级”，等待擦除、编程、校验和结束阶段完成。升级期间不要关闭目标设备或移出蓝牙范围。
7. 如需中止，点击“取消”。如需留存诊断信息，点击日志窗口下方的“导出日志”保存 UTF-8 文本。

## 固件规则

- 文件扩展名不区分大小写，仅接受 `.bin` 和 `.hex`。
- 非 CH579 的 BIN 按 Android demo 的“全镜像文件”处理：擦除地址同时作为 BIN 文件偏移，地址之前的数据不会发送，地址必须小于文件大小。CH579 Image A 使用设备 INFO 返回的 offset，Image B 使用地址 `0`，并发送完整 BIN。
- HEX 必须是有效的 Intel HEX ASCII 文本，包含 EOF 记录并通过逐行校验和检查。支持数据记录、扩展段地址、扩展线性地址以及起始地址记录。
- HEX 中的最小数据地址作为升级起始地址；稀疏地址之间的空洞以 `0x00` 补齐。
- 为限制异常文件占用内存，当前固件数据跨度上限为 64 MiB，HEX 源文件大小上限为 16 MiB。

## 已支持芯片

协议解析目前识别 CH573、CH579、CH583、CH592、CH32V208 和 CH32F208，支持 Image A/B 信息。这里的“支持”表示命令编码、芯片识别和自动化测试已覆盖，不代表所有芯片与镜像组合均已在真实硬件上完成升级验证。

## 日志与真实硬件验证状态

界面日志包含本地时间戳，并记录扫描、连接、设备信息、升级阶段、取消、错误和意外断连等事件。可通过“导出日志”保存，反馈问题时请同时提供日志、芯片型号、当前 Image、固件类型及擦除地址。

当前自动化测试覆盖领域协议、BIN/HEX 解析、WCH DLL 与 Bleak 传输封装、OTA 状态机、主要 UI 状态及日志导出。以下项目仍必须使用真实硬件验证，当前均不得视为已通过：

| 芯片 | Image A | Image B | BIN | HEX | 取消 | 蓝牙关闭/升级中断连 |
|---|---|---|---|---|---|---|
| CH573 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |
| CH579 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |
| CH583 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |
| CH592 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |
| CH32V208 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |
| CH32F208 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 |

真实设备验证还应确认 Windows BLE 写入长度、响应时序、断连恢复，以及在未安装 Python 的 Windows 10/11 主机上从发布目录启动和导出日志。
