#!/usr/bin/env python3
"""定位并启动本机的 Zotero 程序。

只做两件事：找到 Zotero 可执行文件，把它拉起来并等本地 API 就绪。
不读写文献库，不改 Zotero 配置。

查找顺序：--exe 参数 → 环境变量 ZOTERO_EXE → 系统注册表 / 应用目录
→ PATH → 常见安装位置。

作为模块使用：

    import zlaunch
    entry, tried = zlaunch.find()
    proc = zlaunch.spawn(entry)
    ok, stage, detail = zlaunch.wait_ready(api.ping, timeout=90, proc=proc)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

ENV_EXE = "ZOTERO_EXE"
# ShellExecute 的 nCmdShow 取值，与 subprocess 的 STARTUPINFO.SW_* 不是同一套常量
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2


# ---------------- 版本 ----------------

def _absolute(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _read_version(exe):
    """从安装目录的 application.ini 读版本号。返回 (版本, ini 路径)。"""
    base = os.path.dirname(_absolute(exe))
    candidates = []
    if IS_MAC:
        candidates.append(
            os.path.join(os.path.dirname(base), "Resources", "application.ini")
        )
    candidates.extend([
        os.path.join(base, "application.ini"),
        os.path.join(base, "app", "application.ini"),
        os.path.join(os.path.dirname(base), "application.ini"),
    ])
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Version="):
                        return line.split("=", 1)[1].strip(), path
        except OSError:
            continue
    return "", ""


# ---------------- Windows 注册表 ----------------

def _reg_string(key, name):
    import winreg
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "").strip()


def _exe_from_icon(icon):
    """DisplayIcon 形如 "C:\\Program Files\\Zotero\\zotero.exe,0"。"""
    if not icon:
        return ""
    path = icon.strip().strip('"')
    if "," in path:
        path = path.rsplit(",", 1)[0]
    path = path.strip().strip('"')
    return path if path.lower().endswith(".exe") else ""


def _windows_registry(tried):
    try:
        import winreg
    except ImportError:
        tried.append("注册表：当前 Python 没有 winreg 模块")
        return []

    found = []
    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in uninstall_roots:
        try:
            root = winreg.OpenKey(hive, subkey)
        except OSError:
            tried.append("注册表：" + subkey + "（打不开）")
            continue
        with root:
            try:
                count = winreg.QueryInfoKey(root)[0]
            except OSError:
                count = 0
            for index in range(count):
                try:
                    child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                except OSError:
                    continue
                with child:
                    display = _reg_string(child, "DisplayName")
                    if "zotero" not in display.lower():
                        continue
                    version = _reg_string(child, "DisplayVersion")
                    location = _reg_string(child, "InstallLocation")
                    icon = _reg_string(child, "DisplayIcon")
                exe = _exe_from_icon(icon)
                if not exe and location:
                    exe = os.path.join(location, "zotero.exe")
                if exe and os.path.isfile(exe):
                    found.append({"exe": exe, "version": version, "source": "注册表"})
                else:
                    tried.append("注册表：{} 指向 {}".format(
                        display or "Zotero", exe or "（空）"))

    for hive, label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                        (winreg.HKEY_CURRENT_USER, "HKCU")):
        subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\zotero.exe"
        try:
            key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        with key:
            try:
                exe = str(winreg.QueryValue(key, "") or "").strip().strip('"')
            except OSError:
                exe = ""
        if exe and os.path.isfile(exe):
            found.append({"exe": exe, "version": "", "source": "App Paths " + label})
        else:
            tried.append("注册表：App Paths\\zotero.exe（{}）".format(label))
    return found


# ---------------- 常见位置 ----------------

def _common_paths():
    if IS_WINDOWS:
        paths = []
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                paths.append(os.path.join(root, "Zotero", "zotero.exe"))
        paths.append(r"C:\Program Files\Zotero\zotero.exe")
        return paths
    if IS_MAC:
        return [
            "/Applications/Zotero.app/Contents/MacOS/zotero",
            os.path.expanduser("~/Applications/Zotero.app/Contents/MacOS/zotero"),
        ]
    return [
        "/opt/zotero/zotero",
        "/usr/lib/zotero/zotero",
        "/usr/local/bin/zotero",
        "/snap/bin/zotero",
        "/var/lib/flatpak/exports/bin/org.zotero.Zotero",
        os.path.expanduser("~/.local/share/flatpak/exports/bin/org.zotero.Zotero"),
        os.path.expanduser("~/.local/share/zotero/zotero"),
    ]


# ---------------- 定位 ----------------

def find(exe_hint=None):
    """定位 Zotero 可执行文件。

    返回 (entry, tried)。entry 为 None 表示没找到，tried 记录所有探测过的位置，
    供排错时原样展示，不要凭猜测告诉用户"没装"。
    """
    tried = []

    def build(path, source):
        path = _absolute(path)
        if not os.path.isfile(path):
            tried.append("{}（不存在）".format(path))
            return None
        version, ini = _read_version(path)
        return {"exe": path, "version": version, "ini": ini, "source": source}

    for hint, label in ((exe_hint, "参数 --exe"),
                        (os.environ.get(ENV_EXE), "环境变量 " + ENV_EXE)):
        if hint:
            entry = build(hint, label)
            if entry:
                return entry, tried

    if IS_WINDOWS:
        for candidate in _windows_registry(tried):
            entry = build(candidate["exe"], candidate["source"])
            if entry:
                entry["version"] = entry["version"] or candidate["version"]
                return entry, tried

    found = shutil.which("zotero")
    if found:
        entry = build(found, "PATH")
        if entry:
            return entry, tried
    else:
        tried.append("PATH 中没有 zotero 命令")

    for path in _common_paths():
        entry = build(path, "常见安装位置")
        if entry:
            return entry, tried

    return None, tried


# ---------------- 启动 ----------------

def spawn(entry, extra_args=(), minimized=False):
    """启动 Zotero。

    Windows 下返回 None，并且走 explorer.exe 代启动。这一点很关键：用
    subprocess.Popen 或 os.startfile 直接启动时，Zotero 会落在调用方的作业对象里，
    调用方一退出就被连带清理，表现为"API 短暂就绪后立刻断开"。交给资源管理器
    启动才能真正独立存活。

    其他平台返回 Popen，仅用于观察是否异常退出。
    """
    exe = entry["exe"]
    cwd = os.path.dirname(exe) or None

    if IS_WINDOWS:
        try:
            subprocess.Popen(
                ["explorer.exe", exe],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return None
        except OSError:
            # 没有资源管理器的极简环境，退回 ShellExecute
            arguments = " ".join(extra_args) if extra_args else None
            show = SW_SHOWMINIMIZED if minimized else SW_SHOWNORMAL
            try:
                os.startfile(exe, "open", arguments, cwd, show)
            except TypeError:  # Python < 3.10 的 startfile 只接受 path 和 operation
                os.startfile(exe)
            return None

    if IS_MAC and "/Contents/MacOS/" in exe:
        argv = ["open", "-a", exe.split("/Contents/MacOS/")[0]]
    else:
        argv = [exe]
    argv.extend(extra_args)

    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=cwd,
        start_new_session=True,
    )


def wait_ready(ping, timeout=90.0, interval=1.5, proc=None):
    """轮询本地 API 直到就绪。

    返回 (ok, stage, detail)。stage 取值：
      ready     已就绪，detail 为 ping 结果
      disabled  Zotero 在跑但本地 API 未启用
      exited    启动的进程提前退出，detail 为返回码
      timeout   超时仍无响应，detail 为最后一次错误
    """
    import zapi

    deadline = time.time() + max(float(timeout), 1.0)
    detail = "尚未收到响应"
    while True:
        try:
            return True, "ready", ping()
        except zapi.Disabled as exc:
            return False, "disabled", str(exc)
        except Exception as exc:  # NotRunning / OSError / 其他 ApiError
            detail = str(exc)
        if proc is not None and proc.poll() is not None:
            code = proc.returncode
            if code not in (0, None):
                return False, "exited", "启动进程异常退出，返回码 {}".format(code)
            # 返回码 0 不算失败：Firefox 系应用的启动器会交接给真正的进程后正常退出
            proc = None
        if time.time() >= deadline:
            return False, "timeout", detail
        time.sleep(interval)


# ---------------- 排错辅助 ----------------

def profile_root():
    """Zotero 存放 profile（含 parent.lock）的目录。"""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA")
        return os.path.join(base, "Zotero", "Zotero", "Profiles") if base else ""
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/Zotero/Profiles")
    return os.path.expanduser("~/.zotero/zotero")


def stale_locks():
    """列出 profile 下存在的锁文件。

    Zotero 异常退出会留下 parent.lock，下次启动可能被判定为"已有实例"而直接退出。
    这里只报告，不自动删——万一 Zotero 真的在跑，删锁会导致双实例。
    """
    root = profile_root()
    if not root or not os.path.isdir(root):
        return []
    found = []
    try:
        for name in os.listdir(root):
            lock = os.path.join(root, name, "parent.lock")
            if os.path.exists(lock):
                found.append(lock)
    except OSError:
        pass
    return found


if __name__ == "__main__":  # 手工排查：python zlaunch.py
    entry, tried = find(sys.argv[1] if len(sys.argv) > 1 else None)
    if entry:
        print("找到: {}（{}，来源 {}）".format(
            entry["exe"], entry["version"] or "版本未知", entry["source"]))
    else:
        print("未找到 Zotero。已尝试：")
        for item in tried:
            print("  - " + item)
    sys.exit(0 if entry else 1)
