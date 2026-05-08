import argparse
import csv
import ctypes
import datetime as dt
import html
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path


APP_NAME = "LocalWorkTracker"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EVENTS_FILE = DATA_DIR / "events.csv"
PID_FILE = DATA_DIR / "tracker.pid"
STOP_FILE = DATA_DIR / "tracker.stop"
REPORT_REQUEST_FILE = DATA_DIR / "report.request"
REPORT_RESPONSE_FILE = DATA_DIR / "report.response"
EVENT_COLUMNS = ["start", "end", "seconds", "app", "title", "state", "key_presses", "mouse_clicks"]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
KEY_DOWN_MASK = 0x8000
MOUSE_BUTTON_KEYS = {1, 2, 4, 5, 6}
KEYBOARD_KEYS = [vk for vk in range(8, 256) if vk not in MOUSE_BUTTON_KEYS]


def now_local():
    return dt.datetime.now().replace(microsecond=0)


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not EVENTS_FILE.exists():
        with EVENTS_FILE.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(EVENT_COLUMNS)
        return

    with EVENTS_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == EVENT_COLUMNS:
            return
        rows = list(reader)

    with EVENTS_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row.setdefault("key_presses", 0)
            row.setdefault("mouse_clicks", 0)
            writer.writerow(row)


class ActivityCounter:
    def __init__(self):
        self.key_presses = 0
        self.mouse_clicks = 0
        self.previous_keyboard = self._pressed(KEYBOARD_KEYS)
        self.previous_mouse = self._pressed(MOUSE_BUTTON_KEYS)

    def _pressed(self, keys):
        return {vk for vk in keys if user32.GetAsyncKeyState(vk) & KEY_DOWN_MASK}

    def poll(self):
        keyboard = self._pressed(KEYBOARD_KEYS)
        mouse = self._pressed(MOUSE_BUTTON_KEYS)
        self.key_presses += len(keyboard - self.previous_keyboard)
        self.mouse_clicks += len(mouse - self.previous_mouse)
        self.previous_keyboard = keyboard
        self.previous_mouse = mouse

    def consume(self):
        counts = self.key_presses, self.mouse_clicks
        self.key_presses = 0
        self.mouse_clicks = 0
        return counts


def get_idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    elapsed_ms = kernel32.GetTickCount() - info.dwTime
    return max(0, int(elapsed_ms / 1000))


def get_process_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"pid-{pid}"

    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        if not ok:
            return f"pid-{pid}"
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)


def get_foreground_activity():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "unknown", ""

    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value.strip()

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = get_process_name(pid.value)
    return app, title


def append_event(start, end, app, title, state, key_presses=0, mouse_clicks=0):
    seconds = max(0, int((end - start).total_seconds()))
    if seconds <= 0:
        return

    with EVENTS_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            start.isoformat(sep=" "),
            end.isoformat(sep=" "),
            seconds,
            app,
            title,
            state,
            int(key_presses),
            int(mouse_clicks),
        ])


def write_pid():
    ensure_data_dir()
    if PID_FILE.exists():
        pid_text = PID_FILE.read_text(encoding="utf-8").strip()
        if pid_text.isdigit() and process_exists(int(pid_text)):
            raise RuntimeError(f"记录程序已经在运行，进程号: {pid_text}")
        PID_FILE.unlink()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid():
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def clear_stop_file():
    try:
        if STOP_FILE.exists():
            STOP_FILE.unlink()
    except OSError:
        pass


def process_exists(pid):
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        startupinfo=startupinfo,
    )
    return result.returncode == 0


def read_events_for_day(day):
    ensure_data_dir()
    rows = []
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = dt.datetime.combine(day, dt.time.max)

    with EVENTS_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            start = dt.datetime.fromisoformat(row["start"])
            end = dt.datetime.fromisoformat(row["end"])
            if end < day_start or start > day_end:
                continue
            rows.append({
                "start": max(start, day_start),
                "end": min(end, day_end),
                "seconds": int(row["seconds"]),
                "app": row["app"],
                "title": row["title"],
                "state": row["state"],
                "key_presses": int(row.get("key_presses") or 0),
                "mouse_clicks": int(row.get("mouse_clicks") or 0),
            })
    return rows


