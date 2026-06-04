import argparse
import csv
import ctypes
import datetime as dt
import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_NAME = "LocalWorkTracker"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EVENTS_FILE = DATA_DIR / "events.csv"
DASHBOARD_FILE = DATA_DIR / "activity_dashboard.html"
PID_FILE = DATA_DIR / "tracker.pid"
STOP_FILE = DATA_DIR / "tracker.stop"
REPORT_REQUEST_FILE = DATA_DIR / "report.request"
REPORT_RESPONSE_FILE = DATA_DIR / "report.response"
REFRESH_SERVER_HOST = "127.0.0.1"
REFRESH_SERVER_PORT = 8765
EVENT_COLUMNS = ["start", "end", "seconds", "app", "title", "state", "key_presses", "mouse_clicks"]
INSTANCE_MUTEX_NAME = r"Local\LocalWorkTracker_Running"
UNKNOWN_SLEEP_APP = "unknown"
UNKNOWN_SLEEP_TITLES = {"", "无标题", "Untitled"}
LOCK_SCREEN_APPS = {"lockapp.exe"}
SLEEP_BRIDGE_APPS = {"startmenuexperiencehost.exe"}
LEGACY_IDLE_SECONDS = 300
UNOBSERVED_APP = "idle"
UNOBSERVED_TITLE = "系统睡眠或采样暂停"
PENDING_EVENTS = []
EVENTS_WRITE_BLOCKED = False
REPORT_REQUEST_LOCK = threading.Lock()


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ERROR_ALREADY_EXISTS = 183
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


def is_unknown_sleep_event(app, title):
    app_name = (app or "").strip().lower()
    window_title = " ".join((title or "").split())
    return app_name == UNKNOWN_SLEEP_APP and window_title in UNKNOWN_SLEEP_TITLES


def normalize_activity(app, title, state, seconds=0, key_presses=0, mouse_clicks=0):
    if state != "active":
        return app, title, state

    app_name = (app or "").strip().lower()
    no_input = int(key_presses or 0) == 0 and int(mouse_clicks or 0) == 0
    if is_unknown_sleep_event(app, title) or app_name in LOCK_SCREEN_APPS:
        return app, title, "idle"
    if app_name in SLEEP_BRIDGE_APPS and seconds >= LEGACY_IDLE_SECONDS and no_input:
        return app, title, "idle"
    return app, title, state


