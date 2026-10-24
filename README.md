# LogiBattery

给已经在用 Logitech / Logi 键盘和鼠标的 Windows 用户。

如果你平时只是为了查看键鼠电量，才需要安装或打开 **Logitech G HUB**、**Logi Options+**，LogiBattery 可以直接显示兼容设备的电量与充电状态，并在电量偏低时发送 Windows 通知。对支持的设备来说，查看电量不需要让这两个官方软件常驻运行。

LogiBattery 专注于电量显示和提醒，不负责 DPI、按键映射、宏或固件更新等设备配置。需要这些功能时，仍可按需使用 Logitech 官方软件。

## 下载与使用

Windows 单文件版发布后，可在 [GitHub Releases](https://github.com/shiker1996/logi-battery/releases) 下载 `LogiBattery-版本号-windows-x64.exe`。文件约 20 MB，双击即可启动，不需要安装 Python、Logitech 软件或额外驱动。首次运行时，Windows SmartScreen 可能会提示确认。

启动后会打开设备电量面板，显示设备名称、电量、状态和进度条。程序每 3 分钟刷新一次；底部可以设置低电量提醒阈值（5%–100%，每次调整 5%），并点击“测试通知”检查 Windows 通知。关闭面板会将程序收进托盘；可从托盘重新打开，也可勾选“登录 Windows 时自动启动”。

如果你要从源码运行：双击 `setup.bat` 安装运行依赖，再双击 `启动电量显示.bat`。

## 低电量提醒与通知

未充电的设备降到设定阈值时，程序会发送一次 Windows 通知。电量回升到阈值以上 5 个百分点，或设备开始充电后，会重新启用提醒。

Windows 的“请勿打扰”或专注模式可能隐藏通知横幅。若希望 LogiBattery 在请勿打扰期间仍显示横幅，可在 Windows「设置 → 系统 → 通知 → 请勿打扰 → 设置优先通知」中将 LogiBattery 加入允许的应用。

## 支持范围

- Logitech Logi Bolt、Unifying、部分 Lightspeed 接收器上的 HID++ 设备。
- 通过 USB 数据线直连，且支持 HID++ 的 Logitech 设备。
- 优先显示设备固件报告的电量百分比。设备只报告电池电压时，会用通用单节锂电池曲线换算，并标注“约”；估算值会受电池型号、负载和温度影响。
- 不同品牌的 2.4 GHz 接收器协议各异，其他品牌设备需要单独适配。

LogiBattery 使用只读 HID++ 请求读取电量、设备名称和类别，不发起配对或修改设备配置。

## 从 GitHub 发布 Windows 版本

向 GitHub 推送 `v` 开头的版本标签后，GitHub Actions 会在 Windows Runner 上构建单文件程序，并将 `LogiBattery-版本号-windows-x64.exe` 附加到对应 Release。例如：

```powershell
git tag v1.0.1
git push origin v1.0.1
```

工作流配置在 `.github/workflows/release.yml`。

如果该标签对应的 Release 已经由网页提前创建，或需要重新打包，可在仓库的 **Actions → Build and publish Windows release → Run workflow** 中填写标签（例如 `v1.0.0`）。工作流会将 exe 上传到现有 Release；同名文件会被替换。

## 本机自行打包

在已安装 Python 的 Windows 开发电脑上双击 `build_windows.bat`。脚本会生成诊断目录版和单文件版，输出位置分别为 `dist\debug\LogiBattery-debug\LogiBattery-debug.exe` 与 `dist\LogiBattery.exe`。

如单文件版无法启动，可先运行诊断目录版。运行日志保存在 `%LOCALAPPDATA%\LogiBattery\battery_monitor.log`。程序无需管理员权限或内核驱动；关闭或休眠中的设备可能暂时不响应，会在下一次刷新时重试。
