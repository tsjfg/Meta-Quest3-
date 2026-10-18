# -*- coding: utf-8 -*-
"""
QuestCast - 把 Meta Quest 3 画面低延迟投到电脑上的本地工具。

原理: Quest 3 底层是 Android，开启开发者模式后可通过 ADB 连接，
用 scrcpy 直接把头显的画面流（H.264/H.265 硬编码）拉到电脑解码显示，
不经过任何云端服务器。纯标准库实现，无第三方依赖。

用法: 双击 start.bat，或在命令行执行 python questcast.py
"""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

if getattr(sys, "frozen", False):
    # 打包成 exe 后，以 exe 所在目录为根，保证 web/ 与 runtime/ 能被找到
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, "runtime")
ADB_DIR = os.path.join(RUNTIME, "platform-tools")
SCRCPY_DIR = os.path.join(RUNTIME, "scrcpy")
REC_DIR = os.path.join(BASE, "recordings")
WEB_DIR = os.path.join(BASE, "web")

IS_WIN = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WIN else 0

ADB_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
GH_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"

# ---------------------------------------------------------------- 日志

LOG = []
LOG_LOCK = threading.Lock()


def log(msg, level="info"):
    item = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": str(msg)}
    with LOG_LOCK:
        LOG.append(item)
        if len(LOG) > 600:
            del LOG[:-600]
    try:
        print(f"[{item['t']}] {msg}", flush=True)
    except Exception:
        pass


def log_snapshot():
    with LOG_LOCK:
        return list(LOG)


# ---------------------------------------------------------------- 路径

def adb_exe():
    p = os.path.join(ADB_DIR, "adb.exe" if IS_WIN else "adb")
    return p if os.path.isfile(p) else None


def scrcpy_exe():
    p = os.path.join(SCRCPY_DIR, "scrcpy.exe" if IS_WIN else "scrcpy")
    return p if os.path.isfile(p) else None


def tool_env():
    env = os.environ.copy()
    env["PATH"] = ADB_DIR + os.pathsep + SCRCPY_DIR + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- 下载 / 解压

