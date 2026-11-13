# -*- coding: utf-8 -*-
"""
WorkBuddy 账号切换器
- 纯外部切换：快照/恢复登录态文件，不依赖应用内切换
- 共享会话：切换时把本地会话的 user_id 改写为当前账号（cloud 会话除外）
- 共享 memory：切换时把活跃账号的用户记忆同步给下一个账号
- tkinter 图形界面，PyInstaller 打包
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 路径常量

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
AUTH_DIR = LOCALAPPDATA / "CodeBuddyExtension" / "Data" / "Public" / "auth"
AUTH_FILE_NAME = "workbuddy-desktop.info"

WB_DIR = Path.home() / ".workbuddy"
DB_PATH = WB_DIR / "workbuddy.db"
MEMORY_DIR = WB_DIR / "memory"

TOOL_DIR = WB_DIR / "account-switcher"
PROFILES_DIR = TOOL_DIR / "profiles"
BACKUPS_DIR = TOOL_DIR / "backups"
CONFIG_PATH = TOOL_DIR / "config.json"
LOG_PATH = TOOL_DIR / "switcher.log"

APP_CANDIDATES = [
    Path("D:/Program Files/WorkBuddy/WorkBuddy.exe"),
    Path("C:/Program Files/WorkBuddy/WorkBuddy.exe"),
    Path("C:/Program Files (x86)/WorkBuddy/WorkBuddy.exe"),
    LOCALAPPDATA / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
]

# 视为 WorkBuddy 相关、需要关闭的进程名（小写）
WB_PROCESS_NAMES = {
    "workbuddy.exe",
    "workbuddy-editor-sdk-supervisor.exe",
    "sandbox-center.exe",
    "sandbox-cli.exe",
    "sandbox-cli-gc.exe",
}

CREATE_NO_WINDOW = 0x08000000  # GUI 进程里调子进程时不闪黑框


def log_file_write(msg: str) -> None:
    """所有关键动作落一份文本日志，方便排查。"""
    try:
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 配置

DEFAULT_OPTIONS = {
    "exe_path": "",
    "share_sidebar": True,      # 复制侧栏会话列表缓存
    "share_automations": False, # 定时任务 owner 一并改写（默认关）
    "memory_mode": "shared",    # shared=共享记忆 / private=各账号独立记忆
    "keepalive": True,          # 签到后自动为无会话账号发起保活会话（连签保障）
    "keepalive_prompt": "1+1等于几？只回答数字，不要解释。",
    "auto_checkin_interval": 30,  # 运行期自动补签间隔（分钟，0=关闭）
    "token_refresh": True,        # 每日刷新 refresh token（防止长期闲置掉登录）
    "credit_guard": False,          # 长任务保护：积分不足时自动切到富余账号
    "credit_guard_threshold": 100,  # 触发阈值：当前账号可用积分低于该值时切换
    "credit_guard_autoresume": False,      # 切换后自动续跑最近会话（CDP 驱动）
    "credit_guard_resume_prompt": "继续",   # 续跑指令（会话上下文已随迁移带过去）
}


def load_config() -> dict:
    cfg = dict(DEFAULT_OPTIONS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 登录态文件

def find_auth_file() -> Path | None:
    """只认活动登录文件本身。

    注意：auth 目录里还有应用自己生成的轮转备份
    （workbuddy-desktop.<时间戳>.<pid>.<uuid>.info），绝不能当作活动登录态，
    否则快照/恢复都会写错文件（WB 应用找不到活动文件就会进入登录页）。
    """
    preferred = AUTH_DIR / AUTH_FILE_NAME
    return preferred if preferred.is_file() else None


def read_auth(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def auth_uid(path: Path) -> str | None:
    data = read_auth(path)
    if not data:
        return None
    uid = (data.get("account") or {}).get("uid")
    return str(uid) if uid else None


def atomic_write_bytes(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def atomic_copy(src: Path, dst: Path) -> None:
    atomic_write_bytes(dst, src.read_bytes())


# ---------------------------------------------------------------- 账号档案

@dataclass
class Profile:
    uid: str
    alias: str = ""
    uin: str = ""
    registered_at: str = ""
    last_used: str = ""
    memory_seeded: bool = False

    @property
    def display(self) -> str:
        return self.alias or (f"账号{self.uin}" if self.uin else self.uid[:8])

    @property
    def auth_snapshot(self) -> Path:
        return PROFILES_DIR / self.uid / "auth.info"

    @property
    def meta_path(self) -> Path:
        return PROFILES_DIR / self.uid / "meta.json"

    @property
    def memory_file(self) -> Path:
        return MEMORY_DIR / f"{self.uid}_memory.md"

    @property
    def sidebar_dir(self) -> Path:
        return WB_DIR / self.uid


def _meta_write(profile: Profile) -> None:
    profile.meta_path.parent.mkdir(parents=True, exist_ok=True)
    profile.meta_path.write_text(
        json.dumps({
            "uid": profile.uid, "alias": profile.alias, "uin": profile.uin,
            "registered_at": profile.registered_at, "last_used": profile.last_used,
            "memory_seeded": profile.memory_seeded,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8")


def load_profiles() -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    if not PROFILES_DIR.is_dir():
        return profiles
    for d in sorted(PROFILES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        prof = Profile(uid=d.name)
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            prof.alias = meta.get("alias", "")
            prof.uin = meta.get("uin", "")
            prof.registered_at = meta.get("registered_at", "")
            prof.last_used = meta.get("last_used", "")
            prof.memory_seeded = bool(meta.get("memory_seeded", False))
        except (OSError, ValueError):
            pass
        profiles[d.name] = prof
    return profiles


def snapshot_current() -> tuple[str, Profile] | None:
    """把当前登录的账号存入档案库，返回 (uid, profile)。未登录返回 None。"""
    auth = find_auth_file()
    if not auth:
        return None
    uid = auth_uid(auth)
    if not uid:
        return None
    profiles = load_profiles()
    prof = profiles.get(uid) or Profile(uid=uid)
    prof.auth_snapshot.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy(auth, prof.auth_snapshot)
    data = read_auth(auth) or {}
    acct = data.get("account") or {}
    prof.uin = str(acct.get("uin") or prof.uin or "")
    if not prof.registered_at:
        prof.registered_at = datetime.now().isoformat(timespec="seconds")
    prof.last_used = datetime.now().isoformat(timespec="seconds")
    _meta_write(prof)
    log_file_write(f"快照账号 {uid} -> {prof.auth_snapshot}")
    return uid, prof


# ---------------------------------------------------------------- 进程管理

def list_running() -> set[str]:
    """返回当前运行中的进程名集合（小写）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=CREATE_NO_WINDOW).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        first = line.split('","')[0].strip().strip('"')
        if first:
            names.add(first.lower())
    return names


def wb_processes_running() -> set[str]:
    return {n for n in list_running() if n in WB_PROCESS_NAMES}


def kill_node_daemons() -> None:
    """结束命令行里引用了 .workbuddy 的 node.exe 守护进程。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
          "Where-Object { $_.CommandLine -match '\\.workbuddy' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, errors="replace", timeout=30,
                       creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def elevated_kill(log) -> bool:
    """申请管理员权限强制结束 WorkBuddy 进程。

    WorkBuddy 偶尔会以管理员身份运行（比如启动时批准过 UAC、或快捷方式
    勾选了“以管理员身份运行”），普通权限的 taskkill 会收到“拒绝访问”。
    注意：Start-Process -Verb RunAs 不能与 -WindowStyle 同用（会抛异常），
    隐藏窗口要靠被启动的 PowerShell 自己的 -WindowStyle 参数。
    """
    names = " ".join(f"/IM {n}" for n in sorted(WB_PROCESS_NAMES))
    ps = ("try { Start-Process powershell -Verb RunAs -Wait -ArgumentList "
          "@('-NoProfile','-WindowStyle','Hidden','-Command','taskkill /F " + names + "'); "
          "exit 0 } catch { Write-Output ('ERR: ' + $_.Exception.Message); exit 1 }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, errors="replace",
                           timeout=180, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            msg = (r.stdout or "").strip().splitlines()
            log("提权失败：" + (msg[-1] if msg else f"exit={r.returncode}"))
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"管理员权限申请失败：{e}")
        return False


def force_kill_all() -> None:
    for name in sorted(WB_PROCESS_NAMES):
        subprocess.run(["taskkill", "/F", "/IM", name],
                       capture_output=True, text=True, errors="replace",
                       creationflags=CREATE_NO_WINDOW)


def kill_all_workbuddy(log) -> bool:
    """温和关闭 -> 强制关闭 -> （必要时）提权强制关闭，直到相关进程全部退出。"""
    running = wb_processes_running()
    if not running:
        log("WorkBuddy 相关进程未在运行。")
        kill_node_daemons()
        return True

    log(f"检测到运行中的进程：{', '.join(sorted(running))}")
    log("尝试温和关闭 WorkBuddy ...")
    subprocess.run(["taskkill", "/IM", "WorkBuddy.exe"],
                   capture_output=True, text=True, errors="replace",
                   creationflags=CREATE_NO_WINDOW)
    time.sleep(4)

    if wb_processes_running():
        log("温和关闭未完成，强制结束相关进程 ...")
        force_kill_all()

    deadline = time.time() + 25
    while time.time() < deadline:
        kill_node_daemons()
        if not wb_processes_running():
            break
        force_kill_all()  # 边等边补刀，防止个别进程漏杀或被守护进程拉起
        time.sleep(1.5)

    leftover = wb_processes_running()
    if leftover:
        log(f"普通权限无法结束进程（{', '.join(sorted(leftover))}），"
            "WorkBuddy 可能在以管理员身份运行。")
        log("即将弹出 UAC 授权窗口，请在弹窗中点「是」以授权强制结束 ...")
        if elevated_kill(log):
            deadline = time.time() + 15
            while time.time() < deadline and wb_processes_running():
                time.sleep(1.5)
        else:
            log("未获得管理员授权（UAC 被取消）。")

    leftover = wb_processes_running()
    if leftover:
        log(f"⚠ 以下进程未能退出，中止切换：{', '.join(sorted(leftover))}")
        log("提示：也可以在 WorkBuddy 托盘图标右键选择退出，然后重新点「切换」。")
        return False
    log("WorkBuddy 已完全退出。")
    return True


# ---------------------------------------------------------------- 应用路径

def detect_app_exe(cfg: dict) -> str:
    if cfg.get("exe_path") and Path(cfg["exe_path"]).is_file():
        return cfg["exe_path"]
    for cand in APP_CANDIDATES:
        if cand.is_file():
            cfg["exe_path"] = str(cand)
            save_config(cfg)
            return str(cand)
    return ""


def launch_app(exe_path: str) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # --remote-debugging-port：仅监听本机回环，用于连签保障时驱动一条最简会话
    subprocess.Popen([exe_path, f"--remote-debugging-port={CDP_PORT}"],
                     cwd=str(Path(exe_path).parent),
                     creationflags=flags, close_fds=True)
    log_file_write(f"启动 WorkBuddy：{exe_path}")


# ---------------------------------------------------------------- 数据操作

def backup_all(log) -> Path | None:
    """切换前备份登录态和数据库。"""
    if not TOOL_DIR.exists():
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = BACKUPS_DIR / ts
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        auth = find_auth_file()
        if auth:
            shutil.copy2(auth, bdir / auth.name)
        if DB_PATH.is_file():
            shutil.copy2(DB_PATH, bdir / DB_PATH.name)
            for suffix in ("-wal", "-shm"):
                side = Path(str(DB_PATH) + suffix)
                if side.is_file():
                    shutil.copy2(side, bdir / side.name)
        # 只保留最近 10 份备份
        backups = sorted(BACKUPS_DIR.iterdir())
        for old in backups[:-10]:
            shutil.rmtree(old, ignore_errors=True)
        log(f"已备份到 {bdir}")
        return bdir
    except OSError as e:
        log(f"⚠ 备份失败（继续执行）：{e}")
        return None


def retag_sessions(new_uid: str, share_automations: bool, log) -> tuple[int, int]:
    """把本地会话（及可选的定时任务）划归当前账号。"""
    if not DB_PATH.is_file():
        log("⚠ 未找到 workbuddy.db，跳过会话共享。")
        return 0, 0
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    try:
        with con:
            cur = con.execute(
                "UPDATE sessions SET user_id=? "
                "WHERE transport IS NULL OR transport <> 'cloud'",
                (new_uid,))
            n_sessions = max(cur.rowcount, 0)
            n_auto = 0
            if share_automations:
                try:
                    cur = con.execute(
                        "UPDATE automations SET owner_user_id=?", (new_uid,))
                    n_auto = max(cur.rowcount, 0)
                    try:
                        # 定时任务的待投递队列同样按 owner 过滤，不改会导致
                        # 切换后定时任务不触发
                        con.execute(
                            "UPDATE automation_delivery_outbox SET owner_user_id=?",
                            (new_uid,))
                    except sqlite3.Error:
                        pass
                except sqlite3.Error:
                    log("（automations 表不含 owner_user_id，跳过定时任务共享）")
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    finally:
        con.close()
    log_file_write(f"会话归到 {new_uid}: {n_sessions} 条; automations: {n_auto}")
    return n_sessions, n_auto


def _patch_memory_uid(path: Path, uid: str) -> None:
    """把记忆文件 RAW_JSON 里内嵌的 uid 改写为目标账号。

    应用会按这个内嵌 uid 识别文件归属；不改写的话，
    目标账号登录后会把这份记忆当作别人的文件而重置成空模板。
    """
    try:
        text = path.read_text(encoding="utf-8")
        start = text.find("RAW_JSON_START")
        end = text.find("RAW_JSON_END")
        if start == -1 or end == -1:
            return
        seg = text[start:end]
        new_seg = re.sub(r'"uid"\s*:\s*"[0-9a-fA-F-]{36}"', f'"uid": "{uid}"', seg)
        if new_seg != seg:
            path.write_text(text[:start] + new_seg + text[end:], encoding="utf-8")
            log_file_write(f"memory uid patched -> {uid}")
    except OSError:
        pass


def _mark_memory_seeded(uid: str) -> None:
    profiles = load_profiles()
    prof = profiles.get(uid)
    if prof and not prof.memory_seeded:
        prof.memory_seeded = True
        _meta_write(prof)


def _is_empty_memory(path: Path) -> bool:
    """记忆文件是否为空模板（memoryBlock 为空串）。"""
    try:
        text = path.read_text(encoding="utf-8")
        return re.search(r'"memoryBlock"\s*:\s*""', text) is not None
    except OSError:
        return False


def sync_memory(target_uid: str, log) -> None:
    """把已登记账号中最新的用户记忆共享给目标账号。

    稳态：所有账号的记忆文件内容一致，以最近被应用写过的（mtime 最新）为准。
    保护：空模板（如应用给新账号生成的初始文件）永远不覆盖有内容的记忆。
    """
    candidates = [p.memory_file for p in load_profiles().values()
                  if p.uid != target_uid and p.memory_file.is_file()]
    if not candidates:
        log("（没有其他账号的记忆可共享）")
        return
    src = max(candidates, key=lambda p: p.stat().st_mtime)
    dst = MEMORY_DIR / f"{target_uid}_memory.md"
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        copied = False
        if dst.is_file():
            if dst.read_bytes() == src.read_bytes():
                log("用户记忆内容已一致。")
            elif _is_empty_memory(src) and not _is_empty_memory(dst):
                log("来源记忆是空模板、目标有内容，保留目标记忆（防清空保护）。")
            else:
                atomic_copy(src, dst)
                copied = True
                log(f"用户记忆已共享：{src.name} -> {dst.name}")
        else:
            atomic_copy(src, dst)
            copied = True
            log(f"用户记忆已共享：{src.name} -> {dst.name}")
        _patch_memory_uid(dst, target_uid)
        _mark_memory_seeded(target_uid)
        if copied:
            log_file_write(f"memory synced towards {target_uid}")
    except OSError as e:
        log(f"⚠ 记忆同步失败：{e}")


def sync_sidebar(src_uid: str | None, dst_uid: str, log) -> None:
    """把按账号存放的缓存预置给目标账号（纯缓存，应用会自行刷新）。"""
    if not src_uid:
        return
    for name in ("sidebar-list-snapshot.json", "startup-route.json"):
        src = WB_DIR / src_uid / name
        if not src.is_file():
            continue
        dst = WB_DIR / dst_uid / name
        try:
            atomic_copy(src, dst)
            log(f"{name} 已预置。")
        except OSError as e:
            log(f"⚠ {name} 复制失败（不影响切换）：{e}")


# ---------------------------------------------------------------- 主流程

class Switcher:
    def __init__(self, log_fn):
        self.log = log_fn
        self.cfg = load_config()

    # ---- 只读操作

    def status(self) -> dict:
        auth = find_auth_file()
        uid = auth_uid(auth) if auth else None
        profiles = load_profiles()
        running = wb_processes_running()
        exe = detect_app_exe(self.cfg)
        return {
            "logged_in": bool(uid),
            "uid": uid,
            "auth_file": str(auth) if auth else None,
            "profiles": profiles,
            "running": running,
            "exe": exe,
        }

    def set_alias(self, uid: str, alias: str) -> None:
        profiles = load_profiles()
        prof = profiles.get(uid)
        if not prof:
            raise KeyError(f"档案 {uid} 不存在")
        prof.alias = alias.strip()
        _meta_write(prof)

    # ---- 写操作

    def add_current(self) -> tuple[str, Profile] | None:
        r = snapshot_current()
        if r is None:
            self.log("当前没有可登记的登录态（未登录或登录文件缺失）。")
            return None
        uid, prof = r
        self.log(f"已登记当前账号：{prof.display}（uid {uid[:8]}…）")
        return r

    def switch_to(self, target_uid: str, dry_run: bool = False,
                  memory_mode: str | None = None) -> None:
        profiles = load_profiles()
        prof = profiles.get(target_uid)
        if not prof or not prof.auth_snapshot.is_file():
            raise FileNotFoundError(f"账号 {target_uid} 的登录快照不存在，无法切换。")
        snap_uid = auth_uid(prof.auth_snapshot)
        if snap_uid != target_uid:
            raise ValueError(f"快照校验失败（快照内 uid={snap_uid}），中止。")

        mode = (memory_mode or self.cfg.get("memory_mode") or "shared").lower()
        exe = detect_app_exe(self.cfg)
        if not exe and not dry_run:
            raise FileNotFoundError("未找到 WorkBuddy.exe，请在设置中指定安装路径。")

        st = self.status()
        old_uid = st["uid"]
        if old_uid == target_uid and not st["running"]:
            self.log("目标账号就是当前账号，且应用未运行——仅刷新快照后直接启动。")

        if dry_run:
            self.log(f"[演练] 将关闭进程：{', '.join(sorted(st['running'])) or '（无）'}")
            self.log(f"[演练] 将把 {len(st['profiles'])} 个档案中的 {prof.display} 恢复为登录态")
            self.log(f"[演练] 将把本地会话 user_id 改写为 {target_uid}（cloud 会话除外）")
            if mode == "private":
                self.log("[演练] 记忆独立模式：不改动任何记忆文件")
            else:
                self.log(f"[演练] 将把其他账号中最新的用户记忆共享给 {target_uid}")
            return

        if not kill_all_workbuddy(self.log):
            raise RuntimeError("WorkBuddy 未能完全退出，已中止切换（未做任何修改）。")

        backup_all(self.log)

        # 1. 快照当前账号（保住它最新的 token）
        if old_uid:
            r = snapshot_current()
            if r:
                self.log(f"已保存当前账号 {r[1].display} 的最新登录态。")

        # 2. 恢复目标账号登录态
        #    （目标就是当前登录账号时跳过恢复：当前文件里的令牌更新鲜，
        #     刚在上一步被快照保存过，回滚旧快照反而会退化；
        #     活动文件不存在时（如登录页状态）则直接创建。）
        auth = AUTH_DIR / AUTH_FILE_NAME
        if old_uid == target_uid and auth.is_file():
            self.log("目标账号即当前登录账号，保留其最新令牌（不回滚快照）。")
        else:
            atomic_copy(prof.auth_snapshot, auth)
            self.log(f"登录态已切换为：{prof.display}")

        # 3. 会话共享
        n_sessions, _ = retag_sessions(target_uid, bool(self.cfg.get("share_automations")), self.log)
        self.log(f"本地会话已共享（{n_sessions} 条归入当前账号；cloud 会话保持原账号）。")

        # 4. 记忆（共享或独立）
        if mode == "private":
            self.log(f"记忆独立模式：不共享，{prof.display} 继续用自己的记忆。")
        else:
            sync_memory(target_uid, self.log)

        # 5. 侧栏缓存
        if self.cfg.get("share_sidebar"):
            sync_sidebar(old_uid, target_uid, self.log)

        # 6. 重启应用
        launch_app(exe)
        self.log(f"WorkBuddy 已以「{prof.display}」身份重新启动。")
        log_file_write(f"switched -> {target_uid}")

    def begin_login_new(self) -> None:
        """为新账号让路：确认旧账号已登记后，移开登录文件并启动应用。"""
        st = self.status()
        cur_uid = st["uid"]
        if cur_uid and cur_uid not in st["profiles"]:
            raise RuntimeError(
                "当前登录的账号还没登记到切换器，请先点「登记当前登录账号」，"
                "否则切换后将无法找回该账号的登录态。")
        exe = detect_app_exe(self.cfg)
        if not exe:
            raise FileNotFoundError("未找到 WorkBuddy.exe，请先在设置中指定安装路径。")

        if not kill_all_workbuddy(self.log):
            raise RuntimeError("WorkBuddy 未能完全退出，已中止。")
        backup_all(self.log)

        auth = find_auth_file()
        if auth is not None:
            if cur_uid:
                snapshot_current()  # 双保险
            stash = AUTH_DIR / f"{AUTH_FILE_NAME}.stash-{datetime.now():%Y%m%d-%H%M%S}"
            os.replace(auth, stash)
            self.log("已暂时移开登录文件（应用将显示登录界面）。")
            log_file_write(f"auth stashed -> {stash}")

        launch_app(exe)
        self.log("请在 WorkBuddy 登录新账号，完成后回到本窗口点「完成登记」。")

    def finish_login_new(self) -> tuple[str, Profile] | None:
        """新增账号向导第 2 步：登记刚登录的新账号并清理上一步挪出的 stash 备份。"""
        st = self.status()
        if not st["uid"]:
            raise RuntimeError("还没有检测到新的登录态，请先在 WorkBuddy 里完成登录。")
        # 复用「登记当前登录账号」的逻辑（内部已输出登记完成日志）
        r = self.add_current()
        if r is None:
            raise RuntimeError("登录态读取失败，请重试。")
        # 把 begin_login_new 挪出的 stash 备份归档到 backups
        for s in AUTH_DIR.glob(f"{AUTH_FILE_NAME}.stash-*"):
            try:
                bdir = BACKUPS_DIR / ("stash-" + s.stem.split("-")[-1])
                bdir.mkdir(parents=True, exist_ok=True)
                os.replace(s, bdir / s.name)
            except OSError:
                pass
        # 首次归共享：会话与记忆划到新账号
        self.log("提示：如需把现有会话/记忆共享给新账号，请对它执行一次『切换』即可。")
        return r


# ---------------------------------------------------------------- 权益：密钥/令牌/云端 API

WB_ENDPOINT = "https://www.workbuddy.cn"
KEYCACHE_PATH = TOOL_DIR / "keycache.json"
BENEFIT_CACHE_PATH = TOOL_DIR / "benefit_cache.json"
USAGE_HISTORY_PATH = TOOL_DIR / "usage_history.json"


def _get_main_pid():
    """找 WorkBuddy 主进程（优先监听 18488 的那个）。"""
    try:
        import psutil
        cands = []
        for p in psutil.process_iter(['pid', 'name']):
            if (p.info['name'] or '').lower() == 'workbuddy.exe':
                cands.append(p.info['pid'])
        for pid in cands:
            try:
                p = psutil.Process(pid)
                for c in p.net_connections():
                    if c.laddr and c.laddr.port == 18488 and c.status == 'LISTEN':
                        return pid
            except Exception:
                continue
        return cands[0] if cands else None
    except Exception:
        return None


def _scan_memory_for_secret(pid):
    """在进程内存里搜索 at-rest 密钥载荷。"""
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]

    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return None
    needle = b'"atRestSecretKey":"'
    out = None
    addr = 0
    mbi = MBI()
    try:
        while addr < 0x7FFFFFFF0000:
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            if mbi.State == 0x1000 and mbi.Protect in (0x02, 0x04, 0x06, 0x20, 0x40):
                region = (ctypes.c_ubyte * mbi.RegionSize)()
                got = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(base), region, mbi.RegionSize, ctypes.byref(got)):
                    buf = bytes(bytearray(region)[:got.value])
                    i = buf.find(needle)
                    if i >= 0:
                        out = buf[i:i+300].decode('utf-8', 'ignore')
                        break
            addr = base + mbi.RegionSize
            if mbi.RegionSize == 0:
                break
    finally:
        k32.CloseHandle(h)
    return out


def get_protector_key(log):
    """返回 (key_bytes, key_id)。优先用缓存，否则从运行中的 WorkBuddy 内存提取。"""
    import hashlib
    if KEYCACHE_PATH.is_file():
        try:
            c = json.loads(KEYCACHE_PATH.read_text(encoding='utf-8'))
            key = bytes.fromhex(c['key'])
            if hashlib.sha256(key).hexdigest()[:16] == c['keyId']:
                return key, c['keyId']
        except (OSError, ValueError):
            pass
    pid = _get_main_pid()
    if not pid:
        log("⚠ 未找到运行中的 WorkBuddy，无法提取密钥（请先启动一次 WorkBuddy）。")
        return None
    blob = _scan_memory_for_secret(pid)
    if not blob:
        log("⚠ 未能从 WorkBuddy 内存中提取密钥。")
        return None
    m = re.search(r'"atRestSecretKey":"([A-Za-z0-9+/=]{44})"', blob)
    if not m:
        log("⚠ 密钥载荷格式异常。")
        return None
    key = hashlib.sha256(m.group(1).encode('utf-8')).digest()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    KEYCACHE_PATH.write_text(json.dumps({'keyId': key_id, 'key': key.hex()}), encoding='utf-8')
    log(f"已从 WorkBuddy 提取密钥（keyId {key_id}）并缓存。")
    return key, key_id


def _field_aad(key_id: str) -> bytes:
    def lp(b: bytes) -> bytes:
        return len(b).to_bytes(4, 'big') + b
    return (b'WB-AAD\x00\x01' + lp(b'WBEV1') + lp(b'sym-v1')
            + (1).to_bytes(4, 'big') + lp(key_id.encode()) + b'\x02\x00\x00')


def decrypt_auth_token(profile: Profile, log):
    """从账号快照解密 accessToken，返回 (uid, token)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    keypair = get_protector_key(log)
    if not keypair:
        return None
    key, _ = keypair
    data = json.loads(profile.auth_snapshot.read_text(encoding='utf-8'))
    uid = (data.get('account') or {}).get('uid')
    env_b64 = ((data.get('auth') or {}).get('accessToken') or {}).get('envelope')
    if not uid or not env_b64:
        log(f"⚠ {profile.display} 快照缺少登录令牌。")
        return None
    env = json.loads(base64.b64decode(env_b64 + '=' * (-len(env_b64) % 4)))
    nonce = base64.b64decode(env['nonce'])
    tag = base64.b64decode(env['authTag'])
    ct = base64.b64decode(env['ciphertext'])
    token = AESGCM(key).decrypt(nonce, ct + tag, _field_aad(env['keyId'])).decode('utf-8')
    return uid, token


