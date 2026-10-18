#!/usr/bin/env python3
"""
COMBINED BOT: Cloud-Optimized Quest Auto-Completer & Auto-Poster (Pre-filled Updates)
"""

import asyncio
import json
import os
import random
import sys
import threading
import time
import base64
import shutil
import re
import gc
from datetime import datetime
from flask import Flask
from threading import Thread

try:
    import aiohttp
    import discord
    from discord.ext import commands
except ImportError as e:
    print(f"[FATAL] Missing dependency: {e}")
    sys.exit(1)

# ===== FLASK KEEP-ALIVE (Para hindi makatulog sa Render) =====
app = Flask('')

@app.route('/')
def home(): return "Bot is alive!"

@app.route('/health')
def health(): return "OK", 200

def run_web(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

Thread(target=run_web, daemon=True).start()
# ============================================================

# ===== CONFIG =====
try:
    with open("config.json", "r", encoding="utf-8") as f:
        CFG = json.load(f)
except FileNotFoundError:
    CFG = {}

BOT_TOKEN = os.environ.get("BOT_TOKEN") or CFG.get("bot_token", "").strip()
if not BOT_TOKEN:
    print("[FATAL] Missing bot_token. Set env BOT_TOKEN or use config.json.")
    sys.exit(1)

API = "https://discord.com/api/v9"
LOG_FILE = "quest_bot.log"
POSTER_IMG_DIR = "poster_images"
TOKEN_STORAGE_CHANNEL_ID = 1477313811613745163

MAX_CONCURRENT_SESSIONS = CFG.get("max_concurrent", 50)
ADMIN_BOARD_REFRESH = CFG.get("admin_board_refresh", 10)
BOT_ACTIVITY_NAME = CFG.get("bot_activity", "Quest Automated")
BOT_ACTIVITY_TYPE = CFG.get("bot_activity_type", "playing")

# ===== IN-MEMORY STORAGE =====
_tokens_cache = {}      
_message_ids = {}       
_storage_channel = None 

ACTIVE_SESSIONS = {}
SESSION_SEMAPHORE = None
ACTIVE_POSTERS = {}
ACTIVE_ADMIN_BOARD = None

# ===== LOGGING & ERRORS =====
def log(msg, level="INFO"):
    line = f"[{datetime.now():%H:%M:%S}] [{level:7}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    except OSError: pass

class QuestError(Exception): pass

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
            async with session.request(method, url, headers=uheaders(token), json=data, timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.text(); code = r.status
                if 200 <= code < 300:
                    try: return json.loads(body) if body else {}
                    except json.JSONDecodeError: return None
                if code == 429:
                    retry_after = float(json.loads(body).get("retry_after", 5))
                    log(f"Rate limited. Waiting {retry_after}s...", "WARN")
                    await asyncio.sleep(retry_after); continue
                if code == 401: raise QuestError("Invalid token (401)")
                return None
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
        if not us.get("enrolled_at") or us.get("claimed_at"): continue
        cfg = q.get("config") or {}
        qid = q.get("id"); name = cfg.get("messages", {}).get("quest_name") or qid
        if us.get("completed_at"):
            claim_only.append({"quest_id": qid, "name": name}); continue
        tc = cfg.get("task_config") or cfg.get("task_config_v2") or {}
        tasks = tc.get("tasks") or {}
        for ttype in ["WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"]:
            if ttype not in tasks: continue
            task = tasks[ttype]; target = int(task.get("target") or 0)
            if target <= 0: continue
            app_id = None
            if ttype in ["PLAY_ON_DESKTOP", "PLAY_ACTIVITY"]:
                apps = task.get("applications") or []
                if apps and isinstance(apps, list): app_id = apps[0].get("id")
            plan.append({"quest_id": qid, "app_id": app_id, "task_type": ttype, "target_seconds": target, "name": name})
    return plan, claim_only

# ===== DISCORD CHANNEL STORAGE SYSTEM =====
async def sync_tokens_from_channel():
    global _storage_channel
    _storage_channel = bot.get_channel(TOKEN_STORAGE_CHANNEL_ID)
    if not _storage_channel:
        log(f"[STORAGE] Channel {TOKEN_STORAGE_CHANNEL_ID} not found!", "ERROR")
        return
    log("[STORAGE] Syncing tokens from Discord channel...", "INFO")
    count = 0
    async for msg in _storage_channel.history(limit=500):
        if msg.author.id != bot.user.id: continue
        try:
            match = re.search(r'```json\n(.*?)\n```', msg.content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                uid = str(data.get("user_id"))
                _tokens_cache[uid] = {
                    "token": data.get("token", ""),
                    "username": data.get("username", "Unknown"),
                    "poster_config": data.get("poster_config", None)
                }
                _message_ids[uid] = msg.id
                count += 1
        except Exception as e:
            log(f"[STORAGE] Failed to parse message {msg.id}: {e}", "WARN")
    log(f"[STORAGE] Loaded {count} user tokens from channel.", "INFO")

async def save_to_channel(user_id: str):
    if not _storage_channel: return
    data = _tokens_cache.get(str(user_id), {})
    content = f"```json\n{json.dumps(data, indent=2)}\n```"
    try:
        if str(user_id) in _message_ids:
            msg = await _storage_channel.fetch_message(_message_ids[str(user_id)])
            await msg.edit(content=content)
        else:
            msg = await _storage_channel.send(content=content)
            _message_ids[str(user_id)] = msg.id
    except Exception as e:
        log(f"[STORAGE] Failed to save {user_id}: {e}", "ERROR")

# ===== TOKEN LOGIC =====
def get_user_token(user_id: int) -> str:
    return _tokens_cache.get(str(user_id), {}).get("token", "")

def get_user_meta(user_id: int) -> dict:
    return _tokens_cache.get(str(user_id), {})

def has_user_token(user_id: int) -> bool:
    return bool(get_user_token(user_id))

async def set_user_token(user_id: int, token: str, username: str = ""):
    uid = str(user_id)
    if uid not in _tokens_cache: _tokens_cache[uid] = {}
    _tokens_cache[uid]["token"] = token
    _tokens_cache[uid]["username"] = username
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
    if os.path.exists(user_dir): shutil.rmtree(user_dir)

# ===== EMOJI MANAGEMENT =====
EMOJI_CACHE = {"animated": None, "moon2": None, "statusonline30": None, "loading": None, "verified": None, "questicon": None, "statusmajor": None, "warning": None, "redcircle": None, "greencircle": None, "lock": None}
EMOJI_NAME_MAP = {
    "animated": ["animated"], "moon2": ["moon2", "moon_2"], "statusonline30": ["statusonline30", "statusonline", "status_online_30"],
    "loading": ["loading"], "verified": ["verified", "verified_new", "verify"], "questicon": ["questicon3d17", "questicon", "quest_icon"],
    "statusmajor": ["statusmajor", "status_major"], "warning": ["warning", "alert", "exclamation", "!"],
    "redcircle": ["redcircle", "red_circle", "red"], "greencircle": ["greencircle", "green_circle", "green"], "lock": ["lock", "locked", "slowmode"]
}
EMOJI_FALLBACK = {"animated": "📊", "moon2": "🌙", "statusonline30": "🟢", "loading": "⏳", "verified": "✅", "questicon": "🎮", "statusmajor": "⏹️", "warning": "⚠️", "redcircle": "🔴", "greencircle": "🟢", "lock": "🔒"}

def load_emojis():
    for emoji_obj in bot.emojis:
        emoji_lower = emoji_obj.name.lower()
        for key, aliases in EMOJI_NAME_MAP.items():
            if EMOJI_CACHE[key] is None and emoji_lower in [a.lower() for a in aliases]:
                EMOJI_CACHE[key] = emoji_obj; break

def emoji(key: str, fallback: str = "") -> str:
    e = EMOJI_CACHE.get(key)
    return str(e) if e else (fallback or EMOJI_FALLBACK.get(key, ""))

def resolve_button_emoji(key: str):
    e = EMOJI_CACHE.get(key)
    return discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated) if e else EMOJI_FALLBACK.get(key, "❓")

def build_bot_activity():
    act_type = BOT_ACTIVITY_TYPE.lower()
    type_map = {"playing": discord.ActivityType.playing, "watching": discord.ActivityType.watching, "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}
    return discord.Activity(type=type_map.get(act_type, discord.ActivityType.playing), name=BOT_ACTIVITY_NAME)

# ===== QUEST SESSION =====
class QuestSession:
    def __init__(self, owner, user_token):
        self.owner = owner; self.token = user_token; self.running = True; self.started = time.time()
        self.quests = {}; self.done_count = 0; self.total = 0
        self.status_message = None; self.intro_message = None; self.intro_lines = []; self.current_quest = "Initializing"

    def progress_bar(self, pct, width=8):
        if pct >= 100: return "█" * width
        if pct <= 0: return "░" * width
        return "█" * int(pct / 100 * width) + "░" * (width - int(pct / 100 * width))

    def get_sorted_quests(self):
        completed, ongoing = [], []
        for name, q in self.quests.items(): (completed if q["done"] else ongoing).append((name, q))
        ongoing.sort(key=lambda x: x[1]["pct"], reverse=True); return completed + ongoing

    def get_quest_text(self):
        lines = []; loading_em = emoji("loading", "⏳"); done_em = emoji("verified", "✅")
        for name, q in self.get_sorted_quests():
            pct = q["pct"]
            if q["done"]: lines.append(f"{done_em} {self.progress_bar(100)} `100%` — {name}")
            else: lines.append(f"{loading_em} {self.progress_bar(pct)} `{pct:3}%` — {name}")
        return "\n".join(lines) if lines else "*No quests available*"

    def elapsed_str(self):
        e = int(time.time() - self.started); m, s = divmod(e, 60); return f"{m}m {s}s"

    async def status_layout(self):
        elapsed = time.time() - self.started; mins, secs = int(elapsed // 60), int(elapsed % 60)
        color = discord.Colour(0x57F287) if self.done_count == self.total and self.total > 0 else discord.Colour(0xFFA500) if self.done_count > 0 else discord.Colour(0x4E5058)
        status_em = emoji("statusonline30", "🟢"); state = "Active" if self.running else "Complete"
        text = (f"## Quest Session Status\n\n**Progress:** `{self.done_count}/{self.total}` quests completed\n"
                f"{status_em} **{state}** · `{mins}m {secs}s` elapsed\n\n### Quest Log\n{self.get_quest_text()}")
        if len(text) > 3900: text = text[:3890] + "\n..."
        class V(discord.ui.LayoutView): pass
        view = V(); c = discord.ui.Container(accent_colour=color); c.add_item(discord.ui.TextDisplay(text)); view.add_item(c); return view

    async def safe_send(self, content):
        try: await self.owner.send(content); return True
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

    async def run(self):
        async with aiohttp.ClientSession() as session:
            try:
                me = await uapi(session, "GET", f"{API}/users/@me", self.token)
                if not me:
                    await self.safe_send("**Session Failed**\nInvalid token. Use `!settoken` again."); self.running = False; return
                await self.send_intro_line("statusonline30", f"Authenticated as **{me.get('username', 'Unknown')}**")
            except QuestError as e:
                await self.safe_send(f"**Token Error:** {e}"); self.running = False; return

            await self.send_intro_line("loading", "Retrieving quest list...")
            quests = await fetch_quests(session, self.token)
            if not quests:
                await self.safe_send("**Failed:** Unable to fetch quests."); self.running = False; return

            plan, claim_only = build_plan(quests)
            if not plan and not claim_only:
                await self.safe_send(f"**No actionable quests** — Found `{len(quests)}` total, but you haven't accepted any."); self.running = False; return

            self.total = len(plan) + len(claim_only)
            for p in plan: self.quests[p["name"]] = {"pct": 0, "done": False}
            for c in claim_only: self.quests[c["name"]] = {"pct": 100, "done": True}

            await self.send_intro_line("animated", f"Detected **{len(quests)}** total · **{self.total}** actionable.")
            await self.update_embed(); await asyncio.sleep(1)

            for c in claim_only:
                self.current_quest = c["name"]; await self.claim(session, c); self.done_count += 1; await self.update_embed()

            for i, p in enumerate(plan):
                if not self.running: break
                self.current_quest = p["name"]
                try: await self.run_one(session, p)
                except QuestError: pass
                if i < len(plan) - 1: await asyncio.sleep(random.uniform(2, 5))

            self.current_quest = "Complete"; self.running = False; await self.update_embed()
            await self.safe_send("**All quests completed successfully.**")

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
        hb = 0; last_val = None; stuck = 0; start = time.time(); deadline = start + target + 120
        while self.running and time.time() < deadline:
            hb += 1; elapsed = time.time() - start
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
            except Exception: pass
            await asyncio.sleep(20)
        await self.mark_done(session, p)

    async def mark_done(self, session, p):
        self.quests[p["name"]]["pct"] = 100; self.quests[p["name"]]["done"] = True
        self.done_count += 1; await self.claim(session, p); await self.update_embed()

    async def claim(self, session, p):
        for ep in ["complete", "claim"]:
            try:
                result = await uapi(session, "POST", f"{API}/quests/{p['quest_id']}/{ep}", self.token)
                if result and (result.get("claimed_at") or result.get("completed_at")): return True
            except Exception: pass
        return False

# =====================================================================
# AUTO-POSTER ENGINE (PRE-FILLED UPDATES & IMAGE PRESERVATION)
# =====================================================================
class PosterTask:
    def __init__(self, owner, channel_data, token, text, image_paths, image_filenames):
        self.owner = owner
        self.channel_data = channel_data
        self.token = token
        self.text = text
        self.image_paths = image_paths
        self.image_filenames = image_filenames
        self.running = True
        self.states = {cid: {"status": "Starting...", "posts": 0, "wait": 0} for cid in channel_data.keys()}
        self.status_msg = None

    async def start(self):
        await self.update_embed()
        tasks = []
        for i, (cid, slow) in enumerate(self.channel_data.items()):
            if i > 0: await asyncio.sleep(2)
            tasks.append(asyncio.create_task(self.channel_loop(cid, slow)))
        await asyncio.gather(*tasks)

    async def update_embed(self):
        embed = discord.Embed(title=f"{emoji('animated')} Auto-Poster Status", color=0x2b2d31)
        for cid, state in self.states.items():
            status_text = f"{state['status']} (Post #{state['posts']})"
            if state['wait'] > 0: status_text += f"\n{emoji('lock')} Waiting: `{state['wait']}s`"
            embed.add_field(name=f"Channel: <#{cid}>", value=status_text, inline=False)
        embed.set_footer(text=f"Live Updates • {datetime.now().strftime('%H:%M:%S')}")
        
        try:
            if self.status_msg is None: self.status_msg = await self.owner.send(embed=embed)
            else: await self.status_msg.edit(embed=embed)
        except Exception: pass

    async def channel_loop(self, cid, slowmode):
        async with aiohttp.ClientSession() as session:
            while self.running:
                self.states[cid]["status"] = f"{emoji('loading')} Posting..."
                self.states[cid]["wait"] = 0
                await self.update_embed()
                
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps({"content": self.text or ""}), content_type="application/json")
                
                opened_files = []
                if self.image_paths:
                    for idx, (path, fname) in enumerate(zip(self.image_paths, self.image_filenames)):
                        try:
                            f = open(path, "rb")
                            opened_files.append(f)
                            form.add_field(f"files[{idx}]", f, filename=fname)
                        except Exception as e:
                            log(f"[ERROR] Cannot open saved image {path}: {e}", "ERROR")

                headers = uheaders(self.token)
                headers.pop("Content-Type", None)

                success = False; status_code = 500; retry_after = 0
                try:
                    async with session.post(f"{API}/channels/{cid}/messages", headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=45)) as r:
                        success = (r.status in (200, 201)); status_code = r.status
                        if not success and r.status == 429:
                            try: retry_after = float(json.loads(await r.text()).get("retry_after", 5))
                            except Exception: retry_after = 5
                except Exception as e: 
                    log(f"[ERROR] Upload failed: {e}", "ERROR")
                finally:
                    for f in opened_files: f.close()

                gc.collect()
                await asyncio.sleep(1) 

                if success:
                    self.states[cid]["posts"] += 1
                    self.states[cid]["status"] = f"{emoji('verified')} Posted!"
                    wait_time = slowmode + 2
                    for sec in range(wait_time, 0, -1):
                        if not self.running: break
                        self.states[cid]["status"] = f"{emoji('lock')} Cooldown"
                        self.states[cid]["wait"] = sec
                        await self.update_embed()
                        await asyncio.sleep(1)
                elif status_code == 429:
                    self.states[cid]["status"] = f"{emoji('warning')} Rate Limited"
                    for sec in range(int(retry_after), 0, -1):
                        if not self.running: break
                        self.states[cid]["wait"] = sec
                        await self.update_embed()
                        await asyncio.sleep(1)
                else:
                    self.states[cid]["status"] = f"{emoji('redcircle')} Failed (HTTP {status_code})"
                    await self.update_embed()
                    break

class PosterSetupModal(discord.ui.Modal):
    token_input = discord.ui.TextInput(label='User Token', placeholder='Paste User Token here...', style=discord.TextStyle.short, required=True)
    channel_input_1 = discord.ui.TextInput(label='Channel ID 1', placeholder='First Channel ID', style=discord.TextStyle.short, required=True)
    channel_input_2 = discord.ui.TextInput(label='Channel ID 2 (Optional)', placeholder='Second Channel ID', style=discord.TextStyle.short, required=False)
    text_input = discord.ui.TextInput(label='Message Text', placeholder='What to post?', style=discord.TextStyle.paragraph, required=False)

    def __init__(self, title, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__(title=title)
        self.existing_data = existing_data # I-save ang existing data para mabasa sa on_submit
        self.image_urls = image_urls or []
        self.image_filenames = image_filenames or []
        
        # PRE-FILL THE FIELDS
        if existing_data:
            self.token_input.default = existing_data.get("token", "")
            self.channel_input_1.default = existing_data.get("channels", [""])[0]
            if len(existing_data.get("channels", [])) > 1:
                self.channel_input_2.default = existing_data["channels"][1]
            self.text_input.default = existing_data.get("text", "")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        token = self.token_input.value.strip()
        cid1 = self.channel_input_1.value.strip()
        cid2 = self.channel_input_2.value.strip() if self.channel_input_2.value else None
        text = self.text_input.value or ""

        channels = []
        if cid1.isdigit(): channels.append(cid1)
        if cid2 and cid2.isdigit(): channels.append(cid2)

        if not channels: return await interaction.followup.send("❌ Invalid Channel ID(s).", ephemeral=True)
        if not text and not self.image_urls and not (self.existing_data and self.existing_data.get("image_paths")): 
            return await interaction.followup.send("❌ Need text or an image.", ephemeral=True)

        user_dir = os.path.join(POSTER_IMG_DIR, str(uid))
        os.makedirs(user_dir, exist_ok=True)
        
        image_paths = []
        final_filenames = []

        if self.image_urls:
            # May BAGONG images na in-attach
            async with aiohttp.ClientSession() as session:
                for idx, (url, fname) in enumerate(zip(self.image_urls, self.image_filenames)):
                    save_path = os.path.join(user_dir, f"{idx}_{fname}")
                    async with session.get(url) as r:
                        if r.status == 200:
                            with open(save_path, "wb") as f:
                                f.write(await r.read())
                            image_paths.append(save_path)
                            final_filenames.append(fname)
        elif self.existing_data and self.existing_data.get("image_paths"):
            # WALANG bagong images, pero may LUMANG images na naka-save. I-PRESERVE natin!
            image_paths = self.existing_data["image_paths"]
            final_filenames = self.existing_data.get("image_filenames", [])

        channel_data = {}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API}/users/@me", headers=uheaders(token)) as r:
                if r.status != 200: return await interaction.followup.send("❌ Invalid User Token.", ephemeral=True)
                user_data = await r.json()
            
            for cid in channels:
                async with session.get(f"{API}/channels/{cid}", headers=uheaders(token)) as r:
                    if r.status == 200:
                        channel_data[cid] = (await r.json()).get("rate_limit_per_user", 0)
                    else:
                        return await interaction.followup.send(f"❌ Cannot access channel {cid}.", ephemeral=True)

        await set_user_token(uid, token, user_data.get("username", "Unknown"))
        
        conf = {
            "channels": channels, "text": text,
            "image_paths": image_paths, "image_filenames": final_filenames,
            "running": True, "created_at": time.time()
        }
        await set_poster_config(uid, conf)
        
        if uid in ACTIVE_POSTERS: ACTIVE_POSTERS[uid].running = False
        task = PosterTask(interaction.user, channel_data, token, text, image_paths, final_filenames)
        ACTIVE_POSTERS[uid] = task
        
        await interaction.followup.send(f"✅ Setup complete! Config saved to channel.", ephemeral=True)
        asyncio.create_task(task.start())

class PosterSetupRow(discord.ui.ActionRow):
    def __init__(self, is_update=False, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__()
        self.is_update = is_update
        self.existing_data = existing_data
        self.image_urls = image_urls or []
        self.image_filenames = image_filenames or []

    @discord.ui.button(label="Setup Auto-Poster", style=discord.ButtonStyle.secondary, custom_id="poster:setup", emoji=resolve_button_emoji("questicon"))
    async def open_modal(self, itx: discord.Interaction, button: discord.ui.Button):
        title = "Update Auto-Poster" if self.is_update else "Auto-Poster Setup"
        await itx.response.send_modal(PosterSetupModal(title, self.existing_data, self.image_urls, self.image_filenames))

class PosterPanel(discord.ui.LayoutView):
    def __init__(self, is_update=False, existing_data=None, image_urls=None, image_filenames=None):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=ACCENT_GREY)
        
        text = f"## {'Update' if is_update else 'Setup'} Auto-Poster\n\nClick below to configure.\n\n"
        if image_urls: text += f"{emoji('verified')} **New Images Attached:** `{len(image_urls)}` file(s)\n"
        elif existing_data and existing_data.get("image_paths"): text += f"{emoji('verified')} **Using Saved Images:** `{len(existing_data['image_paths'])}` file(s)\n"
        else: text += f"{emoji('warning')} **No image attached.** Text only.\n"

        c.add_item(discord.ui.TextDisplay(text))
        c.add_item(discord.ui.Separator())
        c.add_item(PosterSetupRow(is_update, existing_data, image_urls, image_filenames))
        self.add_item(c)

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
    async def start(self): await self.update(); asyncio.create_task(self._loop())
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
        board_em = emoji("animated", "📊"); moon_em = emoji("moon2", "🌙"); online_em = emoji("statusonline30", "🟢"); loading_em = emoji("loading", "⏳"); verified_em = emoji("verified", "✅")
        lines = [f"## {board_em} Admin Control Board", f"**Active Sessions:** `{len(ACTIVE_SESSIONS)}/{MAX_CONCURRENT_SESSIONS}` · **Uptime:** `{m}m {s}s` · **Updated:** `{now}`", "", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
        if not ACTIVE_SESSIONS:
            lines.extend([f"{moon_em} *No active sessions at this time.*", "", "Users may click **Start Quest** on the panel to begin."])
        else:
            lines.append(f"### {moon_em} Active Sessions"); lines.append("")
            for idx, (uid, sess) in enumerate(list(ACTIVE_SESSIONS.items())[:15]):
                status_marker = emoji("statusonline30", "🟢") if sess.running else "🔴"
                user_pct = int(sess.done_count / sess.total * 100) if sess.total else 0
                user_bar = sess.progress_bar(user_pct, width=16)
                uname = get_user_meta(uid).get("username", "Unknown")
                in_progress = sess.total - sess.done_count
                state_text = f"{verified_em} Complete" if sess.done_count == sess.total and sess.total > 0 else f"{loading_em} {in_progress} remaining" if in_progress > 0 else "Initializing"
                lines.append(f"{status_marker} **{uname}** · `{sess.elapsed_str()}`")
                lines.append(f"     `{user_bar}` **{user_pct}%** · `{sess.done_count}/{sess.total}`")
                lines.append(f"     {state_text}"); lines.append("")
            if len(ACTIVE_SESSIONS) > 15: lines.extend([f"*...and {len(ACTIVE_SESSIONS) - 15} more sessions*", ""])
        lines.extend(["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "", f"### {online_em} Statistics", "", f"**Registered users:** `{len(_tokens_cache)}`"])
        text = "\n".join(lines)
        if len(text) > 3900: text = text[:3890] + "\n...\n*Session limit reached*"
        return text
    async def update(self):
        view = self._build_view()
        if self.message is None: self.message = await self.channel.send(view=view)
        else:
            try: await self.message.edit(view=view)
            except discord.NotFound: self.message = await self.channel.send(view=view)
            except discord.HTTPException as e: log(f"Admin board edit failed: {e}", "WARN")

# ===== QUEST UI =====
ACCENT_GREY = discord.Colour(0x4E5058)

class PanelRow(discord.ui.ActionRow):
    def __init__(self):
        super().__init__()
        self.children[0].emoji = resolve_button_emoji("questicon")
        self.children[1].emoji = resolve_button_emoji("statusonline30")
        self.children[2].emoji = resolve_button_emoji("statusmajor")

    @discord.ui.button(label="Start Quest", style=discord.ButtonStyle.secondary, custom_id="quest:start")
    async def start(self, itx, button):
        uid = itx.user.id
        if uid in ACTIVE_SESSIONS and ACTIVE_SESSIONS[uid].running: return await itx.response.send_message("You already have an active session running.", ephemeral=True)
        user_token = get_user_token(uid)
        if not user_token: return await itx.response.send_message("**No token registered.**\n\n**Setup:** DM this bot `!settoken`", ephemeral=True)
        if len(ACTIVE_SESSIONS) >= MAX_CONCURRENT_SESSIONS: return await itx.response.send_message(f"**Server at capacity.**", ephemeral=True)

        session = QuestSession(itx.user, user_token); ACTIVE_SESSIONS[uid] = session
        log(f"Session started: {itx.user} (ID: {uid}) — active: {len(ACTIVE_SESSIONS)}", "INFO")
        await itx.response.send_message(f"Session initialized. Check your direct messages for progress.", ephemeral=True)
        asyncio.create_task(self._run_session_with_cleanup(uid, session))

    @staticmethod
    async def _run_session_with_cleanup(uid, session):
        try: await session.run()
        except Exception as e: log(f"Session error for {uid}: {e}", "ERROR")
        finally:
            if uid in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[uid]
            log(f"Session ended: user {uid} — active: {len(ACTIVE_SESSIONS)}", "INFO")

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, custom_id="quest:status")
    async def status(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s: return await itx.response.send_message("No active session found.", ephemeral=True)
        await itx.response.send_message(view=await s.status_layout(), ephemeral=True)

    @discord.ui.button(label="Stop Quest", style=discord.ButtonStyle.secondary, custom_id="quest:stop")
    async def stop(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s or not s.running: return await itx.response.send_message("No active session to stop.", ephemeral=True)
        s.running = False
        await itx.response.send_message("Session termination requested. Finishing current task...", ephemeral=True)

class QuestPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=ACCENT_GREY)
        c.add_item(discord.ui.TextDisplay("## Quest Auto-Completer\n\n**Available Actions:**\n• **Start Quest** — Begin automated quest completion\n• **Status** — View your current session progress\n• **Stop Quest** — Terminate your active session\n\n**First Time Users:**\nUse `!settoken` via DM to register."))
        c.add_item(discord.ui.Separator())
        c.add_item(PanelRow())
        self.add_item(c)

# ===== BOT =====
intents = discord.Intents.default()
intents.members = True; intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
_ready_done = False

@bot.event
async def on_ready():
    global _ready_done, SESSION_SEMAPHORE
    log(f"Bot ready: {bot.user}", "INFO")
    if _ready_done: return
    _ready_done = True
    SESSION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    load_emojis()
    bot.add_view(QuestPanel())
    
    await sync_tokens_from_channel()
    
    for uid_str, data in _tokens_cache.items():
        conf = data.get("poster_config")
        if conf and conf.get("running"):
            try:
                user = await bot.fetch_user(int(uid_str))
                channel_data = {cid: 0 for cid in conf["channels"]}
                task = PosterTask(user, channel_data, data["token"], conf["text"], conf.get("image_paths", []), conf.get("image_filenames", []))
                ACTIVE_POSTERS[int(uid_str)] = task
                asyncio.create_task(task.start())
                log(f"[AUTO-RESUME] Restarted poster for {user.name}", "INFO")
            except Exception as e:
                log(f"[AUTO-RESUME] Failed for {uid_str}: {e}", "ERROR")

    await bot.change_presence(activity=build_bot_activity())

@bot.event
async def on_disconnect():
    for uid, s in list(ACTIVE_SESSIONS.items()): s.running = False
    for uid, s in list(ACTIVE_POSTERS.items()): s.running = False

# ================= PREFIX COMMANDS =================
@bot.command(name="posting")
async def posting(ctx):
    image_urls, image_filenames = [], []
    if ctx.message.attachments:
        for att in ctx.message.attachments:
            image_urls.append(att.url); image_filenames.append(att.filename)
    await ctx.send(view=PosterPanel(is_update=False, image_urls=image_urls, image_filenames=image_filenames))

@bot.command(name="updateposting")
async def updateposting(ctx):
    conf = get_user_meta(ctx.author.id).get("poster_config")
    if not conf: return await ctx.reply("❌ Walang naka-save na config. Gamitin muna ang `!posting`.")
    
    image_urls, image_filenames = [], []
    if ctx.message.attachments:
        for att in ctx.message.attachments:
            image_urls.append(att.url); image_filenames.append(att.filename)
            
    # Dito natin ipinapasa ang existing config para ma-pre-fill ang modal
    await ctx.send(view=PosterPanel(is_update=True, existing_data=conf, image_urls=image_urls, image_filenames=image_filenames))

@bot.command(name="checkposting")
async def checkposting(ctx):
    conf = get_user_meta(ctx.author.id).get("poster_config")
    if not conf: return await ctx.reply("❌ Wala kang naka-save na Auto-Poster.")
    embed = discord.Embed(title="📋 Saved Auto-Poster Config", color=0x3498db)
    embed.add_field(name="Channels", value=", ".join([f"<#{c}>" for c in conf["channels"]]), inline=False)
    embed.add_field(name="Text", value=f"```{conf['text'][:100] or 'None'}```", inline=False)
    embed.add_field(name="Images Saved", value=f"`{len(conf.get('image_paths', []))}`", inline=True)
    embed.add_field(name="Status", value="`Running`" if ctx.author.id in ACTIVE_POSTERS else "`Stopped`", inline=True)
    await ctx.reply(embed=embed)

@bot.command(name="deleteposting")
async def deleteposting(ctx):
    uid = ctx.author.id
    if not get_user_meta(uid).get("poster_config"): return await ctx.reply("❌ Walang mabubura na config.")
    if uid in ACTIVE_POSTERS:
        ACTIVE_POSTERS[uid].running = False
        del ACTIVE_POSTERS[uid]
    await delete_poster_config(uid)
    await ctx.reply(f"{emoji('verified')} **Auto-Poster Deleted.**")

@bot.command(name="stopposting")
async def stopposting(ctx):
    uid = ctx.author.id
    conf = get_user_meta(uid).get("poster_config")
    if not conf: return await ctx.reply("❌ Wala kang active na auto-poster.")
    if uid in ACTIVE_POSTERS:
        ACTIVE_POSTERS[uid].running = False
        del ACTIVE_POSTERS[uid]
    conf["running"] = False
    await set_poster_config(uid, conf)
    await ctx.reply(f"{emoji('verified')} **Auto-Poster stopped.**")

@bot.command(name="questpanel")
@commands.has_permissions(manage_guild=True)
async def questpanel(ctx):
    try: await ctx.send(view=QuestPanel())
    except discord.HTTPException as e: await ctx.send(f"Failed: `{e}`")

@bot.command(name="adminboard")
@commands.has_permissions(manage_guild=True)
async def adminboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if ACTIVE_ADMIN_BOARD: await ACTIVE_ADMIN_BOARD.stop()
    board = AdminBoard(ctx.channel)
    ACTIVE_ADMIN_BOARD = board
    await ctx.send("Starting dashboard...")
    await board.start()

@bot.command(name="stopboard")
@commands.has_permissions(manage_guild=True)
async def stopboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if not ACTIVE_ADMIN_BOARD: return await ctx.reply("No active admin board to stop.")
    await ACTIVE_ADMIN_BOARD.stop()
    ACTIVE_ADMIN_BOARD = None
    await ctx.reply("Admin board stopped.")

@bot.command(name="settoken")
async def settoken_cmd(ctx, token: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        try: await ctx.message.delete()
        except: pass
        return await ctx.author.send("**Privacy Notice:** Register via Direct Message.")
    if not token: return await ctx.reply("**Usage:** `!settoken <token>`")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API}/users/@me", headers=uheaders(token), timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return await ctx.reply(f"**Invalid token.** (HTTP {r.status})")
                user_data = await r.json()
        except Exception as e: return await ctx.reply(f"**Network error:** `{e}`")

    username = user_data.get("username", "Unknown")
    await set_user_token(ctx.author.id, token, username=username)
    try: await ctx.message.delete()
    except: pass
    await ctx.author.send(f"**Token Registered!**\nAccount: `{username}`\nYou can now use the panels.")

@bot.command(name="removetoken")
async def removetoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return await ctx.reply("Use in DM.")
    if str(ctx.author.id) in _tokens_cache:
        _tokens_cache.pop(str(ctx.author.id))
        if str(ctx.author.id) in _message_ids:
            try:
                msg = await _storage_channel.fetch_message(_message_ids[str(ctx.author.id)])
                await msg.delete()
                del _message_ids[str(ctx.author.id)]
            except Exception: pass
    await ctx.reply("**Token removed.**")

@bot.command(name="mytoken")
async def mytoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return
    if has_user_token(ctx.author.id):
        meta = get_user_meta(ctx.author.id)
        await ctx.reply(f"**Registered:** Yes\n**Account:** `{meta.get('username', 'Unknown')}`")
    else: await ctx.reply("**Registered:** No\n\nUse `!settoken` to register.")

@bot.command(name="sessions")
@commands.has_permissions(manage_guild=True)
async def sessions_cmd(ctx):
    if not ACTIVE_SESSIONS: return await ctx.reply("No active sessions.")
    lines = [f"**Active:** `{len(ACTIVE_SESSIONS)}`\n"]
    for uid, s in ACTIVE_SESSIONS.items():
        lines.append(f"🟢 **{get_user_meta(uid).get('username', 'Unknown')}** · `{s.done_count}/{s.total}`")
    await ctx.reply("\n".join(lines))

@bot.command(name="stopall")
@commands.has_permissions(manage_guild=True)
async def stopall_cmd(ctx):
    for uid, s in list(ACTIVE_SESSIONS.items()): s.running = False
    await ctx.reply(f"Terminated `{len(ACTIVE_SESSIONS)}` sessions.")

@bot.command(name="stopuser")
@commands.has_permissions(manage_guild=True)
async def stopuser_cmd(ctx, user: discord.User = None):
    if not user: return await ctx.reply("Usage: `!stopuser @user`")
    s = ACTIVE_SESSIONS.get(user.id)
    if not s: return await ctx.reply(f"No active session for {user.mention}.")
    s.running = False
    await ctx.reply(f"Terminating session for {user.mention}...")

@bot.command(name="dmclear")
async def dmclear_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel): return await ctx.reply("Use in DM.")
    notice = await ctx.send("Clearing messages...")
    deleted = await purge_dm_messages(ctx.channel)
    try: await notice.delete()
    except: pass
    confirm = await ctx.send(f"Cleared `{deleted}` message(s).")
    await asyncio.sleep(3)
    try: await confirm.delete()
    except: pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.MissingPermissions): return await ctx.reply("Need Manage Server permission.")
    try: await ctx.reply(f"**Error:** `{error}`")
    except: pass

# ===== MAIN =====
if __name__ == "__main__":
    try:
        bot.run(BOT_TOKEN)
    except KeyboardInterrupt:
        print("\nBot stopped.")