def _download(url, dest, label):
    log(f"开始下载 {label}")
    req = urllib.request.Request(url, headers={"User-Agent": "QuestCast/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -10
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        log(f"{label} {pct}%  ({done/1048576:.1f}/{total/1048576:.1f} MB)")
                else:
                    mb = done / 1048576
                    if mb - last_pct >= 5:
                        last_pct = mb
                        log(f"{label} 已下载 {mb:.1f} MB")
    log(f"{label} 下载完成", "ok")


def _extract_flatten(zip_path, target, marker):
    tmp = tempfile.mkdtemp(dir=RUNTIME)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        found = None
        for root, _dirs, files in os.walk(tmp):
            if marker in files:
                found = root
                break
        if not found:
            raise RuntimeError(f"压缩包内未找到 {marker}")
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(found, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _scrcpy_asset_url():
    """返回 scrcpy win64 压缩包直链。先走 API，失败则解析 releases 页。"""
    try:
        req = urllib.request.Request(GH_API, headers={"User-Agent": "QuestCast/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        for a in data.get("assets", []):
            name = a.get("name", "")
            if name.startswith("scrcpy-win64") and name.endswith(".zip"):
                return a["browser_download_url"], data.get("tag_name", "")
    except Exception as e:
        log(f"GitHub API 不可用（{e}），改用页面解析", "warn")

    req = urllib.request.Request(
        "https://github.com/Genymobile/scrcpy/releases/latest",
        headers={"User-Agent": "QuestCast/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        final = r.geturl()
    tag = final.rstrip("/").split("/")[-1]
    if not tag.startswith("v"):
        raise RuntimeError("无法确定 scrcpy 最新版本号")
    url = (
        f"https://github.com/Genymobile/scrcpy/releases/download/{tag}/"
        f"scrcpy-win64-{tag}.zip"
    )
    return url, tag


def setup_tools():
    """下载并解压 adb 与 scrcpy。返回结果字典。"""
    os.makedirs(RUNTIME, exist_ok=True)
    try:
        if not adb_exe():
            log("准备 Android platform-tools (adb)")
            z = os.path.join(RUNTIME, "platform-tools.zip")
            _download(ADB_URL, z, "platform-tools")
            _extract_flatten(z, ADB_DIR, "adb.exe")
            os.remove(z)
            log("adb 就绪", "ok")
        else:
            log("adb 已存在，跳过", "ok")

        if not scrcpy_exe():
            url, tag = _scrcpy_asset_url()
            log(f"准备 scrcpy {tag}")
            z = os.path.join(RUNTIME, "scrcpy.zip")
            _download(url, z, f"scrcpy {tag}")
            _extract_flatten(z, SCRCPY_DIR, "scrcpy.exe")
            os.remove(z)
            log(f"scrcpy {tag} 就绪", "ok")
        else:
            log("scrcpy 已存在，跳过", "ok")

        run_adb(["start-server"], timeout=30)
        return {"ok": True}
    except Exception as e:
        log(f"组件准备失败: {e}", "err")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- ADB

def run_adb(args, timeout=15, serial=None):
    exe = adb_exe()
    if not exe:
        raise RuntimeError("adb 未安装")
    cmd = [exe] + (["-s", serial] if serial else []) + list(args)
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=tool_env(), creationflags=NO_WINDOW,
        encoding="utf-8", errors="replace",
    )
    return (p.stdout or "") + (p.stderr or "")


_dev_cache = {"t": 0, "data": []}
_dev_lock = threading.Lock()


def list_devices(force=False):
    with _dev_lock:
        if not force and time.time() - _dev_cache["t"] < 2.0:
            return _dev_cache["data"]
    if not adb_exe():
        return []
    try:
        out = run_adb(["devices", "-l"], timeout=12)
    except Exception:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p[6:].replace("_", " ")
        devs.append({"serial": serial, "state": state, "model": model})
    with _dev_lock:
        _dev_cache["t"] = time.time()
        _dev_cache["data"] = devs
    return devs


_info_cache = {}


def device_info(serial):
    """读取型号 / 系统版本 / 电量。"""
    if not serial:
        return {}
    hit = _info_cache.get(serial)
    if hit and time.time() - hit["t"] < 8:
        return hit["data"]
    data = {}
    try:
        out = run_adb(
            ["shell", "getprop ro.product.model; getprop ro.build.version.release; "
             "getprop ro.product.manufacturer"],
            timeout=12, serial=serial,
        )
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        if len(lines) >= 2:
            data = {
                "model": lines[0],
                "android": lines[1],
                "vendor": lines[2] if len(lines) > 2 else "",
            }
    except Exception:
        pass
    try:
        bat = run_adb(["shell", "dumpsys", "battery"], timeout=12, serial=serial)
        m = re.search(r"level:\s*(\d+)", bat)
        if m:
            data["battery"] = int(m.group(1))
    except Exception:
        pass
    data["screen"] = screen_state(serial)
    _info_cache[serial] = {"t": time.time(), "data": data}
    return data


def device_wifi_ip(serial):
    """尽力获取头显当前 Wi-Fi IP。"""
    try:
        out = run_adb(["shell", "ip", "route"], timeout=10, serial=serial)
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        out = run_adb(["shell", "ip", "-f", "inet", "addr", "show", "wlan0"], timeout=10, serial=serial)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def screen_state(serial):
    """返回头显内置屏状态 ON / OFF / None（未知）。

    Quest 未佩戴时会通过近距传感器自动熄屏，此时任何采集都是黑屏。
    """
    try:
        out = run_adb(["shell", "dumpsys", "display"], timeout=20, serial=serial)
    except Exception:
        return None
    i = out.find("内置屏幕")
    if i < 0:
        i = out.find("Built-in Screen")
    if i < 0:
        return None
    nxt = out.find("DisplayDeviceInfo{", i + 20)
    seg = out[i: nxt if nxt > 0 else i + 20000]
    m = re.search(r"state (ON|OFF|DOZE|DOZE_SUSPEND|UNKNOWN)", seg)
    return m.group(1) if m else None


def display_size(serial):
    """返回内置屏物理分辨率 (w, h)。"""
    try:
        out = run_adb(["shell", "wm", "size"], timeout=12, serial=serial)
        m = re.search(r"(\d+)\s*x\s*(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 4128, 2208


def wake_device(serial):
    """唤醒屏幕并保持常亮（插电时生效）。"""
    ok = False
    try:
        run_adb(["shell", "svc", "power", "stayon", "true"], timeout=12, serial=serial)
        run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=12, serial=serial)
        ok = True
    except Exception as e:
        log(f"唤醒失败: {e}", "warn")
    return ok



# ---------------------------------------------------------------- 投屏进程

CAST = {"proc": None, "started": 0, "serial": "", "record": "", "cmd": ""}
CAST_LOCK = threading.Lock()


def cast_running():
    p = CAST["proc"]
    return bool(p and p.poll() is None)


def _watch_cast(proc):
    try:
        proc.wait()
    except Exception:
        pass
    if CAST["proc"] is proc:
        code = proc.returncode
        if code == 0:
            log("投屏已结束", "ok")
        else:
            log(f"投屏进程退出 (code {code})，请检查设备连接或参数", "warn")
        CAST["proc"] = None


def start_cast(cfg):
    with CAST_LOCK:
        if cast_running():
            return {"ok": False, "error": "已在投屏中，请先停止"}
        if not adb_exe() or not scrcpy_exe():
            return {"ok": False, "error": "组件未就绪，请先点击「下载组件」"}

        devs = [d for d in list_devices(force=True) if d["state"] == "device"]
        if not devs:
            return {"ok": False, "error": "未检测到已授权设备。请用 USB 连接头显并在头显里点「允许」"}
        serial = cfg.get("serial") or devs[0]["serial"]

        if cfg.get("keepAwake", True):
            log("唤醒头显并设置常亮…")
            wake_device(serial)
            time.sleep(0.8)
            if screen_state(serial) == "OFF":
                log("头显屏幕仍是熄灭状态：请戴上头显（近距感应会自动熄屏），否则画面全黑", "warn")

        cmd = [scrcpy_exe(), "-s", serial]
        cmd += ["--max-size", str(int(cfg.get("maxSize") or 1920))]
        cmd += ["--video-bit-rate", str(cfg.get("bitrate") or "20M")]
        cmd += ["--max-fps", str(int(cfg.get("fps") or 60))]
        cmd += ["--window-title", "Quest 3 Cast"]

        if cfg.get("alwaysOnTop"):
            cmd.append("--always-on-top")
        if cfg.get("fullscreen"):
            cmd.append("--fullscreen")

        # Quest 输出的是左右眼拼接的鱼眼画面，单眼模式裁掉另一半更好看
        view = (cfg.get("view") or "both").lower()
        if view in ("left", "right"):
            w, h = display_size(serial)
            half = w // 2
            x = 0 if view == "left" else half
            cmd.append(f"--crop={half}:{h}:{x}:0")
            log(f"单眼模式：裁切 {half}x{h} @({x},0)")

        if cfg.get("audio"):
            cmd.append("--audio")
        else:
            cmd.append("--no-audio")

        rec_path = ""
        if cfg.get("record"):
            os.makedirs(REC_DIR, exist_ok=True)
            rec_path = os.path.join(REC_DIR, time.strftime("quest3_%Y%m%d_%H%M%S.mp4"))
            cmd.append("--record=" + rec_path)

        extra = (cfg.get("extra") or "").strip()
        if extra:
            try:
                cmd += shlex.split(extra, posix=False)
            except Exception as e:
                log(f"附加参数解析失败，已忽略: {e}", "warn")

        log("启动: " + " ".join(cmd), "info")
        try:
            proc = subprocess.Popen(
                cmd, env=tool_env(), creationflags=NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log(f"启动失败: {e}", "err")
            return {"ok": False, "error": str(e)}

        CAST["proc"] = proc
        CAST["started"] = time.time()
        CAST["serial"] = serial
        CAST["record"] = rec_path
        CAST["cmd"] = " ".join(cmd)
        threading.Thread(target=_watch_cast, args=(proc,), daemon=True).start()
        log("投屏窗口已打开" + (f"，同时录制到 {os.path.basename(rec_path)}" if rec_path else ""), "ok")
        return {"ok": True, "cmd": CAST["cmd"], "record": rec_path}


def stop_cast():
    p = CAST["proc"]
    if not p or p.poll() is not None:
        return {"ok": True, "msg": "当前没有在投屏"}
    log("正在停止投屏…")
    try:
        p.terminate()
        try:
            p.wait(timeout=4)
        except Exception:
            p.kill()
    except Exception as e:
        log(f"停止异常: {e}", "warn")
    CAST["proc"] = None
    return {"ok": True}


BLANK_SIZE = 300 * 1024  # 纯色/黑帧 PNG 压缩后远小于此


def _grab_frame(serial, exe):
    p = subprocess.run(
        [exe, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=25, env=tool_env(), creationflags=NO_WINDOW,
    )
    return p.stdout or b""


def screenshot(serial=None, tries=3):
    """抓一帧头显画面。熄屏时自动唤醒并重试。"""
    devs = [d for d in list_devices() if d["state"] == "device"]
    if not devs:
        return {"ok": False, "error": "没有可用设备"}
    serial = serial or devs[0]["serial"]
    exe = adb_exe()
    os.makedirs(REC_DIR, exist_ok=True)
    path = os.path.join(REC_DIR, time.strftime("shot_%Y%m%d_%H%M%S.png"))

    data = b""
    try:
        for i in range(tries):
            if i > 0:
                wake_device(serial)
                time.sleep(1.5)
            data = _grab_frame(serial, exe)
            if len(data) < 1000:
                continue
            if len(data) >= BLANK_SIZE:
                break
        if len(data) < 1000:
            return {"ok": False, "error": "抓不到画面：头显屏幕处于熄灭状态，请戴上头显"}
        with open(path, "wb") as f:
            f.write(data)
        blank = len(data) < BLANK_SIZE
        if blank:
            log("抓到的是黑帧：头显未佩戴时近距感应会熄屏", "warn")
            return {
                "ok": True, "path": path, "blank": True,
                "error": "抓到黑帧 —— 头显屏幕熄灭了，戴上头显后再抓一次",
            }
        log(f"截图已保存 {os.path.basename(path)} ({len(data)/1048576:.1f} MB)", "ok")
        return {"ok": True, "path": path, "blank": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- VR 应用

_apps_cache = {"t": 0, "data": []}


def pretty_label(pkg):
    """从包名猜一个可读名字（Quest 不暴露应用显示名）。"""
    parts = [p for p in pkg.split(".") if p]
    if not parts:
        return pkg
    last = parts[-1]
    if last.lower() in ("ovr", "vr", "quest", "app", "main", "client") and len(parts) > 1:
        last = parts[-2] + " " + last
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = " ".join(w[:1].upper() + w[1:] for w in s.split())
    return s.strip() or pkg


def _query_vr_activities(serial=None):
    """一次拿到所有带 com.oculus.intent.category.VR 的活动。"""
    try:
        out = run_adb(
            ["shell", "cmd", "package", "query-activities",
             "-a", "android.intent.action.MAIN",
             "-c", "com.oculus.intent.category.VR"],
            timeout=30, serial=serial,
        )
    except Exception:
        return []
    res = []
    for block in out.split("Activity #")[1:]:
        m_act = re.search(r"^\s*name=(\S+)\s*$", block, re.M)
        m_pkg = re.search(r"^\s*packageName=(\S+)\s*$", block, re.M)
        if m_act and m_pkg:
            res.append({"activity": m_act.group(1), "pkg": m_pkg.group(1)})
    return res


def list_vr_apps(serial=None, force=False):
    """已安装的第三方 VR 应用。"""
    if not force and time.time() - _apps_cache["t"] < 60:
        return _apps_cache["data"]
    third = set()
    try:
        out = run_adb(["shell", "pm", "list", "packages", "-3"], timeout=25, serial=serial)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                third.add(line[8:])
    except Exception:
        pass
    apps, seen = [], set()
    for a in _query_vr_activities(serial):
        if a["pkg"] in seen:
            continue
        if third and a["pkg"] not in third:
            continue
        seen.add(a["pkg"])
        apps.append({
            "pkg": a["pkg"],
            "component": f'{a["pkg"]}/{a["activity"]}',
            "label": pretty_label(a["pkg"]),
        })
    apps.sort(key=lambda x: x["label"].lower())
    _apps_cache.update(t=time.time(), data=apps)
    return apps


def launch_app(pkg, serial=None):
    """在头显里启动一个 VR 应用。"""
    if not pkg:
        return {"ok": False, "error": "未指定应用"}
    comp = None
    for a in list_vr_apps(serial):
        if a["pkg"] == pkg:
            comp = a["component"]
            break
    if not comp:
        for a in _query_vr_activities(serial):
            if a["pkg"] == pkg:
                comp = f'{a["pkg"]}/{a["activity"]}'
                break
    if not comp:
        return {"ok": False, "error": "没找到这个 VR 应用的启动入口"}
    try:
        out = run_adb(["shell", "am", "start", "-n", comp], timeout=25, serial=serial)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    bad = "Error" in out or "Exception" in out
    log(("启动 VR 应用: " + pretty_label(pkg)) if not bad else f"启动失败: {out.strip()[:160]}",
        "warn" if bad else "ok")
    return {"ok": not bad, "error": out.strip()[:200] if bad else "", "component": comp}


def go_home(serial=None):
    try:
        run_adb(["shell", "input", "keyevent", "KEYCODE_HOME"], timeout=15, serial=serial)
        log("已返回头显主界面")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def retrigger_auth():
    """重启 adb 服务，促使头显重新弹出 USB 调试授权框。"""
    exe = adb_exe()
    if not exe:
        return {"ok": False, "error": "adb 未安装"}
    log("正在重新触发 USB 调试授权…")
    try:
        subprocess.run([exe, "kill-server"], capture_output=True, timeout=15,
                       env=tool_env(), creationflags=NO_WINDOW)
        time.sleep(0.8)
        subprocess.run([exe, "start-server"], capture_output=True, timeout=25,
                       env=tool_env(), creationflags=NO_WINDOW)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    time.sleep(2.0)
    devs = list_devices(force=True)
    if any(d["state"] == "device" for d in devs):
        log("设备已授权", "ok")
        return {"ok": True, "msg": "已授权"}
    if any(d["state"] == "unauthorized" for d in devs):
        log("头显仍未授权：请在头显里点「允许」并勾选「一律允许」", "warn")
        return {"ok": False, "error": "头显里应该出现授权弹窗了，请在头显内点允许"}
    return {"ok": False, "error": "未检测到设备，请检查 USB 线"}


# ---------------------------------------------------------------- 无线

def enable_wireless():
    devs = [d for d in list_devices() if d["state"] == "device"]
    if not devs:
        return {"ok": False, "error": "请先用 USB 线连接头显"}
    serial = devs[0]["serial"]
    ip = device_wifi_ip(serial)
    if not ip:
        return {"ok": False, "error": "无法读取头显 Wi-Fi IP，请确认头显已连 Wi-Fi"}
    try:
        run_adb(["tcpip", "5555"], timeout=15, serial=serial)
    except Exception as e:
        return {"ok": False, "error": f"开启无线调试失败: {e}"}
    time.sleep(1.5)
    log(f"尝试无线连接 {ip}:5555")
    try:
        out = run_adb(["connect", f"{ip}:5555"], timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    ok = "connected" in out.lower()
    log(out.strip() or "无输出", "ok" if ok else "warn")
    if ok:
        return {"ok": True, "ip": ip, "msg": f"已连接 {ip}:5555，现在可以拔掉 USB 线"}
    return {"ok": False, "error": out.strip() or "连接失败"}


def connect_manual(addr):
    addr = (addr or "").strip()
    if not addr:
        return {"ok": False, "error": "请输入 IP 或 IP:端口"}
    if ":" not in addr:
        addr += ":5555"
    try:
        out = run_adb(["connect", addr], timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    ok = "connected" in out.lower()
    log(out.strip(), "ok" if ok else "warn")
    return {"ok": ok, "error": "" if ok else out.strip()}


def disconnect_all():
    try:
        out = run_adb(["disconnect"], timeout=15)
        log(out.strip() or "已断开所有无线连接")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 状态

_setup_lock = threading.Lock()
_setup_state = {"running": False, "done": False}


def state():
    devs = list_devices()
    ready = [d for d in devs if d["state"] == "device"]
    info = device_info(ready[0]["serial"]) if ready else {}
    recs = []
    if os.path.isdir(REC_DIR):
        for f in sorted(os.listdir(REC_DIR), reverse=True)[:8]:
            fp = os.path.join(REC_DIR, f)
            recs.append({"name": f, "size": os.path.getsize(fp)})
    elapsed = int(time.time() - CAST["started"]) if cast_running() else 0
    return {
        "tools": {"adb": bool(adb_exe()), "scrcpy": bool(scrcpy_exe())},
        "ready": bool(adb_exe() and scrcpy_exe()),
        "devices": devs,
        "info": info,
        "casting": cast_running(),
        "elapsed": elapsed,
        "record": os.path.basename(CAST["record"]) if CAST["record"] else "",
        "recordings": recs,
        "setupRunning": _setup_state["running"],
        "log": log_snapshot()[-120:],
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        elif p == "/api/state":
            try:
                self._json(state())
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            if p == "/api/setup":
                if _setup_state["running"]:
                    return self._json({"ok": True, "msg": "正在下载中"})
                _setup_state["running"] = True

                def worker():
                    try:
                        setup_tools()
                    finally:
                        _setup_state["running"] = False
                        _setup_state["done"] = True

                threading.Thread(target=worker, daemon=True).start()
                return self._json({"ok": True, "msg": "已开始下载"})
            if p == "/api/start":
                return self._json(start_cast(body))
            if p == "/api/stop":
                return self._json(stop_cast())
            if p == "/api/wireless":
                return self._json(enable_wireless())
            if p == "/api/connect":
                return self._json(connect_manual(body.get("addr", "")))
            if p == "/api/disconnect":
                return self._json(disconnect_all())
            if p == "/api/screenshot":
                return self._json(screenshot(body.get("serial")))
            if p == "/api/wake":
                devs = [d for d in list_devices() if d["state"] == "device"]
                if not devs:
                    return self._json({"ok": False, "error": "没有可用设备"})
                s = body.get("serial") or devs[0]["serial"]
                wake_device(s)
                time.sleep(1.0)
                st = screen_state(s)
                log(f"已发送唤醒指令，屏幕状态: {st or '未知'}", "ok" if st == "ON" else "warn")
                if st != "ON":
                    return self._json({"ok": False, "error": "屏幕仍熄灭，请戴上头显让近距感应激活屏幕"})
                return self._json({"ok": True})
            if p == "/api/apps":
                return self._json({"ok": True, "apps": list_vr_apps(force=bool(body.get("refresh")))})
            if p == "/api/launch":
                return self._json(launch_app(body.get("pkg", "")))
            if p == "/api/home":
                return self._json(go_home())
            if p == "/api/reauth":
                return self._json(retrigger_auth())
            if p == "/api/open-folder":
                os.startfile(REC_DIR)
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            log(f"接口异常 {p}: {e}", "err")
            return self._json({"ok": False, "error": str(e)}, 500)


def find_port(start=8777):
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main():
    for d in (RUNTIME, REC_DIR, WEB_DIR):
        os.makedirs(d, exist_ok=True)
    port = find_port()
    url = f"http://127.0.0.1:{port}/"
    log("QuestCast 控制台已启动 -> " + url, "ok")
    if adb_exe() and scrcpy_exe():
        log("运行组件已就绪", "ok")
    else:
        log("尚未下载运行组件，请在网页里点击「下载组件」", "warn")
    if "--no-browser" not in sys.argv:
        try:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_cast()


if __name__ == "__main__":
    main()