def event_dates_before(day):
    ensure_data_dir()
    dates = set()
    with EVENTS_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                start_date = dt.datetime.fromisoformat(row["start"]).date()
                end_date = dt.datetime.fromisoformat(row["end"]).date()
            except (KeyError, ValueError):
                continue

            cursor = start_date
            while cursor <= end_date and cursor < day:
                dates.add(cursor)
                cursor += dt.timedelta(days=1)
    return sorted(dates)


def fmt_duration(seconds):
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分钟"
    return f"{minutes}分钟"


def title_key(title):
    cleaned = " ".join((title or "无标题").split())
    return cleaned[:80]


def build_report(day):
    rows = read_events_for_day(day)
    active_rows = [r for r in rows if r["state"] == "active"]
    idle_rows = [r for r in rows if r["state"] == "idle"]

    app_totals = {}
    title_totals = {}
    key_total = 0
    mouse_total = 0
    for row in active_rows:
        seconds = max(0, int((row["end"] - row["start"]).total_seconds()))
        app_totals[row["app"]] = app_totals.get(row["app"], 0) + seconds
        key = (row["app"], title_key(row["title"]))
        title_totals[key] = title_totals.get(key, 0) + seconds
        key_total += row["key_presses"]
        mouse_total += row["mouse_clicks"]

    total_active = sum(app_totals.values())
    total_idle = sum(max(0, int((r["end"] - r["start"]).total_seconds())) for r in idle_rows)
    active_minutes = max(1, total_active / 60)
    key_rate = key_total / active_minutes
    mouse_rate = mouse_total / active_minutes

    lines = [
        f"工作记录日报 - {day.isoformat()}",
        "=" * 32,
        f"有效使用时长: {fmt_duration(total_active)}",
        f"空闲时长: {fmt_duration(total_idle)}",
        f"键盘输入次数: {key_total} 次，平均 {key_rate:.1f} 次/分钟",
        f"鼠标点击次数: {mouse_total} 次，平均 {mouse_rate:.1f} 次/分钟",
        "",
        "按应用统计",
        "-" * 32,
    ]

    if app_totals:
        for app, seconds in sorted(app_totals.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"{app}: {fmt_duration(seconds)}")
    else:
        lines.append("暂无有效记录")

    lines.extend(["", "主要窗口/任务", "-" * 32])
    if title_totals:
        for (app, title), seconds in sorted(title_totals.items(), key=lambda x: x[1], reverse=True)[:20]:
            lines.append(f"{fmt_duration(seconds)}  {app}  {title}")
    else:
        lines.append("暂无有效记录")

    lines.extend(["", "时间线", "-" * 32])
    if rows:
        for row in rows:
            start = row["start"].strftime("%H:%M")
            end = row["end"].strftime("%H:%M")
            if row["state"] == "idle":
                label = "空闲"
            else:
                label = (
                    f"{row['app']} - {title_key(row['title'])} "
                    f"(键盘 {row['key_presses']} 次，鼠标 {row['mouse_clicks']} 次)"
                )
            lines.append(f"{start}-{end}  {label}")
    else:
        lines.append("暂无记录")

    return "\n".join(lines) + "\n"


def save_report(day):
    report = build_report(day)
    path = DATA_DIR / f"work_log_{day.isoformat()}.txt"
    path.write_text(report, encoding="utf-8-sig")
    return path


