# WorkBuddy 账号切换器

在多个 WorkBuddy 账号之间切换，同时**共享本地会话历史**和**用户记忆（memory）**。

## 使用

双击 `dist\WorkBuddySwitcher.exe`。

### 首次使用

1. 在 WorkBuddy 里登录账号 A，打开切换器 → 点 **「登记当前登录账号」**。
2. 点 **「登录新账号…」**：切换器会退出 WorkBuddy 并暂时移开登录文件，应用重启后显示登录界面，登录账号 B。
3. 回到切换器点 **「完成登记」**。
4. 以后在列表里选中账号 → **「切换到选中账号」** 即可。

### 记忆模式（切换前可选）

界面上的"记忆模式"单选决定切换时怎么处理用户记忆：

- **共享**（默认）：切换时把活跃账号的最新记忆同步给目标账号，两个账号始终用同一份记忆。
- **独立**：切换时不碰记忆文件，每个账号保留并继续使用自己的记忆（比如大号工作记忆、萌萌生活记忆）。

两种模式下，**会话列表都是共享的**（本地会话始终划归当前账号）。

CLI 等价参数：`switch <账号> --memory shared|private`。

### 切换时会发生什么

1. 完全退出 WorkBuddy（含 sandbox 子进程）
2. 保存当前账号的最新登录态（token 在活跃期间由应用自动续期，快照永远是最新的）
3. 恢复目标账号的登录态
4. 把 `workbuddy.db` 里全部**本地会话**的 `user_id` 划归目标账号（`cloud` 会话除外，仍属原账号）——所有账号看到同一个会话列表
5. 把当前账号的用户记忆 `~\.workbuddy\memory\<uid>_memory.md` 复制给目标账号——所有账号共享同一份记忆
6. 重新启动 WorkBuddy

每次切换前自动备份登录态和数据库到 `~\.workbuddy\account-switcher\backups\`（保留最近 10 份）。

## 注意事项

- **切换前确认 WorkBuddy 里没有正在运行的任务**（切换 = 关闭应用）。
- **每个账号至少每 40 天登录活跃一次**，否则 refresh token（约 50 天）过期需要重新扫码登录。
- 云端副本、用量配额、connectors（邮箱等第三方凭证）仍按账号各自记账；共享的是本机视角。
- 若重装系统或删除 `~\.workbuddy\keyblob`，已保存的登录快照会失效（token 加密密钥绑机器），重新登录登记即可。
- 版本升级后如遇异常，可从 `backups\` 目录手工恢复。

## 重新打包

```
build.bat
```

（需要本机 Python 3.12 + PyInstaller）

## 开发调试

```
python workbuddy_switcher.py --cli status          # 查看当前账号/进程/档案（只读）
python workbuddy_switcher.py --cli add             # 登记当前账号
python workbuddy_switcher.py --cli switch <uid|别名> --dry-run   # 演练切换，不做任何修改
python workbuddy_switcher.py --cli switch <uid|别名>              # 真实切换
```

## 数据位置

| 内容 | 路径 |
|---|---|
| 登录态文件 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| 账号快照 | `~\.workbuddy\account-switcher\profiles\<uid>\auth.info` |
| 会话数据库 | `~\.workbuddy\workbuddy.db` |
| 用户记忆 | `~\.workbuddy\memory\<uid>_memory.md` |
