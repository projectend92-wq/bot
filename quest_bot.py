#!/usr/bin/env python3
"""
COMBINED BOT: Auto-Enroll Quest Completer & Persistent Auto-Poster  (v3 - stable)

Fixes in this version
  * Cloudflare 1015 / IP-ban aware: on login ban the process SLEEPS (no crash loop),
    when running, all posting is PAUSED for 30 min instead of hammering the API
  * Removed on_disconnect (it killed posters on every normal gateway reconnect)
  * Real channel slowmode on auto-resume + minimum interval + global spacing between uploads
  * !resumeposting crash fixed (config never stored the token -> KeyError)
  * Modal inputs are per-instance (before, one user's saved token leaked into
    the next user's form) + only the panel owner can press its button
  * Poster retries with backoff instead of dying on one error
  * Storage: JSON no longer exceeds embed limit, per-user save lock, re-creates deleted message
  * Image backup in the storage channel (Render's disk is wiped on restart)
  * Quest sessions report failed quests honestly instead of "all completed"
  * /health shows real status, logs are flushed immediately
"""

import asyncio
import json
import os
import random
import sys
import time
import shutil
import re
import gc
from collections import defaultdict
from datetime import datetime
from flask import Flask
from threading import Thread

try:
    import aiohttp
    import discord
    from discord.ext import commands
except ImportError as e:
    print(f"[FATAL] Missing dependency: {e}", flush=True)
    sys.exit(1)

# ===== CONFIG =====
try:
    with open("config.json", "r", encoding="utf-8") as f:
        CFG = json.load(f)
except FileNotFoundError:
    CFG = {}

BOT_TOKEN = os.environ.get("BOT_TOKEN") or CFG.get("bot_token", "").strip()
if not BOT_TOKEN:
    print("[FATAL] Missing bot_token.", flush=True)
    sys.exit(1)

API = "https://discord.com/api/v9"
LOG_FILE = "quest_bot.log"
POSTER_IMG_DIR = "poster_images"
TOKEN_STORAGE_CHANNEL_ID = 1477313811613745163

MAX_CONCURRENT_SESSIONS = CFG.get("max_concurrent", 50)
ADMIN_BOARD_REFRESH = CFG.get("admin_board_refresh", 10)
BOT_ACTIVITY_NAME = CFG.get("bot_activity", "Quest Automated")
BOT_ACTIVITY_TYPE = CFG.get("bot_activity_type", "playing")
MIN_POST_INTERVAL = CFG.get("min_post_interval", 30)   # never post to one channel faster than this
POST_GAP = CFG.get("post_gap", 4)                       # min seconds between ANY two uploads (all users)
CF_PAUSE_SECONDS = CFG.get("cf_pause_seconds", 1800)    # pause posting this long after a Cloudflare block

ACCENT_GREY = discord.Colour(0x4E5058)

# ===== SHARED STATE =====
STARTED_AT = time.time()
STATE = {"ready": False, "cooldown": False}
POST_PAUSE = {"until": 0.0}
_BG_TASKS = set()