def save_activity_chart(day):
    rows = read_events_for_day(day)
    active_rows = [r for r in rows if r["state"] == "active"]
    idle_rows = [r for r in rows if r["state"] == "idle"]

    app_totals = {}
    key_total = 0
    mouse_total = 0
    for row in active_rows:
        seconds = max(0, int((row["end"] - row["start"]).total_seconds()))
        app_totals[row["app"]] = app_totals.get(row["app"], 0) + seconds
        key_total += row["key_presses"]
        mouse_total += row["mouse_clicks"]

    idle_total = sum(max(0, int((row["end"] - row["start"]).total_seconds())) for row in idle_rows)
    active_total = sum(app_totals.values())
    top_apps = sorted(app_totals.items(), key=lambda item: item[1], reverse=True)[:10]
    max_app_seconds = max([seconds for _, seconds in top_apps] or [1])

    hourly_active = [0] * 24
    hourly_idle = [0] * 24
    for row in rows:
        cursor = row["start"]
        while cursor < row["end"]:
            next_hour = (cursor.replace(minute=0, second=0) + dt.timedelta(hours=1))
            segment_end = min(row["end"], next_hour)
            seconds = max(0, int((segment_end - cursor).total_seconds()))
            if row["state"] == "idle":
                hourly_idle[cursor.hour] += seconds
            else:
                hourly_active[cursor.hour] += seconds
            cursor = segment_end

    width = 1100
    height = 720
    margin = 56
    bar_x = 220
    bar_width = 620
    row_height = 34
    palette = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2", "#4f46e5", "#65a30d"]

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<style>text{font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif;fill:#0f172a}.muted{fill:#64748b}.small{font-size:13px}.label{font-size:14px}.title{font-size:26px;font-weight:700}.metric{font-size:18px;font-weight:600}</style>',
        f'<text x="{margin}" y="52" class="title">Daily Activity Chart - {html.escape(day.isoformat())}</text>',
        f'<text x="{margin}" y="88" class="metric">Active: {html.escape(fmt_duration(active_total))}</text>',
        f'<text x="300" y="88" class="metric">Idle: {html.escape(fmt_duration(idle_total))}</text>',
        f'<text x="500" y="88" class="metric">Keys: {key_total}</text>',
        f'<text x="650" y="88" class="metric">Mouse: {mouse_total}</text>',
        f'<text x="{margin}" y="132" class="label">Top applications</text>',
    ]

    y = 162
    if top_apps:
        for index, (app, seconds) in enumerate(top_apps):
            color = palette[index % len(palette)]
            bar_len = int((seconds / max_app_seconds) * bar_width)
            parts.extend([
                f'<text x="{margin}" y="{y + 18}" class="small">{html.escape(app[:24])}</text>',
                f'<rect x="{bar_x}" y="{y}" width="{bar_width}" height="22" rx="4" fill="#e2e8f0"/>',
                f'<rect x="{bar_x}" y="{y}" width="{bar_len}" height="22" rx="4" fill="{color}"/>',
                f'<text x="{bar_x + bar_width + 16}" y="{y + 17}" class="small muted">{html.escape(fmt_duration(seconds))}</text>',
            ])
            y += row_height
    else:
        parts.append(f'<text x="{margin}" y="{y}" class="small muted">No active records yet.</text>')

    chart_top = 540
    chart_left = margin
    chart_width = width - margin * 2
    chart_height = 92
    max_hour_seconds = max(hourly_active + hourly_idle + [1])
    slot = chart_width / 24
    parts.extend([
        f'<text x="{margin}" y="{chart_top - 24}" class="label">Hourly activity</text>',
        f'<line x1="{chart_left}" y1="{chart_top + chart_height}" x2="{chart_left + chart_width}" y2="{chart_top + chart_height}" stroke="#cbd5e1"/>',
    ])

    for hour in range(24):
        x = chart_left + hour * slot + 4
        active_h = int((hourly_active[hour] / max_hour_seconds) * chart_height)
        idle_h = int((hourly_idle[hour] / max_hour_seconds) * chart_height)
        base = chart_top + chart_height
        parts.append(f'<rect x="{x:.1f}" y="{base - active_h}" width="{slot - 8:.1f}" height="{active_h}" rx="3" fill="#2563eb"/>')
        if idle_h:
            parts.append(f'<rect x="{x:.1f}" y="{base - active_h - idle_h}" width="{slot - 8:.1f}" height="{idle_h}" rx="3" fill="#94a3b8"/>')
        if hour % 2 == 0:
            parts.append(f'<text x="{x:.1f}" y="{base + 20}" class="small muted">{hour:02d}</text>')

    parts.extend([
        f'<rect x="{margin}" y="{height - 42}" width="16" height="10" fill="#2563eb"/>',
        f'<text x="{margin + 24}" y="{height - 33}" class="small muted">active</text>',
        f'<rect x="{margin + 100}" y="{height - 42}" width="16" height="10" fill="#94a3b8"/>',
        f'<text x="{margin + 124}" y="{height - 33}" class="small muted">idle</text>',
        '</svg>',
    ])

    path = DATA_DIR / f"work_chart_{day.isoformat()}.svg"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def save_daily_outputs(day):
    report_path = save_report(day)
    chart_path = save_activity_chart(day)
    return report_path, chart_path


