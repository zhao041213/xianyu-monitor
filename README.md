# 闲鱼新货雷达

这是一个通用的闲鱼关键词上新监控器。你可以在面板里填写任意搜索关键词和价格门槛；程序每轮在闲鱼搜索页选择“新发布 → 最新”，把符合条件的商品加入面板并显示 Windows 文字弹窗。当前默认示例是 `mardi短袖`、低于 `60` 元、每 `60` 秒扫描一次。

只有同时满足以下条件的商品才会推送：

- 商品 ID 是建立基线后第一次出现；
- 标题与搜索关键词相关；
- 当前价格严格低于你设置的门槛，等于门槛不会推送。

每个关键词的首次扫描只记录当前商品作为基线，不推送历史低价商品。不同关键词的已见商品和推送记录相互隔离，状态保存在 `monitor_state.json`，登录态保存在 `.browser-data`。

## 启动

直接双击 `start_dashboard.cmd`，或者在 PowerShell 中执行：

```powershell
cd D:\咸鱼监控
.\.venv\Scripts\python.exe .\dashboard.py
```

控制面板会自动打开：`http://127.0.0.1:8765`

面板启动后默认自动开始监控。可以在界面中暂停、立即扫描、修改关键词、价格上限和刷新间隔，以及开关 Windows 文字弹窗。刷新间隔可输入 `30` 到 `3600` 秒；修改监控条件前先暂停，重新启动后新关键词会单独建立基线。

## 首次安装

仅在 `.venv` 尚未安装依赖时执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 运行说明

- 监控浏览器必须保持打开。首次登录失效时，请在该 Chromium 窗口中手动登录闲鱼。
- 首次启动时如果尚未登录，程序会等待你完成登录；运行中一旦检测到验证码、访问受限或登录状态失效，会立即停止扫描且不会自动重试。处理页面提示后，需要在面板中手动重新启动。
- 脚本不会读取或保存手机号、密码、验证码，也不会绕过安全验证。
- 面板仅监听本机 `127.0.0.1`，不会向局域网公开。
- 按启动终端中的 `Ctrl+C` 可完全关闭面板和监控。
- 闲鱼网页结构发生变化时，本轮会显示错误并在下一轮继续尝试。
- 请保持合理扫描间隔并遵守闲鱼的平台规则。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

旧的命令行入口 `xianyu_monitor.py` 仍可使用，也支持通过 `--keyword` 和 `--max-price` 指定关键词与价格门槛。