def _refresh_needed(profile: Profile, min_days: float = 14.0) -> bool:
    """快照里的 refresh token 剩余有效期不足 min_days 天时需要保活。"""
    try:
        data = json.loads(profile.auth_snapshot.read_text(encoding='utf-8'))
        re_at = (data.get('auth') or {}).get('refreshExpiresAt')
        if isinstance(re_at, (int, float)):
            return (re_at - time.time() * 1000) / 86400_000 < min_days
    except (OSError, ValueError):
        pass
    return False  # 读不到就交给 API 401 兜底提示


def refresh_profile_token_via_app(sw, prof: Profile, log=print) -> bool:
    """应用正以 prof 身份运行时，等待 WB 自动续期，再把最新登录态回存快照。

    WB 在登录态下会用 refreshToken 自行续期并重写 auth.info（lastRefreshTime
    变化）；切换走时 switch_to 会自动快照，这里再显式兜底一次。"""
    auth = find_auth_file()
    if not auth or auth_uid(auth) != prof.uid:
        log(f"⚠ {prof.display}：应用当前不是该账号的登录态，跳过。")
        return False
    try:
        base_ts = ((json.loads(prof.auth_snapshot.read_text(encoding='utf-8'))
                    .get('auth') or {}).get('lastRefreshTime')) or 0
    except (OSError, ValueError):
        base_ts = 0
    deadline = time.time() + 150
    while time.time() < deadline:
        data = read_auth(auth)
        ts = ((data or {}).get('auth') or {}).get('lastRefreshTime') or 0
        if ts > base_ts:  # 应用刚完成一次续期
            r = snapshot_current()
            if r and r[0] == prof.uid:
                log(f"✓ {prof.display}：应用已续期令牌，新登录态已回存快照。")
                return True
            break
        time.sleep(10)
    r = snapshot_current()
    if r and r[0] == prof.uid:
        log(f"• {prof.display}：未观察到续期（令牌可能仍有效），已回存当前登录态。")
        return True
    log(f"⚠ {prof.display}：回存失败（登录文件与账号不一致）。")
    return False


def refresh_all_tokens(sw, log=print) -> bool:
    """切换式令牌保活：refresh token 剩余 <14 天的账号，切换登录让应用自行续期。

    网关直连刷新不可行（ima 连接器要求 X-Project-Id 授权、Keycloak 登录客户端
    为 confidential），而应用登录态下本来就会续期——切换即可触发并随快照存档。
    结束后恢复原账号；耗时约每账号 3 分钟。"""
    profiles = load_profiles()
    if not profiles:
        log("没有已登记的账号。")
        return False
    need = [p for p in profiles.values() if _refresh_needed(p)]
    if not need:
        log("所有账号令牌有效期充足，无需保活。")
        return True
    log(f"令牌保活：{len(need)} 个账号的 refresh token 将到期"
        f"（{'、'.join(p.display for p in need)}），通过切换让应用自行续期 …")
    ok_all = True
    origin_uid = sw.status()["uid"]
    cur_uid = origin_uid
    try:
        for prof in need:
            if prof.uid == cur_uid:
                ok_all = (refresh_profile_token_via_app(sw, prof, log)
                          and ok_all)
                continue
            try:
                log(f"— 切换到 {prof.display} 以续期令牌 —")
                sw.switch_to(prof.uid)
                cur_uid = prof.uid
                ok_all = (refresh_profile_token_via_app(sw, prof, log)
                          and ok_all)
            except Exception as e:  # noqa: BLE001
                ok_all = False
                log(f"⚠ {prof.display} 令牌保活失败：{e}")
    finally:
        if cur_uid != origin_uid:
            try:
                sw.switch_to(origin_uid)
                log("已切回原账号。")
            except Exception as e:  # noqa: BLE001
                log(f"⚠ 切回原账号失败：{e}")
    return ok_all


def wb_api(profile: Profile, path: str, payload=None, prefix='/v2', log=print):
    """调用 WorkBuddy 云端接口。返回 (ok, data_or_msg)。"""
    import urllib.request
    import urllib.error
    r = decrypt_auth_token(profile, log)
    if not r:
        return False, "无法解密令牌"
    uid, token = r
    body = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(
        f"{WB_ENDPOINT}{prefix}{path}", data=body, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}',
                 'X-User-Id': uid, 'Accept-Language': 'zh',
                 # 网关会按 UA 拦截非浏览器客户端（Python-urllib 直接 403）
                 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                               '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'ignore')[:200]
        if e.code == 401:
            return False, "令牌已失效（切换到该账号一次即可刷新）"
        if detail.lstrip().lower().startswith(('<!doctype', '<html', '<head')):
            # 网关在凭据无效/被 WAF 拦截时回整页 HTML，转成可读提示
            return False, (f"网关返回错误页（HTTP {e.code}）：凭据可能已失效，"
                           "请切换/重新登录该账号后重试")
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:  # noqa: BLE001
        return False, f"网络错误：{e}"
    if text.lstrip().startswith('<'):
        # HTTP 200 但回的是整页 HTML（网关/WAF 拦截），不是 JSON
        return False, "网关返回了非 JSON 错误页（可能被 WAF 拦截或凭据异常）"
    try:
        out = json.loads(text)
    except ValueError:
        return False, "响应不是有效 JSON"
    if out.get('code') not in (0, None):
        return False, f"{out.get('code')}: {out.get('msg', '')}"
    return True, out.get('data') or {}


def checkin_status(profile: Profile, log=print):
    """真实签到状态（注意：必须用不带 /v2 的 activity-status 接口；
    /v2/checkin-status 是另一套活动数据，无活动时恒为未签到）。"""
    ok, data = wb_api(profile, '/billing/meter/checkin-activity-status', prefix='', log=log)
    return (ok, data) if ok else (False, str(data))


def daily_checkin(profile: Profile, log=print):
    results = []
    ok1, data = wb_api(profile, '/billing/meter/daily-checkin', log=log)
    if ok1:
        results.append("签到成功")
    else:
        msg = str(data)
        results.append("今天已签到" if ('已签到' in msg or '10001' in msg) else msg)
    # 若有进行中的签到活动且今天还没领，一并领取
    ok0, act = checkin_status(profile, log=log)
    if ok0 and isinstance(act, dict) and act.get('active') and not act.get('today_checked_in'):
        ok2, data2 = wb_api(profile, '/billing/meter/daily-checkin', prefix='', log=log)
        if ok2:
            results.append("活动奖励已领")
        else:
            m2 = str(data2)
            results.append("活动已签" if ('已签' in m2 or '1001' in m2) else f"活动签到：{m2}")
    return True, "；".join(results)


def _pick_expire_ms(a: dict):
    """取一批积分的真实可用截止时间：扣减截止 / 周期结束 / 套餐有效期三者的最早者。

    ExpiredTime 可能是套餐定义的远期时间（如 2034），月度积分实际当月有效
    （CycleEndTime），日批次则是 DeductionEndTime——取最早才符合真实规则。
    """
    cands = []
    for tf in ('DeductionEndTime', 'ExpiredTime'):
        v = a.get(tf)
        if isinstance(v, (int, float)) and v > 10**11:
            cands.append(v)
    ce = a.get('CycleEndTime')
    if isinstance(ce, str) and ce:
        try:
            cands.append(int(datetime.strptime(ce[:19], '%Y-%m-%d %H:%M:%S').timestamp() * 1000))
        except ValueError:
            pass
    if not cands:
        return None
    future = [c for c in cands if c >= time.time() * 1000 - 86400_000]
    return min(future or cands)