def backfill_missing_reports(today=None):
    today = today or dt.date.today()
    created = []
    for day in event_dates_before(today):
        report_path = DATA_DIR / f"work_log_{day.isoformat()}.txt"
        chart_path = DATA_DIR / f"work_chart_{day.isoformat()}.svg"
        if report_path.exists() and chart_path.exists():
            continue
        save_daily_outputs(day)
        created.append(day)
    if created:
        write_runtime_log("backfilled reports: " + ", ".join(day.isoformat() for day in created))
    return created


def clear_report_ipc_files():
    for path in (REPORT_REQUEST_FILE, REPORT_RESPONSE_FILE):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def handle_report_request(current_start, timestamp, app, title, state, activity_counter):
    if not REPORT_REQUEST_FILE.exists():
        return current_start

    try:
        day_text = REPORT_REQUEST_FILE.read_text(encoding="utf-8").strip() or "today"
        day = parse_day(day_text)
    except Exception as exc:
        REPORT_RESPONSE_FILE.write_text(f"ERROR\n{exc}", encoding="utf-8")
        REPORT_REQUEST_FILE.unlink(missing_ok=True)
        return current_start

    key_presses, mouse_clicks = activity_counter.consume()
    append_event(current_start, timestamp, app, title, state, key_presses, mouse_clicks)
    report_path, chart_path = save_daily_outputs(day)
    REPORT_RESPONSE_FILE.write_text(f"OK\n{report_path}\n{chart_path}", encoding="utf-8")
    REPORT_REQUEST_FILE.unlink(missing_ok=True)
    write_runtime_log(f"generated requested report for {day.isoformat()}")
    return timestamp


