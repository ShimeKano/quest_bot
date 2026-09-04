"""
Discord Quest Bot
=================
Chỉ cần 1 lệnh: /setup
Bot sẽ gửi embed có nút "▶ Chạy Quest" → bấm nút → điền token → tự động chạy.

Cài đặt: pip install -r requirements.txt
Chạy ở local: Tạo file .env điền các giá trị
Chạy trên Azure: Đặt các biến trong Azure Portal → không cần file .env
"""
from keep_alive import keep_alive
import os
import re
import json
import time
import random
import base64
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
import requests

# ── Load .env khi chạy ở LOCAL, trên AZURE sẽ tự động bỏ qua ────────────────
from dotenv import load_dotenv
if os.path.exists(".env"):
    load_dotenv()  # Chỉ đọc file .env khi có ở máy local
# Trên Azure, os.getenv() sẽ lấy trực tiếp từ App Settings → không cần file .env

# ── Bot Config — ĐỌC TỪ BIẾN MÔI TRƯỜNG ──────────────────────────────────────
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OWNER_ID_STR = os.getenv("OWNER_ID", "937182993548718130")
MANAGER_ROLE_ID_STR = os.getenv("MANAGER_ROLE_ID", "937182993548718130")

OWNER_ID = int(OWNER_ID_STR)
MANAGER_ROLE_ID = int(MANAGER_ROLE_ID_STR)

# ── Quest Config ─────────────────────────────────────────────────────────────
API_BASE = "https://discord.com/api/v9"
POLL_INTERVAL = 15
HEARTBEAT_INTERVAL = 5
AUTO_ACCEPT = True
LOG_PROGRESS = True
DEBUG = False
SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE",
]

# ── Global state ─────────────────────────────────────────────────────────────
active_sessions: dict[str, dict] = {}
session_lock = threading.Lock()
session_counter = 0

# ── Build number ─────────────────────────────────────────────────────────────
_cached_build_number: Optional[int] = None

def fetch_latest_build_number() -> int:
    global _cached_build_number
    if _cached_build_number:
        return _cached_build_number
    FALLBACK = 504649
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            return FALLBACK
        scripts = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
        for asset_hash in scripts[-5:]:
            try:
                ar = requests.get(f"https://discord.com/assets/{asset_hash}.js", headers={"User-Agent": ua}, timeout=15)
                m = re.search(r'buildNumber["\s:]+["\s]\*(\d{5,7})', ar.text)
                if m:
                    _cached_build_number = int(m.group(1))
                    return _cached_build_number
            except Exception:
                continue
        return FALLBACK
    except Exception:
        return FALLBACK

def make_super_properties(build_number: int) -> str:
    obj = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9175",
        "os_version": "10.0.26100",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36",
        "browser_version": "32.2.7",
        "client_build_number": build_number,
        "native_build_number": 59498,
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()

# ── Discord API Helper ───────────────────────────────────────────────────────
class DiscordAPI:
    def __init__(self, token: str, build_number: int):
        self.token = token
        self.session = requests.Session()
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36"
        sp = make_super_properties(build_number)
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": ua,
            "X-Super-Properties": sp,
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "Asia/Ho_Chi_Minh",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
        })

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{API_BASE}{path}", **kwargs)

    def post(self, path: str, payload: Optional[dict] = None, **kwargs) -> requests.Response:
        return self.session.post(f"{API_BASE}{path}", json=payload, **kwargs)

    def validate_token(self) -> Optional[dict]:
        try:
            r = self.get("/users/@me")
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None

# ── Quest helpers ────────────────────────────────────────────────────────────
def _get(d: Optional[dict], *keys):
    if d is None: return None
    for k in keys:
        if k in d: return d[k]
    return None

def get_task_config(quest: dict) -> Optional[dict]:
    cfg = quest.get("config", {})
    return _get(cfg, "taskConfig", "task_config", "taskConfigV2", "task_config_v2")

def get_quest_name(quest: dict) -> str:
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    name = _get(msgs, "questName", "quest_name")
    if name: return name.strip()
    game = _get(msgs, "gameTitle", "game_title")
    if game: return game.strip()
    app_name = cfg.get("application", {}).get("name")
    if app_name: return app_name
    return f"Quest#{quest.get('id', '?')}"

def get_expires_at(quest: dict) -> Optional[str]:
    cfg = quest.get("config", {})
    return _get(cfg, "expiresAt", "expires_at")