def resource_summary(profile: Profile, log=print):
    """返回逐笔积分批次 [{name, remain, total, expire}]，按到期时间升序。
    summary 只能空参数调用（拿 PackageCode 列表），批次明细需要带
    PackageCodes + Status 查询；每笔批次的到期时间用 DeductionEndTime。"""
    ok, data = wb_api(profile, '/billing/meter/get-user-resource-summary',
                      payload={}, prefix='', log=log)
    if not ok:
        # 新接口不可用（可能 WB 升级改了接口）：回退旧 get-user-resource 兜底
        return _resource_summary_legacy(profile, log=log)
    codes = [x.get('PackageCode') for x in (data.get('Packages') or []) if x.get('PackageCode')]
    rows = []
    for kind, extra in (('get-user-resource-paid-packages', {'NeedRenewInfo': True}),
                        ('get-user-resource-free-packages', {})):
        if not codes:
            break
        ok2, d2 = wb_api(profile, f'/billing/meter/{kind}',
                         payload={'PageNumber': 1, 'PageSize': 100, 'PackageCodes': codes,
                                  'Status': [0, 3], 'IsDisplayTotalInfo': True, **extra},
                         prefix='', log=log)
        if not ok2 or not isinstance(d2, dict):
            continue
        for a in (d2.get('Accounts') or []):
            try:
                remain = float(a.get('CapacityRemainPrecise') or a.get('CapacityRemain') or 0)
                total = float(a.get('CapacitySizePrecise') or a.get('CapacitySize') or 0)
            except (TypeError, ValueError):
                continue
            if remain <= 0:
                continue  # 已用完/已过期的批次不占显示
            rows.append({'name': a.get('PackageName') or a.get('PackageCode') or '积分包',
                         'remain': round(remain, 2), 'total': round(total, 2),
                         'expire': _pick_expire_ms(a), 'code': a.get('PackageCode')})
    rows.sort(key=lambda r: (r['expire'] is None, r['expire'] or 0))
    # 过期批次直接剔除：服务端对已过扣减截止的批次可能仍报 remain>0，
    # 不剔除会虚增总数与 7 天到期预警
    now_ms = datetime.now().timestamp() * 1000
    rows = [r for r in rows if r['expire'] is None or r['expire'] > now_ms]
    # 对账：summary 的 CycleRemainCapacity 是账户级权威剩余；批次明细接口的
    # CapacityRemain 可能滞后（实测已用完的体验包仍报满额 500）。只做向下校正：
    # 按"先到期先扣"的消耗顺序把权威余量坐实到各批次，对不上的尾部批次清零。
    auth = {}
    for x in (data.get('Packages') or []):
        pc = x.get('PackageCode')
        try:
            if pc:
                auth[pc] = float(x.get('CycleRemainCapacity') or 0)
        except (TypeError, ValueError):
            pass
    if auth:
        by_code = {}
        for r in rows:
            by_code.setdefault(r.get('code'), []).append(r)
        for pc, grp in by_code.items():
            if pc not in auth:
                continue
            left = auth[pc]
            if sum(r['remain'] for r in grp) <= left + 0.01:
                continue
            for r in grp:  # rows 已按到期升序，组内保持该顺序
                r['remain'] = min(r['remain'], max(left, 0.0))
                left -= r['remain']
        rows = [r for r in rows if r['remain'] > 0]
    return rows


def _resource_summary_legacy(profile: Profile, log=print):
    """旧版积分资源接口兜底（WB 升级改掉新三接口时保命）。

    请求体与官网套餐页一致：ProductCode 固定 p_tcaca，时间窗拉到 101 年
    覆盖全部批次。返回格式与 resource_summary 相同（无对账，旧接口没有
    账户级权威余量可对照）。"""
    begin = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    end = (datetime.now() + timedelta(days=365 * 101)).strftime('%Y-%m-%d %H:%M:%S')
    ok, data = wb_api(profile, '/billing/meter/get-user-resource', prefix='/v2',
                      payload={'PageNumber': 1, 'PageSize': 100, 'ProductCode': 'p_tcaca',
                               'Status': [0, 3], 'PackageEndTimeRangeBegin': begin,
                               'PackageEndTimeRangeEnd': end}, log=log)
    if not ok:
        return None
    acc = data.get('Accounts')
    if acc is None:  # 旧接口实际响应嵌在 data.Response.Data.Accounts
        acc = ((data.get('Response') or {}).get('Data') or {}).get('Accounts')
    rows = []
    for a in (acc or []):
        try:
            remain = float(a.get('CapacityRemainPrecise') or a.get('CapacityRemain') or 0)
            total = float(a.get('CapacitySizePrecise') or a.get('CapacitySize') or 0)
        except (TypeError, ValueError):
            continue
        if remain <= 0:
            continue
        rows.append({'name': a.get('PackageName') or a.get('PackageCode') or '积分包',
                     'remain': round(remain, 2), 'total': round(total, 2),
                     'expire': _pick_expire_ms(a), 'code': a.get('PackageCode')})
    rows.sort(key=lambda r: (r['expire'] is None, r['expire'] or 0))
    now_ms = datetime.now().timestamp() * 1000
    rows = [r for r in rows if r['expire'] is None or r['expire'] > now_ms]
    return rows


def account_credit_balance(profile: Profile, log=print):
    """账号可用积分（权威口径）：summary 各包 CycleRemainCapacity 之和。

    比 resource_summary 轻量：单次调用、不拉批次明细，专供自动切换判断。"""
    ok, data = wb_api(profile, '/billing/meter/get-user-resource-summary',
                      payload={}, prefix='', log=log)
    if not ok:
        return None
    tot = 0.0
    for x in (data.get('Packages') or []):
        try:
            tot += float(x.get('CycleRemainCapacity') or 0)
        except (TypeError, ValueError):
            pass
    return round(tot, 2)


_GUARD_SWITCHING = threading.Lock()  # 进程内防重入：切换+续跑期间不再触发下一次


def _keepalive_lock_busy() -> bool:
    """签到/保活跨进程锁是否被占用（非阻塞试探）。占用时积分保护不切换，
    避免和 09:05 定时任务的账号轮转互相踩。"""
    import msvcrt
    try:
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(KEEPALIVE_LOCK_PATH, "a+b")
    except OSError:
        return False
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return True
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fh.close()
    return False


def guard_switch_if_low(cfg: dict, log=print, force: bool = False):
    """长任务保护：当前账号可用积分低于阈值时，自动切换到最富余的账号。

    只读账户级权威余量（每账号 1 次 API 调用）。目标账号余量也必须高于
    阈值，否则不切换（切过去一样卡），只提醒。返回 (是否切换, 简述)。"""
    if not force and not cfg.get("credit_guard", False):
        return False, "未启用积分保护"
    try:
        thr = float(cfg.get("credit_guard_threshold", 100))
    except (TypeError, ValueError):
        thr = 100.0
    sw2 = Switcher(lambda m: None)
    st = sw2.status()
    cur = st.get("uid")
    profiles = st.get("profiles") or {}
    cur_prof = profiles.get(cur) if cur else None
    if cur_prof is None:
        return False, "当前账号未登记，跳过检查"
    bal = account_credit_balance(cur_prof, log=lambda m: None)
    if bal is None:
        return False, "查询当前账号积分失败（网络/凭据）"
    if bal >= thr:
        return False, ""
    log(f"（积分保护）{cur_prof.display} 剩余 {bal:g} 分，低于阈值 {thr:g}，寻找富余账号……")
    best, best_bal = None, -1.0
    for p in profiles.values():
        if p.uid == cur:
            continue
        b = account_credit_balance(p, log=lambda m: None)
        if b is not None and b > best_bal:
            best, best_bal = p, b
    if best is None:
        log("（积分保护）没有其他已登记账号，无法切换。")
        return False, "无其他账号"
    if best_bal < thr:
        log(f"（积分保护）其他账号最高为 {best.display} {best_bal:g} 分（阈值 {thr:g}），"
            "切过去也不够用，暂不切换。")
        return False, "所有账号积分均不足"
    if not _GUARD_SWITCHING.acquire(blocking=False):
        return False, "上一次积分保护切换仍在进行，跳过"
    try:
        if _keepalive_lock_busy():
            return False, "签到/保活正在进行，本次跳过"
        log(f"（积分保护）切换到最富余的 {best.display}（{best_bal:g} 分）……")
        todo_title = _last_session_title()  # 被打断任务=最近会话（迁移不动 updated_at）
        sw2.switch_to(best.uid)
        log(f"（积分保护）已切换到 {best.display}。本地会话已划归该账号。")
        if cfg.get("credit_guard_autoresume") and todo_title:
            log(f"（积分保护）尝试自动续跑「{todo_title[:30]}」……")
            ws_url = _cdp_page_ws(timeout=240)
            if not ws_url:
                log("（积分保护）⚠ 应用未就绪（CDP 不可达），请手动打开会话续跑。")
            elif resume_last_session(ws_url, todo_title,
                                     str(cfg.get("credit_guard_resume_prompt") or "继续"),
                                     log):
                log("（积分保护）✅ 任务已自动续跑（无需人工点击）。")
            else:
                log("（积分保护）⚠ 自动续跑未成功；会话已迁移，手动打开即可续跑。")
        else:
            log("（积分保护）请在 WorkBuddy 里重新打开原会话、点重试即可续跑任务。")
        return True, f"已切换到 {best.display}"
    finally:
        _GUARD_SWITCHING.release()


def _credit_entry_day(key: str, fallback_upd) -> str:
    """credit_json 的 key 是 UUIDv7 时前 48bit 为请求发生的毫秒时间戳，
    按真实发生时间归档；随机 UUID（v4 等）退回会话最后活跃日。"""
    try:
        ms = int(key.replace('-', '')[:12], 16)
        dt = datetime.fromtimestamp(ms / 1000)
        if 2024 <= dt.year <= datetime.now().year + 1:
            return dt.strftime('%Y-%m-%d')
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return datetime.fromtimestamp((fallback_upd or 0) / 1000).strftime('%Y-%m-%d')


def local_usage_stats():
    """本地 token/积分统计：返回 dict。"""
    if not DB_PATH.is_file():
        return None
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    today_s = datetime.now().strftime('%Y-%m-%d')
    today0 = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    total_credits = today_credits = 0.0
    total_ctx = today_ctx = 0
    try:
        for cj, used, upd in con.execute(
                "SELECT su.credit_json, su.used, su.updated_at FROM session_usage su "
                "LEFT JOIN sessions s ON s.id = su.session_id"):
            try:
                if cj and cj != 'None':
                    for k, v in json.loads(cj).items():
                        val = float(v)
                        total_credits += val
                        if _credit_entry_day(k, upd) == today_s:
                            today_credits += val
            except (ValueError, TypeError):
                pass
            try:
                total_ctx += int(used or 0)
                if (upd or 0) >= today0:
                    today_ctx += int(used or 0)
            except (TypeError, ValueError):
                pass
        n_sessions = con.execute("SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        con.close()
    return {'today_credits': round(today_credits, 1), 'total_credits': round(total_credits, 1),
            'today_ctx': today_ctx, 'total_ctx': total_ctx, 'sessions': n_sessions}


def auto_checkin_all(log):
    """所有已登记账号执行签到（幂等）。"""
    kp = get_protector_key(log)
    if not kp:
        return False
    profiles = load_profiles()
    if not profiles:
        log("没有已登记的账号。")
        return False
    ok_all = True
    for p in profiles.values():
        try:
            ok, msg = daily_checkin(p, log=log)
            log(f"{p.display}：{msg}")
            ok_all = ok_all and ok
        except Exception as e:  # noqa: BLE001
            log(f"{p.display}：签到失败 {e}")
            ok_all = False
    refresh_wb_ui(log)
    return ok_all


def refresh_wb_ui(log=print) -> None:
    """签到后刷新运行中的 WB 界面。

    签到按钮的状态是 WB 启动时拉取的缓存，之后不会自动重查——服务端
    已签但按钮仍显示可签。无任务生成中时执行一次 Page.reload 同步；
    WB 未运行则跳过（下次启动自然拿到最新状态）。"""
    import asyncio
    try:
        asyncio.run(_wb_ui_reload(log))
    except Exception as e:  # noqa: BLE001
        log(f"    （WB 界面刷新跳过：{e}）")


async def _wb_ui_reload(log) -> None:
    import asyncio
    import json as _json
    import urllib.request
    import websockets

    # 账户/积分等页面是独立 BrowserWindow，各有各的目标——刷新所有 page 目标
    try:
        with urllib.request.urlopen("http://127.0.0.1:9223/json/list", timeout=5) as r:
            targets = _json.loads(r.read().decode())
    except (OSError, ValueError):
        return
    pages = [t.get("webSocketDebuggerUrl") for t in targets
             if t.get("type") == "page" and "devtools" not in t.get("url", "")
             and t.get("webSocketDebuggerUrl")]
    if not pages:
        return
    reloaded = 0
    for ws_url in pages:
        try:
            async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
                mid = 0

                async def send(method: str, params: dict | None = None,
                               timeout: float = 30.0) -> dict:
                    nonlocal mid
                    mid += 1
                    await ws.send(_json.dumps({"id": mid, "method": method,
                                               "params": params or {}}))
                    while True:
                        raw = _json.loads(await asyncio.wait_for(ws.recv(), timeout))
                        if raw.get("id") == mid:
                            return raw

                async def ev(expr: str):
                    r = await send("Runtime.evaluate",
                                   {"expression": expr, "returnByValue": True})
                    return r.get("result", {}).get("result", {}).get("value")

                try:
                    st = _json.loads(await ev(_JS_STATE) or "{}")
                except (ValueError, TypeError):
                    st = {}
                if "停止" in (st.get("label") or ""):
                    log("    检测到任务生成中，跳过该窗口刷新（下次启动时自动同步）。")
                    continue
                await send("Page.enable")
                await send("Page.reload")
                reloaded += 1
        except (OSError, asyncio.TimeoutError) as e:
            log(f"    （一个窗口刷新失败，跳过：{e}）")
    if reloaded:
        log(f"    已刷新 WB 界面（{reloaded} 个窗口），签到按钮将同步为已签。")


def benefit_snapshot(log):
    """拉取所有账号的积分到期汇总，写入缓存（主页面提醒条用）并返回。"""
    if not get_protector_key(log):
        return None
    snap = {'updated_at': datetime.now().isoformat(timespec='seconds'), 'accounts': []}
    for p in load_profiles().values():
        packs = resource_summary(p, log=log) or []
        now = datetime.now()
        exp7 = total = 0.0
        nearest = None
        for pk in packs:
            total += pk['remain']
            if pk.get('expire'):
                days = (datetime.fromtimestamp(pk['expire'] / 1000) - now).days
                if 0 <= days <= 7:
                    exp7 += pk['remain']
                if nearest is None or pk['expire'] < nearest.get('expire', 0):
                    nearest = pk
        snap['accounts'].append({'alias': p.display, 'uid': p.uid,
                                 'exp7': round(exp7, 1),
                                 'total': round(total, 1),
                                 'nearest': ({'remain': nearest['remain'],
                                              'expire': nearest['expire']} if nearest else None)})
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    BENEFIT_CACHE_PATH.write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')
    return snap


def load_benefit_cache():
    try:
        return json.loads(BENEFIT_CACHE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def burn_forecast():
    """按每日快照差值估算消耗速率，预测当前余额可用天数。
    返回 {'burn': 分/天, 'balance': 余额, 'days': 天或None}；快照不足返回 None。"""
    hist = load_usage_history()
    today = datetime.now().strftime('%Y-%m-%d')
    earlier = sorted(d for d in hist.keys() if d < today)
    if len(earlier) < 2:
        return None
    rates = []
    for i in range(1, len(earlier)):
        prev, cur = hist[earlier[i - 1]], hist[earlier[i]]
        delta = sum(max(c - prev.get(sid, 0), 0) for sid, c in cur.items())
        span = max((datetime.strptime(earlier[i], '%Y-%m-%d')
                    - datetime.strptime(earlier[i - 1], '%Y-%m-%d')).days, 1)
        rates.append(delta / span)
    burn = sum(rates[-7:]) / len(rates[-7:])
    snap = load_benefit_cache()
    if not snap or not snap.get('accounts'):
        return None
    balance = sum(a.get('total', 0) for a in snap['accounts'])
    days = int(balance // burn) if burn > 0 else None
    return {'burn': round(burn, 1), 'balance': round(balance, 1), 'days': days}


def set_auto_schedule(enable: bool, log=print) -> bool:
    """创建/删除每日 09:05 自动签到的任务计划。"""
    exe = sys.executable if getattr(sys, 'frozen', False) else None
    if not exe:
        log("开发模式不支持任务计划。")
        return False
    tn = "WorkBuddySwitcher_AutoCheckin"
    if enable:
        cmd = f'"{exe}" --cli checkin'
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "09:05",
                            "/RL", "HIGHEST", "/TN", tn, "/TR", cmd],
                           capture_output=True, text=True, errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        ok = r.returncode == 0
        log("已创建每日 09:05 自动签到任务（最高权限，可无人值守打补丁）。" if ok
            else f"创建任务失败：{(r.stderr or r.stdout).strip()}")
        return ok
    r = subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"],
                       capture_output=True, text=True, errors="replace",
                       creationflags=CREATE_NO_WINDOW)
    log("已删除自动签到任务。" if r.returncode == 0 else "未找到自动签到任务。")
    return True


# ---------------------------------------------------------------- 连签保障（保活会话，CDP 驱动）
#
# 背景：签到活动的“连签奖励”要求账号当天发生过会话（系统侧判定活跃）。
# 纯 API 签到不会产生会话。这里通过 Chrome DevTools 协议驱动 WorkBuddy 界面，
# 自动发起一条最省积分的问答：
#   1) launch_app 启动应用时统一带上 --remote-debugging-port（仅监听本机回环）
#   2) 打开模型选择器，解析各档倍率（如“快速0.21x”“Hy3限时免费0.00x”），
#      自动选当前可用的最低倍率模型，会话结束后恢复原模型
#   3) 向输入框注入一句极短提示词并回车，等按钮从“停止”回到“发送”即完成
# 提示词极短 + 最低倍率模型 ≈ “消耗积分最小的最简会话”，全程不做任何文件补丁。

CDP_PORT = 9223

KEEPALIVE_STATE_PATH = TOOL_DIR / "keepalive.json"
KEEPALIVE_LOCK_PATH = TOOL_DIR / "keepalive.lock"


def _cdp_page_ws(timeout: float = 240.0) -> str | None:
    """轮询获取运行中 WorkBuddy 主窗口的 CDP WebSocket 地址。"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3) as r:
                targets = json.loads(r.read())
            page = next((t for t in targets if t.get("type") == "page"
                         and "devtools" not in t.get("url", "")), None)
            if page:
                return page.get("webSocketDebuggerUrl")
        except Exception:  # noqa: BLE001  端口未就绪/应用未启动
            pass
        time.sleep(2)
    return None


def _cdp_version() -> str:
    """从 CDP /json/version 的 UA 里取 WorkBuddy 版本号（如 5.8.3）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3) as r:
            ua = json.loads(r.read()).get("User-Agent", "")
        m = re.search(r"WorkBuddy/([\d.]+)", ua)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _note_wb_version(log=print) -> None:
    """记录 WorkBuddy 版本；升级时提醒。

    保活驱动依赖应用界面结构（模型选择器/发送按钮等），WorkBuddy 升级改版
    可能导致驱动失败。失败账号不会被标记完成、下次自动重试，这里把“升级”
    与“失败”关联起来，便于归因。
    """
    ver = _cdp_version()
    if not ver:
        return
    st = load_keepalive_state()
    old = st.get("_wb_version")
    if old == ver:
        return
    if old:
        log(f"ℹ 检测到 WorkBuddy 已升级（{old} → {ver}）。保活会话依赖应用界面结构，"
            f"若本次保活失败，多半是新版本改了界面，需要更新切换器适配。")
    st["_wb_version"] = ver
    try:
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        KEEPALIVE_STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    except OSError:
        pass