def request_report_from_tracker(day, timeout_seconds=8):
    ensure_data_dir()
    clear_report_ipc_files()
    REPORT_REQUEST_FILE.write_text(day.isoformat(), encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        if REPORT_RESPONSE_FILE.exists():
            response = REPORT_RESPONSE_FILE.read_text(encoding="utf-8").splitlines()
            clear_report_ipc_files()
            if response and response[0] == "OK" and len(response) >= 3:
                return Path(response[1]), Path(response[2])
            raise RuntimeError("\n".join(response[1:]) if len(response) > 1 else "report request failed")
        time.sleep(0.2)

    clear_report_ipc_files()
    return None


def write_runtime_log(message):
    ensure_data_dir()
    path = DATA_DIR / "runtime.log"
    timestamp = now_local().isoformat(sep=" ")
    with path.open("a", encoding="utf-8-sig") as f:
        f.write(f"[{timestamp}] {message}\n")


def parse_clock_time(value):
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("off", "none", "disabled"):
        return None
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("daily report time must be HH:MM")
    return dt.time(hour=hour, minute=minute)


def next_report_datetime(after, report_time):
    if report_time is None:
        return None
    candidate = dt.datetime.combine(after.date(), report_time)
    if after >= candidate:
        candidate += dt.timedelta(days=1)
    return candidate


def run_tracker(sample_seconds, idle_after_seconds, activity_poll_seconds, daily_report_time):
    ensure_data_dir()
    clear_stop_file()
    clear_report_ipc_files()
    write_pid()
    write_runtime_log("tracker started")
    backfill_missing_reports()

    stop = False

    def handle_stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    current_start = now_local()
    app, title = get_foreground_activity()
    state = "idle" if get_idle_seconds() >= idle_after_seconds else "active"
    current_key = (app, title, state)
    activity_counter = ActivityCounter()
    next_sample_at = time.monotonic() + sample_seconds
    next_daily_report_at = next_report_datetime(current_start, daily_report_time)

    try:
        while not stop and not STOP_FILE.exists():
            time.sleep(activity_poll_seconds)
            activity_counter.poll()
            if REPORT_REQUEST_FILE.exists():
                current_start = handle_report_request(current_start, now_local(), app, title, state, activity_counter)
                next_sample_at = time.monotonic() + sample_seconds

            if time.monotonic() < next_sample_at:
                continue
            next_sample_at = time.monotonic() + sample_seconds

            timestamp = now_local()
            new_state = "idle" if get_idle_seconds() >= idle_after_seconds else "active"
            new_app, new_title = ("idle", "用户空闲") if new_state == "idle" else get_foreground_activity()
            new_key = (new_app, new_title, new_state)

            crossed_midnight = current_start.date() != timestamp.date()
            report_due = next_daily_report_at is not None and timestamp >= next_daily_report_at
            if new_key != current_key or crossed_midnight or report_due:
                key_presses, mouse_clicks = activity_counter.consume()
                append_event(current_start, timestamp, app, title, state, key_presses, mouse_clicks)
                report_days = set()
                if crossed_midnight:
                    report_days.add(current_start.date())
                while next_daily_report_at is not None and timestamp >= next_daily_report_at:
                    report_days.add(next_daily_report_at.date())
                    next_daily_report_at += dt.timedelta(days=1)
                for report_day in sorted(report_days):
                    save_daily_outputs(report_day)
                current_start = timestamp
                app, title, state = new_app, new_title, new_state
                current_key = new_key
    except Exception as exc:
        write_runtime_log(f"tracker error: {exc!r}")
        raise
    finally:
        activity_counter.poll()
        key_presses, mouse_clicks = activity_counter.consume()
        append_event(current_start, now_local(), app, title, state, key_presses, mouse_clicks)
        save_daily_outputs(now_local().date())
        write_runtime_log("tracker stopped")
        clear_stop_file()
        clear_pid()


def stop_tracker():
    if not PID_FILE.exists():
        print("没有找到正在运行的记录进程。")
        return 1

    pid_text = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit():
        print("PID 文件内容异常，可以手动删除 data/tracker.pid。")
        return 1

    pid = int(pid_text)
    if not process_exists(pid):
        PID_FILE.unlink()
        clear_stop_file()
        print("之前的记录进程已经不存在，已清理残留状态。")
        return 0

    STOP_FILE.write_text(now_local().isoformat(sep=" "), encoding="utf-8")

    for _ in range(20):
        if not PID_FILE.exists():
            print(f"已停止记录进程 {pid}，并生成了今天的日报。")
            return 0
        time.sleep(0.5)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"已请求停止，但进程未及时退出: {exc}")
        return 1

    print(f"记录进程 {pid} 未及时响应，已强制停止。")
    return 0


def tracker_status():
    if not PID_FILE.exists():
        print("未运行")
        return 1

    pid_text = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit():
        print("状态异常：PID 文件内容异常")
        return 1

    pid = int(pid_text)
    if not process_exists(pid):
        PID_FILE.unlink()
        clear_stop_file()
        print("未运行：发现残留 PID 文件")
        return 1

    print(f"正在运行，进程号: {pid}")
    return 0