def get_user_status(quest: dict) -> dict:
    us = _get(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def is_completable(quest: dict) -> bool:
    expires = get_expires_at(quest)
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return False
    tasks_ = tc["tasks"]
    return any(tasks_.get(t) is not None for t in SUPPORTED_TASKS)

def is_enrolled(quest: dict) -> bool:
    us = get_user_status(quest)
    return bool(_get(us, "enrolledAt", "enrolled_at"))

def is_completed(quest: dict) -> bool:
    us = get_user_status(quest)
    return bool(_get(us, "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc:
        return None
    for t in SUPPORTED_TASKS:
        if tc["tasks"].get(t) is not None:
            return t
    return None

def get_seconds_needed(quest: dict) -> int:
    tc = get_task_config(quest)
    task_type = get_task_type(quest)
    if not tc or not task_type: return 0
    return tc["tasks"][task_type].get("target", 0)

def get_seconds_done(quest: dict) -> float:
    task_type = get_task_type(quest)
    if not task_type: return 0
    us = get_user_status(quest)
    progress = us.get("progress", {})
    return progress.get(task_type, {}).get("value", 0) if progress else 0

def get_enrolled_at(quest: dict) -> Optional[str]:
    us = get_user_status(quest)
    return _get(us, "enrolledAt", "enrolled_at")

# ── Quest Autocompleter ──────────────────────────────────────────────────────
class QuestAutocompleter:
    def __init__(self, api: DiscordAPI, log_callback=None, stop_event: threading.Event = None):
        self.api = api
        self.completed_ids: set = set()
        self.quest_threads: dict = {}
        self.lock = threading.Lock()
        self.log_callback = log_callback or (lambda msg, lvl: None)
        self.stop_event = stop_event or threading.Event()
        self.results: list[str] = []

    def log(self, msg: str, level: str = "info"):
        self.log_callback(msg, level)
        self.results.append(f"[{level.upper()}] {msg}")

    def fetch_quests(self) -> list:
        try:
            r = self.api.get("/quests/@me")
            if r.status_code == 200:
                data = r.json()
                return data.get("quests", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            elif r.status_code == 429:
                retry_after = r.json().get("retry_after", 10)
                self.log(f"Rate Limited – Chờ {retry_after}s", "warn")
                time.sleep(retry_after)
                return self.fetch_quests()
            else:
                self.log(f"Lỗi fetch quest ({r.status_code})", "warn")
                return []
        except Exception as e:
            self.log(f"Lỗi kết nối: {e}", "error")
            return []

    def enroll_quest(self, quest: dict) -> bool:
        name = get_quest_name(quest)
        qid = quest["id"]
        for attempt in range(1, 4):
            if self.stop_event.is_set(): return False
            try:
                r = self.api.post(f"/quests/{qid}/enroll", {
                    "location": 11, "is_targeted": False,
                    "metadata_raw": None, "metadata_sealed": None,
                    "traffic_metadata_raw": quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    retry_after = r.json().get("retry_after", 5)
                    self.log(f"Rate Limited khi nhận \"{name}\" (lần {attempt}/3) – Chờ {retry_after+1}s", "warn")
                    time.sleep(retry_after + 1)
                    continue
                if r.status_code in (200, 201, 204):
                    self.log(f"✅ Đã nhận quest: {name}", "ok")
                    return True
                return False
            except Exception as e:
                self.log(f"Lỗi enroll \"{name}\": {e}", "error")
                return False
        return False

    def auto_accept(self, quests: list) -> list:
        if not AUTO_ACCEPT: return quests
        unaccepted = [q for q in quests if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if not unaccepted: return quests
        self.log(f"Tìm thấy {len(unaccepted)} quest chưa nhận", "info")
        for q in unaccepted:
            if self.stop_event.is_set(): break
            self.enroll_quest(q)
            time.sleep(3)
        time.sleep(2)
        return self.fetch_quests()

    def complete_video(self, quest: dict):
        name = get_quest_name(quest)
        qid = quest["id"]
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        enrolled_at_str = get_enrolled_at(quest)
        enrolled_ts = datetime.fromisoformat(enrolled_at_str.replace("Z", "+00:00")).timestamp() if enrolled_at_str else time.time()
        self.log(f"🎬 Video: {name} ({seconds_done:.0f}/{seconds_needed}s)", "info")
        max_future = 15; speed = 15; interval = 0.5
        while seconds_done < seconds_needed:
            if self.stop_event.is_set():
                self.log(f"⛔ Dừng quest: {name}", "warn"); return
            max_allowed = (time.time() - enrolled_ts) + max_future
            timestamp = seconds_done + speed
            if (max_allowed - seconds_done) >= speed:
                try:
                    r = self.api.post(f"/quests/{qid}/video-progress",
                        {"timestamp": min(seconds_needed, timestamp + random.random())})
                    if r.status_code == 200:
                        body = r.json()
                        if body.get("completed_at"):
                            self.log(f"✅ Hoàn thành: {name}", "ok"); return
                        seconds_done = min(seconds_needed, timestamp)
                    elif r.status_code == 429:
                        retry_after = r.json().get("retry_after", 5)
                        time.sleep(retry_after + 1); continue
                except Exception as e:
                    self.log(f"Lỗi video progress: {e}", "error")
            time.sleep(interval)
        try:
            self.api.post(f"/quests/{qid}/video-progress", {"timestamp": seconds_needed})
        except Exception: pass
        self.log(f"✅ Hoàn thành: {name}", "ok")

    def complete_heartbeat(self, quest: dict):
        name = get_quest_name(quest)
        qid = quest["id"]
        task_type = get_task_type(quest)
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        remaining = max(0, seconds_needed - seconds_done)
        self.log(f"🎮 {task_type}: {name} (~{remaining // 60} phút)", "info")
        pid = random.randint(1000, 30000)
        while seconds_done < seconds_needed:
            if self.stop_event.is_set():
                self.log(f"⛔ Dừng quest: {name}", "warn"); return
            try:
                r = self.api.post(f"/quests/{qid}/heartbeat",
                    {"stream_key": f"call:0:{pid}", "terminal": False})
                if r.status_code == 200:
                    body = r.json()
                    progress_data = body.get("progress", {})
                    if progress_data and task_type in progress_data:
                        seconds_done = progress_data[task_type].get("value", seconds_done)
                    if body.get("completed_at") or seconds_done >= seconds_needed:
                        break
                elif r.status_code == 429:
                    retry_after = r.json().get("retry_after", 10)
                    time.sleep(retry_after + 1); continue
            except Exception as e:
                self.log(f"Lỗi heartbeat: {e}", "error")
            time.sleep(HEARTBEAT_INTERVAL)
        try:
            self.api.post(f"/quests/{qid}/heartbeat",
                {"stream_key": f"call:0:{pid}", "terminal": True})
        except Exception: pass
        self.log(f"✅ Hoàn thành: {name}", "ok")

    def complete_activity(self, quest: dict):
        name = get_quest_name(quest)
        qid = quest["id"]
        seconds_needed = get_seconds_needed(quest)
        seconds_done = get_seconds_done(quest)
        remaining = max(0, seconds_needed - seconds_done)
        self.log(f"🕹️ Activity: {name} (~{remaining // 60} phút)", "info")
        stream_key = "call:0:1"
        while seconds_done < seconds_needed:
            if self.stop_event.is_set():
                self.log(f"⛔ Dừng quest: {name}", "warn"); return
            try:
                r = self.api.post(f"/quests/{qid}/heartbeat",
                    {"stream_key": stream_key, "terminal": False})
                if r.status_code == 200:
                    body = r.json()
                    progress_data = body.get("progress", {})
                    if progress_data and "PLAY_ACTIVITY" in progress_data:
                        seconds_done = progress_data["PLAY_ACTIVITY"].get("value", seconds_done)
                    if body.get("completed_at") or seconds_done >= seconds_needed:
                        break
                elif r.status_code == 429:
                    retry_after = r.json().get("retry_after", 10)
                    time.sleep(retry_after + 1); continue
            except Exception as e:
                self.log(f"Lỗi: {e}", "error")
            time.sleep(HEARTBEAT_INTERVAL)
        try:
            self.api.post(f"/quests/{qid}/heartbeat",
                {"stream_key": stream_key, "terminal": True})
        except Exception: pass
        self.log(f"✅ Hoàn thành: {name}", "ok")

    def process_quest(self, quest: dict):
        qid = quest.get("id")
        name = get_quest_name(quest)
        task_type = get_task_type(quest)
        if not task_type:
            self.log(f"Task không hỗ trợ cho \"{name}\", bỏ qua", "warn"); return
        with self.lock:
            if qid in self.completed_ids: return
            self.log(f"━━━ Bắt đầu: {name} (Task: {task_type}) ━━━", "info")
            if task_type in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                self.complete_video(quest)
            elif task_type in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
                self.complete_heartbeat(quest)
            elif task_type == "PLAY_ACTIVITY":
                self.complete_activity(quest)
            self.completed_ids.add(qid)

    def _run_quest(self, quest: dict):
        time.sleep(random.uniform(1.0, 3.0))
        try:
            self.process_quest(quest)
        except Exception as e:
            self.log(f"Lỗi thread quest: {e}", "error")

    def run(self):
        quests = self.fetch_quests()
        if not quests:
            self.log("Không có quest nào.", "info")
            return
        total = len(quests)
        enrolled_count = sum(1 for q in quests if is_enrolled(q))
        completed_count = sum(1 for q in quests if is_completed(q))
        completable_count = sum(1 for q in quests if is_completable(q))
        self.log(f"Tổng: {total} | Enrolled: {enrolled_count} | Completed: {completed_count} | Completable: {completable_count}", "info")
        quests = self.auto_accept(quests)
        if self.stop_event.is_set(): return
        actionable = [q for q in quests if is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if actionable:
            self.log(f"{len(actionable)} quest cần hoàn thành...", "info")
            threads = []
            for q in actionable:
                if self.stop_event.is_set(): break
                qid = q.get("id")
                if not qid or qid in self.completed_ids: continue
                t = threading.Thread(target=self._run_quest, args=(q,), daemon=True)
                t.start()
                threads.append(t)
                self.quest_threads[qid] = t
            for t in threads:
                t.join()
        else:
            self.log("Không có quest nào cần hoàn thành.", "info")
        self.log("🎉 Hoàn tất tất cả quest!", "ok")

# ── Discord Bot ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def mask_token(token: str) -> str:
    if len(token) < 10:
        return "***"
    return token[:4] + "..." + token[-4:]

def new_session_id() -> str:
    global session_counter
    session_counter += 1
    return f"S{session_counter:03d}"

async def is_owner(interaction: discord.Interaction) -> bool:
    if OWNER_ID and interaction.user.id != OWNER_ID:
        embed = discord.Embed(title="🔒 Không có quyền truy cập",
            description="Chỉ chủ bot mới được dùng lệnh này.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if not OWNER_ID:
        print("⚠️ OWNER_ID chưa được cấu hình!")
    return True

async def is_manager(interaction: discord.Interaction) -> bool:
    if OWNER_ID and interaction.user.id == OWNER_ID:
        return True
    if MANAGER_ROLE_ID:
        role_ids = [r.id for r in getattr(interaction.user, "roles", [])]
        if MANAGER_ROLE_ID in role_ids:
            return True
    embed = discord.Embed(title="🔒 Không có quyền truy cập",
        description="Bạn cần có role quản lý hoặc là chủ bot để dùng chức năng này.",
        color=discord.Color.red())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    return False

# ── Log Channel ──────────────────────────────────────────────────────────────
log_channel_id: Optional[int] = None
import collections
_log_queue = collections.deque()
_log_queue_lock = threading.Lock()

ICON_MAP = {"ok": "✅", "warn": "⚠️", "error": "❌", "info": "ℹ️", "progress": "🔄"}
COLOR_MAP = {"ok": 0x57F287, "warn": 0xFEE75C, "error": 0xED4245, "info": 0x5865F2, "progress": 0x00B0F4}
LABEL_MAP = {"ok": "Hoàn thành", "warn": "Cảnh báo", "error": "Lỗi", "info": "Thông tin", "progress": "Đang xử lý"}

def enqueue_log(msg: str, level: str, session_id: str, username: str, user_mention: str = ""):
    if not log_channel_id: return
    with _log_queue_lock:
        _log_queue.append({"msg": msg, "level": level, "session_id": session_id,
                           "username": username, "user_mention": user_mention})

@tasks.loop(seconds=5)
async def flush_log_queue():
    if not log_channel_id: return
    with _log_queue_lock:
        if not _log_queue: return
        entries = list(_log_queue)
        _log_queue.clear()
    channel = client.get_channel(log_channel_id)
    if not channel: return
    groups: list[dict] = []
    for e in entries:
        if (groups and groups[-1]["session_id"] == e["session_id"] and
            groups[-1]["level"] == e["level"] and len(groups[-1]["lines"]) < 10):
            groups[-1]["lines"].append(e)
        else:
            groups.append({"session_id": e["session_id"], "username": e["username"],
                "user_mention": e.get("user_mention", ""), "level": e["level"], "lines": [e]})
    for g in groups:
        level = g["level"]
        icon = ICON_MAP.get(level, "📝")
        color = COLOR_MAP.get(level, 0x99AAB5)
        label = LABEL_MAP.get(level, level.upper())
        sid = g["session_id"]
        uname = g["username"]
        lines = g["lines"]
        embed = discord.Embed(color=color)
        embed.set_author(name=f"{icon} {label}", icon_url=None)
        if len(lines) == 1:
            embed.description = f"> {lines[0]['msg']}"
        else:
            parts = [f"╸ {e['msg']}" for e in lines]
            body = "\n".join(parts)
            if len(body) > 3900: body = body[:3900] + "\n…"
            embed.description = body
        embed.set_footer(text=f"[{sid}] {uname}")
        try:
            await channel.send(embed=embed)
        except Exception: pass

# ── Modal: Điền Token ────────────────────────────────────────────────────────
class TokenModal(discord.ui.Modal, title="🎮 Nhập Token Discord"):
    token_input = discord.ui.TextInput(
        label="Discord Account Token",
        placeholder="Dán token của bạn vào đây...",
        style=discord.TextStyle.short, required=True, min_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        token = self.token_input.value.strip()
        build_number = fetch_latest_build_number()
        api = DiscordAPI(token, build_number)
        user_info = api.validate_token()
        if not user_info:
            embed = discord.Embed(title="❌ Token Không Hợp Lệ",
                description="Token sai hoặc đã hết hạn. Vui lòng thử lại.",
                color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        username = user_info.get("username", "Unknown")
        user_id = user_info.get("id", "?")
        session_id = new_session_id()
        stop_event = threading.Event()
        log_buffer = []
        _sid = session_id; _uname = username
        _mention = interaction.user.mention; _discord_uid = interaction.user.id

        def log_cb(msg, level):
            log_buffer.append(f"[{level.upper()}] {msg}")
            enqueue_log(msg, level, _sid, _uname, _mention)

        async def notify_done():
            if not log_channel_id: return
            channel = client.get_channel(log_channel_id)
            if not channel: return
            embed = discord.Embed(title="🎉 Hoàn Thành Tất Cả Quest!",
                description=(f"{_mention} — Tất cả nhiệm vụ đã hoàn thành!\n\n"
                f"**Tài khoản:** `{_uname}`\n**Session:** `{_sid}`"),
                color=0x57F287)
            embed.set_footer(text="Quest Bot • Tự động hoàn thành")
            await channel.send(content=_mention, embed=embed)

        def thread_wrapper():
            completer.run()
            future = asyncio.run_coroutine_threadsafe(notify_done(), client.loop)
            try: future.result(timeout=10)
            except Exception: pass

        completer = QuestAutocompleter(api, log_callback=log_cb, stop_event=stop_event)
        t = threading.Thread(target=thread_wrapper, daemon=True, name=f"Session-{session_id}")
        t.start()
        with session_lock:
            active_sessions[session_id] = {
                "thread": t, "completer": completer, "stop_event": stop_event,
                "token_masked": mask_token(token), "username": username, "user_id": user_id,
                "log_buffer": log_buffer, "started_at": datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
                "discord_user": str(interaction.user)}

        embed = discord.Embed(title="🚀 Đã Khởi Động Quest Session", color=discord.Color.green())
        embed.add_field(name="🆔 Session ID", value=f"`{session_id}`", inline=True)
        embed.add_field(name="👤 Account", value=f"**{username}** (`{user_id}`)", inline=True)
        embed.add_field(name="🔑 Token", value=f"`{mask_token(token)}`", inline=True)
        embed.add_field(name="📋 Quản lý",
            value="Bấm nút **📊 Trạng Thái** để xem tiến trình\nBấm nút **⛔ Dừng Tất Cả** để dừng", inline=False)
        embed.set_footer(text=f"Bắt đầu lúc {active_sessions[session_id]['started_at']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

# ── View: Bảng Điều Khiển ────────────────────────────────────────────────────
class QuestControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="▶ Chạy Quest", style=discord.ButtonStyle.success, emoji="🎮", custom_id="btn_run")
    async def btn_run(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TokenModal())

    @discord.ui.button(label="Trạng Thái", style=discord.ButtonStyle.primary, emoji="📊", custom_id="btn_status")
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_manager(interaction): return
        with session_lock:
            sessions = dict(active_sessions)
            if not sessions:
                await interaction.response.send_message("Không có session nào đang chạy.", ephemeral=True)
                return
            embed = discord.Embed(title="📊 Quest Sessions", color=discord.Color.gold())
            for sid, info in sessions.items():
                is_alive = info["thread"].is_alive()
                status = "🟢 Đang chạy" if is_alive else "⚫ Đã xong"
                logs = info["log_buffer"]
                last_log = logs[-1] if logs else "_(chưa có log)_"
                embed.add_field(name=f"`{sid}` – {info['username']} {status}",
                    value=f"🔑 `{info['token_masked']}`\n🕐 {info['started_at']}\n📝 {last_log}", inline=False)
            embed.set_footer(text=f"Tổng: {len(sessions)} session(s)")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Dừng Tất Cả", style=discord.ButtonStyle.danger, emoji="⛔", custom_id="btn_stop_all")
    async def btn_stop_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_manager(interaction): return
        with session_lock:
            sessions = dict(active_sessions)
            count = sum(1 for info in sessions.values() if info["thread"].is_alive())
            if count == 0:
                await interaction.response.send_message("Không có session nào đang chạy.", ephemeral=True)
                return
            for info in sessions.values():
                info["stop_event"].set()
            await interaction.response.send_message(f"⛔ Đã gửi lệnh dừng cho **{count}** session.", ephemeral=True)

# ── Slash Command: /setup ────────────────────────────────────────────────────
@tree.command(name="setup", description="Tạo bảng điều khiển Quest Bot")
async def setup(interaction: discord.Interaction):
    if not await is_owner(interaction): return
    embed = discord.Embed(title="🎮 Discord Quest Bot",
        description=("Tự động hoàn thành Discord Quests.\n\n"
        "**Cách dùng:**\n"
        "1. Bấm **▶ Chạy Quest** → dán token vào\n"
        "2. Bot tự động nhận & hoàn thành tất cả quest\n\n"
        "**Hỗ trợ:** Video · Desktop Play · Stream · Activity"),
        color=discord.Color.blurple())
    embed.set_footer(text="⚠️ Chỉ dùng token của chính bạn. Không chia sẻ token với ai.")
    await interaction.response.send_message(embed=embed, view=QuestControlView())

# ── Slash Command: /setup-log ────────────────────────────────────────────────
@tree.command(name="setup-log", description="Đặt kênh hiện tại làm nơi nhận nhật ký quest")
async def setup_log(interaction: discord.Interaction):
    if not await is_owner(interaction): return
    global log_channel_id
    log_channel_id = interaction.channel_id
    embed = discord.Embed(title="📋 Đã Cấu Hình Kênh Nhật Ký",
        description=(f"Mọi tiến trình quest sẽ được gửi vào {interaction.channel.mention}.\n"
        "Để thay đổi, dùng lại lệnh `/setup-log` ở kênh khác."),
        color=discord.Color.green())
    embed.set_footer(text=f"Thiết lập bởi {interaction.user}")
    await interaction.response.send_message(embed=embed)

# ── Bot Events ───────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    client.add_view(QuestControlView())
    await tree.sync()
    if not flush_log_queue.is_running():
        flush_log_queue.start()
    print(f"✅ Bot đã sẵn sàng: {client.user} (ID: {client.user.id})")
    print(f" Dùng lệnh /setup trong server để tạo bảng điều khiển.")
    print(f" Mời bot: https://discord.com/api/oauth2/authorize?client_id={client.user.id}&permissions=2147483648&scope=bot%20applications.commands")

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Kiểm tra biến môi trường bắt buộc
    if not DISCORD_BOT_TOKEN:
        print("❌ Chưa cấu hình DISCORD_BOT_TOKEN!")
        print("👉 Trên Azure: Vào Cấu hình → Cài đặt ứng dụng → Thêm biến DISCORD_BOT_TOKEN")
        print("👉 Ở local: Tạo file .env với dòng: DISCORD_BOT_TOKEN=token_của_bạn")
        exit(1)

    if not OWNER_ID:
        print("⚠️ OWNER_ID chưa được cấu hình! Dùng giá trị mặc định.")
    if not MANAGER_ROLE_ID:
        print("⚠️ MANAGER_ROLE_ID chưa được cấu hình! Dùng giá trị mặc định.")

    client.run(DISCORD_BOT_TOKEN)
