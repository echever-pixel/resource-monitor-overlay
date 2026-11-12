# -*- coding: utf-8 -*-
"""Resource Monitor Overlay (자원 모니터): GPU·VRAM·CPU·RAM·FPS를 작은 오버레이로 보여 준다.

- 오버레이: 끌어서 옮기기, 더블클릭하면 큰 창(프로그램별 사용량), 오른쪽 클릭하면 메뉴.
- FPS: Intel 공식 PresentMon으로 앞에 띄운 창의 초당 프레임을 센다. 처음 켤 때 공식 GitHub에서 자동으로 받는다.
- Ollama 모델이 CPU에 올라가 있는데 GPU가 놀고 GPU 메모리가 넉넉하면, 모델을 내렸다가 다시 올려서 GPU로 옮긴다.
  (Ollama는 모델을 올릴 때의 빈 GPU 메모리를 보고 자리를 정하기 때문에, GPU가 바쁠 때 올라간 모델은 CPU에 남는다.)
Windows + NVIDIA 그래픽카드 전용. 다른 프로그램은 끄지 않는다.
"""
import collections
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from ctypes import wintypes

import psutil

APP_VERSION = "1.0.0"
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # bundled files (icon)
DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ResourceMonitorOverlay")
os.makedirs(DATA, exist_ok=True)
LOG = os.path.join(DATA, "ollama_switch.log")
OLLAMA = "http://127.0.0.1:11434"
NOWIN = 0x08000000  # CREATE_NO_WINDOW

# 자동 전환 조건
GPU_IDLE_PCT = 35      # 최근 30초 GPU 평균 사용률이 이보다 낮으면 "논다"
GPU_SHARE_OK = 0.9     # 모델의 90% 이상이 GPU에 있으면 그대로 둔다
VRAM_MARGIN_GB = 1.0   # 옮기고 나서도 남겨 둘 GPU 메모리
COOLDOWN_S = 600       # 한 번 옮긴 뒤 10분은 다시 안 옮긴다

BG, CARD, LINE = "#14161c", "#1d2029", "#2a2e3a"
FG, DIM = "#e8eaf0", "#8b90a0"
C_GPU, C_VRAM, C_CPU, C_RAM = "#7ee0a8", "#b49cff", "#7cc4ff", "#ffc078"
C_WARN, C_OK = "#ff9f6b", "#7ee0a8"
FONT = "Malgun Gothic"

NAMES = {
    "llama-server": "Ollama 모델", "ollama": "Ollama", "ollama app": "Ollama", "whale": "웨일",
    "chrome": "크롬", "msedge": "엣지", "firefox": "파이어폭스", "dwm": "윈도우 화면", "explorer": "탐색기",
    "obs64": "OBS", "discord": "디스코드", "kakaotalk": "카카오톡", "node": "Node", "swarmui": "SwarmUI",
    "nvcontainer": "NVIDIA", "nvidia overlay": "NVIDIA 오버레이", "memcompression": "메모리 압축",
    "resourcemonitoroverlay": "자원 모니터",
}


def friendly(name, cmd):
    n = name.lower().removesuffix(".exe")
    c = cmd.lower()
    if n in ("python", "pythonw"):
        if "comfy" in c:
            return "ComfyUI 이미지 생성"
        if "resmon.pyw" in c:
            return "자원 모니터"
        parts = [p for p in cmd.replace('"', " ").split() if p.lower().endswith((".py", ".pyw"))]
        return "파이썬 " + os.path.basename(parts[-1]) if parts else "파이썬"
    return NAMES.get(n, name.removesuffix(".exe"))