def show_notification(kind):
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            user32.SetProcessDPIAware()

        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        title = "\u672c\u5730\u5de5\u4f5c\u8bb0\u5f55"
        if kind == "started":
            messagebox.showinfo(
                title,
                "\u5de5\u4f5c\u8bb0\u5f55\u5df2\u542f\u52a8\u3002\n\n"
                "\u73b0\u5728\u4f1a\u5728\u540e\u53f0\u7edf\u8ba1\uff1a\n"
                "\u2022 \u5f53\u524d\u7a97\u53e3\n"
                "\u2022 \u952e\u76d8\u8f93\u5165\u6b21\u6570\n"
                "\u2022 \u9f20\u6807\u70b9\u51fb\u6b21\u6570\n"
                "\u2022 \u7a7a\u95f2\u65f6\u95f4\n\n"
                "\u4e0b\u73ed\u540e\u53cc\u51fb generate_today_report.bat \u751f\u6210\u4eca\u5929\u7684\u65e5\u62a5\u3002",
                parent=root,
            )
        else:
            messagebox.showwarning(
                title,
                "\u5de5\u4f5c\u8bb0\u5f55\u53ef\u80fd\u6ca1\u6709\u542f\u52a8\u6210\u529f\u3002\n\n"
                "\u8bf7\u67e5\u770b data\\startup.log\uff0c\u6216\u5148\u53cc\u51fb stop_tracker.bat \u540e\u518d\u542f\u52a8\u3002",
                parent=root,
            )
        root.destroy()
        return 0
    except Exception as exc:
        print(f"notification failed: {exc}")
        return 1


def parse_day(value):
    if value in (None, "today"):
        return dt.date.today()
    if value == "yesterday":
        return dt.date.today() - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


def main():
    parser = argparse.ArgumentParser(description="本地工作内容自动记录工具")
    subparsers = parser.add_subparsers(dest="command")

    run_cmd = subparsers.add_parser("run", help="开始记录")
    run_cmd.add_argument("--sample-seconds", type=int, default=5, help="采样间隔，默认 5 秒")
    run_cmd.add_argument("--idle-after-seconds", type=int, default=300, help="多少秒无操作算空闲，默认 300 秒")
    run_cmd.add_argument("--activity-poll-seconds", type=float, default=0.05, help="键盘鼠标计数轮询间隔，默认 0.05 秒")

    run_cmd.add_argument("--daily-report-time", default="23:59", help="daily report time, HH:MM, or off")

    report_cmd = subparsers.add_parser("report", help="生成日报")
    report_cmd.add_argument("--day", default="today", help="today、yesterday 或 YYYY-MM-DD")

    subparsers.add_parser("stop", help="停止后台记录")
    subparsers.add_parser("status", help="查看后台记录状态")
    subparsers.add_parser("backfill", help="补生成今天以前缺失的日报和图表")

    notify_cmd = subparsers.add_parser("notify", help="show desktop notification")
    notify_cmd.add_argument("kind", choices=["started", "failed"])

    args = parser.parse_args()
    if args.command in (None, "run"):
        run_tracker(
            args.sample_seconds if args.command else 5,
            args.idle_after_seconds if args.command else 300,
            args.activity_poll_seconds if args.command else 0.05,
            parse_clock_time(args.daily_report_time) if args.command else parse_clock_time("23:59"),
        )
        return 0
    if args.command == "report":
        ensure_data_dir()
        day = parse_day(args.day)
        tracker_was_running = PID_FILE.exists()
        requested_paths = request_report_from_tracker(day) if tracker_was_running else None
        if requested_paths is None:
            if tracker_was_running:
                print("后台记录没有及时响应，将根据已经写入的数据生成报告。")
            else:
                print("后台记录未运行，将根据已经写入的数据生成报告。")
            report_path, chart_path = save_daily_outputs(day)
        else:
            report_path, chart_path = requested_paths
        print(report_path)
        print(chart_path)
        return 0
    if args.command == "stop":
        return stop_tracker()
    if args.command == "status":
        return tracker_status()
    if args.command == "backfill":
        created = backfill_missing_reports()
        if created:
            print("补生成完成: " + ", ".join(day.isoformat() for day in created))
        else:
            print("没有缺失的历史日报。")
        return 0
    if args.command == "notify":
        return show_notification(args.kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