_JS_FOCUS = r"""
(() => {
  const el = document.querySelector('div[contenteditable="true"]');
  if (!el) return 'NO_EDITOR';
  el.focus();
  return 'READY';
})()
"""

_JS_STATE = r"""
(() => {
  const el = document.querySelector('div[contenteditable="true"]');
  let label = '', disabled = null;
  document.querySelectorAll('button').forEach(b => {
    const l = b.getAttribute('aria-label') || '';
    if (/发送|停止/.test(l)) { label = l; disabled = b.disabled === true; }
  });
  return JSON.stringify({editor: !!el, label, disabled,
                         modelTrigger: !!document.querySelector('.cr-model-selector__trigger')});
})()
"""

_JS_MODEL_OPEN = r"""
(() => {
  const t = document.querySelector('.cr-model-selector__trigger');
  if (!t) return JSON.stringify({ok: false, why: 'NO_TRIGGER'});
  const orig = (t.getAttribute('aria-label') || '').replace(/^Select model:\s*/i, '').trim();
  t.click();
  return JSON.stringify({ok: true, orig});
})()
"""

_JS_MODEL_ITEMS = r"""
(() => {
  const t = document.querySelector('.cr-model-selector__trigger');
  const orig = t ? (t.getAttribute('aria-label') || '').replace(/^Select model:\s*/i, '').trim() : '';
  const items = [...document.querySelectorAll('.cr-model-selector__item')];
  if (!items.length) return JSON.stringify({ok: false, why: 'NO_ITEMS', orig});
  const out = items.map((el, i) => {
    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
    const m = txt.match(/(\d+(?:\.\d+)?)x/i);
    const sel = el.getAttribute('aria-selected') === 'true' ||
                /selected/i.test(String(el.className));
    return {i, txt: txt.slice(0, 50), rate: m ? parseFloat(m[1]) : null, sel};
  });
  return JSON.stringify({ok: true, orig, items: out});
})()
"""

_JS_MODEL_CLICK = r"""
(() => {
  const items = [...document.querySelectorAll('.cr-model-selector__item')];
  if (__IDX__ >= items.length) return 'BAD_INDEX';
  items[__IDX__].click();
  return 'CLICKED';
})()
"""

_JS_MSG_COUNT = "document.querySelectorAll('[data-message-id]').length"


async def _model_menu(ev) -> dict:
    """确保模型下拉已展开并返回条目信息（已展开则不重复点击 trigger）。"""
    import asyncio
    try:
        info = json.loads(await ev(_JS_MODEL_ITEMS) or "{}")
    except (ValueError, TypeError):
        info = {}
    if info.get("ok") and info.get("items"):
        return info
    try:
        open_info = json.loads(await ev(_JS_MODEL_OPEN) or "{}")
    except (ValueError, TypeError):
        open_info = {}
    if not open_info.get("ok"):
        return {"ok": False, "orig": (open_info.get("orig") or "")}
    await asyncio.sleep(1.2)
    try:
        return json.loads(await ev(_JS_MODEL_ITEMS) or "{}")
    except (ValueError, TypeError):
        return {"ok": False}


async def _restore_model(ev, orig_name: str, log=print) -> None:
    """把模型选择恢复为保活前的档位（找不到则保持现状并收起下拉）。"""
    import asyncio
    info = await _model_menu(ev)
    items = info.get("items", []) if info.get("ok") else []
    hit = next((it for it in items if orig_name and orig_name in it.get("txt", "")), None)
    pick = hit["i"] if hit else next((it["i"] for it in items if it.get("sel")), None)
    if pick is not None:
        await ev(_JS_MODEL_CLICK.replace("__IDX__", str(pick)))
        await asyncio.sleep(0.5)
    if hit:
        log(f"    已恢复原模型（{orig_name}）。")
    else:
        log(f"    ⚠ 模型列表里没找到「{orig_name}」，请手动确认模型档位。")