def http_json(path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def log(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%m-%d %H:%M:%S ") + line + "\n")


class _Value(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _Item(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _Value)]


class GpuCounters:
    """Windows 성능 카운터(PDH)로 프로세스별 GPU 엔진 사용률과 GPU 메모리를 직접 읽는다. 작업 관리자와 같은 출처."""

    FMT = 0x00000200 | 0x00008000  # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh")
        self.q = wintypes.HANDLE()
        self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q))
        self.eng = self._add("\\GPU Engine(*)\\Utilization Percentage")
        self.mem = self._add("\\GPU Process Memory(*)\\Dedicated Usage")
        self.pcpu = self._add("\\Process(*)\\% Processor Time")
        self.pws = self._add("\\Process(*)\\Working Set")
        self.pid = self._add("\\Process(*)\\ID Process")
        self.pdh.PdhCollectQueryData(self.q)

    def _add(self, path):
        h = wintypes.HANDLE()
        self.pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h))
        return h

    def _array(self, h):
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        self.pdh.PdhGetFormattedCounterArrayW(h, self.FMT, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return []
        buf = (ctypes.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(h, self.FMT, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        # invalid samples stay in the list as None, so arrays from one collection keep the same instance order
        return [(items[i].szName, items[i].FmtValue.doubleValue if items[i].FmtValue.CStatus in (0, 1) else None)
                for i in range(count.value)]

    def read(self):
        """-> ({pid: {engine type: %}}, {pid: GPU memory bytes}, {pid: (name, cpu % of one core, working set bytes)})"""
        self.pdh.PdhCollectQueryData(self.q)
        eng, mem, procs = {}, {}, {}
        ids, cpus, wss = self._array(self.pid), self._array(self.pcpu), self._array(self.pws)
        if len(ids) == len(cpus) == len(wss):  # one collection, so the three lists share one instance order
            for (name, pid), (n2, cpu), (n3, ws) in zip(ids, cpus, wss):
                if name == n2 == n3 and pid and name not in ("_Total", "Idle"):
                    procs[int(pid)] = (name, cpu or 0.0, ws or 0.0)
        for name, v in self._array(self.eng):
            if v is not None and name.startswith("pid_") and "engtype_" in name:
                pid = int(name.split("_")[1])
                t = name.split("engtype_")[1].lower()
                d = eng.setdefault(pid, {})
                d[t] = d.get(t, 0.0) + v
        for name, v in self._array(self.mem):
            if v is not None and name.startswith("pid_"):
                pid = int(name.split("_")[1])
                mem[pid] = mem.get(pid, 0.0) + v
        return eng, mem, procs


class Collector(threading.Thread):
    """2초마다 GPU·CPU·Ollama 상태를 모아 snap에 넣는다."""

    def __init__(self):
        super().__init__(daemon=True)
        self.snap = None
        self.procs = {}
        self.gc = GpuCounters()
        self.util_hist = collections.deque(maxlen=15)

    def run(self):
        psutil.cpu_percent(None)
        while True:
            try:
                self.snap = self.collect()
            except Exception as ex:  # keep the window alive whatever one sample does
                log("수집 오류: %r" % ex)
            time.sleep(2)

    def gpu_total(self):
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,utilization.decoder",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, creationflags=NOWIN).stdout
        name, util, used, total, dec = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        return {"name": name.replace("NVIDIA GeForce ", ""), "util": float(util), "used": float(used) / 1024,
                "total": float(total) / 1024, "dec": float(dec)}

    def collect(self):
        ncpu = psutil.cpu_count() or 1
        gpu = self.gpu_total()
        eng, mem, plist = self.gc.read()
        rows, seen = {}, set()
        for pid, (pname, cpu1, ram) in plist.items():
            seen.add(pid)
            fname = self.procs.get(pid)
            if fname is None:  # command line is read once per process, only to give it a friendly name
                try:
                    cmd = " ".join(psutil.Process(pid).cmdline())
                except (psutil.Error, OSError):
                    cmd = ""
                fname = self.procs[pid] = friendly(pname, cmd)
            cpu = cpu1 / ncpu
            r = rows.setdefault(fname, {"cpu": 0.0, "ram": 0, "gpu": 0.0, "gtype": "", "vram": 0.0, "dec": 0.0})
            r["cpu"] += cpu
            r["ram"] += ram
            e = eng.get(pid) or {}
            best, btype = 0.0, ""
            for t, v in e.items():
                if t.startswith("video"):
                    r["dec"] += v
                elif t not in ("copy", "security", "legacyoverlay", "vr", "ofa_0") and v > best:
                    best, btype = v, t
            r["gpu"] += best
            if best > 0.5:
                r["gtype"] = "CUDA" if btype == "cuda" else "3D" if btype.startswith("3d") or btype.startswith("graphics") else "계산"
            vram = mem.get(pid, 0.0) / 1024 ** 3
            if vram <= gpu["total"]:  # a few system processes report impossible values; skip those
                r["vram"] += vram
        for pid in list(self.procs):
            if pid not in seen:
                del self.procs[pid]

        self.util_hist.append(gpu["util"])
        vm = psutil.virtual_memory()
        try:
            models = []
            for m in http_json("/api/ps", timeout=1.5).get("models", []):
                size, vram = float(m.get("size", 0)), float(m.get("size_vram", 0))
                models.append({"name": m.get("name", "?"), "size": size, "vram": vram,
                               "share": vram / size if size else 0.0})
        except Exception:
            models = None
        return {"t": time.time(), "rows": rows, "gpu": gpu, "gpu_avg": sum(self.util_hist) / len(self.util_hist),
                "cpu": psutil.cpu_percent(None), "ram_used": (vm.total - vm.available) / 1024 ** 3,
                "ram_total": vm.total / 1024 ** 3, "models": models}


class Switcher:
    """Ollama 모델이 CPU에 몰려 있고 GPU가 놀면 GPU로 옮긴다."""

    def __init__(self):
        self.auto = True
        self.busy = False
        self.last = 0.0
        self.msg = "지켜보는 중"

    @staticmethod
    def cpu_model(snap):
        for m in snap["models"] or []:
            if m["share"] < GPU_SHARE_OK:
                return m
        return None

    def check(self, snap):
        if self.busy or not snap or snap["models"] is None:
            return
        m = self.cpu_model(snap)
        if not m:
            self.msg = "모델이 GPU에 있어요" if snap["models"] else "올라간 모델 없음"
            return
        need = (m["size"] - m["vram"]) / 1024 ** 3 + VRAM_MARGIN_GB
        free = snap["gpu"]["total"] - snap["gpu"]["used"]
        if snap["gpu_avg"] >= GPU_IDLE_PCT:
            self.msg = "GPU가 바빠서 기다려요 (평균 %d%%)" % snap["gpu_avg"]
        elif free < need:
            self.msg = "GPU 메모리 부족: 남은 %.1fGB, 필요 %.1fGB" % (free, need)
        elif time.time() - self.last < COOLDOWN_S:
            self.msg = "방금 옮겨서 %d분 쉬어요" % ((COOLDOWN_S - (time.time() - self.last)) // 60 + 1)
        elif self.auto:
            self.start(m["name"], "자동: GPU 평균 %d%%, 남은 GPU 메모리 %.1fGB" % (snap["gpu_avg"], free))
        else:
            self.msg = "옮길 수 있어요 (자동 꺼짐)"

    def start(self, name, why):
        if self.busy:
            return
        self.busy, self.last = True, time.time()
        threading.Thread(target=self._move, args=(name, why), daemon=True).start()

    def _move(self, name, why):
        try:
            log("%s 를 GPU로 옮기기 시작 (%s)" % (name, why))
            self.msg = "%s 내리는 중..." % name
            http_json("/api/generate", {"model": name, "keep_alive": 0}, timeout=60)
            for _ in range(30):
                if not any(m.get("name") == name for m in http_json("/api/ps").get("models", [])):
                    break
                time.sleep(1)
            self.msg = "%s GPU로 올리는 중..." % name
            http_json("/api/generate", {"model": name, "keep_alive": "10m"}, timeout=300)
            share = 0.0
            for m in http_json("/api/ps").get("models", []):
                if m.get("name") == name and m.get("size"):
                    share = float(m.get("size_vram", 0)) / float(m["size"])
            self.msg = "옮김 완료: GPU %d%%" % (share * 100)
            log("%s 다시 올림: GPU %d%%" % (name, share * 100))
        except Exception as ex:
            self.msg = "옮기기 실패: %s" % type(ex).__name__
            log("%s 옮기기 실패: %r" % (name, ex))
        finally:
            self.busy = False


class Bar(tk.Canvas):
    def __init__(self, master, width, color, height=6):
        super().__init__(master, width=width, height=height, bg=LINE, highlightthickness=0, bd=0)
        self.w, self.h, self.color = width, height, color
        self.fill = self.create_rectangle(0, 0, 0, height, fill=color, width=0)

    def set(self, frac, color=None):
        frac = max(0.0, min(1.0, frac))
        w = max(self.winfo_width(), self.w) if self.winfo_ismapped() else self.w  # bars stretched by pack() are wider than asked
        self.coords(self.fill, 0, 0, int(w * frac), self.h)
        self.itemconfigure(self.fill, fill=color or self.color)


# Intel's official PresentMon (MIT license). Downloaded on first use from the official release and checked by hash.
PRESENTMON = os.path.join(DATA, "PresentMon-2.6.0-x64.exe")
PRESENTMON_URL = "https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe"
PRESENTMON_SHA256 = "B2A706BC6AD475749E3B7E3409263AA1E6906D45BDCF993F6DBC0F660188F1AF"
FPS_CSV = os.path.join(DATA, "fps_admin.csv")


def ensure_presentmon():
    """Returns True when the verified PresentMon is in place, downloading it once if needed."""
    if os.path.exists(PRESENTMON):
        return True
    tmp = PRESENTMON + ".part"
    try:
        urllib.request.urlretrieve(PRESENTMON_URL, tmp)
        with open(tmp, "rb") as f:
            ok = hashlib.sha256(f.read()).hexdigest().upper() == PRESENTMON_SHA256
        if ok:
            os.replace(tmp, PRESENTMON)
            return True
    except OSError:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


class _JobLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
                ("IoCounters", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_with_me(proc):
    """Ties a child process to this app, so Windows ends it when the app ends for any reason."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _JobLimit()
    info.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    return job


class FpsReader:
    """앞에 띄운 창(게임, 영상 등)의 초당 프레임을 센다.

    Intel 공식 PresentMon이 화면에 올라간 프레임을 줄마다 내보내고, 이 클래스가 바로 읽는다.
    - 보통 모드: 관리자 권한 없이 돈다(계정이 Performance Log Users 그룹이거나 관리자 실행일 때). 파일을 안 남긴다.
    - 권한이 없으면 FPS 칸에 "권한"이 뜬다. 오른쪽 클릭 → "FPS 켜기 (관리자 확인)"을 누르고 '예'를 누르면
      관리자 모드로 1시간 동안 재고, 그동안의 기록을 파일 하나에 받아 읽는다(다음에 켤 때 지운다).
    앱이 꺼지면 보통 모드의 PresentMon은 같이 꺼지고, 관리자 모드는 1시간이 지나면 스스로 꺼진다.
    """

    def __init__(self):
        self.on = True
        self.proc = None
        self.job = None
        self.cols = None
        self.times = collections.defaultdict(collections.deque)  # pid -> present times (s)
        self.latest = 0.0
        self.last_read = 0.0
        self.lock = threading.Lock()
        self.state = "준비"   # 준비 | 받는중 | 없음 | 보통 | 권한 | 관리자
        self.fails = 0
        self.admin_started = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def running(self):
        return self.on and time.time() - self.last_read < 6

    def _reset(self):
        with self.lock:
            self.cols, self.latest = None, 0.0
            self.times.clear()

    def _spawn(self):
        self.proc = subprocess.Popen(
            [PRESENTMON, "--session_name", "ResourceMonitorOverlayFPS", "--stop_existing_session", "--output_stdout", "--v1_metrics",
             "--no_console_stats", "--exclude_dropped", "--no_track_input"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
        self.job = _kill_with_me(self.proc)
        self._reset()

    def _loop(self):
        if not os.path.exists(PRESENTMON):
            self.state = "받는중"
            if not ensure_presentmon():
                self.state = "없음"
                return
        self.state = "보통"
        while True:
            if not self.on:
                time.sleep(0.5)
            elif self.state == "관리자":
                self._tail_admin()
            elif self.state == "권한":
                time.sleep(0.5)
            else:
                started = time.time()
                self._spawn()
                for line in self.proc.stdout:
                    self._take(line)
                    if not self.on or self.state != "보통":
                        break
                if self.proc.poll() is None:
                    self.proc.kill()
                if self.on and self.state == "보통" and time.time() - started < 5:
                    self.fails += 1  # ends at once without trace rights
                    if self.fails >= 2:
                        self.state = "권한"
                time.sleep(2)

    def start_admin(self):
        """One click: run PresentMon as administrator for an hour (Windows asks once)."""
        if not os.path.exists(PRESENTMON):
            return
        try:
            os.remove(FPS_CSV)
        except OSError:
            pass
        args = ('--session_name ResourceMonitorOverlayFPSAdmin --stop_existing_session --output_file "%s" --v1_metrics '
                '--no_console_stats --exclude_dropped --no_track_input --timed 3600 --terminate_after_timed' % FPS_CSV)
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", PRESENTMON, args, None, 0) > 32:
            self._reset()
            self.admin_started, self.on, self.state = time.time(), True, "관리자"

    def _tail_admin(self):
        pos, buf = 0, ""
        while self.on and self.state == "관리자":
            try:
                with open(FPS_CSV, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    buf += f.read()
                    pos = f.tell()
            except OSError:
                pass
            *lines, buf = buf.split("\n")
            for line in lines:
                self._take(line)
            quiet_since = max(self.last_read, self.admin_started + 20)
            if time.time() - quiet_since > 15:  # the hour is over or the prompt was declined
                self.state = "권한"
                return
            time.sleep(0.3)

    def _take(self, line):
        parts = line.strip().split(",")
        with self.lock:
            if self.cols is None:
                if "ProcessID" in parts and "TimeInSeconds" in parts:
                    self.cols = (parts.index("ProcessID"), parts.index("TimeInSeconds"))
                return
            try:
                pid, t = int(parts[self.cols[0]]), float(parts[self.cols[1]])
            except (ValueError, IndexError):
                return
            self.last_read = time.time()
            self.times[pid].append(t)
            if t > self.latest:
                self.latest = t
                for q in self.times.values():
                    while q and q[0] < t - 1.0:
                        q.popleft()

    def toggle(self):
        self.on = not self.on
        if not self.on and self.proc and self.proc.poll() is None:
            self.proc.kill()

    def text(self):
        if not self.on:
            return "꺼짐"
        if self.state in ("준비", "받는중"):
            return "받는중"
        if self.state in ("없음", "권한"):
            return self.state
        if not self.running:
            return "-"
        return str(self.fps_of_foreground())

    def fps_of_foreground(self):
        """Frames per second of the app in front. Browsers and Electron apps draw from a child GPU process,
        so the window's process and its children are all checked and the busiest one counts."""
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if getattr(self, "_fg", (None,))[0] != pid.value or time.time() - self._fg[2] > 5:
            pids = {pid.value}
            try:
                pids |= {c.pid for c in psutil.Process(pid.value).children(recursive=True)}
            except (psutil.Error, OSError):
                pass
            self._fg = (pid.value, pids, time.time())
        with self.lock:
            return max((len(self.times[p]) for p in self._fg[1] if p in self.times), default=0)


class Overlay:
    """NVIDIA 오버레이처럼 작게 떠 있는 창. 끌어서 옮기고, 더블클릭하면 큰 창, 오른쪽 클릭하면 메뉴."""

    KEY = "#010203"  # painted transparent, so the card gets rounded corners
    POS = os.path.join(DATA, "overlay_pos.json")
    ROWS = (("GPU", C_GPU), ("VRAM", C_VRAM), ("CPU", C_CPU), ("RAM", C_RAM))

    def __init__(self, app):
        self.app = app
        s = app.root.winfo_fpixels("1i") / 96.0
        self.s = s
        self.w, self.rh, self.pad = int(143 * s), int(19 * s), int(10 * s)
        self.h = self.pad * 2 + self.rh * 5
        win = self.win = tk.Toplevel(app.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.9)
        win.configure(bg=self.KEY)
        win.attributes("-transparentcolor", self.KEY)
        x, y = self._load_pos()
        win.geometry("%dx%d+%d+%d" % (self.w, self.h, x, y))
        c = self.c = tk.Canvas(win, width=self.w, height=self.h, bg=self.KEY, highlightthickness=0, bd=0)
        c.pack()
        self._round(0, 0, self.w, self.h, int(12 * s), fill=BG)
        f = (FONT, 9, "bold")
        lx, bx, bw, vx = self.pad, int(54 * s), int(35 * s), self.w - self.pad
        self.items = []
        for i, (name, color) in enumerate(self.ROWS + (("FPS", C_WARN),)):
            y0 = self.pad + i * self.rh + self.rh // 2
            c.create_text(lx, y0, text=name, anchor="w", fill=DIM, font=(FONT, 8))
            if name != "FPS":
                c.create_rectangle(bx, y0 - 3 * s, bx + bw, y0 + 3 * s, fill=LINE, width=0)
                bar = c.create_rectangle(bx, y0 - 3 * s, bx, y0 + 3 * s, fill=color, width=0)
            else:
                bar = None
            val = c.create_text(vx, y0, text="-", anchor="e", fill=FG, font=f)
            self.items.append((bar, val, color, bx, bw, y0))
        for w in (win, c):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save_pos)
            w.bind("<Double-Button-1>", lambda e: app.show_main())
            w.bind("<Button-3>", self._menu)
        self.menu = tk.Menu(win, tearoff=0, font=(FONT, 9))
        self.menu.add_command(label="큰 창 열기", command=app.show_main)
        self.menu.add_command(label="FPS 끄기", command=app.fps.toggle)
        self.menu.add_command(label="FPS 켜기 (관리자 확인)", command=app.fps.start_admin)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=app.quit)

    def _round(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.c.create_polygon(pts, smooth=True, **kw)

    def _load_pos(self):
        try:
            with open(self.POS, encoding="utf-8") as f:
                p = json.load(f)
            return int(p["x"]), int(p["y"])
        except (OSError, ValueError, KeyError):
            return int(self.app.root.winfo_screenwidth() - self.w - 30 * self.s), int(30 * self.s)

    def _press(self, e):
        self._off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag(self, e):
        self.win.geometry("+%d+%d" % (e.x_root - self._off[0], e.y_root - self._off[1]))

    def _save_pos(self, e=None):
        try:
            with open(self.POS, "w", encoding="utf-8") as f:
                json.dump({"x": self.win.winfo_x(), "y": self.win.winfo_y()}, f)
        except OSError:
            pass

    def _menu(self, e):
        fps = self.app.fps
        self.menu.entryconfigure(1, label="FPS 끄기" if fps.on else "FPS 켜기")
        # the admin item is offered only when tracing needs the permission prompt
        self.menu.entryconfigure(2, state="normal" if fps.state == "권한" else "disabled")
        self.menu.tk_popup(e.x_root, e.y_root)

    def render(self, s, fps_text):
        g = s["gpu"]
        vals = (g["util"] / 100, g["used"] / g["total"], s["cpu"] / 100, s["ram_used"] / s["ram_total"])
        for (bar, val, color, bx, bw, y0), frac in zip(self.items, vals):
            frac = max(0.0, min(1.0, frac))
            self.c.coords(bar, bx, y0 - 3 * self.s, bx + bw * frac, y0 + 3 * self.s)
            self.c.itemconfigure(bar, fill=C_WARN if frac > 0.92 else color)
            self.c.itemconfigure(val, text="%d%%" % round(frac * 100))
        self.c.itemconfigure(self.items[4][1], text=fps_text)
        self.win.attributes("-topmost", True)


class App:
    ROWS = 6

    def __init__(self):
        self.col = Collector()
        self.col.start()
        self.sw = Switcher()
        self.fps = FpsReader()
        self.root = tk.Tk()
        self.root.title("자원 모니터")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.geometry("+60+60")
        try:
            self.root.iconbitmap(os.path.join(HERE, "resmon.ico"))
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)  # X hides the big window; the overlay stays
        self.build()
        self.root.withdraw()
        self.overlay = Overlay(self)
        self.root.after(500, self.tick)

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        self.root.destroy()  # the FPS task notices the stopped heartbeat and ends by itself

    def label(self, master, text="", size=9, color=FG, bold=False, **kw):
        return tk.Label(master, text=text, bg=master["bg"], fg=color, font=(FONT, size, "bold" if bold else "normal"), **kw)

    def card(self, title, color):
        f = tk.Frame(self.root, bg=CARD, padx=12, pady=10)
        f.pack(fill="x", padx=10, pady=(10, 0))
        head = tk.Frame(f, bg=CARD)
        head.pack(fill="x")
        dot = tk.Canvas(head, width=8, height=8, bg=CARD, highlightthickness=0)
        dot.create_oval(0, 0, 8, 8, fill=color, width=0)
        dot.pack(side="left", padx=(0, 6))
        t = self.label(head, title, 10, bold=True)
        t.pack(side="left")
        big = self.label(head, "-", 13, color, bold=True)
        big.pack(side="right")
        return f, t, big

    def meter(self, master, color):
        row = tk.Frame(master, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        bar = Bar(row, 300, color, 8)
        bar.pack(fill="x")
        txt = self.label(row, "", 8, DIM)
        txt.pack(anchor="w")
        return bar, txt

    def table(self, master, color, mem_color, busy_title, mem_title):
        """Rows of: name | how busy (bar) | how much memory it holds (bar) | numbers."""
        rows = []
        box = tk.Frame(master, bg=CARD)
        box.pack(fill="x", pady=(6, 0))
        self.label(box, busy_title, 7, DIM).grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.label(box, mem_title, 7, DIM).grid(row=0, column=2, sticky="w", padx=(6, 0))
        for i in range(self.ROWS):
            name = self.label(box, "", 9, anchor="w", width=15)
            bar = Bar(box, 64, color, 5)
            mbar = Bar(box, 64, mem_color, 5)
            val = self.label(box, "", 8, DIM, anchor="e", width=16)
            name.grid(row=i + 1, column=0, sticky="w")
            bar.grid(row=i + 1, column=1, padx=(6, 0))
            mbar.grid(row=i + 1, column=2, padx=(6, 0))
            val.grid(row=i + 1, column=3, sticky="e")
            rows.append((name, bar, mbar, val))
        return rows

    def build(self):
        f, self.gpu_title, self.gpu_big = self.card("GPU", C_GPU)
        self.gpu_bar, self.gpu_txt = self.meter(f, C_GPU)
        self.vram_bar, self.vram_txt = self.meter(f, C_VRAM)
        self.gpu_rows = self.table(f, C_GPU, C_VRAM, "GPU 사용", "VRAM 차지")

        f, _, self.cpu_big = self.card("CPU", C_CPU)
        self.cpu_bar, self.cpu_txt = self.meter(f, C_CPU)
        self.ram_bar, self.ram_txt = self.meter(f, C_RAM)
        self.cpu_rows = self.table(f, C_CPU, C_RAM, "CPU 사용", "RAM 차지")

        f, self.ol_title, self.ol_big = self.card("Ollama", C_WARN)
        self.ol_msg = self.label(f, "", 9, DIM, anchor="w", justify="left", wraplength=320)
        self.ol_msg.pack(fill="x", pady=(6, 4))
        ctl = tk.Frame(f, bg=CARD)
        ctl.pack(fill="x")
        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctl, text="GPU 놀면 자동으로 옮기기", variable=self.auto_var, command=self.on_auto,
                       bg=CARD, fg=FG, selectcolor=LINE, activebackground=CARD, activeforeground=FG,
                       font=(FONT, 9), bd=0, highlightthickness=0).pack(side="left")
        self.move_btn = tk.Button(ctl, text="지금 GPU로", command=self.on_move, bg=LINE, fg=FG, relief="flat",
                                  activebackground=C_VRAM, font=(FONT, 9, "bold"), padx=10, pady=2, bd=0, cursor="hand2")
        self.move_btn.pack(side="right")

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=12, pady=8)
        self.top_var = tk.BooleanVar(value=True)
        tk.Checkbutton(foot, text="항상 위", variable=self.top_var, command=self.on_top, bg=BG, fg=DIM,
                       selectcolor=LINE, activebackground=BG, activeforeground=FG, font=(FONT, 8),
                       bd=0, highlightthickness=0).pack(side="left")
        self.foot = self.label(foot, "모으는 중...", 8, DIM)
        self.foot.pack(side="right")

    def on_auto(self):
        self.sw.auto = self.auto_var.get()

    def on_top(self):
        self.root.attributes("-topmost", self.top_var.get())

    def on_move(self):
        snap = self.col.snap
        m = Switcher.cpu_model(snap) if snap else None
        if m is None and snap and snap["models"]:
            m = snap["models"][0]
        if m:
            self.sw.start(m["name"], "버튼")
        else:
            self.sw.msg = "올라간 모델이 없어요"

    @staticmethod
    def fill_rows(widgets, items):
        for i, (name, bar, mbar, val) in enumerate(widgets):
            if i < len(items):
                n, frac, mfrac, text = items[i]
                name.configure(text=n if len(n) <= 14 else n[:13] + "…")
                for b, f in ((bar, frac), (mbar, mfrac)):
                    b.configure(bg=LINE)
                    b.set(f, C_WARN if f > 0.5 and b is mbar else None)  # one program holding over half: orange
                val.configure(text=text)
            else:
                name.configure(text="")
                for b in (bar, mbar):
                    b.configure(bg=CARD)  # hide the empty track on unused rows
                    b.set(0)
                val.configure(text="")

    def tick(self):
        snap = self.col.snap
        if snap:
            if self.root.state() == "normal":  # the big window is drawn only while it is open
                self.render(snap)
            self.overlay.render(snap, self.fps.text())
            self.sw.check(snap)
        self.root.after(1000, self.tick)

    def render(self, s):
        g = s["gpu"]
        self.gpu_title.configure(text="GPU  " + g["name"])
        self.gpu_big.configure(text="%d%%" % g["util"])
        self.gpu_bar.set(g["util"] / 100)
        self.gpu_txt.configure(text="계산 %d%%  ·  영상 디코더 %d%%" % (g["util"], g["dec"]))
        self.vram_bar.set(g["used"] / g["total"], C_WARN if g["used"] / g["total"] > 0.92 else C_VRAM)
        self.vram_txt.configure(text="VRAM %.1f / %.1f GB  (%d%%)" % (g["used"], g["total"], g["used"] / g["total"] * 100))
        # rank by load and by memory held: a model sitting in 9GB of GPU memory matters even while it idles
        rows = sorted(s["rows"].items(), key=lambda kv: max(kv[1]["gpu"], kv[1]["dec"]) + kv[1]["vram"] * 10, reverse=True)
        items = []
        for n, r in rows:
            if r["gpu"] < 0.5 and r["dec"] < 0.5 and r["vram"] < 0.15:
                continue
            kind = r["gtype"] or ("디코더" if r["dec"] >= 0.5 else "")
            pct = min(100, r["gpu"] if r["gpu"] >= r["dec"] else r["dec"])
            items.append((n, pct / 100, r["vram"] / g["total"],
                          "%s %d%% · %.1fGB" % (kind, pct, r["vram"]) if kind else "VRAM %.1fGB" % r["vram"]))
        self.fill_rows(self.gpu_rows, items[:self.ROWS])

        self.cpu_big.configure(text="%d%%" % s["cpu"])
        self.cpu_bar.set(s["cpu"] / 100)
        self.cpu_txt.configure(text="코어 %d개 전체 기준" % (psutil.cpu_count() or 1))
        ratio = s["ram_used"] / s["ram_total"]
        self.ram_bar.set(ratio, C_WARN if ratio > 0.9 else C_RAM)
        self.ram_txt.configure(text="RAM %.1f / %.1f GB  (%d%%)" % (s["ram_used"], s["ram_total"], ratio * 100))
        # rank by CPU load and by RAM held, so a program sitting on 8GB of RAM shows up even while it idles
        rows = sorted(s["rows"].items(), key=lambda kv: kv[1]["cpu"] + kv[1]["ram"] / 1024 ** 3 / s["ram_total"] * 100,
                      reverse=True)
        self.fill_rows(self.cpu_rows, [(n, r["cpu"] / 100, r["ram"] / 1024 ** 3 / s["ram_total"],
                                        "%.1f%% · RAM %.1fGB" % (r["cpu"], r["ram"] / 1024 ** 3))
                                       for n, r in rows[:self.ROWS]])

        models = s["models"]
        if models is None:
            self.ol_title.configure(text="Ollama")
            self.ol_big.configure(text="꺼짐", fg=DIM)
        elif not models:
            self.ol_title.configure(text="Ollama")
            self.ol_big.configure(text="비어 있음", fg=DIM)
        else:
            m = Switcher.cpu_model(s) or models[0]
            self.ol_title.configure(text="Ollama  " + m["name"])
            if m["share"] >= GPU_SHARE_OK:
                self.ol_big.configure(text="GPU %d%%" % (m["share"] * 100), fg=C_OK)
            else:
                self.ol_big.configure(text="CPU %d%%" % ((1 - m["share"]) * 100), fg=C_WARN)
        self.ol_msg.configure(text=self.sw.msg)
        self.move_btn.configure(state="disabled" if self.sw.busy else "normal")
        self.foot.configure(text=time.strftime("%H:%M:%S 갱신"))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\resource_monitor_overlay_single")
    if ctypes.windll.kernel32.GetLastError() == 183:  # already running: keep the one window
        raise SystemExit
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled monitors
    except (AttributeError, OSError):
        pass
    App().run()