def spawn(coro):
    """create_task that keeps a reference (otherwise tasks can be garbage-collected)."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t

# ===== FLASK KEEP-ALIVE + HEALTH =====
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

@app.route('/health')
def health():
    if STATE["cooldown"]:
        return "cooldown: Discord/Cloudflare is blocking this IP, waiting", 200
    if time.time() - STARTED_AT < 180:
        return "starting", 200
    try:
        if STATE["ready"] and not bot.is_closed():
            return "ok", 200
    except NameError:
        pass
    return "bot down", 500

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), use_reloader=False)

Thread(target=run_web, daemon=True).start()

# ===== IN-MEMORY STORAGE =====
_tokens_cache = {}
_message_ids = {}
_storage_channel = None
_save_locks = defaultdict(asyncio.Lock)

ACTIVE_SESSIONS = {}
ACTIVE_POSTERS = {}
ACTIVE_ADMIN_BOARD = None

# ===== LOGGING & ERRORS =====
def log(msg, level="INFO"):
    line = f"[{datetime.now():%H:%M:%S}] [{level:7}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

class QuestError(Exception): pass

def is_cf_block(body: str) -> bool:
    b = (body or "").lstrip().lower()
    return b.startswith("<!doctype") or b.startswith("<html") or "cloudflare" in b[:2000]

def trigger_cf_pause(reason=""):
    POST_PAUSE["until"] = max(POST_PAUSE["until"], time.time() + CF_PAUSE_SECONDS)
    log(f"[CLOUDFLARE] Block detected {reason} - pausing all posting for {CF_PAUSE_SECONDS // 60} min", "ERROR")

# ===== API HEADERS =====
def uheaders(token: str) -> dict:
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9047 "
                     "Chrome/128.0.6613.119 Electron/32.0.0 Safari/537.36",
    }

async def uapi(session, method, url, token, data=None, retries=2):
    for attempt in range(1, retries + 1):
        try:
            async with session.request(method, url, headers=uheaders(token), json=data,
                                       timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.text(); code = r.status
                if 200 <= code < 300:
                    try: return json.loads(body) if body else {}
                    except json.JSONDecodeError: return None
                if code == 429:
                    if is_cf_block(body):
                        trigger_cf_pause(f"on {url}")
                        return None
                    try: retry_after = float(json.loads(body).get("retry_after", 5))
                    except Exception: retry_after = float(r.headers.get("Retry-After", 5) or 5)
                    log(f"[API] 429 on {url} - waiting {retry_after}s", "WARN")
                    await asyncio.sleep(min(retry_after, 120)); continue
                if code == 401: raise QuestError("Invalid token (401)")
                return None
        except QuestError:
            raise
        except Exception:
            await asyncio.sleep(3)
    return None

async def fetch_quests(session, token):
    result = await uapi(session, "GET", f"{API}/quests/@me", token)
    if result and isinstance(result, dict) and "quests" in result:
        return result.get("quests", [])
    return None

def build_plan(quests):
    plan = []; claim_only = []
    for q in quests:
        if not isinstance(q, dict): continue
        us = q.get("user_status") or {}
        if us.get("claimed_at"): continue

        cfg = q.get("config") or {}
        qid = q.get("id"); name = cfg.get("messages", {}).get("quest_name") or qid

        if us.get("completed_at"):
            claim_only.append({"quest_id": qid, "name": name}); continue

        tc = cfg.get("task_config") or cfg.get("task_config_v2") or {}
        tasks = tc.get("tasks") or {}

        for ttype in ["WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"]:
            if ttype not in tasks: continue
            task = tasks[ttype]; target = int(task.get("target", 0) or 0)
            if target <= 0: continue
            app_id = None
            if ttype in ["PLAY_ON_DESKTOP", "PLAY_ACTIVITY"]:
                apps = task.get("applications") or []
                if apps and isinstance(apps, list): app_id = apps[0].get("id")
            plan.append({
                "quest_id": qid, "app_id": app_id, "task_type": ttype,
                "target_seconds": target, "name": name, "enrolled": bool(us.get("enrolled_at"))
            })
    return plan, claim_only

# ===== DISCORD CHANNEL STORAGE SYSTEM =====
async def sync_tokens_from_channel():
    global _storage_channel
    _storage_channel = bot.get_channel(TOKEN_STORAGE_CHANNEL_ID)
    if not _storage_channel:
        try: _storage_channel = await bot.fetch_channel(TOKEN_STORAGE_CHANNEL_ID)
        except Exception as e:
            log(f"[STORAGE] Cannot access storage channel: {e}", "ERROR"); return
    count = 0
    async for msg in _storage_channel.history(limit=500):
        if msg.author.id != bot.user.id: continue
        try:
            raw_text = msg.content
            if msg.embeds and msg.embeds[0].description:
                raw_text = msg.embeds[0].description

            match = re.search(r'```json\n(.*?)\n```', raw_text, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                uid = str(data.get("user_id"))

                if uid == "None" or not uid:
                    paths = (data.get("poster_config") or {}).get("image_paths", [])
                    if paths:
                        try: uid = paths[0].split("/")[1]
                        except Exception: pass

                if uid and uid != "None":
                    _tokens_cache[uid] = {
                        "token": data.get("token", ""),
                        "username": data.get("username", "Unknown"),
                        "poster_config": data.get("poster_config", None)
                    }
                    _message_ids[uid] = msg.id
                    count += 1
        except Exception as e:
            log(f"[STORAGE] Failed to parse {msg.id}: {e}", "WARN")
    log(f"[STORAGE] Loaded {count} tokens from channel.", "INFO")

async def save_to_channel(user_id: str):
    if not _storage_channel: return
    uid = str(user_id)
    async with _save_locks[uid]:
        data = dict(_tokens_cache.get(uid, {}))
        data["user_id"] = uid

        # ensure_ascii=False keeps emojis as 1 char instead of a 6-char escape (embed limit is 4096)
        payload = json.dumps(data, indent=1, ensure_ascii=False)
        if len(payload) > 3900:
            log(f"[STORAGE] Config for {uid} too large ({len(payload)}), shorten the poster text.", "ERROR")
            return
        embed = discord.Embed(title="🔐 Secure Token & Config Storage", color=0x2b2d31)
        embed.description = f"```json\n{payload}\n```"
        embed.set_footer(text=f"User ID: {uid}")

        try:
            if uid in _message_ids:
                try:
                    msg = await _storage_channel.fetch_message(_message_ids[uid])
                    await msg.edit(embed=embed)
                    return
                except discord.NotFound:
                    pass   # message was deleted -> create a new one below
            msg = await _storage_channel.send(embed=embed)
            _message_ids[uid] = msg.id
        except Exception as e:
            log(f"[STORAGE] Save failed: {e}", "ERROR")

async def backup_images(paths, filenames):
    """Upload images to the storage channel so they survive Render restarts."""
    if not _storage_channel or not paths: return None
    try:
        files = [discord.File(p, filename=fn) for p, fn in zip(paths, filenames)]
        m = await _storage_channel.send(content="🖼️ Image backup", files=files[:10])
        return m.id
    except Exception as e:
        log(f"[STORAGE] Image backup failed: {e}", "WARN")
        return None

async def restore_missing_images(uid):
    """Re-download images from the backup message if Render wiped the disk."""
    conf = get_user_meta(uid).get("poster_config")
    if not conf or not conf.get("image_paths"): return
    if all(os.path.exists(p) for p in conf["image_paths"]): return
    mid = conf.get("image_msg_id")
    if not mid or not _storage_channel:
        log(f"[RESTORE] No backup available for user {uid}", "WARN"); return
    try:
        msg = await _storage_channel.fetch_message(mid)
        for path, att in zip(conf["image_paths"], msg.attachments):
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                await att.save(path)
        log(f"[RESTORE] Images restored for user {uid}", "INFO")
    except Exception as e:
        log(f"[RESTORE] Failed for {uid}: {e}", "ERROR")

# ===== TOKEN LOGIC =====
def get_user_token(user_id: int) -> str: return _tokens_cache.get(str(user_id), {}).get("token", "")
def get_user_meta(user_id: int) -> dict: return _tokens_cache.get(str(user_id), {})
def has_user_token(user_id: int) -> bool: return bool(get_user_token(user_id))

async def set_user_token(user_id: int, token: str, username: str = ""):
    uid = str(user_id)
    if uid not in _tokens_cache: _tokens_cache[uid] = {}
    _tokens_cache[uid]["token"] = token; _tokens_cache[uid]["username"] = username
    await save_to_channel(uid)

async def set_poster_config(user_id: int, config: dict):
    uid = str(user_id)
    if uid not in _tokens_cache: _tokens_cache[uid] = {}
    _tokens_cache[uid]["poster_config"] = config
    await save_to_channel(uid)

async def delete_poster_config(user_id: int):
    uid = str(user_id)
    if uid in _tokens_cache:
        _tokens_cache[uid]["poster_config"] = None
        await save_to_channel(uid)
    user_dir = os.path.join(POSTER_IMG_DIR, uid)
    if os.path.exists(user_dir): shutil.rmtree(user_dir, ignore_errors=True)

async def fetch_slowmodes(token, channels):
    """Real slowmode of each channel (0 if it can't be read)."""
    result = {}
    async with aiohttp.ClientSession() as session:
        for cid in channels:
            sm = 0
            try:
                async with session.get(f"{API}/channels/{cid}", headers=uheaders(token),
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        sm = (await r.json()).get("rate_limit_per_user", 0) or 0
                    elif r.status == 429 and is_cf_block(await r.text()):
                        trigger_cf_pause("while reading slowmode")
            except Exception as e:
                log(f"[POSTER] Could not read slowmode of {cid}: {e}", "WARN")
            result[cid] = sm
    return result

# ===== EMOJI MANAGEMENT =====
EMOJI_CACHE = {
    "animated": None, "moon2": None, "statusonline30": None, "loading": None,
    "verified": None, "questicon": None, "statusmajor": None, "warning": None,
    "redcircle": None, "greencircle": None, "lock": None,
    "arrow": None, "Verified": None
}

EMOJI_NAME_MAP = {
    "animated": ["animated"], "moon2": ["moon2", "moon_2"],
    "statusonline30": ["statusonline30", "statusonline", "status_online_30"],
    "loading": ["loading"], "verified": ["verified", "verified_new", "verify"],
    "questicon": ["questicon3d17", "questicon", "quest_icon"],
    "statusmajor": ["statusmajor", "status_major"],
    "warning": ["warning", "alert", "exclamation", "!"],
    "redcircle": ["redcircle", "red_circle", "red"],
    "greencircle": ["greencircle", "green_circle", "green"],
    "lock": ["lock", "locked", "slowmode"],
    "arrow": ["arrow", "arrow_right", "arrow_forward"],
    "Verified": ["verified", "Verified", "verify_badge"]
}

EMOJI_FALLBACK = {
    "animated": "📊", "moon2": "🌙", "statusonline30": "🟢", "loading": "⏳",
    "verified": "✅", "questicon": "🎮", "statusmajor": "⏹️", "warning": "⚠️",
    "redcircle": "🔴", "greencircle": "🟢", "lock": "🔒",
    "arrow": "➡️", "Verified": "✅"
}

def load_emojis():
    for emoji_obj in bot.emojis:
        for key, aliases in EMOJI_NAME_MAP.items():
            if EMOJI_CACHE[key] is None and emoji_obj.name.lower() in [a.lower() for a in aliases]:
                EMOJI_CACHE[key] = emoji_obj; break

def emoji(key: str, fallback: str = "") -> str:
    e = EMOJI_CACHE.get(key)
    return str(e) if e else (fallback or EMOJI_FALLBACK.get(key, ""))

def resolve_button_emoji(key: str):
    e = EMOJI_CACHE.get(key)
    return discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated) if e else EMOJI_FALLBACK.get(key, "❓")

def build_bot_activity():
    act_type = BOT_ACTIVITY_TYPE.lower()
    type_map = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching,
                "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}
    return discord.Activity(type=type_map.get(act_type, discord.ActivityType.playing), name=BOT_ACTIVITY_NAME)

# ===== QUEST SESSION =====
class QuestSession:
    def __init__(self, owner, user_token):
        self.owner = owner; self.token = user_token; self.running = True; self.started = time.time()
        self.quests = {}; self.done_count = 0; self.failed_count = 0; self.total = 0
        self.status_message = None; self.intro_message = None; self.intro_lines = []; self.current_quest = "Initializing"

    @property
    def remaining(self):
        return max(0, self.total - self.done_count - self.failed_count)

    def progress_bar(self, pct, width=8):
        if pct >= 100: return "█" * width
        if pct <= 0: return "░" * width
        return "█" * int(pct / 100 * width) + "░" * (width - int(pct / 100 * width))

    def get_sorted_quests(self):
        completed, ongoing = [], []
        for name, q in self.quests.items(): (completed if q["done"] else ongoing).append((name, q))
        ongoing.sort(key=lambda x: x[1]["pct"], reverse=True); return completed + ongoing

    def get_quest_text(self):
        lines = []
        for name, q in self.get_sorted_quests():
            pct = q["pct"]
            if q["done"]: lines.append(f"{emoji('verified')} {self.progress_bar(100)} `100%` — {name}")
            elif q.get("failed"): lines.append(f"{emoji('warning')} {self.progress_bar(pct)} `{pct:3}%` — {name} *(failed)*")
            else: lines.append(f"{emoji('loading')} {self.progress_bar(pct)} `{pct:3}%` — {name}")
        return "\n".join(lines) if lines else "*No quests available*"

    def elapsed_str(self):
        e = int(time.time() - self.started); m, s = divmod(e, 60); return f"{m}m {s}s"

    async def status_layout(self):
        elapsed = time.time() - self.started; mins, secs = int(elapsed // 60), int(elapsed % 60)
        color = discord.Colour(0x57F287) if self.done_count == self.total and self.total > 0 else discord.Colour(0xFFA500) if self.done_count > 0 else discord.Colour(0x4E5058)
        state = "Active" if self.running else "Finished"
        fail_txt = f" · `{self.failed_count}` failed" if self.failed_count else ""
        text = (f"## Quest Session Status\n\n**Progress:** `{self.done_count}/{self.total}` quests completed{fail_txt}\n"
                f"{emoji('statusonline30')} **{state}** · `{mins}m {secs}s` elapsed\n\n### Quest Log\n{self.get_quest_text()}")
        if len(text) > 3900: text = text[:3890] + "\n..."
        class V(discord.ui.LayoutView): pass
        view = V(); c = discord.ui.Container(accent_colour=color); c.add_item(discord.ui.TextDisplay(text)); view.add_item(c); return view

    async def safe_send_embed(self, title, description, color=0x3498db):
        embed = discord.Embed(title=title, description=description, color=color)
        try: await self.owner.send(embed=embed); return True
        except Exception: return False

    async def send_intro_line(self, key, text):
        prefix = emoji(key, ""); self.intro_lines.append(f"{prefix}  {text}" if prefix else text)
        class V(discord.ui.LayoutView): pass
        view = V(); c = discord.ui.Container(accent_colour=discord.Colour(0x4E5058)); c.add_item(discord.ui.TextDisplay("\n".join(self.intro_lines))); view.add_item(c)
        try:
            if self.intro_message is None: self.intro_message = await self.owner.send(view=view)
            else:
                try: await self.intro_message.edit(view=view)
                except discord.NotFound: self.intro_message = await self.owner.send(view=view)
        except Exception: pass

    async def update_embed(self):
        try:
            view = await self.status_layout()
            if self.status_message is None: self.status_message = await self.owner.send(view=view)
            else:
                try: await self.status_message.edit(view=view)
                except discord.NotFound: self.status_message = await self.owner.send(view=view)
        except Exception: pass

    async def enroll_quest(self, session, qid):
        url = f"{API}/quests/{qid}/enroll"
        try:
            res = await uapi(session, "POST", url, self.token, data={"location": 11})
            if res is not None: return True
        except Exception: pass
        return False

    async def run(self):
        async with aiohttp.ClientSession() as session:
            try:
                me = await uapi(session, "GET", f"{API}/users/@me", self.token)
                if not me:
                    await self.safe_send_embed("Session Failed", "Invalid token (or Discord is rate limiting). Use `!settoken` again later.", 0xe74c3c)
                    self.running = False; return
                await self.send_intro_line("statusonline30", f"Authenticated as **{me.get('username', 'Unknown')}**")
            except QuestError as e:
                await self.safe_send_embed("Token Error", str(e), 0xe74c3c); self.running = False; return

            await self.send_intro_line("loading", "Retrieving quest list...")
            quests = await fetch_quests(session, self.token)
            if not quests:
                await self.safe_send_embed("Failed", "Unable to fetch quests. Token might be expired.", 0xe74c3c)
                self.running = False; return

            plan, claim_only = build_plan(quests)
            if not plan and not claim_only:
                await self.safe_send_embed("No Actionable Quests", f"Found `{len(quests)}` total, but none are automatable or claimable.", 0xf39c12)
                self.running = False; return

            self.total = len(plan) + len(claim_only)
            for p in plan: self.quests[p["name"]] = {"pct": 0, "done": False}
            for c in claim_only: self.quests[c["name"]] = {"pct": 100, "done": True}

            await self.send_intro_line("animated", f"Detected **{len(quests)}** total · **{self.total}** actionable.")
            await self.update_embed(); await asyncio.sleep(1)

            for c in claim_only:
                if not self.running: break
                self.current_quest = c["name"]; await self.claim(session, c); self.done_count += 1; await self.update_embed()

            for i, p in enumerate(plan):
                if not self.running: break
                self.current_quest = p["name"]

                if not p.get("enrolled", False):
                    log(f"[QUEST] Auto-enrolling {p['name']}...", "INFO")
                    await self.enroll_quest(session, p["quest_id"])
                    await asyncio.sleep(2)

                try: await self.run_one(session, p)
                except QuestError as e:
                    log(f"[QUEST] {p['name']}: {e}", "WARN")
                    await self.mark_failed(p)
                if i < len(plan) - 1: await asyncio.sleep(random.uniform(2, 5))

            was_stopped = not self.running
            self.current_quest = "Complete"; self.running = False; await self.update_embed()
            if self.done_count >= self.total:
                await self.safe_send_embed("Success", "All quests completed successfully.", 0x2ecc71)
            elif was_stopped:
                await self.safe_send_embed("Stopped", f"Session stopped. `{self.done_count}/{self.total}` completed.", 0xf39c12)
            else:
                await self.safe_send_embed("Finished with issues", f"`{self.done_count}/{self.total}` completed, `{self.failed_count}` failed.", 0xf39c12)

    async def run_one(self, session, p):
        if p["task_type"] == "WATCH_VIDEO": await self.run_video_quest(session, p)
        else: await self.run_play_quest(session, p)

    async def run_video_quest(self, session, p):
        qid = p["quest_id"]; target = p["target_seconds"]; url = f"{API}/quests/{qid}/video-progress"
        done = 0
        while self.running and done < target:
            inc = min(target // 8 + random.randint(0, max(0, target // 10)), target - done)
            done = min(done + max(1, inc), target + 5)
            data = await uapi(session, "POST", url, self.token, data={"timestamp": done})
            if data:
                self.quests[p["name"]]["pct"] = min(99, int(done / target * 100)); await self.update_embed()
            if done >= target: break
            await asyncio.sleep(random.uniform(12, 18))
        if self.running: await self.mark_done(session, p)

    async def run_play_quest(self, session, p):
        qid = p["quest_id"]; aid = p["app_id"]; target = p["target_seconds"]; url = f"{API}/quests/{qid}/heartbeat"
        last_val = None; stuck = 0; start = time.time(); deadline = start + target + 120
        while self.running and time.time() < deadline:
            elapsed = time.time() - start
            data = await uapi(session, "POST", url, self.token, data={"application_id": aid, "terminal": False})
            if data:
                prog = data.get("progress") or {}
                if prog.get("completed_at"): await self.mark_done(session, p); return
                val = 0
                for k in ["PLAY_ON_DESKTOP", "PLAY_ACTIVITY", "value"]:
                    if k in prog: val = prog[k].get("value") if isinstance(prog[k], dict) else prog[k]; break
                val = val or 0; self.quests[p["name"]]["pct"] = min(99, int(val / max(1, target) * 100)); await self.update_embed()
                if val == last_val and val > 0:
                    stuck += 1
                    if stuck >= 5:
                        if val >= target: await self.mark_done(session, p); return
                        break
                else: stuck = 0; last_val = val
            if elapsed >= target: break
            await asyncio.sleep(28 + random.uniform(0, 6))
        for g in range(6):
            if not self.running: return
            try:
                data = await uapi(session, "POST", url, self.token, data={"application_id": aid, "terminal": True})
                if data and (data.get("progress") or {}).get("completed_at"): await self.mark_done(session, p); return
            except QuestError: raise
            except Exception: pass
            await asyncio.sleep(20)
        # Last resort: try to claim. Only count it as done if the claim really worked.
        if await self.claim(session, p): await self.mark_done(session, p, do_claim=False)
        else: await self.mark_failed(p)

    async def mark_done(self, session, p, do_claim=True):
        self.quests[p["name"]]["pct"] = 100; self.quests[p["name"]]["done"] = True
        self.done_count += 1
        if do_claim: await self.claim(session, p)
        await self.update_embed()

    async def mark_failed(self, p):
        q = self.quests.get(p["name"])
        if q and not q["done"] and not q.get("failed"):
            q["failed"] = True; self.failed_count += 1
            await self.update_embed()

    async def claim(self, session, p):
        for ep in ["complete", "claim"]:
            try:
                result = await uapi(session, "POST", f"{API}/quests/{p['quest_id']}/{ep}", self.token)
                if result and (result.get("claimed_at") or result.get("completed_at")): return True
            except QuestError: raise
            except Exception: pass
        return False

# =====================================================================
# AUTO-POSTER ENGINE (self-healing)
# =====================================================================
FATAL_CODES = (400, 401, 403, 404)   # these will never fix themselves -> stop and tell the user

_post_gate = asyncio.Lock()
_last_post_ts = 0.0

async def post_gate():
    """Global spacing so many users/channels never burst the API from one IP."""
    global _last_post_ts
    async with _post_gate:
        wait = POST_GAP - (time.time() - _last_post_ts)
        if wait > 0: await asyncio.sleep(wait)
        _last_post_ts = time.time()

class PosterTask:
    def __init__(self, owner, channel_data, token, text, image_paths, image_filenames):
        self.owner = owner; self.channel_data = channel_data; self.token = token
        self.text = text; self.image_paths = image_paths; self.image_filenames = image_filenames
        self.running = True
        self.states = {cid: {"status": "Starting...", "posts": 0, "wait": 0} for cid in channel_data.keys()}
        self.status_msg = None
        self._last_embed = 0

    async def start(self):
        await restore_missing_images(self.owner.id)
        missing = [fn for p, fn in zip(self.image_paths or [], self.image_filenames or []) if not os.path.exists(p)]
        if missing:
            try:
                await self.owner.send(embed=discord.Embed(
                    description=f"⚠️ **Image Missing:** `{', '.join(missing)}`\nPaki-`!updateposting` at i-attach ulit ang images.",
                    color=0xe74c3c))
            except Exception: pass
        await self.update_embed(force=True)
        tasks = []
        for i, (cid, slow) in enumerate(self.channel_data.items()):
            if i > 0: await asyncio.sleep(2)
            tasks.append(asyncio.create_task(self.channel_loop(cid, slow)))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def update_embed(self, force=False):
        if not force and time.time() - self._last_embed < 3: return
        self._last_embed = time.time()
        embed = discord.Embed(title=f"{emoji('animated')} Auto-Poster Status", color=0x2b2d31)
        for cid, state in self.states.items():
            status_text = f"{state['status']} (Post #{state['posts']})"
            if state['wait'] > 0: status_text += f"\n{emoji('arrow')} Ready <t:{state['wait']}:R>"
            embed.add_field(name=f"Channel: <#{cid}>", value=status_text, inline=False)
        embed.set_footer(text=f"Live Updates • {datetime.now().strftime('%H:%M:%S')}")
        try:
            if self.status_msg is None: self.status_msg = await self.owner.send(embed=embed)
            else: await self.status_msg.edit(embed=embed)
        except Exception: pass

    async def _post_once(self, session, cid):
        """One upload attempt. Returns (status_code, retry_after)."""
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({"content": self.text or ""}), content_type="application/json")

        opened_files = []
        for idx, (path, fname) in enumerate(zip(self.image_paths or [], self.image_filenames or [])):
            if os.path.exists(path):
                try:
                    f = open(path, "rb"); opened_files.append(f)
                    form.add_field(f"files[{idx}]", f, filename=fname)
                except Exception as e:
                    log(f"[ERROR] Cannot open {path}: {e}", "ERROR")

        headers = uheaders(self.token); headers.pop("Content-Type", None)
        status_code = 0; retry_after = 0
        try:
            async with session.post(f"{API}/channels/{cid}/messages", headers=headers, data=form,
                                    timeout=aiohttp.ClientTimeout(total=120)) as r:
                status_code = r.status
                body = await r.text()
                if r.status not in (200, 201):
                    log(f"[POSTER] {cid} -> HTTP {r.status}: {body[:150]!r}", "ERROR")
                if r.status == 429:
                    if is_cf_block(body):
                        trigger_cf_pause(f"posting to {cid}")
                        retry_after = CF_PAUSE_SECONDS
                    else:
                        try: retry_after = float(json.loads(body).get("retry_after", 5))
                        except Exception: retry_after = float(r.headers.get("Retry-After", 60) or 60)
        except Exception as e:
            log(f"[POSTER] Upload failed ({cid}): {e}", "ERROR")
            status_code = 0
        finally:
            for f in opened_files: f.close()
        gc.collect()
        return status_code, retry_after

    async def channel_loop(self, cid, slowmode):
        interval = max(slowmode + 2, MIN_POST_INTERVAL)
        fail_streak = 0
        async with aiohttp.ClientSession() as session:
            while self.running:
                st = self.states[cid]

                # Global pause (Cloudflare block detected anywhere)
                if time.time() < POST_PAUSE["until"]:
                    st["status"] = f"{emoji('warning')} Paused (Discord is limiting this server's IP)"
                    st["wait"] = int(POST_PAUSE["until"])
                    await self.update_embed()
                    await self._sleep(30)
                    continue

                st["status"] = f"{emoji('loading')} Posting..."; st["wait"] = 0
                spawn(self.update_embed())

                await post_gate()
                status_code, retry_after = await self._post_once(session, cid)

                if status_code in (200, 201):
                    fail_streak = 0
                    st["posts"] += 1
                    wait_time = interval + random.uniform(0, 3)
                    st["status"] = f"{emoji('lock')} Cooldown"
                    st["wait"] = int(time.time() + wait_time)
                    await self.update_embed(force=True)
                    await self._sleep(wait_time)

                elif status_code == 429:
                    wait_time = min(max(retry_after, 5), 3600)
                    st["status"] = f"{emoji('warning')} Rate Limited"
                    st["wait"] = int(time.time() + wait_time)
                    await self.update_embed(force=True)
                    await self._sleep(wait_time)

                elif status_code in FATAL_CODES:
                    st["status"] = f"{emoji('redcircle')} Stopped (HTTP {status_code})"
                    await self.update_embed(force=True)
                    try:
                        await self.owner.send(embed=discord.Embed(
                            description=f"🔴 Auto-poster stopped for <#{cid}> (HTTP {status_code}). "
                                        f"Check the token, channel access and message content, then `!resumeposting`.",
                            color=0xe74c3c))
                    except Exception: pass
                    break

                else:
                    fail_streak += 1
                    wait_time = min(30 * (2 ** (fail_streak - 1)), 600)
                    st["status"] = f"{emoji('redcircle')} Retrying (HTTP {status_code or 'net'})"
                    st["wait"] = int(time.time() + wait_time)
                    await self.update_embed(force=True)
                    await self._sleep(wait_time)

    async def _sleep(self, seconds):
        """Sleep in small steps so stop commands take effect quickly."""
        end = time.time() + seconds
        while self.running and time.time() < end:
            await asyncio.sleep(min(5, max(0.1, end - time.time())))

class PosterSetupModal(discord.ui.Modal):
    def __init__(self, title, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__(title=title)
        self.existing_data = existing_data; self.image_urls = image_urls or []; self.image_filenames = image_filenames or []
        ex = existing_data or {}
        chans = ex.get("channels") or []
        # Inputs are created PER INSTANCE (class-level inputs were shared between users,
        # so one user's saved token could show up as the default in another user's form)
        self.token_input = discord.ui.TextInput(label='User Token', placeholder='Paste User Token here...',
                                                style=discord.TextStyle.short, required=True, default=ex.get("token") or None)
        self.channel_input_1 = discord.ui.TextInput(label='Channel ID 1', placeholder='First Channel ID',
                                                    style=discord.TextStyle.short, required=True,
                                                    default=chans[0] if chans else None)
        self.channel_input_2 = discord.ui.TextInput(label='Channel ID 2 (Optional)', placeholder='Second Channel ID',
                                                    style=discord.TextStyle.short, required=False,
                                                    default=chans[1] if len(chans) > 1 else None)
        self.text_input = discord.ui.TextInput(label='Message Text', placeholder='What to post?',
                                               style=discord.TextStyle.paragraph, required=False,
                                               max_length=1200, default=ex.get("text") or None)
        for item in (self.token_input, self.channel_input_1, self.channel_input_2, self.text_input):
            self.add_item(item)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log(f"[MODAL] {type(error).__name__}: {error}", "ERROR")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong. Check the bot logs.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong. Check the bot logs.", ephemeral=True)
        except Exception: pass

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        token = self.token_input.value.strip(); cid1 = self.channel_input_1.value.strip()
        cid2 = self.channel_input_2.value.strip() if self.channel_input_2.value else None; text = self.text_input.value or ""

        def err(msg):
            return interaction.followup.send(embed=discord.Embed(title="Error", description=msg, color=0xe74c3c), ephemeral=True)

        channels = []
        if cid1.isdigit(): channels.append(cid1)
        if cid2 and cid2.isdigit(): channels.append(cid2)
        if not channels: return await err("Invalid Channel ID.")

        has_saved_images = bool(self.existing_data and self.existing_data.get("image_paths"))
        if not text and not self.image_urls and not has_saved_images:
            return await err("Need text or an image.")

        user_dir = os.path.join(POSTER_IMG_DIR, str(uid))
        image_paths = []; final_filenames = []; image_msg_id = None

        if self.image_urls:
            if os.path.exists(user_dir): shutil.rmtree(user_dir, ignore_errors=True)
            os.makedirs(user_dir, exist_ok=True)
            async with aiohttp.ClientSession() as session:
                for idx, (url, fname) in enumerate(zip(self.image_urls, self.image_filenames)):
                    save_path = os.path.join(user_dir, f"{idx}_{fname}")
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                            if r.status == 200:
                                with open(save_path, "wb") as f: f.write(await r.read())
                                image_paths.append(save_path); final_filenames.append(fname)
                    except Exception as e:
                        log(f"[SETUP] Image download failed: {e}", "WARN")
            if not image_paths and not text:
                return await err("Could not download the image(s) and there is no text.")
            image_msg_id = await backup_images(image_paths, final_filenames)
        elif has_saved_images:
            image_paths = self.existing_data["image_paths"]; final_filenames = self.existing_data.get("image_filenames", [])
            image_msg_id = self.existing_data.get("image_msg_id")

        channel_data = {}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{API}/users/@me", headers=uheaders(token), timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200: return await err(f"Invalid Token (HTTP {r.status}).")
                    user_data = await r.json()
                for cid in channels:
                    async with session.get(f"{API}/channels/{cid}", headers=uheaders(token), timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200: channel_data[cid] = (await r.json()).get("rate_limit_per_user", 0)
                        else: return await err(f"Cannot access channel {cid} (HTTP {r.status}).")
            except Exception as e:
                return await err(f"Network error: {e}")

        await set_user_token(uid, token, user_data.get("username", "Unknown"))
        conf = {"channels": channels, "text": text, "image_paths": image_paths, "image_filenames": final_filenames,
                "image_msg_id": image_msg_id, "running": True, "created_at": time.time()}
        await set_poster_config(uid, conf)

        if uid in ACTIVE_POSTERS: ACTIVE_POSTERS[uid].running = False
        task = PosterTask(interaction.user, channel_data, token, text, image_paths, final_filenames)
        ACTIVE_POSTERS[uid] = task

        await interaction.followup.send(embed=discord.Embed(title="Success", description="Setup complete! Check DMs.", color=0x2ecc71), ephemeral=True)
        spawn(task.start())

class PosterSetupRow(discord.ui.ActionRow):
    def __init__(self, owner_id, is_update=False, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__()
        self.owner_id = owner_id; self.is_update = is_update; self.existing_data = existing_data
        self.image_urls = image_urls or []; self.image_filenames = image_filenames or []

    @discord.ui.button(label="Setup Auto-Poster", style=discord.ButtonStyle.secondary, custom_id="poster:setup", emoji=resolve_button_emoji("questicon"))
    async def open_modal(self, itx: discord.Interaction, button: discord.ui.Button):
        # Only the person who ran the command may open the form (it can contain their saved token)
        if itx.user.id != self.owner_id:
            return await itx.response.send_message("This panel belongs to someone else. Run `!posting` yourself.", ephemeral=True)
        title = "Update Auto-Poster" if self.is_update else "Auto-Poster Setup"
        await itx.response.send_modal(PosterSetupModal(title, self.existing_data, self.image_urls, self.image_filenames))

class PosterPanel(discord.ui.LayoutView):
    def __init__(self, owner_id, is_update=False, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=ACCENT_GREY)
        text = f"## {'Update' if is_update else 'Setup'} Auto-Poster\n\nClick below to configure.\n\n"
        if image_urls: text += f"{emoji('verified')} **New Images Attached:** `{len(image_urls)}` file(s)\n"
        elif existing_data and existing_data.get("image_paths"): text += f"{emoji('verified')} **Using Saved Images:** `{len(existing_data['image_paths'])}` file(s)\n"
        else: text += f"{emoji('warning')} **No image attached.** Text only.\n"
        c.add_item(discord.ui.TextDisplay(text)); c.add_item(discord.ui.Separator())
        c.add_item(PosterSetupRow(owner_id, is_update, existing_data, image_urls, image_filenames)); self.add_item(c)

# ===== DM CLEANUP =====
async def purge_dm_messages(channel, limit=200, concurrency=3):
    try: messages = [m async for m in channel.history(limit=limit) if m.author.id == bot.user.id]
    except Exception: return 0
    sem = asyncio.Semaphore(concurrency); deleted = 0
    async def _del(msg):
        nonlocal deleted
        async with sem:
            try: await msg.delete(); deleted += 1
            except discord.HTTPException: pass
    await asyncio.gather(*(_del(m) for m in messages)); return deleted

# ===== ADMIN BOARD =====
class AdminBoard:
    def __init__(self, channel):
        self.channel = channel; self.message = None; self.running = True; self.started = time.time()
    async def start(self): await self.update(); spawn(self._loop())
    async def _loop(self):
        while self.running:
            await asyncio.sleep(ADMIN_BOARD_REFRESH)
            try: await self.update()
            except Exception as e: log(f"Admin board update error: {e}", "ERROR")
    async def stop(self): self.running = False
    def _build_view(self):
        class V(discord.ui.LayoutView): pass
        view = V(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour(0x4E5058))
        container.add_item(discord.ui.TextDisplay(self._build_text()))
        view.add_item(container); return view
    def _build_text(self):
        now = datetime.now().strftime("%H:%M:%S"); elapsed = int(time.time() - self.started); m, s = divmod(elapsed, 60)
        paused = time.time() < POST_PAUSE["until"]
        lines = [f"## {emoji('animated')} Admin Control Board", f"**Active Sessions:** `{len(ACTIVE_SESSIONS)}/{MAX_CONCURRENT_SESSIONS}` · **Uptime:** `{m}m {s}s` · **Updated:** `{now}`", "", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
        if not ACTIVE_SESSIONS: lines.extend([f"{emoji('moon2')} *No active sessions at this time.*"])
        else:
            lines.append(f"### {emoji('moon2')} Active Sessions"); lines.append("")
            for uid, sess in list(ACTIVE_SESSIONS.items())[:15]:
                user_pct = int(sess.done_count / sess.total * 100) if sess.total else 0
                uname = get_user_meta(uid).get("username", "Unknown")
                state_text = f"{emoji('verified')} Complete" if sess.remaining == 0 else f"{emoji('loading')} {sess.remaining} remaining"
                lines.append(f"{emoji('statusonline30')} **{uname}** · `{sess.elapsed_str()}`")
                lines.append(f"     `{sess.progress_bar(user_pct, 16)}` **{user_pct}%** · `{sess.done_count}/{sess.total}`")
                lines.append(f"     {state_text}"); lines.append("")
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "", f"### {emoji('statusonline30')} Statistics", "",
                      f"**Registered users:** `{len(_tokens_cache)}`", f"**Active posters:** `{len(ACTIVE_POSTERS)}`",
                      f"**Posting:** {'⏸️ paused (IP limited)' if paused else '▶️ normal'}"])
        return "\n".join(lines)
    async def update(self):
        view = self._build_view()
        if self.message is None: self.message = await self.channel.send(view=view)
        else:
            try: await self.message.edit(view=view)
            except discord.NotFound: self.message = await self.channel.send(view=view)
            except discord.HTTPException: pass

# ===== QUEST UI =====
class PanelRow(discord.ui.ActionRow):
    def __init__(self):
        super().__init__()
        self.children[0].emoji = resolve_button_emoji("questicon"); self.children[1].emoji = resolve_button_emoji("statusonline30"); self.children[2].emoji = resolve_button_emoji("statusmajor")

    @discord.ui.button(label="Start Quest", style=discord.ButtonStyle.secondary, custom_id="quest:start")
    async def start(self, itx, button):
        uid = itx.user.id
        if uid in ACTIVE_SESSIONS and ACTIVE_SESSIONS[uid].running: return await itx.response.send_message(embed=discord.Embed(description="You already have an active session running.", color=0xf39c12), ephemeral=True)
        user_token = get_user_token(uid)
        if not user_token: return await itx.response.send_message(embed=discord.Embed(description="**No token registered.**\nUse `!settoken` via DM.", color=0xe74c3c), ephemeral=True)
        if len(ACTIVE_SESSIONS) >= MAX_CONCURRENT_SESSIONS: return await itx.response.send_message(embed=discord.Embed(description="**Server at capacity.**", color=0xe74c3c), ephemeral=True)
        if time.time() < POST_PAUSE["until"]:
            return await itx.response.send_message(embed=discord.Embed(description="Discord is temporarily limiting this server's IP. Try again in a while.", color=0xf39c12), ephemeral=True)

        session = QuestSession(itx.user, user_token); ACTIVE_SESSIONS[uid] = session
        log(f"Session started: {itx.user} (ID: {uid}) — active: {len(ACTIVE_SESSIONS)}", "INFO")
        await itx.response.send_message(embed=discord.Embed(description="Session initialized. Check your DMs for progress.", color=0x2ecc71), ephemeral=True)
        spawn(self._run_session_with_cleanup(uid, session))

    @staticmethod
    async def _run_session_with_cleanup(uid, session):
        try: await session.run()
        except Exception as e: log(f"Session error for {uid}: {e}", "ERROR")
        finally:
            ACTIVE_SESSIONS.pop(uid, None)
            log(f"Session ended: user {uid} — active: {len(ACTIVE_SESSIONS)}", "INFO")

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, custom_id="quest:status")
    async def status(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s: return await itx.response.send_message(embed=discord.Embed(description="No active session found.", color=0xe74c3c), ephemeral=True)
        await itx.response.send_message(view=await s.status_layout(), ephemeral=True)

    @discord.ui.button(label="Stop Quest", style=discord.ButtonStyle.secondary, custom_id="quest:stop")
    async def stop(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s or not s.running: return await itx.response.send_message(embed=discord.Embed(description="No active session to stop.", color=0xe74c3c), ephemeral=True)
        s.running = False
        await itx.response.send_message(embed=discord.Embed(description="Session termination requested. Finishing current task...", color=0xf39c12), ephemeral=True)

class QuestPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=ACCENT_GREY)
        c.add_item(discord.ui.TextDisplay("## Quest Auto-Completer\n\n**Available Actions:**\n• **Start Quest** — Begin automated quest completion\n• **Status** — View your current session progress\n• **Stop Quest** — Terminate your active session\n\n**First Time Users:**\nUse `!settoken` via DM to register."))
        c.add_item(discord.ui.Separator()); c.add_item(PanelRow()); self.add_item(c)

# ===== BOT =====
intents = discord.Intents.default()
intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, chunk_guilds_at_startup=False)
_ready_done = False

async def watchdog():
    """Probe the Discord REST API. If it keeps failing, PAUSE posting (never restart:
    restarting from a blocked IP only makes the Cloudflare ban last longer)."""
    fails = 0
    while True:
        await asyncio.sleep(180)
        try:
            await asyncio.wait_for(bot.application_info(), timeout=30)
            if fails >= 3: log("[WATCHDOG] Discord REST API reachable again.", "INFO")
            fails = 0
        except Exception as e:
            fails += 1
            log(f"[WATCHDOG] REST probe failed ({fails}): {type(e).__name__}: {str(e)[:120]}", "WARN")
            if fails == 3:
                trigger_cf_pause("(REST probe failing)")

async def resume_posters():
    for uid_str, data in list(_tokens_cache.items()):
        conf = data.get("poster_config")
        if not (conf and conf.get("running")): continue
        if not data.get("token"): continue
        try:
            user = await bot.fetch_user(int(uid_str))
            channel_data = await fetch_slowmodes(data["token"], conf["channels"])
            task = PosterTask(user, channel_data, data["token"], conf["text"],
                              conf.get("image_paths", []), conf.get("image_filenames", []))
            ACTIVE_POSTERS[int(uid_str)] = task
            spawn(task.start())
            log(f"[AUTO-RESUME] Restarted poster for {user.name}", "INFO")
            await asyncio.sleep(5)   # stagger users
        except Exception as e:
            log(f"[AUTO-RESUME] Failed for {uid_str}: {e}", "ERROR")

@bot.event
async def on_ready():
    global _ready_done
    log(f"Bot ready: {bot.user}", "INFO")
    STATE["ready"] = True; STATE["cooldown"] = False
    os.environ.pop("LOGIN_BACKOFF", None)
    if _ready_done: return
    _ready_done = True
    load_emojis()
    bot.add_view(QuestPanel())

    await sync_tokens_from_channel()
    await bot.change_presence(activity=build_bot_activity())

    spawn(watchdog())
    spawn(resume_posters())

# NOTE: on_disconnect was REMOVED on purpose. It fires on every normal gateway
# reconnect and used to stop all posters/sessions permanently.

@bot.event
async def on_resumed():
    STATE["ready"] = True
    log("Gateway session resumed.", "INFO")

# ================= PREFIX COMMANDS =================
@bot.command(name="ping")
async def ping_cmd(ctx):
    paused = "yes" if time.time() < POST_PAUSE["until"] else "no"
    await ctx.send(f"pong · latency `{round(bot.latency * 1000)}ms` · posters `{len(ACTIVE_POSTERS)}` · paused `{paused}`")

def _attachments(ctx):
    urls, names = [], []
    for att in ctx.message.attachments:
        urls.append(att.url); names.append(att.filename)
    return urls, names

@bot.command(name="posting")
async def posting(ctx):
    image_urls, image_filenames = _attachments(ctx)
    await ctx.send(view=PosterPanel(ctx.author.id, is_update=False, image_urls=image_urls, image_filenames=image_filenames))

@bot.command(name="updateposting")
async def updateposting(ctx):
    conf = get_user_meta(ctx.author.id).get("poster_config")
    if not conf: return await ctx.reply(embed=discord.Embed(description="❌ Walang naka-save na config. Gamitin muna ang `!posting`.", color=0xe74c3c))
    image_urls, image_filenames = _attachments(ctx)
    existing = dict(conf); existing["token"] = get_user_token(ctx.author.id)   # token lives outside poster_config
    await ctx.send(view=PosterPanel(ctx.author.id, is_update=True, existing_data=existing, image_urls=image_urls, image_filenames=image_filenames))

@bot.command(name="checkposting")
async def checkposting(ctx):
    conf = get_user_meta(ctx.author.id).get("poster_config")
    if not conf: return await ctx.reply(embed=discord.Embed(description="❌ Wala kang naka-save na Auto-Poster.", color=0xe74c3c))
    running = ctx.author.id in ACTIVE_POSTERS and ACTIVE_POSTERS[ctx.author.id].running
    embed = discord.Embed(title="📋 Saved Auto-Poster Config", color=0x3498db)
    embed.add_field(name="Channels", value=", ".join([f"<#{c}>" for c in conf["channels"]]), inline=False)
    embed.add_field(name="Text", value=f"```{(conf.get('text') or 'None')[:100].replace('`', chr(39))}```", inline=False)
    embed.add_field(name="Images Saved", value=f"`{len(conf.get('image_paths', []))}`", inline=True)
    embed.add_field(name="Status", value="`Running`" if running else "`Stopped`", inline=True)
    await ctx.reply(embed=embed)

@bot.command(name="resumeposting")
async def resumeposting(ctx):
    uid = ctx.author.id
    conf = get_user_meta(uid).get("poster_config")
    if not conf: return await ctx.reply(embed=discord.Embed(description="❌ Wala kang naka-save na Auto-Poster. (Kung nasa channel naman, i-restart ang bot para mabasa).", color=0xe74c3c))
    if uid in ACTIVE_POSTERS and ACTIVE_POSTERS[uid].running: return await ctx.reply(embed=discord.Embed(description="✅ Naka-run na ang Auto-Poster mo.", color=0x2ecc71))

    token = get_user_token(uid)     # FIX: the token is NOT inside poster_config (conf["token"] raised KeyError)
    if not token: return await ctx.reply(embed=discord.Embed(description="❌ Walang naka-save na token. Gamitin ang `!settoken` sa DM.", color=0xe74c3c))

    conf["running"] = True; await set_poster_config(uid, conf)
    channel_data = await fetch_slowmodes(token, conf["channels"])

    task = PosterTask(ctx.author, channel_data, token, conf.get("text", ""), conf.get("image_paths", []), conf.get("image_filenames", []))
    ACTIVE_POSTERS[uid] = task
    await ctx.reply(embed=discord.Embed(description="✅ **Auto-Poster Resumed!**", color=0x2ecc71))
    spawn(task.start())

@bot.command(name="deleteposting")
async def deleteposting(ctx):
    uid = ctx.author.id
    if not get_user_meta(uid).get("poster_config"): return await ctx.reply(embed=discord.Embed(description="❌ Walang mabubura na config.", color=0xe74c3c))
    if uid in ACTIVE_POSTERS: ACTIVE_POSTERS[uid].running = False; del ACTIVE_POSTERS[uid]
    await delete_poster_config(uid)
    await ctx.reply(embed=discord.Embed(description="✅ **Auto-Poster Deleted.**", color=0x2ecc71))

@bot.command(name="stopposting")
async def stopposting(ctx):
    uid = ctx.author.id
    conf = get_user_meta(uid).get("poster_config")
    if not conf: return await ctx.reply(embed=discord.Embed(description="❌ Wala kang active na auto-poster.", color=0xe74c3c))
    if uid in ACTIVE_POSTERS: ACTIVE_POSTERS[uid].running = False; del ACTIVE_POSTERS[uid]
    conf["running"] = False; await set_poster_config(uid, conf)
    await ctx.reply(embed=discord.Embed(description="✅ **Auto-Poster stopped.**", color=0x2ecc71))

@bot.command(name="questpanel")
@commands.has_permissions(manage_guild=True)
async def questpanel(ctx):
    try: await ctx.send(view=QuestPanel())
    except discord.HTTPException as e: await ctx.send(embed=discord.Embed(description=f"Failed: `{e}`", color=0xe74c3c))

@bot.command(name="adminboard")
@commands.has_permissions(manage_guild=True)
async def adminboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if ACTIVE_ADMIN_BOARD: await ACTIVE_ADMIN_BOARD.stop()
    board = AdminBoard(ctx.channel); ACTIVE_ADMIN_BOARD = board
    await ctx.send(embed=discord.Embed(description="Starting dashboard...", color=0x3498db))
    await board.start()

@bot.command(name="stopboard")
@commands.has_permissions(manage_guild=True)
async def stopboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if not ACTIVE_ADMIN_BOARD: return await ctx.reply(embed=discord.Embed(description="No active admin board to stop.", color=0xf39c12))
    await ACTIVE_ADMIN_BOARD.stop(); ACTIVE_ADMIN_BOARD = None
    await ctx.reply(embed=discord.Embed(description="Admin board stopped.", color=0x2ecc71))

@bot.command(name="settoken")
async def settoken_cmd(ctx, token: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        try: await ctx.message.delete()
        except Exception: pass
        return await ctx.author.send(embed=discord.Embed(description="**Privacy Notice:** Register via Direct Message.", color=0xf39c12))
    if not token: return await ctx.reply(embed=discord.Embed(description="**Usage:** `!settoken <token>`", color=0xf39c12))

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API}/users/@me", headers=uheaders(token), timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return await ctx.reply(embed=discord.Embed(description=f"**Invalid token.** (HTTP {r.status})", color=0xe74c3c))
                user_data = await r.json()
        except Exception as e: return await ctx.reply(embed=discord.Embed(description=f"**Network error:** `{e}`", color=0xe74c3c))

    username = user_data.get("username", "Unknown")
    await set_user_token(ctx.author.id, token, username=username)
    try: await ctx.message.delete()
    except Exception: pass
    await ctx.author.send(embed=discord.Embed(description=f"**Token Registered!**\nAccount: `{username}`\nYou can now use the panels.", color=0x2ecc71))

@bot.command(name="removetoken")
async def removetoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return await ctx.reply(embed=discord.Embed(description="Use in DM.", color=0xf39c12))
    uid = str(ctx.author.id)
    if ctx.author.id in ACTIVE_POSTERS:                    # stop the poster that uses this token
        ACTIVE_POSTERS[ctx.author.id].running = False; del ACTIVE_POSTERS[ctx.author.id]
    if ctx.author.id in ACTIVE_SESSIONS:
        ACTIVE_SESSIONS[ctx.author.id].running = False
    if uid in _tokens_cache:
        _tokens_cache.pop(uid)
        if uid in _message_ids and _storage_channel:
            try:
                msg = await _storage_channel.fetch_message(_message_ids[uid])
                await msg.delete()
            except Exception: pass
            _message_ids.pop(uid, None)
    user_dir = os.path.join(POSTER_IMG_DIR, uid)
    if os.path.exists(user_dir): shutil.rmtree(user_dir, ignore_errors=True)
    await ctx.reply(embed=discord.Embed(description="**Token removed.**", color=0x2ecc71))

@bot.command(name="mytoken")
async def mytoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return
    if has_user_token(ctx.author.id):
        meta = get_user_meta(ctx.author.id)
        await ctx.reply(embed=discord.Embed(description=f"**Registered:** Yes\n**Account:** `{meta.get('username', 'Unknown')}`", color=0x2ecc71))
    else: await ctx.reply(embed=discord.Embed(description="**Registered:** No\n\nUse `!settoken` to register.", color=0xe74c3c))

@bot.command(name="sessions")
@commands.has_permissions(manage_guild=True)
async def sessions_cmd(ctx):
    if not ACTIVE_SESSIONS: return await ctx.reply(embed=discord.Embed(description="No active sessions.", color=0xf39c12))
    lines = [f"**Active:** `{len(ACTIVE_SESSIONS)}`\n"]
    for uid, s in ACTIVE_SESSIONS.items():
        lines.append(f"🟢 **{get_user_meta(uid).get('username', 'Unknown')}** · `{s.done_count}/{s.total}`")
    await ctx.reply(embed=discord.Embed(description="\n".join(lines), color=0x3498db))

@bot.command(name="stopall")
@commands.has_permissions(manage_guild=True)
async def stopall_cmd(ctx):
    n = len(ACTIVE_SESSIONS)
    for uid, s in list(ACTIVE_SESSIONS.items()): s.running = False
    await ctx.reply(embed=discord.Embed(description=f"Terminated `{n}` sessions.", color=0x2ecc71))

@bot.command(name="stopuser")
@commands.has_permissions(manage_guild=True)
async def stopuser_cmd(ctx, user: discord.User = None):
    if not user: return await ctx.reply(embed=discord.Embed(description="Usage: `!stopuser @user`", color=0xf39c12))
    s = ACTIVE_SESSIONS.get(user.id)
    if not s: return await ctx.reply(embed=discord.Embed(description=f"No active session for {user.mention}.", color=0xe74c3c))
    s.running = False
    await ctx.reply(embed=discord.Embed(description=f"Terminating session for {user.mention}...", color=0x2ecc71))

@bot.command(name="dmclear")
async def dmclear_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return await ctx.reply(embed=discord.Embed(description="Use in DM.", color=0xf39c12))
    notice = await ctx.send(embed=discord.Embed(description="Clearing messages...", color=0x3498db))
    deleted = await purge_dm_messages(ctx.channel)
    try: await notice.delete()
    except Exception: pass
    confirm = await ctx.send(embed=discord.Embed(description=f"Cleared `{deleted}` message(s).", color=0x2ecc71))
    await asyncio.sleep(3)
    try: await confirm.delete()
    except Exception: pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    log(f"[CMD ERROR] {ctx.command}: {type(error).__name__}: {error}", "ERROR")
    if isinstance(error, commands.MissingPermissions):
        try: return await ctx.reply(embed=discord.Embed(description="Need Manage Server permission.", color=0xe74c3c))
        except Exception: return
    try: await ctx.reply(embed=discord.Embed(description=f"**Error:** `{error}`", color=0xe74c3c))
    except Exception: pass

# ===== MAIN =====
if __name__ == "__main__":
    try:
        bot.run(BOT_TOKEN)
    except KeyboardInterrupt:
        print("\nBot stopped.", flush=True)
    except discord.HTTPException as e:
        # Login refused. Status 429 / Cloudflare 1015 = this Render IP is temporarily banned.
        # Do NOT crash (Render would restart us instantly and every retry extends the ban).
        # Sleep, then re-exec the process with a longer wait each time.
        if e.status == 429 or "1015" in str(e) or "cloudflare" in str(e).lower():
            delay = int(os.environ.get("LOGIN_BACKOFF", "900"))
            STATE["cooldown"] = True
            log(f"[LOGIN] Cloudflare 1015 - this IP is temporarily banned by Discord. "
                f"Sleeping {delay // 60} min before trying again (no restart loop).", "ERROR")
            time.sleep(delay)
            os.environ["LOGIN_BACKOFF"] = str(min(delay * 2, 7200))
            os.execv(sys.executable, [sys.executable] + sys.argv)
        raise