async def _keepalive_turn(ws_url: str, prompt: str, log=print) -> bool:
    """驱动一轮最简问答（CDP）：选最低倍率模型 → 注入提示词回车 → 等完成。"""
    import asyncio
    import websockets

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        mid = 0

        async def send(method: str, params: dict | None = None,
                       timeout: float = 30.0) -> dict:
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
            while True:
                raw = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if raw.get("id") == mid:
                    return raw

        async def ev(expr: str, timeout: float = 30.0):
            r = await send("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True, "timeout": int(timeout * 1000)},
                           timeout + 10)
            det = r.get("result", {}).get("exceptionDetails")
            if det:
                desc = (det.get("exception") or {}).get("description") or det.get("text")
                raise RuntimeError(f"页面脚本执行失败：{desc}")
            return r.get("result", {}).get("result", {}).get("value")

        async def state() -> dict:
            try:
                return json.loads(await ev(_JS_STATE) or "{}")
            except (ValueError, TypeError):
                return {}

        # 0. 等输入框 + 模型选择器都出现（WB 冷启动后编辑器先出现、composer
        #    其他部分晚几秒才挂载，过早发送会无效）；若上一轮生成还在跑先等它结束
        label = ""
        t0 = time.time()
        while time.time() - t0 < 90:
            st = await state()
            label = st.get("label") or ""
            if st.get("editor") and st.get("modelTrigger") and "停止" not in label:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("会话界面未就绪（应用可能仍在启动或界面结构有变化）。")

        # 1. 模型选择：解析各档倍率，取最低；解析不了就保持默认
        target_idx = None
        info = await _model_menu(ev)
        orig_name = (info.get("orig") or "").strip()
        items = info.get("items", []) if info.get("ok") else []
        if items:
            rates = sorted((it for it in items if it.get("rate") is not None),
                           key=lambda it: (it["rate"], it["i"]))
            cur = next((it for it in items if it.get("sel")), None)
            pick = None
            if rates:
                lo = rates[0]
                if cur and cur["i"] == lo["i"]:
                    log(f"    当前模型已是最低倍率：{lo['txt']}（x{lo['rate']}）。")
                else:
                    target_idx = lo["i"]
                    pick = lo["i"]
                    cur_txt = (f"x{cur['rate']}"
                               if cur and cur.get("rate") is not None else "默认")
                    log(f"    选最低倍率模型：{lo['txt']}（x{lo['rate']}，原 {cur_txt}）。")
            else:
                log("    未能解析模型倍率，保持当前默认模型。")
            if pick is None and cur:  # 不切换也要收起下拉
                pick = cur["i"]
            if pick is not None:
                await ev(_JS_MODEL_CLICK.replace("__IDX__", str(pick)))
                await asyncio.sleep(1.0)
        else:
            log("    未找到模型选择器或模型列表为空，保持当前默认模型。")

        # 2. 注入提示词并发送（记录发送前消息数，用于完成判定）
        before_cnt = None
        try:
            if await ev(_JS_FOCUS) != "READY":
                raise RuntimeError("找不到会话输入框。")
            try:
                before_cnt = await ev(_JS_MSG_COUNT)
            except Exception:  # noqa: BLE001
                before_cnt = None
            await send("Input.insertText", {"text": prompt})
            # 确认发送按钮被激活（内容真正进了输入框），避免把消息发进未挂载完的界面
            typed = False
            t1 = time.time()
            while time.time() - t1 < 10:
                await asyncio.sleep(1)
                st = await state()
                lbl = st.get("label") or ""
                if "发送" in lbl and st.get("disabled") is False:
                    typed = True
                    break
            if not typed:
                raise RuntimeError("输入框未接受内容（发送按钮未激活）。")
            key = ({"type": "keyDown", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
                   {"type": "keyUp", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            # 回车后确认消息真的发出去了（按钮变“停止”或输入框清空）；最多重试 2 次
            sent = False
            for attempt in range(3):
                for args in key:
                    await send("Input.dispatchKeyEvent", args)
                t2 = time.time()
                while time.time() - t2 < 10:
                    await asyncio.sleep(1)
                    st = await state()
                    lbl = st.get("label") or ""
                    if "停止" in lbl or ("发送" in lbl and st.get("disabled") is True):
                        sent = True
                        break
                if sent:
                    break
                log(f"    回车未触发发送（第 {attempt + 1} 次），重试 …")
            if not sent:
                raise RuntimeError("回车未触发发送（界面事件未生效）。")
        except Exception:
            if target_idx is not None:
                await _restore_model(ev, orig_name, log)
            raise

        # 3. 等本轮完成：按钮回到“发送”态且消息数增加即完成；
        #    消息数拿不到时退化为按钮状态推断（停止→发送）
        t0 = time.time()
        seen_progress = False
        done = False
        while time.time() - t0 < 300:
            await asyncio.sleep(1)
            st = await state()
            label = st.get("label") or ""
            disabled = st.get("disabled")
            idle = bool(label) and "发送" in label and disabled is True
            if idle:
                cnt = None
                try:
                    cnt = await ev(_JS_MSG_COUNT)
                except Exception:  # noqa: BLE001
                    cnt = None
                if ((cnt is not None and before_cnt is not None and cnt > before_cnt)
                        or seen_progress):
                    done = True
                    break
            elif "停止" in label or (label and "发送" in label and disabled is False):
                seen_progress = True
        if not done:
            log(f"    ⚠ 消息已发出但 300 秒内未观察到完成状态（按钮：{label or '无'}，"
                f"模型可能响应过慢）。")

        # 4. 恢复原模型
        if target_idx is not None:
            await _restore_model(ev, orig_name, log)
        return done


_JS_CHECKIN_CARD = r"""
(() => {
  let btn = null;
  function walk(root, d) {
    if (d > 25 || btn) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const b = el.shadowRoot.querySelector(
          'button[data-track-id*="carousel_item_checkin"]');
        if (b) { btn = b; return; }
        walk(el.shadowRoot, d + 1);
      }
    }
  }
  walk(document, 0);
  if (!btn) return 'NO_CARD';
  return JSON.stringify({txt: (btn.textContent || '').trim(),
                         dis: btn.disabled === true});
})()
"""

_JS_CHECKIN_CARD_CLICK = r"""
(() => {
  let btn = null;
  function walk(root, d) {
    if (d > 25 || btn) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const b = el.shadowRoot.querySelector(
          'button[data-track-id*="carousel_item_checkin"]');
        if (b) { btn = b; return; }
        walk(el.shadowRoot, d + 1);
      }
    }
  }
  walk(document, 0);
  if (!btn) return 'NO_CARD';
  btn.click();
  return 'CLICKED';
})()
"""

_JS_CAROUSEL_DOTS = r"""
(() => {
  const dots = [];
  function walk(root, d) {
    if (d > 25) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        el.shadowRoot.querySelectorAll('[data-track-props]').forEach(x => {
          if (/"to"\s*:\s*\d+/.test(x.getAttribute('data-track-props') || ''))
            dots.push(x);
        });
        walk(el.shadowRoot, d + 1);
      }
    }
  }
  walk(document, 0);
  return String(dots.length);
})()
"""

_JS_CAROUSEL_DOT_CLICK_TMPL = r"""
(() => {
  const dots = [];
  function walk(root, d) {
    if (d > 25) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        el.shadowRoot.querySelectorAll('[data-track-props]').forEach(x => {
          if (/"to"\s*:\s*\d+/.test(x.getAttribute('data-track-props') || ''))
            dots.push(x);
        });
        walk(el.shadowRoot, d + 1);
      }
    }
  }
  walk(document, 0);
  if (!dots.length) return 'NO_DOTS';
  dots[__IDX__ % dots.length].click();
  return 'CLICKED';
})()
"""

_JS_CHECKIN_TOAST = r"""
(() => {
  const out = [];
  function walk(root, d) {
    if (d > 25) return;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        el.shadowRoot.querySelectorAll('.tt').forEach(t => {
          const s = (t.textContent || '').trim();
          if (s) out.push(s);
        });
        walk(el.shadowRoot, d + 1);
      }
    }
  }
  walk(document, 0);
  return JSON.stringify(out);
})()
"""


def checkin_via_card(ws_url: str, log=print) -> str:
    """签到首选：点侧栏 Buddy加油站 卡片的「立即领取」，由 WB 自己完成领取。

    这样卡片的本地状态会翻转为「已领取」（API 签到不会翻转它）。
    返回 'claimed'（本次完成领取）/ 'already'（今天已签）/ 'no_card'
    （卡片未出现或点击无效，调用方回退 API 签到）。"""
    import asyncio
    try:
        return asyncio.run(_checkin_card(ws_url, log))
    except Exception as e:  # noqa: BLE001
        log(f"    卡片领取失败（将回退 API 签到）：{e}")
        return 'no_card'


async def _checkin_card(ws_url: str, log=print) -> str:
    import asyncio
    import websockets

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        mid = 0

        async def send(method: str, params: dict | None = None,
                       timeout: float = 30.0) -> dict:
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
            while True:
                raw = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if raw.get("id") == mid:
                    return raw

        async def ev(expr: str):
            r = await send("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("result", {}).get("value")

        # 轮播卡片是远程运营组件、异步挂载，且只有激活的那张幻灯片才渲染。
        # 先等它自然轮播出现；不出现就逐个点击小圆点强制渲染每张幻灯片。
        async def find_card() -> str:
            raw = await ev(_JS_CHECKIN_CARD)
            if raw and raw != 'NO_CARD':
                try:
                    return json.loads(raw).get("txt") or ""
                except (ValueError, TypeError):
                    pass
            return ""

        txt = ""
        t0 = time.time()
        # 先等自然轮播 10 秒
        while time.time() - t0 < 10:
            txt = await find_card()
            if txt:
                break
            await asyncio.sleep(2)
        if not txt:
            # 手动翻页：每点一个圆点，等该幻灯片充分渲染后再决定换下一张，
            # 避免翻页过快与渲染竞态、刚好跳过签到卡片
            for i in range(8):
                await ev(_JS_CAROUSEL_DOT_CLICK_TMPL.replace("__IDX__", str(i)))
                t1 = time.time()
                while time.time() - t1 < 6:
                    await asyncio.sleep(1.5)
                    txt = await find_card()
                    if txt:
                        break
                if txt:
                    break
        if not txt:
            return 'no_card'
        if '已领取' in txt or '已签' in txt:
            return 'already'
        if '领取' not in txt and '签到' not in txt:
            return 'no_card'
        # 记录点击前已存在的提示语（提示语在 DOM 里会驻留，只认点击后新出现的）
        try:
            base_toasts = json.loads(await ev(_JS_CHECKIN_TOAST) or "[]")
        except (ValueError, TypeError):
            base_toasts = []
        base_joined = " ".join(str(t) for t in base_toasts)
        if await ev(_JS_CHECKIN_CARD_CLICK) != 'CLICKED':
            return 'no_card'
        # 点击后轮询：按钮翻转为已领取 → claimed；新提示“已签到/已领取” → already；
        # 新提示“领取失败”（重复点击限频等）→ 立即交回 API 兜底
        t1 = time.time()
        while time.time() - t1 < 12:
            raw = await ev(_JS_CHECKIN_CARD)
            cur = ""
            if raw and raw != 'NO_CARD':
                try:
                    cur = json.loads(raw).get("txt") or ""
                except (ValueError, TypeError):
                    cur = ""
            if '已领取' in cur or '已签' in cur:
                return 'claimed'
            try:
                toasts = json.loads(await ev(_JS_CHECKIN_TOAST) or "[]")
            except (ValueError, TypeError):
                toasts = []
            new_joined = " ".join(str(t) for t in toasts
                                  if str(t) not in base_joined)
            if '已签到' in new_joined or '已领取' in new_joined:
                return 'already'
            if '成功' in new_joined or '到账' in new_joined:
                return 'claimed'
            if '失败' in new_joined:
                log("    卡片领取返回失败（可能当日重复点击受限），回退 API 签到。")
                return 'no_card'
            await asyncio.sleep(1)
        return 'no_card'


def drive_keepalive_session(ws_url: str, prompt: str, log=print) -> bool:
    """驱动一轮最简会话：选最低倍率模型 → 发送一句话 → 等完成 → 恢复原模型。"""
    import asyncio
    try:
        return asyncio.run(_keepalive_turn(ws_url, prompt, log))
    except Exception as e:  # noqa: BLE001
        log(f"    CDP 驱动失败：{e}")
        return False


async def _resume_last_turn(ws_url: str, title: str, prompt: str, log=print) -> bool:
    """切换后自动续跑：侧栏打开最近会话 → 发送续跑指令 → 确认任务开跑。

    不等任务跑完（长任务可能数小时），确认进入生成态（按钮变“停止”）即返回。
    不改动模型档位，沿用会话原模型。"""
    import asyncio
    import websockets

    want = json.dumps(title, ensure_ascii=False)
    # open 判定用主区标题——DOM 里有 4 个 topbar-title（含隐藏模板），取可见且非空的
    js_find = ("(() => {const items=[...document.querySelectorAll("
               "'[class*=\"conversation-item\"]')];"
               "const hit=items.find(x=>(x.textContent||'').includes(" + want + "));"
               "const ts=[...document.querySelectorAll('[class*=\"topbar-title\"]')]"
               ".filter(e=>e.getBoundingClientRect().width>0&&(e.textContent||'').trim());"
               "const t=ts[0];"
               "const rr=hit?hit.getBoundingClientRect():null;"
               "return JSON.stringify({total:items.length,found:!!hit,"
               "open:!!t&&(t.textContent||'').includes(" + want + "),"
               "x:rr?Math.round(rr.x+rr.width/2):0,"
               "y:rr?Math.round(rr.y+rr.height/2):0});})()")

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        mid = 0

        async def send(method: str, params: dict | None = None,
                       timeout: float = 30.0) -> dict:
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method,
                                      "params": params or {}}))
            while True:
                raw = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if raw.get("id") == mid:
                    return raw

        async def ev(expr: str, timeout: float = 30.0):
            r = await send("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True, "timeout": int(timeout * 1000)},
                           timeout + 10)
            det = r.get("result", {}).get("exceptionDetails")
            if det:
                desc = (det.get("exception") or {}).get("description") or det.get("text")
                raise RuntimeError(f"页面脚本执行失败：{desc}")
            return r.get("result", {}).get("result", {}).get("value")

        async def state() -> dict:
            try:
                return json.loads(await ev(_JS_STATE) or "{}")
            except (ValueError, TypeError):
                return {}

        # 0. 等界面就绪（同保活：编辑器 + composer 挂载完）
        label = ""
        t0 = time.time()
        while time.time() - t0 < 90:
            st = await state()
            label = st.get("label") or ""
            if st.get("editor") and st.get("modelTrigger") and "停止" not in label:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("会话界面未就绪。")

        # 1. 侧栏找到最近会话并打开（列表虚拟化+异步渲染，轮询等待）
        opened = False
        t1 = time.time()
        while time.time() - t1 < 60:
            try:
                info = json.loads(await ev(js_find) or "{}")
            except (ValueError, TypeError):
                info = {}
            if info.get("open"):
                opened = True
                break
            if info.get("found"):
                x, y = int(info.get("x") or 0), int(info.get("y") or 0)
                if x > 0 and y > 0:
                    # CDP 真实鼠标事件（React 对合成 click 的响应不稳定）
                    for args in ({"type": "mousePressed", "x": x, "y": y,
                                  "button": "left", "clickCount": 1},
                                 {"type": "mouseReleased", "x": x, "y": y,
                                  "button": "left", "clickCount": 1}):
                        await send("Input.dispatchMouseEvent", args)
                    log(f"    已点击会话「{title[:30]}」。")
                    await asyncio.sleep(3)  # 等会话正文加载
                    continue
            await asyncio.sleep(2)
        if not opened:
            raise RuntimeError(f"未能打开会话「{title[:30]}」（侧栏可能仍在加载）。")

        # 2. 发送续跑指令
        if await ev(_JS_FOCUS) != "READY":
            raise RuntimeError("找不到会话输入框。")
        await send("Input.insertText", {"text": prompt})
        typed = False
        t2 = time.time()
        while time.time() - t2 < 10:
            await asyncio.sleep(1)
            st = await state()
            lbl = st.get("label") or ""
            if "发送" in lbl and st.get("disabled") is False:
                typed = True
                break
        if not typed:
            raise RuntimeError("输入框未接受续跑指令。")
        key = ({"type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
               {"type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        for attempt in range(3):
            for args in key:
                await send("Input.dispatchKeyEvent", args)
            t3 = time.time()
            while time.time() - t3 < 10:
                await asyncio.sleep(1)
                st = await state()
                if "停止" in (st.get("label") or ""):
                    return True
            log(f"    回车未触发发送（第 {attempt + 1} 次），重试 …")
        raise RuntimeError("续跑指令未发出（界面事件未生效）。")


def resume_last_session(ws_url: str, title: str, prompt: str, log=print) -> bool:
    """切换后自动续跑最近会话。返回 True=任务已开跑（无需人工点击）。"""
    import asyncio
    try:
        return asyncio.run(_resume_last_turn(ws_url, title, prompt, log))
    except Exception as e:  # noqa: BLE001
        log(f"    自动续跑失败：{e}")
        return False


def _last_session_title() -> str | None:
    """最近一条未删除会话的展示标题（custom_title 优先）。

    会话迁移只改 user_id 不动 updated_at，切换后查库得到的仍是
    被打断的那条任务，作为侧栏匹配凭据。"""
    if not DB_PATH.is_file():
        return None
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=10)
        try:
            row = con.execute(
                "SELECT COALESCE(NULLIF(custom_title,''), title) FROM sessions "
                "WHERE deleted_at IS NULL "
                "AND COALESCE(NULLIF(custom_title,''), title) != '' "
                "ORDER BY updated_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_keepalive_state() -> dict:
    try:
        return json.loads(KEEPALIVE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_keepalive_done(uid: str) -> None:
    st = load_keepalive_state()
    st[uid] = {"date": _today_key()}
    try:
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        KEEPALIVE_STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    except OSError:
        pass


class _KeepaliveLock:
    """跨进程互斥（GUI 与 09:05 定时任务可能同时跑）：文件字节锁，进程退出自动释放。"""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        import msvcrt
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(KEEPALIVE_LOCK_PATH, "a+b")
        deadline = time.time() + 300
        while True:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise RuntimeError("另一个签到/保活任务正在进行（等待 5 分钟超时）。")
                time.sleep(2)

    def __exit__(self, *exc):
        if self._fh:
            import msvcrt
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
        return False


def ensure_all_daily_sessions(cfg: dict, log=print) -> bool:
    """连签保障：确保每个账号今天都发生过一次会话（系统判定活跃、发连签奖励的依据）。

    只处理“今天还没保活过”的账号。当前账号若应用在跑就直接驱动（旧实例没带
    调试端口则重启一次补上），其余账号通过切换（退出→恢复登录态→重启）逐个
    驱动；结束后恢复原账号现场。
    """
    if not cfg.get("keepalive", True):
        log("连签保障未开启（积分面板可打开），跳过会话保活。")
        return True
    profiles = load_profiles()
    if not profiles:
        log("没有已登记账号，跳过会话保活。")
        return True
    state = load_keepalive_state()
    today = _today_key()
    todo = [p for p in profiles.values()
            if state.get(p.uid, {}).get("date") != today]
    if not todo:
        log("所有账号今天都已有保活会话，跳过。")
        return True

    exe = detect_app_exe(cfg)
    if not exe:
        log("⚠ 未找到 WorkBuddy.exe，无法发起保活会话。")
        return False

    prompt = (cfg.get("keepalive_prompt") or "1+1等于几？只回答数字，不要解释。").strip()
    sw = Switcher(log)
    ok_all = True

    with _KeepaliveLock():
        st = sw.status()
        origin_uid = st["uid"]
        origin_running = bool(st["running"])
        todo.sort(key=lambda p: 0 if p.uid == origin_uid else 1)  # 当前账号先做，省一次切换
        log(f"连签保障：{len(todo)} 个账号今天还没有会话"
            f"（{'、'.join(p.display for p in todo)}）。")
        cur_uid = origin_uid
        checked_ver = False
        try:
            for prof in todo:
                try:
                    if prof.uid != cur_uid:
                        log(f"— 切换到 {prof.display} 以发起保活会话 —")
                        sw.switch_to(prof.uid)
                        cur_uid = prof.uid
                    else:
                        log(f"— {prof.display} 是当前账号，直接发起保活会话 —")
                    ws_url = (_cdp_page_ws(timeout=45)
                              if wb_processes_running() else None)
                    if ws_url is None and prof.uid == cur_uid:
                        # 应用没在跑，或旧实例没带调试端口：重启一次以启用
                        if wb_processes_running():
                            log("WorkBuddy 已在运行但未开启调试端口，重启以启用 …")
                            kill_all_workbuddy(log)
                        launch_app(exe)
                        ws_url = _cdp_page_ws(timeout=240)
                    if ws_url is None:
                        raise RuntimeError("WorkBuddy 调试端口未就绪（应用未能启动）。")
                    if not checked_ver:
                        checked_ver = True
                        _note_wb_version(log)
                    # 签到：界面卡片领取优先（按钮翻转为「已领取」），API 兜底
                    try:
                        okc, act = checkin_status(prof, log=log)
                        signed = okc and isinstance(act, dict) \
                            and act.get('today_checked_in')
                    except Exception:  # noqa: BLE001
                        signed = False
                    if signed:
                        log(f"  {prof.display} 今天已签到。")
                    else:
                        r = checkin_via_card(ws_url, log)
                        if r == 'claimed':
                            log(f"✓ {prof.display} 已通过卡片领取今日签到奖励"
                                f"（界面按钮同步为已领取）。")
                        elif r == 'already':
                            log(f"  {prof.display} 今天已签到。")
                        else:
                            okc2, msgc = daily_checkin(prof, log=log)
                            log(f"  {prof.display} 签到（卡片未出现，走 API）：{msgc}")
                    if drive_keepalive_session(ws_url, prompt, log):
                        mark_keepalive_done(prof.uid)
                        log(f"✓ {prof.display} 保活会话完成，今天可正常累计连签。")
                    else:
                        raise RuntimeError("保活会话未完成。")
                except Exception as e:  # noqa: BLE001
                    ok_all = False
                    log(f"⚠ {prof.display} 保活会话失败：{e}")
        finally:
            # 恢复现场：回到操作前的登录账号 / 应用运行状态
            try:
                if not origin_running:
                    log("恢复现场：关闭 WorkBuddy（操作前它未在运行）。")
                    kill_all_workbuddy(log)
                elif origin_uid and cur_uid != origin_uid:
                    log("恢复现场：切回原账号。")
                    sw.switch_to(origin_uid)
            except Exception as e:  # noqa: BLE001
                log(f"⚠ 恢复现场失败：{e}")
    return ok_all


# ---------------------------------------------------------------- CLI（开发/调试用）

def run_cli(argv: list[str]) -> int:
    msgs: list[str] = []

    def log(m: str) -> None:
        msgs.append(m)
        try:
            print(m)
        except OSError:
            pass
        log_file_write("[cli] " + m)

    sw = Switcher(log)
    cmd = argv[0] if argv else "status"

    if cmd == "status":
        st = sw.status()
        cur = st["uid"]
        cur_prof = st["profiles"].get(cur) if cur else None
        log(f"登录态文件：{st['auth_file']}")
        log(f"当前账号：{(cur_prof.display if cur_prof else cur) if cur else '未登录'}"
            f"{'（未登记）' if cur and not cur_prof else ''}")
        log(f"WorkBuddy 运行中：{', '.join(sorted(st['running'])) if st['running'] else '否'}")
        log(f"应用路径：{st['exe'] or '未找到'}")
        log(f"已登记账号 {len(st['profiles'])} 个：")
        for p in st["profiles"].values():
            mark = " <- 当前" if p.uid == cur else ""
            log(f"  - {p.display}  uin={p.uin or '-'}  uid={p.uid[:8]}…{mark}")
        return 0

    if cmd == "list":
        for p in load_profiles().values():
            log(f"{p.uid}\t{p.display}\t{p.uin}")
        return 0

    if cmd == "add":
        r = sw.add_current()
        return 0 if r else 1

    if cmd == "checkin":
        # 先走逐账号流程：界面卡片领取优先（按钮同步为已领取）+ 连签保活，
        # 卡片未出现时流程内部已用 API 兜底
        ensure_all_daily_sessions(sw.cfg, log)
        # 清扫：循环未覆盖的账号（如今天已保活但还没签到）用 API 补签（幂等）
        ok = auto_checkin_all(log)
        benefit_snapshot(log)  # 顺手刷新主页面提醒条的缓存
        snapshot_usage_history()  # 记录当日积分快照（次日差值算精确日消耗）
        if sw.cfg.get("token_refresh", True):
            # 切换式令牌保活：refresh token 剩余 <14 天的账号让应用自行续期
            #（需要切换账号、每次约 3 分钟，放在最后避免拖慢签到主流程）
            refresh_all_tokens(Switcher(log), log)
        return 0 if ok else 1

    if cmd == "keepalive":
        ok = ensure_all_daily_sessions(sw.cfg, log)
        return 0 if ok else 1

    if cmd == "refresh":
        ok = refresh_all_tokens(Switcher(log), log)
        return 0 if ok else 1

    if cmd == "guard":
        # CLI 一次性执行积分保护检查（--force 忽略 GUI 开关）
        ok, msg = guard_switch_if_low(sw.cfg, log, force="--force" in argv)
        if not ok and msg:
            log(msg)
        return 0 if ok else 1

    if cmd == "credits":
        st = local_usage_stats()
        if st:
            log(f"今日积分消耗: {st['today_credits']} / 累计: {st['total_credits']}")
            log(f"今日上下文: {st['today_ctx']} / 累计: {st['total_ctx']} tokens, 会话 {st['sessions']} 个")
        return 0

    if cmd == "switch":
        if len(argv) < 2:
            log("用法：switch <uid|别名> [--memory shared|private]")
            return 2
        key = argv[1]
        target = None
        for p in load_profiles().values():
            if p.uid == key or p.alias == key or p.uin == key:
                target = p
                break
        if not target:
            log(f"未找到账号：{key}")
            return 1
        memory_mode = None
        if "--memory" in argv:
            i = argv.index("--memory")
            if i + 1 < len(argv):
                memory_mode = argv[i + 1]
        sw.switch_to(target.uid, dry_run="--dry-run" in argv, memory_mode=memory_mode)
        return 0

    log(f"未知命令：{cmd}（支持 status/list/add/checkin/keepalive/switch）")
    return 2


# ---------------------------------------------------------------- 单实例

MUTEX_NAME = r"Local\WorkBuddySwitcher.SingleInstance"
MAIN_WINDOW_TITLE = "WorkBuddy 账号切换器"


def acquire_single_instance(log=print) -> bool:
    """GUI 单实例：命名互斥量防止重复启动；已在运行时把已有窗口拉到前台。

    只约束图形界面；--cli（定时签到）不受影响，可与 GUI 并行。
    """
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, MAIN_WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE：托盘最小化时也能唤起
            user32.SetForegroundWindow(hwnd)
            log("切换器已在运行，已唤起主窗口。")
        else:
            log("切换器已在运行（可能最小化在托盘）。")
        return False
    return True


# ---------------------------------------------------------------- GUI

def run_gui() -> None:
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
    from tkinter.scrolledtext import ScrolledText

    q = queue.Queue()
    sw = Switcher(lambda m: q.put(("info", m)))
    busy = {"flag": False}

    root = tk.Tk()
    root.title("WorkBuddy 账号切换器")
    root.geometry("900x760")
    root.minsize(820, 620)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    # ---- 标签页：账号切换 / 积分签到 / 使用统计（不再另开独立窗口）
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
    tab_main = ttk.Frame(nb)
    tab_benefit = ttk.Frame(nb)
    tab_usage = ttk.Frame(nb)
    nb.add(tab_main, text="  账号切换  ")
    nb.add(tab_benefit, text="  积分 / 签到  ")
    nb.add(tab_usage, text="  使用统计  ")

    # 两个数据页只构建一次，之后切回时只刷新数据（带 TTL，避免频繁切页重复请求接口）
    tab_cache = {"benefit": None, "usage": None}

    def _ensure_benefit(force: bool = False) -> None:
        if tab_cache["benefit"] is None:
            tab_cache["benefit"] = build_benefits_tab(tab_benefit, sw)
        else:
            tab_cache["benefit"](force)

    def _ensure_usage(force: bool = False) -> None:
        if tab_cache["usage"] is None:
            tab_cache["usage"] = build_usage_tab(tab_usage)
        else:
            tab_cache["usage"](force)

    def show_tab(idx: int) -> None:
        """切到指定标签页（托盘菜单 / 提醒条 / 按钮共用）。"""
        try:
            root.deiconify()
            root.lift()
        except tk.TclError:
            pass
        try:
            same = nb.index(nb.select()) == idx
        except tk.TclError:
            same = False
        nb.select(idx)
        if same:  # 已在目标页：当作手动刷新
            if idx == 1:
                _ensure_benefit(True)
            elif idx == 2:
                _ensure_usage(True)

    def on_tab_changed(_e=None) -> None:
        try:
            idx = nb.index(nb.select())
        except tk.TclError:
            return
        if idx == 1:
            _ensure_benefit()
        elif idx == 2:
            _ensure_usage()

    nb.bind("<<NotebookTabChanged>>", on_tab_changed)

    # ---- 顶部状态
    top = ttk.Frame(tab_main, padding=(12, 10, 12, 4))
    top.pack(fill="x")
    status_var = tk.StringVar(value="读取状态中…")
    ttk.Label(top, textvariable=status_var, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    detail_var = tk.StringVar(value="")
    ttk.Label(top, textvariable=detail_var, foreground="#666").pack(anchor="w")

    # ---- 积分提醒条（主页面一眼可见，点击打开积分面板）
    banner = tk.Text(top, height=2, wrap="word", relief="flat", bd=0, highlightthickness=0,
                     cursor="hand2", font=("Microsoft YaHei UI", 9), bg=root.cget('bg'))
    banner.pack(fill="x", pady=(4, 0))
    banner.tag_configure("red", foreground="#c0392b")
    banner.tag_configure("orange", foreground="#d68910")
    banner.tag_configure("gray", foreground="#888")
    banner.configure(state="disabled")
    banner.bind("<Button-1>", lambda e: show_tab(1))

    def render_banner(snap):
        banner.configure(state="normal")
        banner.delete("1.0", "end")
        if not snap or not snap.get('accounts'):
            banner.insert("end", "积分提醒：暂无数据，点此打开积分面板刷新", ("gray",))
        else:
            reds = [(a['alias'], a['exp7']) for a in snap['accounts'] if a.get('exp7', 0) > 0]
            ts = (snap.get('updated_at') or '')[5:16].replace('T', ' ')
            bal = "，".join(f"{a['alias']} {a['total']:g}" for a in snap['accounts'])
            if reds:
                banner.insert("end", "⚠ 7 天内到期 ", ("red",))
                banner.insert("end", "，".join(f"{a} {v:g} 分" for a, v in reds), ("red",))
            else:
                banner.insert("end", "✓ 7 天内无积分到期", ("gray",))
            banner.insert("end", f"　· 余额 {bal} · 截至 {ts}，点击看明细", ("gray",))
            fc = burn_forecast()
            if fc and fc.get('days') is not None:
                tag = "red" if fc['days'] <= 7 else ("orange" if fc['days'] <= 14 else "gray")
                banner.insert("end", f"　· 按当前速度（{fc['burn']:g} 分/天）约可用 {fc['days']} 天", (tag,))
            elif fc:
                banner.insert("end", f"　· 近 7 日无积分消耗", ("gray",))
        banner.configure(state="disabled")

    def refresh_banner_async():
        def job():
            q.put(("banner", benefit_snapshot(lambda m: None)))
        threading.Thread(target=job, daemon=True).start()

    _tray = {}

    def tray_notify(msg):
        icon = _tray.get('icon')
        if icon:
            try:
                icon.notify(msg, "WorkBuddy 切换器")
            except Exception:
                pass

    def stop_tray():
        """停掉托盘图标并从登记表里摘掉，保证退出路径一定可用。"""
        icon = _tray.pop('icon', None)
        if icon:
            try:
                icon.stop()
            except Exception:
                pass

    def start_tray():
        try:
            import pystray
            from PIL import Image
        except Exception as e:  # noqa: BLE001
            ui_log("info", f"托盘不可用（{e}）")
            return
        if getattr(sys, 'frozen', False):
            ico_path = os.path.join(os.path.dirname(sys.executable), 'wbicon.ico')
            # 打包时图标作为数据文件打进了 _MEIPASS，exe 同目录若没有就回退去那里找
            if not os.path.isfile(ico_path):
                meipass = getattr(sys, '_MEIPASS', None)
                if meipass:
                    ico_path = os.path.join(meipass, 'wbicon.ico')
        else:
            ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wbicon.ico')
        try:
            img = Image.open(ico_path)
        except Exception as e:  # noqa: BLE001
            ui_log("info", f"托盘图标加载失败：{e}")
            return

        def make_menu():
            items = [pystray.MenuItem("显示主窗口", lambda *a: q.put(("tray_show", None)), default=True),
                     pystray.Menu.SEPARATOR]
            for p in load_profiles().values():
                items.append(pystray.MenuItem(f"切换到 {p.display}",
                                              lambda *a, uid=p.uid: q.put(("tray_switch", uid))))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("积分 / 签到面板", lambda *a: q.put(("open_benefits", None))))
            items.append(pystray.MenuItem("使用统计", lambda *a: q.put(("open_usage", None))))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("退出", lambda *a: q.put(("app_exit", None))))
            return pystray.Menu(*items)

        icon = pystray.Icon("WorkBuddySwitcher", img, "WorkBuddy 账号切换器",
                            menu=pystray.Menu(make_menu))
        _tray['icon'] = icon

        def run_icon():
            try:
                icon.run_detached()
            except Exception as e:  # noqa: BLE001
                # 起不来就把图标摘掉：否则点关闭会最小化到托盘而托盘实际不存在，退不掉
                _tray.pop('icon', None)
                ui_log("info", f"托盘启动失败：{e}")

        threading.Thread(target=run_icon, daemon=True).start()

    # ---- 账号列表
    mid = ttk.Frame(tab_main, padding=(12, 6))
    mid.pack(fill="both", expand=True)
    cols = ("alias", "uin", "uid", "last")
    tree = ttk.Treeview(mid, columns=cols, show="headings", height=7)
    for cid, text, w in [("alias", "别名", 140), ("uin", "账号 (uin)", 130),
                         ("uid", "uid", 250), ("last", "上次使用", 130)]:
        tree.heading(cid, text=text)
        tree.column(cid, width=w, anchor="w")
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    # ---- 按钮
    btns = ttk.Frame(tab_main, padding=(12, 2))
    btns.pack(fill="x")
    btn_switch = ttk.Button(btns, text="切换到选中账号")
    btn_add = ttk.Button(btns, text="登记当前登录账号")
    # 「新增账号」和「完成登记」是同一个向导的两步，用分组框提示
    grp_new = ttk.LabelFrame(btns, text="新账号向导", padding=(4, 2))
    btn_new = ttk.Button(grp_new, text="新增账号（扫码/验证码）…")
    btn_finish = ttk.Button(grp_new, text="完成登记（登录新账号后点这里）")
    btn_new.pack(side="left", padx=(0, 4))
    btn_finish.pack(side="left")
    btn_alias = ttk.Button(btns, text="修改别名")
    btn_refresh = ttk.Button(btns, text="刷新")
    btn_pick_exe = ttk.Button(btns, text="指定 WorkBuddy 安装路径…")
    btn_switch.pack(side="left", padx=(0, 6))
    btn_add.pack(side="left", padx=(0, 6))
    grp_new.pack(side="left", padx=(0, 6))
    btn_alias.pack(side="left", padx=(0, 6))
    btn_refresh.pack(side="left", padx=(0, 6))
    btn_pick_exe.pack(side="left")

    # ---- 选项
    opts = ttk.Frame(tab_main, padding=(12, 2))
    opts.pack(fill="x")
    memory_mode_var = tk.StringVar(value=str(sw.cfg.get("memory_mode") or "shared"))
    ttk.Label(opts, text="记忆模式：").pack(side="left")
    ttk.Radiobutton(opts, text="共享（各账号同一份记忆）", value="shared",
                    variable=memory_mode_var).pack(side="left")
    ttk.Radiobutton(opts, text="独立（各账号用自己的）", value="private",
                    variable=memory_mode_var).pack(side="left", padx=(8, 0))
    opts2 = ttk.Frame(tab_main, padding=(12, 0))
    opts2.pack(fill="x")
    share_sidebar_var = tk.BooleanVar(value=bool(sw.cfg.get("share_sidebar")))
    share_auto_var = tk.BooleanVar(value=bool(sw.cfg.get("share_automations")))
    ttk.Checkbutton(opts2, text="共享侧栏会话列表缓存", variable=share_sidebar_var).pack(side="left")
    ttk.Checkbutton(opts2, text="同时共享定时任务", variable=share_auto_var).pack(side="left", padx=(12, 0))
    credit_guard_var = tk.BooleanVar(value=bool(sw.cfg.get("credit_guard")))
    credit_thr_var = tk.StringVar(value=str(sw.cfg.get("credit_guard_threshold", 100)))
    ttk.Checkbutton(opts2, text="积分不足自动切换（长任务保护）",
                    variable=credit_guard_var).pack(side="left", padx=(12, 0))
    ttk.Label(opts2, text="阈值").pack(side="left", padx=(6, 2))
    ttk.Spinbox(opts2, from_=0, to=100000, increment=50, width=6,
                textvariable=credit_thr_var).pack(side="left")
    ttk.Label(opts2, text="分").pack(side="left", padx=(2, 0))
    opts3 = ttk.Frame(tab_main, padding=(12, 0))
    opts3.pack(fill="x")
    credit_autoresume_var = tk.BooleanVar(value=bool(sw.cfg.get("credit_guard_autoresume")))
    credit_prompt_var = tk.StringVar(value=str(sw.cfg.get("credit_guard_resume_prompt") or "继续"))
    ttk.Checkbutton(opts3, text="切换后自动续跑最近会话（无需点击）",
                    variable=credit_autoresume_var).pack(side="left")
    ttk.Label(opts3, text="续跑指令").pack(side="left", padx=(12, 2))
    ttk.Entry(opts3, textvariable=credit_prompt_var, width=26).pack(side="left")

    # ---- 日志
    logf = ttk.LabelFrame(tab_main, text="执行日志", padding=(6, 4))
    logf.pack(fill="both", expand=True, padx=12, pady=(6, 10))
    logbox = ScrolledText(logf, height=10, state="disabled", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True)

    def ui_log(level: str, msg: str) -> None:
        logbox.configure(state="normal")
        logbox.insert("end", msg + "\n")
        if level == "error":
            logbox.tag_add("err", "end-2l", "end-1l")
            logbox.tag_configure("err", foreground="#c0392b")
        logbox.see("end")
        logbox.configure(state="disabled")

    def save_opts() -> None:
        sw.cfg["share_sidebar"] = bool(share_sidebar_var.get())
        sw.cfg["share_automations"] = bool(share_auto_var.get())
        sw.cfg["memory_mode"] = memory_mode_var.get()
        sw.cfg["credit_guard"] = bool(credit_guard_var.get())
        try:
            sw.cfg["credit_guard_threshold"] = max(0, int(float(credit_thr_var.get())))
        except (TypeError, ValueError):
            sw.cfg["credit_guard_threshold"] = 100
        sw.cfg["credit_guard_autoresume"] = bool(credit_autoresume_var.get())
        sw.cfg["credit_guard_resume_prompt"] = credit_prompt_var.get().strip() or "继续"
        save_config(sw.cfg)

    def set_busy(b: bool) -> None:
        busy["flag"] = b
        state = "disabled" if b else "normal"
        for btn in (btn_switch, btn_add, btn_new, btn_alias, btn_refresh):
            btn.configure(state=state)

    def refresh() -> None:
        try:
            st = sw.status()
        except Exception as e:  # noqa: BLE001
            ui_log("error", f"读取状态失败：{e}")
            return
        cur = st["uid"]
        profiles = st["profiles"]
        if cur:
            cur_prof = profiles.get(cur)
            name = cur_prof.display if cur_prof else f"{cur[:8]}…（未登记）"
            status_var.set(f"当前登录：{name}")
        else:
            status_var.set("当前登录：未登录（或登录文件缺失）")
        detail_var.set(
            f"WorkBuddy {'运行中' if st['running'] else '未运行'} · 应用：{Path(st['exe']).parent if st['exe'] else '未找到'}"
            f" · 已登记 {len(profiles)} 个账号")
        for iid in tree.get_children():
            tree.delete(iid)
        for p in sorted(profiles.values(), key=lambda x: x.last_used, reverse=True):
            mark = "✓ 当前" if p.uid == cur else ""
            tree.insert("", "end", iid=p.uid, values=(
                p.display, p.uin or "-", p.uid, (p.last_used or "-")[:16] + mark))
        if cur and cur not in profiles:
            tree.insert("", "end", iid=cur, values=("（未登记）", "-", cur, "-"))
        # 自动选中当前账号所在行：切换后列表重建会清空选中状态，避免“无选中”死循环
        if cur:
            try:
                tree.selection_set(cur)
                tree.see(cur)
            except tk.TclError:
                pass
        render_banner(load_benefit_cache())

    def run_async(fn) -> None:
        def worker():
            err = None
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                err = e
            # 按钮状态恢复不经过消息队列：即使轮询循环出问题也保证解锁
            def finish():
                if err is not None:
                    import traceback
                    ui_log("error", f"操作失败：{err}")
                    ui_log("error", "".join(traceback.format_exception(err)[-4:]))
                set_busy(False)
                try:
                    refresh()
                except Exception as e2:  # noqa: BLE001
                    import traceback
                    ui_log("error", f"刷新界面出错：{e2}")
                    ui_log("error", "".join(traceback.format_exception(e2)[-4:]))
            try:
                root.after(0, finish)
            except RuntimeError:
                pass  # 窗口已关闭
        set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def poll_queue() -> None:
        try:
            while True:
                level, msg = q.get_nowait()
                try:
                    if level == "__done__":
                        set_busy(False)
                        refresh()
                    elif level == "banner":
                        render_banner(msg)
                    # ---- 托盘菜单消息（必须在主线程里处理，不能放进子窗口的轮询）
                    elif level == "tray_show":
                        try:
                            root.deiconify()
                            root.lift()
                            root.focus_force()
                        except Exception:
                            pass
                    elif level == "tray_switch":
                        if busy["flag"]:
                            tray_notify("有操作正在进行，请稍后再试")
                        else:
                            prof = load_profiles().get(msg)
                            alias = prof.display if prof else str(msg)[:8]
                            ui_log("info", f"（托盘）开始切换到 {alias} …")
                            run_async(lambda uid=msg, al=alias: (sw.switch_to(uid),
                                                                 tray_notify(f"已切换到 {al}")))
                    elif level == "open_benefits":
                        show_tab(1)
                    elif level == "open_usage":
                        show_tab(2)
                    elif level == "app_exit":
                        stop_tray()
                        root.destroy()
                        return  # 不再续排轮询，让主循环自然退出
                    else:
                        ui_log(level, msg)
                except Exception as e:  # noqa: BLE001
                    # 单条消息处理出错不能弄死整个轮询循环
                    import traceback
                    try:
                        ui_log("error", f"界面消息处理出错（已恢复）：{e}")
                        ui_log("error", "".join(traceback.format_exception(e)[-4:]))
                    except Exception:
                        pass
        except queue.Empty:
            pass
        root.after(120, poll_queue)

    # ---- 运行期后台任务：每 auto_checkin_interval 分钟补签 + 每日令牌保活
    # 线程里不能直接动 Tk，统一经 q 投递回主线程（info/banner 分支已存在）。
    _auto = {"next_checkin": time.time() + 90, "refresh_day": None}

    def catchup_checkin():
        def job():
            try:
                if not get_protector_key(lambda m: None):
                    return
                for p in load_profiles().values():
                    try:
                        ok, data = checkin_status(p, log=lambda m: None)
                        if ok and isinstance(data, dict) and data.get('today_checked_in'):
                            continue  # 已签，不打扰
                        _, msg = daily_checkin(p, log=lambda m: None)
                        q.put(("info", f"（自动补签）{p.display}：{msg}"))
                    except Exception as e:  # noqa: BLE001
                        q.put(("error", f"（自动补签）{p.display}：{e}"))
                q.put(("banner", benefit_snapshot(lambda m: None)))
            except Exception as e:  # noqa: BLE001
                q.put(("error", f"（自动补签）任务出错：{e}"))
        threading.Thread(target=job, daemon=True).start()

    def credit_guard_tick():
        def job():
            try:
                switched, _ = guard_switch_if_low(
                    sw.cfg, log=lambda m: q.put(("info", m)))
            except Exception as e:  # noqa: BLE001
                q.put(("error", f"（积分保护）检查出错：{e}"))
                return
            if switched:
                # 切换完成后冷却 15 分钟：等应用重启稳定，也避免立刻重复触发
                _auto["next_credit"] = time.time() + 15 * 60
                try:
                    root.after(0, refresh)  # 刷新“当前登录”与账号列表
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=job, daemon=True).start()

    def token_refresh_check():
        """GUI 内只做检测与提醒：续期需要切换账号（约 3 分钟/个，会打断使用），
        交给一键签到或每日 09:05 定时任务在无人值守时段完成。"""
        def job():
            need = [p.display for p in load_profiles().values() if _refresh_needed(p)]
            if need:
                q.put(("info", f"（令牌保活）{'、'.join(need)} 的登录令牌将在 14 天内到期，"
                               "下次一键签到时会自动切换续期，无需手动处理。"))
        threading.Thread(target=job, daemon=True).start()

    def auto_tick():
        try:
            if not busy["flag"]:
                today = datetime.now().strftime('%Y-%m-%d')
                try:
                    iv = int(sw.cfg.get("auto_checkin_interval", 30))
                except (TypeError, ValueError):
                    iv = 30
                if iv > 0 and time.time() >= _auto["next_checkin"]:
                    _auto["next_checkin"] = time.time() + max(iv, 1) * 60
                    catchup_checkin()
                if (sw.cfg.get("credit_guard", False) and not busy["flag"]
                        and time.time() >= _auto.get("next_credit", 0)):
                    _auto["next_credit"] = time.time() + 120  # 每 2 分钟查一次余额
                    credit_guard_tick()
                if _auto["refresh_day"] != today and sw.cfg.get("token_refresh", True):
                    _auto["refresh_day"] = today
                    token_refresh_check()
        except Exception:  # noqa: BLE001  定时循环自身绝不能死
            pass
        root.after(60_000, auto_tick)

    root.after(30_000, auto_tick)

    def do_switch() -> None:
        sel = tree.selection()
        if not sel:
            # 用非模态提示代替弹窗：避免 Enter 关闭提示时焦点再次触发按钮、循环弹框
            ui_log("info", "请先在列表中单击选中要切换到的账号，再点「切换到选中账号」。")
            return
        target = sel[0]
        profiles = load_profiles()
        prof = profiles.get(target)
        if prof is None:
            messagebox.showwarning("提示", "该账号还没有登录快照，无法切换。", parent=root)
            return
        save_opts()
        mem_txt = ("记忆同步为最新共享内容" if memory_mode_var.get() == "shared"
                   else "记忆保持各自独立（不改动记忆文件）")
        if not messagebox.askyesno(
                "确认切换",
                f"将执行：\n\n"
                f"1. 完全退出 WorkBuddy\n"
                f"2. 保存当前账号登录态，恢复为「{prof.display}」\n"
                f"3. 本地会话共享给该账号；{mem_txt}\n"
                f"4. 重新启动 WorkBuddy\n\n"
                f"※ 若 WorkBuddy 正以管理员身份运行，切换器会自动申请管理员权限"
                f"来结束它；如系统弹出确认窗口，请点「是」。\n"
                f"请确认 WorkBuddy 里没有正在执行的任务。",
                parent=root):
            return
        ui_log("info", f"—— 开始切换到 {prof.display} ——")
        run_async(lambda: sw.switch_to(target))

    def do_add() -> None:
        save_opts()
        run_async(sw.add_current)

    def do_login_new() -> None:
        save_opts()
        if not messagebox.askyesno(
                "新增账号",
                "这是【新增账号】向导，不是切换！\n"
                "如果只是想切换到已登记的账号，请改用『切换到选中账号』。\n\n"
                "确认要新增账号后，将执行：\n\n"
                "1. 完全退出 WorkBuddy\n"
                "2. 暂时移开登录文件（应用会显示登录界面）\n"
                "3. 重新启动 WorkBuddy\n\n"
                "登录完成后，回到本窗口点『完成登记』。",
                parent=root):
            return
        run_async(sw.begin_login_new)

    def do_finish_new() -> None:
        run_async(sw.finish_login_new)

    def do_alias() -> None:
        sel = tree.selection()
        if not sel:
            ui_log("info", "请先在列表中选中一个账号，再点「修改别名」。")
            return
        uid = sel[0]
        profiles = load_profiles()
        prof = profiles.get(uid)
        old = prof.display if prof else ""
        new = simpledialog.askstring("修改别名", "输入显示别名（留空则用默认名称）：",
                                     initialvalue=old, parent=root)
        if new is None:
            return
        try:
            sw.set_alias(uid, new)
            refresh()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e), parent=root)

    def do_pick_exe() -> None:
        path = filedialog.askopenfilename(
            title="选择 WorkBuddy.exe",
            filetypes=[("WorkBuddy.exe", "WorkBuddy.exe"), ("所有程序", "*.exe")],
            parent=root)
        if path:
            sw.cfg["exe_path"] = path
            save_config(sw.cfg)
            refresh()

    def export_backup() -> None:
        path = filedialog.asksaveasfilename(defaultextension=".zip", initialfile="wb-switcher-backup.zip",
                                            filetypes=[("ZIP", "*.zip")], parent=root)
        if not path:
            return
        try:
            import zipfile
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                if PROFILES_DIR.is_dir():
                    for f in PROFILES_DIR.rglob('*'):
                        if f.is_file():
                            z.write(f, os.path.relpath(f, TOOL_DIR))
                for f in (KEYCACHE_PATH, CONFIG_PATH, USAGE_HISTORY_PATH, BENEFIT_CACHE_PATH):
                    if f.is_file():
                        z.write(f, f.name)
            ui_log("info", f"已导出账号数据备份：{path}")
            messagebox.showinfo("导出完成",
                                f"已导出到：\n{path}\n\n注意：令牌加密与当前电脑绑定，"
                                "跨电脑导入后各账号需重新登录一次。",
                                parent=root)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e), parent=root)

    def import_backup() -> None:
        path = filedialog.askopenfilename(filetypes=[("ZIP", "*.zip")], parent=root)
        if not path:
            return
        if not messagebox.askyesno("导入账号数据", "将覆盖当前的账号快照与配置文件，是否继续？",
                                   parent=root):
            return
        try:
            import zipfile
            tool_root = os.path.abspath(TOOL_DIR)
            with zipfile.ZipFile(path) as z:
                for m in z.namelist():
                    norm = os.path.normpath(m)
                    if norm.startswith('..') or os.path.isabs(norm):
                        continue  # 防 zip-slip
                    target = os.path.join(tool_root, norm)
                    if not os.path.abspath(target).startswith(tool_root):
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(m) as src, open(target, 'wb') as dst:
                        dst.write(src.read())
            ui_log("info", f"已导入账号数据备份：{path}")
            refresh()
            refresh_banner_async()
            messagebox.showinfo("导入完成",
                                "导入完成。若备份来自其他电脑，各账号需在 WorkBuddy 重新登录一次"
                                "（令牌加密与电脑绑定）。",
                                parent=root)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导入失败", str(e), parent=root)

    btn_switch.configure(command=do_switch)
    btn_add.configure(command=do_add)
    btn_new.configure(command=do_login_new)
    btn_finish.configure(command=do_finish_new)
    btn_alias.configure(command=do_alias)
    btn_refresh.configure(command=lambda: (refresh(), refresh_banner_async()))
    btn_pick_exe.configure(command=do_pick_exe)

    # 菜单里补充说明
    menubar = tk.Menu(root)
    helpmenu = tk.Menu(menubar, tearoff=0)
    helpmenu.add_command(label="工作原理", command=lambda: messagebox.showinfo(
        "工作原理",
        "· 登录态保存在 CodeBuddyExtension\\Data\\Public\\auth\\ 下的 .info 文件，切换器为每个账号保存快照，"
        "切换 = 退出应用 → 快照当前 → 恢复目标 → 重启。\n\n"
        "· 会话共享：本地会话在 workbuddy.db 中按账号过滤，切换时把全部本地会话划归当前账号"
        "（cloud 会话除外，仍属原账号）。\n\n"
        "· 记忆模式：共享 = 切换时把活跃账号的记忆复制给下一个账号（各账号同一份）；"
        "独立 = 切换时不碰记忆文件，各账号用自己的记忆。\n\n"
        "· 记忆文件：~/.workbuddy/memory/<uid>_memory.md 按账号隔离，复制时自动改写内嵌 uid，"
        "否则应用会把它重置为空。\n\n"
        "· 每次切换前自动备份登录态和数据库（保留最近 10 份）。\n\n"
        "注意：每个账号至少每 40 天登录活跃一次，避免 refresh token 过期。",
        parent=root))
    def open_data_dir() -> None:
        try:
            TOOL_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(TOOL_DIR)  # noqa: S606
        except OSError as e:
            messagebox.showerror("失败", f"无法打开目录：{e}", parent=root)

    helpmenu.add_command(label="打开数据目录", command=open_data_dir)
    helpmenu.add_separator()
    helpmenu.add_command(label="退出", command=lambda: (stop_tray(), root.destroy()))
    backupmenu = tk.Menu(menubar, tearoff=0)
    backupmenu.add_command(label="导出账号数据…", command=export_backup)
    backupmenu.add_command(label="导入账号数据…", command=import_backup)
    menubar.add_cascade(label="备份", menu=backupmenu)
    menubar.add_cascade(label="帮助", menu=helpmenu)
    root.configure(menu=menubar)

    def on_close():
        # 只有托盘图标确实起来了才最小化到托盘；否则直接退出，
        # 避免“点关闭后窗口没了、又没有任何入口能退出”的死局。
        if _tray.get('icon'):
            root.withdraw()
            tray_notify("已最小化到托盘：右键托盘图标可切换账号、打开面板或退出")
        else:
            stop_tray()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    refresh()
    refresh_banner_async()
    start_tray()
    poll_queue()
    ui_log("info", "就绪。切换前请确认 WorkBuddy 中没有正在运行的任务。")
    root.mainloop()


