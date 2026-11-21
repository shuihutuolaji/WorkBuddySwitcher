# WorkBuddySwitcher — WorkBuddy 多账号切换器

纯外部的 WorkBuddy 多账号管理工具：通过快照/恢复登录态实现多账号一键切换，可共享本地会话与用户记忆；并附带**积分签到、连签保活、成长中心、积分保护、用量统计、外观主题**等账号养护功能。

- 纯外部切换：不依赖应用内切换，直接快照/恢复登录态文件
- tkinter 图形界面 + 系统托盘常驻，PyInstaller 打包为单目录 exe
- 支持 CLI 模式，可脚本化调用

## ✨ 功能

### 账号切换

- **多账号登记**：登记当前登录账号，或直接扫码/验证码新增账号；支持修改别名
- **一键切换**：完全退出 WorkBuddy（含 sandbox 子进程）→ 保存当前账号登录态 → 恢复目标账号 → 重启应用
- **记忆模式**（切换前可选）：
  - `共享`（默认）：把活跃账号的最新用户记忆同步给目标账号，所有账号始终用同一份记忆
  - `独立`：不碰记忆文件，每个账号保留自己的记忆（比如大号工作记忆、小号生活记忆）
- **会话共享**：切换时把 `workbuddy.db` 里全部本地会话的 `user_id` 划归目标账号（`cloud` 会话除外），所有账号看到同一个会话列表；侧栏会话列表缓存一并复制
- **自动备份**：每次切换前备份登录态和数据库到 `backups\`（保留最近 10 份）

### 积分 / 签到

- **一键签到（全部账号）**：优先走界面卡片领取（按钮同步翻转为"已领取"），API 兜底
- **积分批次明细**：各账号积分批次的剩余 / 总量 / 到期时间 / 剩余天数
- **积分保护**：当前账号积分低于阈值时，自动寻找富余账号并切换过去续跑任务
- **连签保活**：自动为无会话的账号发起保活会话，保障连续签到不掉链
- **refresh token 保活**：快照里 refresh token 剩余有效期不足 14 天的账号，自动切换登录让应用自行续期；每日刷新 token，防止长期闲置掉登录
- **自动补签**：运行期按间隔自动补签（默认 30 分钟，可配置/关闭）
- **成长中心任务**：签到与自动补签时一并为全部账号执行，两类任务失败互不影响

### 成长中心

- **稳妥版（默认）**：只领取已抵达的旅行礼物、接取未接任务、领取服务端明确标记 `completed` 的任务奖励、兑换已解锁的连登档位——不主动派出 Buddy、不碰可选玩法接口
- **可选玩法**（设置里开关，默认关）：派 Buddy 旅行、开每日盲盒、用能量开 Buddy、确认断登时最多补登最近一天
- **补登候选**：来自连登状态与活跃日历，只挑服务端允许且未补登的零分日期；实物奖需到成长中心自行填写收件信息

### 使用统计

- 指标卡：累计积分消耗 / 累计上下文 Token / 峰值单日 Token / 当前连续使用 / 最长连续使用
- 活动热力图：近 12 周每日 Token 用量
- 近 7 日 / 30 日趋势图：Token 与积分双折线

### 其他

- **外观主题**：浅色 / 深色 / 跟随系统，标签栏右侧下拉即时切换（深色用 `clam` 主题重绘控件），选择持久化到配置
- **时区检查**：系统时区非 UTC+8 时按天提醒一次，避免签到/连登日期与服务器（UTC+8）错位导致断登
- 系统托盘常驻
- 全部关键动作落日志 `switcher.log`，方便排查
- 自动识别 WorkBuddy 版本，升级改版导致保活失配时给出提醒

## 🖥️ 使用

双击 `WorkBuddySwitcher.exe`（onedir 打包，需保留同目录 `_internal\`）。

### 首次使用

1. 在 WorkBuddy 里登录账号 A，打开切换器 → 点 **「登记当前登录账号」**
2. 点 **「新增账号（扫码/验证码）…」** 或 **「登录新账号…」**：切换器会退出 WorkBuddy 并暂时移开登录文件，应用重启后显示登录界面，登录账号 B
3. 回到切换器点 **「完成登记」**
4. 以后在列表里选中账号 → **「切换到选中账号」** 即可

### ⌨️ CLI 模式

```bash
WorkBuddySwitcher.exe --cli status                      # 查看当前账号/进程/档案（只读）
WorkBuddySwitcher.exe --cli add                         # 登记当前账号
WorkBuddySwitcher.exe --cli switch <uid|别名> --dry-run  # 演练切换，不做任何修改
WorkBuddySwitcher.exe --cli switch <uid|别名> --memory shared|private  # 真实切换
```

## 🔁 切换时会发生什么

1. 完全退出 WorkBuddy（含 sandbox 子进程）
2. 保存当前账号的最新登录态（token 在活跃期间由应用自动续期，快照永远是最新的）
3. 恢复目标账号的登录态
4. 把 `workbuddy.db` 里全部本地会话的 `user_id` 划归目标账号（`cloud` 会话除外，仍属原账号）
5. 按记忆模式同步（或不碰）用户记忆文件
6. 重新启动 WorkBuddy

## ⚠️ 注意事项

- **切换前确认 WorkBuddy 里没有正在运行的任务**（切换 = 关闭应用）
- **每个账号至少每 40 天登录活跃一次**，否则 refresh token（约 50 天）过期需要重新扫码登录
- **系统时区保持 UTC+8**：服务器按 UTC+8 记录签到/连登，跨时区会在跨午夜时把活跃记到次日、造成断登（程序会按天提醒）
- 云端副本、用量配额、connectors（邮箱等第三方凭证）仍按账号各自记账；共享的只是本机视角
- 若重装系统或删除 `~\.workbuddy\keyblob`，已保存的登录快照会失效（token 加密密钥绑机器），重新登录登记即可
- 保活驱动依赖 WorkBuddy 界面结构（模型选择器/发送按钮等），**应用大版本升级后保活可能失效**，需要更新切换器适配
- 版本升级后如遇异常，可从 `backups\` 目录手工恢复

## 📁 数据位置

| 内容 | 路径 |
|---|---|
| 登录态文件 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| 账号快照 | `~\.workbuddy\account-switcher\profiles\<uid>\auth.info` |
| 切换前备份 | `~\.workbuddy\account-switcher\backups\`（保留最近 10 份） |
| 配置（含主题、可选玩法开关） | `~\.workbuddy\account-switcher\config.json` |
| 运行日志 | `~\.workbuddy\account-switcher\switcher.log` |
| 会话数据库 | `~\.workbuddy\workbuddy.db` |
| 用户记忆 | `~\.workbuddy\memory\<uid>_memory.md` |

## 🔨 从源码构建

需要本机 Python 3.12 + PyInstaller，依赖 `psutil`、`cryptography`、`websockets`。

```bat
build.bat
```

或直接：

```bash
python -m PyInstaller --noconfirm --clean --onedir --noconsole ^
  --hidden-import psutil --hidden-import cryptography --hidden-import websockets ^
  --name WorkBuddySwitcher --icon wbicon.ico workbuddy_switcher.py
```

产物在 `dist\WorkBuddySwitcher\`。

## 📜 项目结构

| 文件 | 说明 |
|---|---|
| `workbuddy_switcher.py` | 主程序（单文件，GUI + CLI + 全部逻辑） |
| `WorkBuddySwitcher.spec` | PyInstaller 打包配置 |
| `build.bat` | 一键打包并部署到工具目录 |
| `make_icon.py` | 图标生成脚本 |
| `wbicon.ico` | 应用图标 |

## 🧩 开发调试

```bash
python workbuddy_switcher.py --cli status          # 查看当前账号/进程/档案（只读）
python workbuddy_switcher.py --cli add             # 登记当前账号
python workbuddy_switcher.py --cli switch <uid|别名> --dry-run   # 演练切换
python workbuddy_switcher.py                       # 启动图形界面
```

> 本工具通过快照/恢复登录态文件的方式工作，仅在本机管理自己的多个账号，不修改 WorkBuddy 客户端本身。