def append_event(start, end, app, title, state, key_presses=0, mouse_clicks=0):
    global EVENTS_WRITE_BLOCKED
    seconds = max(0, int((end - start).total_seconds()))
    if seconds <= 0:
        return
    app, title, state = normalize_activity(app, title, state, seconds, key_presses, mouse_clicks)

    PENDING_EVENTS.append([
        start.isoformat(sep=" "),
        end.isoformat(sep=" "),
        seconds,
        app,
        title,
        state,
        int(key_presses),
        int(mouse_clicks),
    ])
    try:
        with EVENTS_FILE.open("a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows(PENDING_EVENTS)
        PENDING_EVENTS.clear()
        if EVENTS_WRITE_BLOCKED:
            write_runtime_log("events.csv became writable again; queued records saved")
            EVENTS_WRITE_BLOCKED = False
    except OSError as exc:
        if not EVENTS_WRITE_BLOCKED:
            write_runtime_log(f"events.csv temporarily unavailable; records will be retried: {exc!r}")
            EVENTS_WRITE_BLOCKED = True


def acquire_instance_mutex():
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError("记录程序已经在运行")
    return handle


def tracker_instance_exists():
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, INSTANCE_MUTEX_NAME)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def write_pid():
    ensure_data_dir()
    if PID_FILE.exists():
        PID_FILE.unlink()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid():
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def clear_stale_state():
    for path in (PID_FILE, STOP_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_overlapping_events(rows):
    """Return a single timeline when recovered or legacy records overlap."""
    if not rows:
        return []

    boundaries = sorted({point for row in rows for point in (row["start"], row["end"])})
    segments = []
    for start, end in zip(boundaries, boundaries[1:]):
        if start >= end:
            continue
        candidates = [
            (index, row)
            for index, row in enumerate(rows)
            if row["start"] < end and row["end"] > start
        ]
        if not candidates:
            continue

        # A sampled active window is more reliable than an overlapping inferred idle period.
        source_index, winner = max(
            candidates,
            key=lambda item: (item[1]["state"] == "active", item[0]),
        )
        segment = dict(winner)
        segment.update({
            "start": start,
            "end": end,
            "seconds": int((end - start).total_seconds()),
            "_source_index": source_index,
        })
        merge_idle = (
            segments
            and segments[-1]["end"] == start
            and segments[-1]["state"] == "idle"
            and segment["state"] == "idle"
        )
        merge_same_source = (
            segments
            and segments[-1]["end"] == start
            and segments[-1]["_source_index"] == source_index
        )
        if merge_idle or merge_same_source:
            segments[-1]["end"] = end
            segments[-1]["seconds"] += segment["seconds"]
        else:
            segments.append(segment)

    counted_sources = set()
    for segment in segments:
        source_index = segment.pop("_source_index")
        if source_index in counted_sources:
            segment["key_presses"] = 0
            segment["mouse_clicks"] = 0
        else:
            counted_sources.add(source_index)
    return segments


def clear_stop_file():
    try:
        if STOP_FILE.exists():
            STOP_FILE.unlink()
    except OSError:
        pass


def read_events_for_day(day):
    ensure_data_dir()
    rows = []
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)

    with EVENTS_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                start = dt.datetime.fromisoformat(row["start"].lstrip("\x00"))
                end = dt.datetime.fromisoformat(row["end"])
                seconds = int(row["seconds"])
                app = row["app"]
                title = row["title"]
                state = row["state"]
                key_presses = int(row.get("key_presses") or 0)
                mouse_clicks = int(row.get("mouse_clicks") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if end <= day_start or start >= day_end:
                continue
            app, title, state = normalize_activity(app, title, state, seconds, key_presses, mouse_clicks)
            rows.append({
                "start": max(start, day_start),
                "end": min(end, day_end),
                "seconds": seconds,
                "app": app,
                "title": title,
                "state": state,
                "key_presses": key_presses,
                "mouse_clicks": mouse_clicks,
            })
    return resolve_overlapping_events(rows)


def event_dates_before(day):
    ensure_data_dir()
    dates = set()
    with EVENTS_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                start_date = dt.datetime.fromisoformat(row["start"].lstrip("\x00")).date()
                end_date = dt.datetime.fromisoformat(row["end"]).date()
            except (KeyError, TypeError, ValueError):
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


def fmt_precise_duration(seconds):
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分钟{seconds:02d}秒"
    if minutes:
        return f"{minutes}分钟{seconds:02d}秒"
    return f"{seconds}秒"


def title_key(title):
    cleaned = " ".join((title or "无标题").split())
    return cleaned[:80]


def observation_end_for_day(day, timestamp=None):
    timestamp = timestamp or now_local()
    day_start = dt.datetime.combine(day, dt.time.min)
    day_end = day_start + dt.timedelta(days=1)
    if day < timestamp.date():
        return day_end
    if day > timestamp.date():
        return day_start
    return min(day_end, timestamp)


def unrecorded_rows_for_day(rows, day, timestamp=None):
    day_start = dt.datetime.combine(day, dt.time.min)
    observed_end = observation_end_for_day(day, timestamp)
    cursor = day_start
    gaps = []

    for row in rows:
        if cursor >= observed_end:
            break
        if row["end"] <= cursor:
            continue
        segment_start = min(max(row["start"], day_start), observed_end)
        if cursor < segment_start:
            gaps.append({
                "start": cursor,
                "end": segment_start,
                "seconds": int((segment_start - cursor).total_seconds()),
                "app": "unrecorded",
                "title": "未统计",
                "state": "unrecorded",
                "key_presses": 0,
                "mouse_clicks": 0,
            })
        cursor = max(cursor, min(row["end"], observed_end))

    if cursor < observed_end:
        gaps.append({
            "start": cursor,
            "end": observed_end,
            "seconds": int((observed_end - cursor).total_seconds()),
            "app": "unrecorded",
            "title": "未统计",
            "state": "unrecorded",
            "key_presses": 0,
            "mouse_clicks": 0,
        })
    return gaps


def build_report(day):
    rows = read_events_for_day(day)
    active_rows = [r for r in rows if r["state"] == "active"]
    idle_rows = [r for r in rows if r["state"] == "idle"]
    unrecorded_rows = unrecorded_rows_for_day(rows, day)
    timeline_rows = sorted(rows + unrecorded_rows, key=lambda row: row["start"])

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
    total_unrecorded = sum(row["seconds"] for row in unrecorded_rows)
    active_minutes = max(1, total_active / 60)
    key_rate = key_total / active_minutes
    mouse_rate = mouse_total / active_minutes

    lines = [
        f"工作记录日报 - {day.isoformat()}",
        "=" * 32,
        f"有效使用时长: {fmt_duration(total_active)}",
        f"空闲时长: {fmt_duration(total_idle)}",
        f"未统计时长: {fmt_precise_duration(total_unrecorded)}",
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
    if timeline_rows:
        for row in timeline_rows:
            start = row["start"].strftime("%H:%M")
            end = row["end"].strftime("%H:%M")
            if row["state"] == "idle":
                label = "空闲"
            elif row["state"] == "unrecorded":
                label = f"未统计（{fmt_precise_duration(row['seconds'])}）"
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

    hourly_unrecorded = [0] * 24
    for row in unrecorded_rows_for_day(rows, day):
        cursor = row["start"]
        while cursor < row["end"]:
            next_hour = cursor.replace(minute=0, second=0) + dt.timedelta(hours=1)
            segment_end = min(row["end"], next_hour)
            hourly_unrecorded[cursor.hour] += max(0, int((segment_end - cursor).total_seconds()))
            cursor = segment_end
    unrecorded_total = sum(hourly_unrecorded)

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
        f'<text x="820" y="88" class="metric">Unrecorded: {html.escape(fmt_precise_duration(unrecorded_total))}</text>',
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
    slot = chart_width / 24
    parts.extend([
        f'<text x="{margin}" y="{chart_top - 24}" class="label">Hourly activity</text>',
        f'<line x1="{chart_left}" y1="{chart_top + chart_height}" x2="{chart_left + chart_width}" y2="{chart_top + chart_height}" stroke="#cbd5e1"/>',
    ])

    for hour in range(24):
        x = chart_left + hour * slot + 4
        active_h = int((hourly_active[hour] / 3600) * chart_height)
        idle_h = int((hourly_idle[hour] / 3600) * chart_height)
        unrecorded_h = int((hourly_unrecorded[hour] / 3600) * chart_height)
        base = chart_top + chart_height
        parts.append(f'<rect x="{x:.1f}" y="{chart_top}" width="{slot - 8:.1f}" height="{chart_height}" rx="3" fill="#f1f5f9" stroke="#e2e8f0"/>')
        parts.append(f'<rect x="{x:.1f}" y="{base - active_h}" width="{slot - 8:.1f}" height="{active_h}" rx="3" fill="#2563eb"/>')
        if idle_h:
            parts.append(f'<rect x="{x:.1f}" y="{base - active_h - idle_h}" width="{slot - 8:.1f}" height="{idle_h}" rx="3" fill="#94a3b8"/>')
        if unrecorded_h:
            parts.append(f'<rect x="{x:.1f}" y="{base - active_h - idle_h - unrecorded_h}" width="{slot - 8:.1f}" height="{unrecorded_h}" rx="3" fill="#cbd5e1"/>')
        if hour % 2 == 0:
            parts.append(f'<text x="{x:.1f}" y="{base + 20}" class="small muted">{hour:02d}</text>')

    parts.extend([
        f'<rect x="{margin}" y="{height - 42}" width="16" height="10" fill="#2563eb"/>',
        f'<text x="{margin + 24}" y="{height - 33}" class="small muted">active</text>',
        f'<rect x="{margin + 100}" y="{height - 42}" width="16" height="10" fill="#94a3b8"/>',
        f'<text x="{margin + 124}" y="{height - 33}" class="small muted">idle</text>',
        f'<rect x="{margin + 184}" y="{height - 42}" width="16" height="10" fill="#cbd5e1"/>',
        f'<text x="{margin + 208}" y="{height - 33}" class="small muted">unrecorded</text>',
        '</svg>',
    ])

    path = DATA_DIR / f"work_chart_{day.isoformat()}.svg"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def dashboard_chart_days():
    days = []
    for path in DATA_DIR.glob("work_chart_*.svg"):
        try:
            days.append(dt.date.fromisoformat(path.stem[len("work_chart_"):]))
        except ValueError:
            continue
    return sorted(set(days), reverse=True)


def save_activity_dashboard():
    days = dashboard_chart_days()
    summaries = []
    total_active = 0
    total_idle = 0
    total_keys = 0
    total_mouse = 0

    for day in days:
        rows = read_events_for_day(day)
        active_rows = [row for row in rows if row["state"] == "active"]
        idle_rows = [row for row in rows if row["state"] == "idle"]
        app_totals = {}
        key_total = 0
        mouse_total = 0
        for row in active_rows:
            seconds = max(0, int((row["end"] - row["start"]).total_seconds()))
            app_totals[row["app"]] = app_totals.get(row["app"], 0) + seconds
            key_total += row["key_presses"]
            mouse_total += row["mouse_clicks"]

        active_seconds = sum(app_totals.values())
        idle_seconds = sum(
            max(0, int((row["end"] - row["start"]).total_seconds()))
            for row in idle_rows
        )
        top_app = max(app_totals.items(), key=lambda item: item[1])[0] if app_totals else "无活动记录"
        summaries.append((day, active_seconds, idle_seconds, key_total, mouse_total, top_app))
        total_active += active_seconds
        total_idle += idle_seconds
        total_keys += key_total
        total_mouse += mouse_total

    nav_items = []
    cards = []
    for day, active_seconds, idle_seconds, key_total, mouse_total, top_app in summaries:
        date_text = day.isoformat()
        chart_name = f"work_chart_{date_text}.svg"
        report_name = f"work_log_{date_text}.txt"
        report_link = ""
        if (DATA_DIR / report_name).exists():
            report_link = f'<a class="secondary" href="{html.escape(report_name)}">文字日报</a>'
        nav_items.append(
            f'<a href="#day-{date_text}"><strong>{date_text}</strong>'
            f'<span>{html.escape(fmt_duration(active_seconds))}</span></a>'
        )
        cards.append(
            f'<section class="day-card" id="day-{date_text}">'
            f'<header><div><h2>{date_text}</h2><p>主要应用：{html.escape(top_app)}</p></div>'
            f'<div class="links">{report_link}'
            f'<a href="{html.escape(chart_name)}" target="_blank">单独打开图表</a></div></header>'
            f'<div class="daily-metrics">'
            f'<span>有效使用 <strong>{html.escape(fmt_duration(active_seconds))}</strong></span>'
            f'<span>空闲 <strong>{html.escape(fmt_duration(idle_seconds))}</strong></span>'
            f'<span>键盘 <strong>{key_total}</strong></span>'
            f'<span>鼠标 <strong>{mouse_total}</strong></span></div>'
            f'<img loading="lazy" src="{html.escape(chart_name)}" alt="{date_text} 活动图表"></section>'
        )

    if not cards:
        cards.append('<section class="empty">暂无已生成的每日活动图表。</section>')

    updated_at = now_local().strftime("%Y-%m-%d %H:%M")
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>电脑活动总览</title>
<style>
:root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --blue:#2563eb; --panel:#ffffff; --bg:#f1f5f9; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif; color:var(--ink); background:var(--bg); }}
.hero {{ background:#0f172a; color:#fff; padding:36px max(28px, calc((100vw - 1320px) / 2)); }}
.hero-bar {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:28px; }}
.hero h1 {{ font-size:32px; margin:0 0 8px; }}
.hero p {{ color:#cbd5e1; margin:0; }}
.refresh-actions {{ display:flex; flex-direction:column; align-items:flex-end; gap:8px; min-width:190px; }}
.refresh-button {{ border:0; border-radius:10px; background:#38bdf8; color:#082f49; cursor:pointer; font:600 14px "Segoe UI","Microsoft YaHei",Arial,sans-serif; padding:11px 16px; }}
.refresh-button:hover {{ background:#7dd3fc; }}
.refresh-button:disabled {{ cursor:wait; opacity:.72; }}
.refresh-status {{ color:#cbd5e1; font-size:12px; min-height:18px; text-align:right; }}
.overview {{ display:grid; grid-template-columns:repeat(4,minmax(130px,1fr)); gap:14px; max-width:820px; }}
.metric {{ background:rgba(255,255,255,.09); border-radius:14px; padding:14px 16px; }}
.metric span {{ display:block; font-size:12px; color:#cbd5e1; margin-bottom:5px; }}
.metric strong {{ font-size:22px; }}
.layout {{ display:grid; grid-template-columns:220px minmax(0, 1100px); gap:24px; max-width:1360px; margin:24px auto; padding:0 20px; }}
nav {{ position:sticky; top:20px; align-self:start; background:var(--panel); border-radius:16px; padding:16px; box-shadow:0 1px 3px rgba(15,23,42,.08); }}
nav h2 {{ margin:0 0 12px; font-size:15px; }}
nav a {{ display:flex; justify-content:space-between; gap:6px; color:var(--ink); text-decoration:none; padding:9px 8px; border-radius:8px; font-size:13px; }}
nav a:hover {{ background:#eff6ff; color:var(--blue); }}
nav span {{ color:var(--muted); }}
main {{ display:grid; gap:22px; }}
.day-card {{ background:var(--panel); border-radius:18px; padding:20px; box-shadow:0 1px 4px rgba(15,23,42,.08); scroll-margin-top:20px; }}
.day-card header {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }}
.day-card h2 {{ font-size:22px; margin:0 0 5px; }}
.day-card header p {{ margin:0; color:var(--muted); font-size:14px; }}
.links {{ display:flex; gap:8px; }}
.links a {{ background:var(--blue); color:#fff; padding:9px 12px; border-radius:9px; text-decoration:none; font-size:13px; }}
.links .secondary {{ background:#e2e8f0; color:var(--ink); }}
.daily-metrics {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
.daily-metrics span {{ background:#f8fafc; border:1px solid var(--line); border-radius:9px; padding:8px 11px; font-size:13px; color:var(--muted); }}
.daily-metrics strong {{ color:var(--ink); margin-left:5px; }}
.day-card img {{ display:block; width:100%; height:auto; border:1px solid var(--line); border-radius:12px; background:#f8fafc; }}
.empty {{ background:var(--panel); border-radius:16px; padding:48px; color:var(--muted); text-align:center; }}
@media (max-width:820px) {{ .hero-bar {{ display:block; }} .refresh-actions {{ align-items:flex-start; margin-top:18px; }} .refresh-status {{ text-align:left; }} .overview {{ grid-template-columns:repeat(2,1fr); }} .layout {{ display:block; }} nav {{ position:static; margin-bottom:20px; }} .day-card header {{ display:block; }} .links {{ margin-top:14px; }} }}
</style>
</head>
<body>
<header class="hero">
<div class="hero-bar">
<div>
<h1>电脑活动总览</h1>
<p>汇集全部每日图表 · 最近更新于 {html.escape(updated_at)}</p>
</div>
<div class="refresh-actions">
<button class="refresh-button" type="button" id="refreshTodayButton">&#21047;&#26032;&#20170;&#26085;&#27963;&#21160;</button>
<span class="refresh-status" id="refreshStatus" role="status" aria-live="polite"></span>
</div>
</div>
<div class="overview">
<div class="metric"><span>已记录日期</span><strong>{len(days)} 天</strong></div>
<div class="metric"><span>累计有效使用</span><strong>{html.escape(fmt_duration(total_active))}</strong></div>
<div class="metric"><span>累计空闲</span><strong>{html.escape(fmt_duration(total_idle))}</strong></div>
<div class="metric"><span>累计输入</span><strong>{total_keys + total_mouse}</strong></div>
</div>
</header>
<div class="layout">
<nav><h2>按日期查看</h2>{"".join(nav_items)}</nav>
<main>{"".join(cards)}</main>
</div>
<script>
const refreshButton = document.getElementById("refreshTodayButton");
const refreshStatus = document.getElementById("refreshStatus");
const refreshEndpoint = "http://{REFRESH_SERVER_HOST}:{REFRESH_SERVER_PORT}/refresh?day=today";

async function refreshTodayActivity() {{
  refreshButton.disabled = true;
  refreshStatus.textContent = "\\u6b63\\u5728\\u66f4\\u65b0...";
  try {{
    const response = await fetch(refreshEndpoint, {{ cache: "no-store" }});
    const payload = await response.json().catch(() => ({{}}));
    if (!response.ok || !payload.ok) {{
      throw new Error(payload.message || "\\u5237\\u65b0\\u5931\\u8d25");
    }}
    const refreshedUrl = new URL(window.location.href);
    refreshedUrl.searchParams.set("refreshed", Date.now().toString());
    window.location.replace(refreshedUrl.toString());
  }} catch (error) {{
    refreshStatus.textContent = error.message || "\\u540e\\u53f0\\u8bb0\\u5f55\\u672a\\u8fd0\\u884c\\uff0c\\u8bf7\\u5148\\u542f\\u52a8\\u8bb0\\u5f55\\u3002";
    refreshButton.disabled = false;
  }}
}}

refreshButton.addEventListener("click", refreshTodayActivity);
</script>
</body>
</html>
"""
    DASHBOARD_FILE.write_text(document, encoding="utf-8")
    return DASHBOARD_FILE


def save_daily_outputs(day):
    report_path = save_report(day)
    chart_path = save_activity_chart(day)
    save_activity_dashboard()
    return report_path, chart_path


def open_daily_outputs(report_path, chart_path):
    failures = []
    browser_output = DASHBOARD_FILE if DASHBOARD_FILE.exists() else chart_path
    for path in (report_path, browser_output):
        try:
            os.startfile(str(path))
        except OSError as exc:
            failures.append((path, exc))

    if failures:
        for path, exc in failures:
            print(f"无法打开 {path}: {exc}")
        return 1
    return 0


def save_daily_outputs_safely(day, reason):
    try:
        return save_daily_outputs(day)
    except Exception as exc:
        write_runtime_log(f"{reason} report failed for {day.isoformat()}: {exc!r}")
        return None


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
    save_activity_dashboard()
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
        try:
            REPORT_REQUEST_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return current_start

    try:
        REPORT_REQUEST_FILE.unlink()
    except OSError as exc:
        write_runtime_log(f"report request file temporarily unavailable; will retry: {exc!r}")
        return current_start

    key_presses, mouse_clicks = activity_counter.consume()
    append_event(current_start, timestamp, app, title, state, key_presses, mouse_clicks)
    paths = save_daily_outputs_safely(day, "requested")
    if paths is None:
        REPORT_RESPONSE_FILE.write_text("ERROR\n日报文件暂时无法写入，请稍后重试。", encoding="utf-8")
    else:
        report_path, chart_path = paths
        REPORT_RESPONSE_FILE.write_text(f"OK\n{report_path}\n{chart_path}", encoding="utf-8")
        write_runtime_log(f"generated requested report for {day.isoformat()}")
    return timestamp


def request_report_from_tracker(day, timeout_seconds=8):
    with REPORT_REQUEST_LOCK:
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


def start_dashboard_refresh_server():
    class RefreshHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            write_runtime_log("refresh server: " + (format % args))

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_json(200, {"ok": True})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.send_json(200, {"ok": True})
                return
            if parsed.path != "/refresh":
                self.send_json(404, {"ok": False, "message": "unknown endpoint"})
                return

            query = parse_qs(parsed.query)
            day_text = query.get("day", ["today"])[0]
            try:
                day = parse_day(day_text)
                paths = request_report_from_tracker(day, timeout_seconds=10)
                if paths is None:
                    self.send_json(503, {"ok": False, "message": "后台记录暂时没有响应，请稍后再试。"})
                    return
                report_path, chart_path = paths
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "day": day.isoformat(),
                        "report": str(report_path),
                        "chart": str(chart_path),
                        "dashboard": str(DASHBOARD_FILE),
                        "updated_at": now_local().isoformat(sep=" "),
                    },
                )
            except Exception as exc:
                write_runtime_log(f"dashboard refresh failed: {exc!r}")
                self.send_json(500, {"ok": False, "message": str(exc)})

    try:
        server = ThreadingHTTPServer((REFRESH_SERVER_HOST, REFRESH_SERVER_PORT), RefreshHandler)
    except OSError as exc:
        write_runtime_log(f"refresh server failed to start: {exc!r}")
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        name="dashboard-refresh-server",
        daemon=True,
    )
    thread.start()
    write_runtime_log(f"refresh server listening on http://{REFRESH_SERVER_HOST}:{REFRESH_SERVER_PORT}")
    return server


def stop_dashboard_refresh_server(server):
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
        write_runtime_log("refresh server stopped")
    except Exception as exc:
        write_runtime_log(f"refresh server stop failed: {exc!r}")


def write_runtime_log(message):
    try:
        ensure_data_dir()
        path = DATA_DIR / "runtime.log"
        timestamp = now_local().isoformat(sep=" ")
        with path.open("a", encoding="utf-8-sig") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


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


def _run_tracker(sample_seconds, idle_after_seconds, activity_poll_seconds, daily_report_time):
    ensure_data_dir()
    clear_stop_file()
    clear_report_ipc_files()
    write_pid()
    write_runtime_log("tracker started")
    refresh_server = start_dashboard_refresh_server()
    try:
        backfill_missing_reports()
    except Exception as exc:
        write_runtime_log(f"backfill failed during startup: {exc!r}")

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
    last_poll_at = current_start
    unobserved_after_seconds = max(15, sample_seconds * 3, activity_poll_seconds * 20)

    def save_reports_through(previous_start, timestamp):
        nonlocal next_daily_report_at
        report_days = set()
        if previous_start.date() != timestamp.date():
            report_days.add(previous_start.date())
        while next_daily_report_at is not None and timestamp >= next_daily_report_at:
            report_days.add(next_daily_report_at.date())
            next_daily_report_at += dt.timedelta(days=1)
        for report_day in sorted(report_days):
            save_daily_outputs_safely(report_day, "automatic")

    try:
        while not stop and not STOP_FILE.exists():
            time.sleep(activity_poll_seconds)
            timestamp = now_local()
            gap_seconds = int((timestamp - last_poll_at).total_seconds())
            if gap_seconds >= unobserved_after_seconds:
                key_presses, mouse_clicks = activity_counter.consume()
                append_event(current_start, last_poll_at, app, title, state, key_presses, mouse_clicks)
                append_event(last_poll_at, timestamp, UNOBSERVED_APP, UNOBSERVED_TITLE, "idle")
                write_runtime_log(f"marked {gap_seconds} seconds without samples as idle")
                save_reports_through(current_start, timestamp)

                state = "idle" if get_idle_seconds() >= idle_after_seconds else "active"
                app, title = ("idle", "用户空闲") if state == "idle" else get_foreground_activity()
                current_key = (app, title, state)
                current_start = timestamp
                next_sample_at = time.monotonic() + sample_seconds

            last_poll_at = timestamp
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
                save_reports_through(current_start, timestamp)
                current_start = timestamp
                app, title, state = new_app, new_title, new_state
                current_key = new_key
    except Exception:
        write_runtime_log("tracker error:\n" + traceback.format_exc().rstrip())
        raise
    finally:
        stop_dashboard_refresh_server(refresh_server)
        timestamp = now_local()
        gap_seconds = int((timestamp - last_poll_at).total_seconds())
        activity_counter.poll()
        key_presses, mouse_clicks = activity_counter.consume()
        if gap_seconds >= unobserved_after_seconds:
            append_event(current_start, last_poll_at, app, title, state, key_presses, mouse_clicks)
            append_event(last_poll_at, timestamp, UNOBSERVED_APP, UNOBSERVED_TITLE, "idle")
        else:
            append_event(current_start, timestamp, app, title, state, key_presses, mouse_clicks)
        save_daily_outputs_safely(now_local().date(), "shutdown")
        write_runtime_log("tracker stopped")
        clear_stop_file()
        clear_pid()


def run_tracker(sample_seconds, idle_after_seconds, activity_poll_seconds, daily_report_time):
    mutex_handle = acquire_instance_mutex()
    try:
        _run_tracker(sample_seconds, idle_after_seconds, activity_poll_seconds, daily_report_time)
    finally:
        kernel32.CloseHandle(mutex_handle)


def stop_tracker():
    if not tracker_instance_exists():
        clear_stale_state()
        print("没有找到正在运行的记录进程；已清理残留状态。")
        return 0

    if not PID_FILE.exists():
        print("记录程序正在启动，请稍后再停止。")
        return 1

    pid_text = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit():
        print("PID 文件内容异常，可以手动删除 data/tracker.pid。")
        return 1

    pid = int(pid_text)
    STOP_FILE.write_text(now_local().isoformat(sep=" "), encoding="utf-8")

    for _ in range(20):
        if not tracker_instance_exists():
            print(f"已停止记录进程 {pid}，并生成了今天的日报。")
            return 0
        time.sleep(0.5)

    print(f"记录进程 {pid} 未及时响应；为避免误结束其他进程，未进行强制终止。")
    return 1


def tracker_status():
    if not tracker_instance_exists():
        clear_stale_state()
        print("未运行")
        return 1

    if not PID_FILE.exists():
        print("正在启动")
        return 0

    pid_text = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit():
        print("正在运行，但 PID 文件内容异常")
        return 0

    print(f"正在运行，进程号: {pid_text}")
    return 0


def tracker_status_text():
    if not tracker_instance_exists():
        clear_stale_state()
        return "后台记录当前未运行。"
    if not PID_FILE.exists():
        return "后台记录正在启动。"
    pid_text = PID_FILE.read_text(encoding="utf-8").strip()
    if pid_text.isdigit():
        return f"后台记录正在运行。\n\n进程号: {pid_text}"
    return "后台记录正在运行，但状态文件内容异常。"


def packaged_command(*arguments):
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    return [sys.executable, str(Path(__file__).resolve()), *arguments]


def show_control_panel():
    import tkinter as tk
    from tkinter import messagebox

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        user32.SetProcessDPIAware()

    root = tk.Tk()
    root.title("电脑活动小日报")
    root.geometry("430x332")
    root.resizable(False, False)

    title = tk.Label(root, text="电脑活动小日报", font=("Microsoft YaHei UI", 18, "bold"))
    title.pack(pady=(22, 4))
    description = tk.Label(root, text="记录窗口使用情况，生成每日活动报告", font=("Microsoft YaHei UI", 10))
    description.pack(pady=(0, 16))
    status_var = tk.StringVar()
    status_label = tk.Label(root, textvariable=status_var, font=("Microsoft YaHei UI", 10), fg="#475569")
    status_label.pack(pady=(0, 14))

    def refresh_status():
        status_var.set(tracker_status_text().split("\n", 1)[0])

    def start_tracking():
        if tracker_instance_exists():
            messagebox.showinfo("电脑活动小日报", "后台记录已经在运行。", parent=root)
            refresh_status()
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            packaged_command("run"),
            cwd=str(BASE_DIR),
            creationflags=creationflags,
        )
        root.after(600, refresh_status)
        messagebox.showinfo("电脑活动小日报", "已请求启动后台记录。", parent=root)

    def generate_today_report():
        day = dt.date.today()
        try:
            paths = request_report_from_tracker(day) if tracker_instance_exists() else None
            report_path, chart_path = paths if paths else save_daily_outputs(day)
            open_daily_outputs(report_path, chart_path)
        except Exception as exc:
            messagebox.showerror("电脑活动小日报", f"日报生成失败：\n{exc}", parent=root)

    def stop_tracking():
        if not tracker_instance_exists():
            messagebox.showinfo("电脑活动小日报", "后台记录当前未运行。", parent=root)
            refresh_status()
            return
        result = stop_tracker()
        refresh_status()
        if result == 0:
            messagebox.showinfo("电脑活动小日报", "后台记录已停止，今天的日报已刷新。", parent=root)
        else:
            messagebox.showwarning("电脑活动小日报", "后台记录未能及时停止，请稍后再试。", parent=root)

    button_style = {"font": ("Microsoft YaHei UI", 10), "width": 26, "pady": 6}
    tk.Button(root, text="开始后台记录", command=start_tracking, **button_style).pack(pady=4)
    tk.Button(root, text="生成今天的日报", command=generate_today_report, **button_style).pack(pady=4)
    tk.Button(root, text="停止后台记录", command=stop_tracking, **button_style).pack(pady=4)
    tk.Button(root, text="刷新状态", command=refresh_status, **button_style).pack(pady=4)

    refresh_status()
    root.mainloop()
    return 0


def show_status_notification():
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("电脑活动小日报", tracker_status_text(), parent=root)
        root.destroy()
        return 0
    except Exception as exc:
        print(f"notification failed: {exc}")
        return 1


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
                "\u4e0b\u73ed\u540e\u53ef\u5728\u4e3b\u9762\u677f\u4e2d\u751f\u6210\u4eca\u5929\u7684\u65e5\u62a5\u3002",
                parent=root,
            )
        else:
            messagebox.showwarning(
                title,
                "\u5de5\u4f5c\u8bb0\u5f55\u53ef\u80fd\u6ca1\u6709\u542f\u52a8\u6210\u529f\u3002\n\n"
                "\u8bf7\u7a0d\u540e\u518d\u5728\u4e3b\u9762\u677f\u4e2d\u5c1d\u8bd5\u542f\u52a8\u3002",
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
    report_cmd.add_argument("--open", action="store_true", dest="open_outputs", help="生成后打开日报和活动总览")

    subparsers.add_parser("stop", help="停止后台记录")
    status_cmd = subparsers.add_parser("status", help="查看后台记录状态")
    status_cmd.add_argument("--popup", action="store_true", help="以弹窗显示运行状态")
    subparsers.add_parser("backfill", help="补生成今天以前缺失的日报和图表")

    notify_cmd = subparsers.add_parser("notify", help="show desktop notification")
    notify_cmd.add_argument("kind", choices=["started", "failed"])

    args = parser.parse_args()
    if args.command is None and getattr(sys, "frozen", False):
        return show_control_panel()
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
        tracker_was_running = tracker_instance_exists()
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
        if DASHBOARD_FILE.exists():
            print(DASHBOARD_FILE)
        if args.open_outputs:
            return open_daily_outputs(report_path, chart_path)
        return 0
    if args.command == "stop":
        return stop_tracker()
    if args.command == "status":
        if args.popup:
            return show_status_notification()
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