def collect_usage_stats():
    """汇总本地使用统计：每日积分/Token/会话数、累计、峰值、连续天数、最长会话。

    积分日消耗：credit_json 的 key 是 UUIDv7（含请求发生时间戳），逐笔按真实发生日归档；
    非时间序 key 退回会话最后活跃日。当天数值若存在昨日快照，用快照差值精确覆盖。
    Token 始终按最后活跃日归档（上下文会压缩重置，无法做差值）。
    """
    if not DB_PATH.is_file():
        return None
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    daily = {}
    longest_ms = 0
    try:
        for cj, used, upd in con.execute(
                "SELECT credit_json, used, updated_at FROM session_usage WHERE updated_at IS NOT NULL"):
            day = datetime.fromtimestamp(upd / 1000).strftime('%Y-%m-%d')
            d = daily.setdefault(day, {'credits': 0.0, 'tokens': 0, 'sessions': 0})
            d['tokens'] += int(used or 0)
            d['sessions'] += 1
            try:
                if cj and cj != 'None':
                    for k, v in json.loads(cj).items():
                        kd = daily.setdefault(_credit_entry_day(k, upd),
                                              {'credits': 0.0, 'tokens': 0, 'sessions': 0})
                        kd['credits'] += float(v)
            except (ValueError, TypeError):
                pass
        row = con.execute(
            "SELECT MAX(updated_at - created_at) FROM sessions "
            "WHERE updated_at IS NOT NULL AND created_at IS NOT NULL").fetchone()
        if row and row[0]:
            longest_ms = int(row[0])
    finally:
        con.close()

    # 注：旧版曾用"每日快照差值"覆盖当天数值，但快照间隔跨自然日（如昨天 09:05→
    # 今天 09:05），会把昨晚消耗算进今天。逐笔 UUIDv7 时间归档已按自然日精确切分，
    # 无需再用快照校准；快照仅保留给 burn_forecast 估算消耗速率。

    total_credits = sum(v['credits'] for v in daily.values())
    total_tokens = sum(v['tokens'] for v in daily.values())
    peak = max((v['tokens'] for v in daily.values()), default=0)
    active_days = sorted(d for d, v in daily.items() if v['sessions'] > 0)
    cur_streak = max_streak = 0
    if active_days:
        day_set = set(active_days)
        probe = datetime.now()
        if probe.strftime('%Y-%m-%d') not in day_set:
            probe -= timedelta(days=1)  # 今天还没用也不断签
        while probe.strftime('%Y-%m-%d') in day_set:
            cur_streak += 1
            probe -= timedelta(days=1)
        run = 0
        prev = None
        for ds in active_days:
            dt = datetime.strptime(ds, '%Y-%m-%d')
            if prev and (dt - prev).days == 1:
                run += 1
            else:
                run = 1
            max_streak = max(max_streak, run)
            prev = dt
    return {'daily': daily, 'total_credits': round(total_credits, 1),
            'total_tokens': total_tokens, 'peak_tokens': peak,
            'cur_streak': cur_streak, 'max_streak': max_streak,
            'longest_hours': round(longest_ms / 3600000, 1) if longest_ms else 0}


def snapshot_usage_history():
    """记录当日各会话的累计积分快照（自动签到任务与统计窗口打开时调用）。
    相邻两天的快照差值 = 当日精确消耗。"""
    if not DB_PATH.is_file():
        return
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=10)
        cur = {}
        try:
            for sid, cj in con.execute(
                    "SELECT session_id, credit_json FROM session_usage "
                    "WHERE credit_json IS NOT NULL AND credit_json != 'None'"):
                try:
                    cur[sid] = sum(float(v) for v in json.loads(cj).values())
                except (ValueError, TypeError):
                    pass
        finally:
            con.close()
        today = datetime.now().strftime('%Y-%m-%d')
        hist = load_usage_history()
        hist[today] = cur
        hist = dict(sorted(hist.items())[-90:])  # 保留 90 天
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        USAGE_HISTORY_PATH.write_text(json.dumps(hist), encoding='utf-8')
    except (OSError, sqlite3.Error):
        pass


def load_usage_history():
    try:
        data = json.loads(USAGE_HISTORY_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _schedule_exists() -> bool:
    r = subprocess.run(["schtasks", "/Query", "/TN", "WorkBuddySwitcher_AutoCheckin"],
                       capture_output=True, text=True, errors="replace",
                       creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0


def build_benefits_tab(host, sw):
    """积分 / 签到页（内嵌标签页）。"""
    import queue
    import tkinter as tk
    from tkinter import ttk

    win = ttk.Frame(host, padding=(0, 4))
    win.pack(fill="both", expand=True)

    q = queue.Queue()
    busy = {"f": False}
    last_upd = {"at": 0.0}   # 上次发起刷新的时间（TTL 用）
    pending = {"refresh": False}

    ttk.Label(win, text="签到状态（所有已登记账号）", font=("Microsoft YaHei UI", 10, "bold")
              ).pack(anchor="w", padx=12, pady=(10, 2))
    cols1 = ("alias", "checked", "streak", "week", "total")
    tv1 = ttk.Treeview(win, columns=cols1, show="headings", height=4)
    for cid, t, w in [("alias", "账号", 110), ("checked", "今日签到", 80), ("streak", "连续天数", 80),
                      ("week", "本周签到", 80), ("total", "总积分", 90)]:
        tv1.heading(cid, text=t)
        tv1.column(cid, width=w, anchor="w")
    tv1.pack(fill="x", padx=12)

    ttk.Label(win, text="积分套餐与到期", font=("Microsoft YaHei UI", 10, "bold")
              ).pack(anchor="w", padx=12, pady=(10, 2))
    cols2 = ("alias", "package", "remain", "total", "expire", "days")
    tv2wrap = ttk.Frame(win)
    tv2wrap.pack(fill="both", expand=True, padx=12)
    tv2 = ttk.Treeview(tv2wrap, columns=cols2, show="headings", height=12)
    for cid, t, w in [("alias", "账号", 80), ("package", "积分批次", 230), ("remain", "剩余", 80),
                      ("total", "总量", 80), ("expire", "到期时间", 130), ("days", "剩余天数", 80)]:
        tv2.heading(cid, text=t)
        tv2.column(cid, width=w, anchor="w")
    tv2sb = ttk.Scrollbar(tv2wrap, orient="vertical", command=tv2.yview)
    tv2.configure(yscrollcommand=tv2sb.set)
    tv2.tag_configure('red', foreground='#c0392b')
    tv2.tag_configure('orange', foreground='#d68910')
    tv2.tag_configure('toggle', foreground='#2471a3')
    tv2.pack(side="left", fill="both", expand=True)
    tv2sb.pack(side="right", fill="y")

    # 同名批次折叠/展开状态（线程只经队列投递，这里全部主线程访问）
    ck_children: dict = {}      # 折叠中的汇总行 iid -> [(values, tag), ...]
    ck_open: dict = {}          # 已展开的汇总行 iid -> (子行iid列表, 收起行iid, 子明细)
    ck_toggle_owner: dict = {}  # 收起行 iid -> 所属汇总行 iid

    def _ck_collapse(owner, kids, tgl, children):
        for k in kids + [tgl]:
            tv2.delete(k)
        ck_children[owner] = children

    def on_tv2_click(event):
        iid = tv2.identify_row(event.y)
        if not iid:
            return
        if iid in ck_open:  # 再次点汇总行：收起
            kids, tgl, children = ck_open.pop(iid)
            _ck_collapse(iid, kids, tgl, children)
        elif iid in ck_children:  # 点汇总行：展开（汇总行保留，可再点收起）
            children = ck_children.pop(iid)
            idx = tv2.index(iid) + 1
            kids = []
            for v, t in children:
                kids.append(tv2.insert("", idx, values=v, tags=(t,) if t else ()))
                idx += 1
            tgl = tv2.insert("", idx,
                             values=(tv2.item(iid, "values")[0], "▲ 收起明细", "", "", "", ""),
                             tags=("toggle",))
            ck_open[iid] = (kids, tgl, children)
            ck_toggle_owner[tgl] = iid
        elif iid in ck_toggle_owner:  # 点「▲ 收起明细」：收起所属组
            owner = ck_toggle_owner.pop(iid)
            kids, tgl, children = ck_open.pop(owner)
            _ck_collapse(owner, kids, tgl, children)

    tv2.bind("<Button-1>", on_tv2_click)

    stats_var = tk.StringVar(value="本地用量统计：刷新后显示")
    ttk.Label(win, textvariable=stats_var, foreground="#555").pack(anchor="w", padx=12, pady=4)

    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=12, pady=(2, 4))
    auto_var = tk.BooleanVar(value=_schedule_exists())

    def set_busy(b):
        busy["f"] = b
        state = "disabled" if b else "normal"
        btn_refresh.configure(state=state)
        btn_checkin.configure(state=state)

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    log_var.set(payload)
                elif kind == "row1":
                    tv1.insert("", "end", values=payload)
                elif kind == "row2":
                    values, tag = payload
                    tv2.insert("", "end", values=values, tags=(tag,) if tag else ())
                elif kind == "row2c":  # 签到赠送批次：折叠为一行，点击展开
                    values, tag, children = payload
                    iid = tv2.insert("", "end", values=values,
                                     tags=((tag,) if tag else ()) + ("toggle",))
                    ck_children[iid] = children
                # 说明：托盘消息（tray_* / open_benefits / open_usage / app_exit）由主窗口
                # run_gui.poll_queue 统一处理。这里的旧分支引用的是 run_gui 的闭包变量
                # （root / ui_log / run_async / tray_notify / _tray / render_banner），
                # 在积分面板作用域里根本不存在，一旦被触发就是 NameError，会直接打断本
                # 窗口的轮询循环、导致面板数据空白。本窗口队列实际只产生
                # log / row1 / row2 / stats / done，故移除这些死分支。
                elif kind == "stats":
                    stats_var.set(payload)
                elif kind == "refresh":
                    pending["refresh"] = True
                elif kind == "done":
                    set_busy(False)
                    auto_var.set(_schedule_exists())
                    upd_var.set(f"数据更新于 {datetime.now().strftime('%H:%M:%S')}")
                    if pending["refresh"]:  # 签到完成后补一次刷新（等 busy 释放再跑）
                        pending["refresh"] = False
                        refresh_data(True)
        except queue.Empty:
            pass
        except tk.TclError:
            return  # 页面已切走/销毁，结束轮询
        try:
            win.after(150, poll)
        except tk.TclError:
            pass

    def worker(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                q.put(("log", f"出错：{e}"))
            q.put(("done", ""))
        set_busy(True)
        threading.Thread(target=run, daemon=True).start()

    def refresh_data(force=False):
        """拉取签到状态与积分批次。非强制时 60 秒内不重复打接口（切页复用上次结果）。"""
        if busy["f"]:
            return
        if not force and time.time() - last_upd["at"] < 60:
            return
        last_upd["at"] = time.time()
        for iid in tv1.get_children():
            tv1.delete(iid)
        for iid in tv2.get_children():
            tv2.delete(iid)
        ck_children.clear()
        ck_open.clear()

        def job():
            def L(m):
                q.put(("log", m))
            if not get_protector_key(L):
                return
            for p in load_profiles().values():
                ok, st = checkin_status(p, log=L)
                if ok:
                    q.put(("row1", (p.display,
                                    "✓ 已签到" if st.get("today_checked_in") else "未签到",
                                    st.get("streak_days", 0),
                                    st.get("week_checkin_days", 0),
                                    st.get("total_credits", 0))))
                else:
                    q.put(("row1", (p.display, "令牌失效/错误", "-", "-", "-")))
                packs = resource_summary(p, log=L)
                if packs:
                    def _pack_vals(pk):
                        exp, days, tag = "-", "-", ""
                        if pk.get("expire"):
                            dt = datetime.fromtimestamp(pk["expire"] / 1000)
                            exp = dt.strftime("%Y-%m-%d")
                            dleft = (dt - datetime.now()).days
                            if dleft < 0:
                                days, tag = "已过期", "red"
                            else:
                                days = dleft
                                if dleft <= 7:
                                    tag = "red"
                        return (p.display, pk['name'][:24],
                                f"{pk['remain']:g}", f"{pk['total']:g}", exp, days), tag

                    # 同名批次 ≥3 笔的折叠为一行汇总（签到/裂变类每天一笔、太占板面），
                    # 不按名称关键词匹配（各平台叫法不同），点击可展开明细
                    groups = {}
                    for pk in packs:
                        groups.setdefault(pk['name'], []).append(pk)
                    for name, group in groups.items():
                        if len(group) < 3:
                            v, t = _pack_vals(group[0])
                            q.put(("row2", (v, t)))
                            continue
                        vs = [_pack_vals(pk) for pk in group]
                        e0 = min((pk.get("expire") for pk in group if pk.get("expire")),
                                 default=None)
                        if e0:
                            d0 = (datetime.fromtimestamp(e0 / 1000) - datetime.now()).days
                            exp_s = datetime.fromtimestamp(e0 / 1000).strftime("%Y-%m-%d")
                            d_s = str(d0) if d0 >= 0 else "已过期"
                        else:
                            exp_s, d_s = "-", "-"
                        tag = "red" if any(t == "red" for _, t in vs) else ""
                        q.put(("row2c", ((p.display, f"{name[:20]}（{len(vs)} 笔）",
                                          f"{sum(pk['remain'] for pk in group):g}",
                                          f"{sum(pk['total'] for pk in group):g}",
                                          exp_s, d_s), tag, vs)))
            stats = local_usage_stats()
            if stats:
                q.put(("stats", f"本地用量统计：今日积分 {stats['today_credits']} / 累计 {stats['total_credits']}"
                                f" · 今日上下文 {stats['today_ctx']:,} / 累计 {stats['total_ctx']:,} tokens"
                                f" · 会话 {stats['sessions']} 个"))

        worker(job)

    def do_checkin():
        def job():
            def L(m):
                q.put(("log", m))
            # 先走逐账号流程：卡片领取优先（按钮同步为已领取）+ 连签保活
            ensure_all_daily_sessions(sw.cfg, L)
            # 清扫：循环未覆盖的账号用 API 补签（幂等）
            auto_checkin_all(L)
            q.put(("refresh", ""))  # 交给主线程轮询统一刷新，避免跨线程操作控件
        worker(job)

    def toggle_keepalive():
        sw.cfg["keepalive"] = bool(keepalive_var.get())
        save_config(sw.cfg)
        q.put(("log", "连签保障已开启：签到后会为无会话账号自动发起最小保活会话。"
                if sw.cfg["keepalive"] else
                "连签保障已关闭：不再自动发起保活会话。"))

    def toggle_schedule():
        try:
            set_auto_schedule(bool(auto_var.get()), lambda m: q.put(("log", m)))
        except Exception as e:  # noqa: BLE001
            q.put(("log", f"任务计划操作失败：{e}"))

    btn_refresh = ttk.Button(btns, text="刷新", command=lambda: refresh_data(True))
    btn_checkin = ttk.Button(btns, text="一键签到（全部账号）")
    auto_check = ttk.Checkbutton(btns, text="每天 09:05 自动签到（任务计划）", variable=auto_var,
                                 command=toggle_schedule)
    keepalive_var = tk.BooleanVar(value=bool(sw.cfg.get("keepalive", True)))
    keepalive_check = ttk.Checkbutton(btns, text="签到后保活会话（连签保障）",
                                      variable=keepalive_var, command=toggle_keepalive)
    btn_refresh.pack(side="left")
    btn_checkin.pack(side="left", padx=(8, 0))
    auto_check.pack(side="left", padx=(16, 0))
    keepalive_check.pack(side="left", padx=(16, 0))

    log_var = tk.StringVar(value="就绪。")
    ttk.Label(win, textvariable=log_var, foreground="#0a7").pack(anchor="w", padx=12)
    upd_var = tk.StringVar(value="")
    ttk.Label(win, textvariable=upd_var, foreground="#888").pack(anchor="w", padx=12, pady=(0, 8))

    btn_checkin.configure(command=do_checkin)
    refresh_data(True)
    poll()
    return refresh_data  # 供标签页切回时按 TTL 刷新数据


def build_usage_tab(host):
    """使用统计页（内嵌标签页）：指标卡 + 活动热力图 + 每日趋势图。

    控件只建一次，之后切回只重算数据并重绘（30 秒 TTL），返回 refresh 供调度。"""
    import tkinter as tk
    from tkinter import ttk

    win = ttk.Frame(host, padding=(0, 2))
    win.pack(fill="both", expand=True)
    n_acc = len(load_profiles())
    state = {'daily': {}, 'at': 0.0, 'loaded': False}

    # ---- 指标卡
    cards = ttk.Frame(win)
    cards.pack(fill="x", padx=12, pady=(12, 4))
    card_vars = [tk.StringVar(value="-") for _ in range(5)]
    for i, (var, small) in enumerate(zip(
            card_vars, ("累计积分消耗", "累计上下文 Token", "峰值单日 Token",
                        "当前连续使用", "最长连续使用"))):
        cell = ttk.Frame(cards, relief="solid", borderwidth=1, padding=(10, 8))
        cell.grid(row=0, column=i, padx=4, sticky="nsew")
        cards.columnconfigure(i, weight=1)
        ttk.Label(cell, textvariable=var, font=("Microsoft YaHei UI", 13, "bold"),
                  anchor="center").pack(fill="x")
        ttk.Label(cell, text=small, foreground="#666", anchor="center").pack(fill="x")

    burn_var = tk.StringVar(value="")
    ttk.Label(win, textvariable=burn_var, foreground="#555").pack(anchor="w", padx=12, pady=(4, 0))
    upd_var = tk.StringVar(value="")
    ttk.Label(win, textvariable=upd_var, foreground="#888").pack(anchor="w", padx=12)

    # ---- 活动热力图（近 12 周，按每日 Token）
    ttk.Label(win, text="活动热力图（近 12 周 · 按每日 Token 用量）", font=("Microsoft YaHei UI", 10, "bold")
              ).pack(anchor="w", padx=12, pady=(10, 2))
    HM = tk.Canvas(win, width=820, height=7 * 22 + 26, bg="#fbfbfb", highlightthickness=1,
                   highlightbackground="#e0e0e0")
    HM.pack(fill="x", padx=12)
    cell_w, gap = 20, 3

    def draw_heatmap():
        HM.delete("all")
        daily = state['daily']
        today_d = datetime.now().date()
        start = today_d - timedelta(days=83)
        start = start - timedelta(days=start.weekday())  # 对齐到 start 当周的周一
        max_tok = max((v['tokens'] for v in daily.values()), default=0) or 1

        def heat_color(v):
            if v <= 0:
                return "#ececec"
            t = min(v / max_tok, 1.0)
            return (f"#{int(0xec + t*(0x2f-0xec)):02x}"
                    f"{int(0xed + t*(0x81-0xed)):02x}"
                    f"{int(0xf0 + t*(0xf7-0xf0)):02x}")

        month_marks = []
        for col in range(12):
            for rowi in range(7):
                d = start + timedelta(days=col * 7 + rowi)
                if d > today_d:
                    continue
                v = daily.get(d.strftime('%Y-%m-%d'), {}).get('tokens', 0)
                HM.create_rectangle(8 + col * (cell_w + gap), 4 + rowi * (cell_w + gap),
                                    8 + col * (cell_w + gap) + cell_w,
                                    4 + rowi * (cell_w + gap) + cell_w,
                                    fill=heat_color(v), outline="")
            ds = start + timedelta(days=col * 7)
            if ds.day <= 7 and ds != start:
                month_marks.append((8 + col * (cell_w + gap), f"{ds.month}月"))
        for x, label in month_marks:
            HM.create_text(x + 10, 7 * (cell_w + gap) + 10, text=label, fill="#888",
                           font=("Microsoft YaHei UI", 8))

    # ---- 每日趋势图（近 7 / 30 日，Token 折线 + 积分折线）
    bar = ttk.Frame(win)
    bar.pack(fill="x", padx=12, pady=(10, 2))
    ttk.Label(bar, text="每日趋势", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
    trend_range = {"days": 7}
    TR = tk.Canvas(win, width=820, height=300, bg="#fbfbfb", highlightthickness=1,
                   highlightbackground="#e0e0e0")
    TR.pack(fill="x", padx=12, pady=(0, 8))
    trend_state = {'items': [], 'data': None}

    def draw_trend():
        TR.delete("all")
        daily = state['daily']
        today_d = datetime.now().date()
        days_n = trend_range["days"]
        W, H, L, R, T, Bm = 820, 300, 56, 20, 30, 46
        plot_w, plot_h = W - L - R, H - T - Bm
        dates = [(today_d - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days_n - 1, -1, -1)]
        toks = [daily.get(d, {}).get('tokens', 0) for d in dates]
        creds = [daily.get(d, {}).get('credits', 0.0) for d in dates]
        max_t, max_c = max(toks) or 1, max(creds) or 1
        n = len(dates)
        def px(i): return L + (plot_w * i / max(n - 1, 1))
        def py_t(v): return T + plot_h * (1 - v / max_t)
        def py_c(v): return T + plot_h * (1 - v / max_c)
        for g in range(5):
            y = T + plot_h * g / 4
            TR.create_line(L, y, W - R, y, fill="#e8e8e8")
        # Token 折线（蓝）
        pts = [(px(i), py_t(v)) for i, v in enumerate(toks)]
        if n > 1:
            TR.create_line(*[c for p in pts for c in p], fill="#2f81f7", width=2, smooth=True)
        for i, (x, y) in enumerate(pts):
            TR.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#2f81f7", outline="")
        # 积分折线（橙）
        pts_c = [(px(i), py_c(v)) for i, v in enumerate(creds)]
        if n > 1:
            TR.create_line(*[c for p in pts_c for c in p], fill="#e67e22", width=2, smooth=True)
        for i, (x, y) in enumerate(pts_c):
            TR.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#e67e22", outline="")
        # 轴标签
        TR.create_text(L - 6, T + 4, text=f"{max_t:,}", anchor="e", fill="#2f81f7", font=("Consolas", 8))
        TR.create_text(L - 6, T + plot_h, text="0", anchor="e", fill="#888", font=("Consolas", 8))
        TR.create_text(W - R + 4, T + 4, text=f"{max_c:g} 分", anchor="w", fill="#e67e22", font=("Microsoft YaHei UI", 8))
        step = max(1, n // 7)
        for i in range(0, n, step):
            TR.create_text(px(i), H - Bm + 16, text=dates[i][5:].replace('-', '/'),
                           fill="#888", font=("Microsoft YaHei UI", 8))
        TR.create_text(L + 8, T - 12, text="● Token（上下文）", fill="#2f81f7", anchor="w", font=("Microsoft YaHei UI", 8))
        TR.create_text(L + 130, T - 12, text="● 积分消耗", fill="#e67e22", anchor="w", font=("Microsoft YaHei UI", 8))
        trend_state['data'] = {'dates': dates, 'toks': toks, 'creds': creds,
                               'px': [px(i) for i in range(n)],
                               'pyt': [py_t(v) for v in toks],
                               'pyc': [py_c(v) for v in creds],
                               'top': T, 'bottom': H - Bm, 'left': L, 'right': W - R}
        for iid in trend_state['items']:
            TR.delete(iid)
        trend_state['items'] = []

    def on_motion(e):
        d = trend_state.get('data')
        if not d:
            return
        for iid in trend_state['items']:
            TR.delete(iid)
        trend_state['items'] = []
        if not (d['left'] - 6 <= e.x <= d['right'] + 6 and d['top'] - 8 <= e.y <= d['bottom'] + 8):
            return
        best, bd = 0, 1e9
        for i, x in enumerate(d['px']):
            dist = abs(x - e.x)
            if dist < bd:
                bd, best = dist, i
        if bd > 32:
            return
        x, yt, yc = d['px'][best], d['pyt'][best], d['pyc'][best]
        trend_state['items'].append(TR.create_line(x, d['top'], x, d['bottom'], fill="#bbb", dash=(3, 3)))
        trend_state['items'].append(TR.create_oval(x - 5, yt - 5, x + 5, yt + 5, outline="#2f81f7", width=2))
        trend_state['items'].append(TR.create_oval(x - 5, yc - 5, x + 5, yc + 5, outline="#e67e22", width=2))
        txt = (f"{d['dates'][best][5:].replace('-', '/')}\n"
               f"积分 {d['creds'][best]:g}\n"
               f"Token {d['toks'][best]:,}")
        bw, bh = 112, 62
        bx = min(max(x - bw / 2, 2), 820 - bw - 2)
        by = min(yt, yc) - bh - 10
        if by < 2:
            by = max(yt, yc) + 10
        trend_state['items'].append(TR.create_rectangle(bx, by, bx + bw, by + bh,
                                                        fill="#ffffff", outline="#999"))
        trend_state['items'].append(TR.create_text(bx + 8, by + 5, text=txt, anchor="nw",
                                                   font=("Microsoft YaHei UI", 8), fill="#333"))

    def on_leave(e):
        for iid in trend_state['items']:
            TR.delete(iid)
        trend_state['items'] = []

    TR.bind("<Motion>", on_motion)
    TR.bind("<Leave>", on_leave)

    def set_range(days):
        trend_range["days"] = days
        for b, d in ((btn_7, 7), (btn_30, 30)):
            b.state(["pressed" if d == days else "!pressed"])
        draw_trend()

    btn_7 = ttk.Button(bar, text="近 7 日", style="Toggle.TButton", command=lambda: set_range(7))
    btn_30 = ttk.Button(bar, text="近 30 日", style="Toggle.TButton", command=lambda: set_range(30))
    btn_7.pack(side="right", padx=(6, 0))
    btn_30.pack(side="right")
    try:
        ttk.Style(win).configure("Toggle.TButton", padding=(8, 2))
    except tk.TclError:
        pass

    ttk.Label(win, text=(f"注：{n_acc} 个账号共用同一份会话库，统计为合并值。"
                         "积分日消耗按每笔请求的真实发生时间（UUIDv7 时间戳）归档；"
                         "Token 按会话最后活跃日归档。"),
              foreground="#888", wraplength=820, justify="left").pack(anchor="w", padx=12, pady=(0, 8))

    def refresh(force=False):
        """重算统计并就地更新指标卡/热力图/趋势图；非强制时 30 秒内复用上次结果。"""
        if not force and state['loaded'] and time.time() - state['at'] < 30:
            return
        state['at'] = time.time()
        snapshot_usage_history()  # 记录今日快照：次日即可用差值算出精确日消耗
        stats = collect_usage_stats()
        state['loaded'] = True
        if not stats:
            upd_var.set("未找到本地会话数据库。")
            return
        state['daily'] = stats['daily']
        vals = (f"{stats['total_credits']:g}", f"{stats['total_tokens']:,}",
                f"{stats['peak_tokens']:,}", f"{stats['cur_streak']} 天",
                f"{stats['max_streak']} 天")
        for var, v in zip(card_vars, vals):
            var.set(v)
        fc = burn_forecast()
        if fc and fc.get('days') is not None:
            burn_var.set(f"按近 7 日均速 {fc['burn']:g} 积分/天，当前余额 {fc['balance']:g}，"
                         f"预计可用约 {fc['days']} 天")
        elif fc:
            burn_var.set(f"当前余额 {fc['balance']:g}，近 7 日无积分消耗记录")
        else:
            burn_var.set("")
        upd_var.set(f"数据更新于 {datetime.now().strftime('%H:%M:%S')}")
        draw_heatmap()
        draw_trend()

    set_range(7)
    refresh(True)
    return refresh  # 供标签页切回时按 TTL 刷新数据


def main() -> None:
    argv = sys.argv[1:]
    if "--cli" in argv:
        argv.remove("--cli")
        sys.exit(run_cli(argv))
    if argv and argv[0] in ("status", "list", "add", "switch", "checkin", "keepalive",
                            "refresh"):
        sys.exit(run_cli(argv))
    # GUI 单实例：重复启动不再另开窗口，唤起已有实例
    if not acquire_single_instance():
        sys.exit(0)
    run_gui()


if __name__ == "__main__":
    main()
