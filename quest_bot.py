#!/usr/bin/env python3
"""
Discord Quest Auto-Completer Bot — Multi-User Edition
Professional English Interface · Server-Based Emoji Loading
Bot Activity: Playing "Quest Automated"
Admin Board: Per-user overall progress
DM Status: Full quest log with complete indicators
"""

import asyncio
import json
import os
import random
import sys
import threading
import time
from datetime import datetime

try:
    import aiohttp
    import discord
    from discord.ext import commands
except ImportError as e:
    print(f"[FATAL] Missing dependency: {e}")
    print("Install: pip install discord.py aiohttp")
    sys.exit(1)

# ===== CONFIG =====
try:
    with open("config.json", "r", encoding="utf-8") as f:
        CFG = json.load(f)
except FileNotFoundError:
    print("[FATAL] config.json not found!")
    sys.exit(1)

BOT_TOKEN = CFG.get("bot_token", "").strip()
if not BOT_TOKEN:
    print("[FATAL] Missing bot_token in config.json")
    sys.exit(1)

API = "https://discord.com/api/v9"
LOG_FILE = "quest_bot.log"
TOKENS_FILE = "tokens.json"

MAX_CONCURRENT_SESSIONS = CFG.get("max_concurrent", 50)
ADMIN_BOARD_REFRESH = CFG.get("admin_board_refresh", 10)

BOT_ACTIVITY_NAME = CFG.get("bot_activity", "Quest Automated")
BOT_ACTIVITY_TYPE = CFG.get("bot_activity_type", "playing")

# ===== STATE =====
_tokens_lock = threading.Lock()
ACTIVE_SESSIONS = {}
SESSION_SEMAPHORE = None
ACTIVE_ADMIN_BOARD = None

# ===== TOKEN STORAGE (PLAINTEXT) =====

def load_tokens() -> dict:
    if not os.path.exists(TOKENS_FILE):
        return {}
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _write_tokens(data: dict):
    try:
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        log(f"Token write error: {e}", "ERROR")

def set_user_token(user_id: int, token: str, username: str = ""):
    with _tokens_lock:
        data = load_tokens()
        data[str(user_id)] = {
            "token": token,
            "set_at": time.time(),
            "username": username,
        }
        _write_tokens(data)

def get_user_token(user_id: int) -> str:
    data = load_tokens()
    entry = data.get(str(user_id))
    return entry.get("token", "") if entry else ""

def get_user_meta(user_id: int) -> dict:
    data = load_tokens()
    return data.get(str(user_id), {})

def remove_user_token(user_id: int):
    with _tokens_lock:
        data = load_tokens()
        data.pop(str(user_id), None)
        _write_tokens(data)

def has_user_token(user_id: int) -> bool:
    return bool(get_user_token(user_id))

# ===== LOGGING =====
def log(msg, level="INFO"):
    line = f"[{datetime.now():%H:%M:%S}] [{level:7}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

class QuestError(Exception):
    pass

# ===== API =====
def uheaders(token: str) -> dict:
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9047 "
                     "Chrome/128.0.6613.119 Electron/32.0.0 Safari/537.36",
    }

async def uapi(session, method, url, token, context="", data=None,
               retries=2, log_response=False):
    for attempt in range(1, retries + 1):
        try:
            async with session.request(
                method, url, headers=uheaders(token), json=data,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                body = await r.text()
                code = r.status
                if log_response or code >= 400:
                    log(f"[{method}] {url.split('/api/v9/')[-1]} -> {code}", "DEBUG")
                if 200 <= code < 300:
                    try:
                        return json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        return None
                if code == 429:
                    retry_after = 5
                    try:
                        retry_after = float(json.loads(body).get("retry_after", 5))
                    except Exception:
                        pass
                    await asyncio.sleep(retry_after)
                    continue
                if 500 <= code < 600:
                    await asyncio.sleep((2 ** attempt) + random.random())
                    continue
                if code == 401:
                    raise QuestError("Invalid token (401)")
                if code == 403:
                    raise QuestError("Forbidden (403)")
                return None
        except (asyncio.TimeoutError, aiohttp.ClientError):
            await asyncio.sleep(2)
            continue
    return None

async def fetch_quests(session, token):
    result = await uapi(session, "GET", f"{API}/quests/@me", token,
                        "fetch", log_response=True)
    if result and isinstance(result, dict) and "quests" in result:
        return result.get("quests", [])
    return None

def build_plan(quests):
    plan = []
    claim_only = []
    for q in quests:
        if not isinstance(q, dict):
            continue
        us = q.get("user_status") or {}
        if not us.get("enrolled_at") or us.get("claimed_at"):
            continue
        cfg = q.get("config") or {}
        qid = q.get("id")
        name = cfg.get("messages", {}).get("quest_name") or qid
        if us.get("completed_at"):
            claim_only.append({"quest_id": qid, "name": name})
            continue
        tc = cfg.get("task_config") or cfg.get("task_config_v2") or {}
        tasks = tc.get("tasks") or {}
        for ttype in ["WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY"]:
            if ttype not in tasks:
                continue
            task = tasks[ttype]
            target = int(task.get("target") or 0)
            if target <= 0:
                continue
            app_id = None
            if ttype in ["PLAY_ON_DESKTOP", "PLAY_ACTIVITY"]:
                apps = task.get("applications") or []
                if apps and isinstance(apps, list):
                    app_id = apps[0].get("id")
            plan.append({
                "quest_id": qid, "app_id": app_id,
                "task_type": ttype, "target_seconds": target, "name": name,
            })
    log(f"Found {len(plan)} automatable, {len(claim_only)} pending claim", "INFO")
    return plan, claim_only

# ===== EMOJI MANAGEMENT =====
EMOJI_CACHE = {
    "animated": None,
    "moon2": None,
    "statusonline30": None,
    "loading": None,
    "verified": None,
    "questicon": None,
    "statusmajor": None,
}

EMOJI_NAME_MAP = {
    "animated": ["animated"],
    "moon2": ["moon2", "moon_2"],
    "statusonline30": ["statusonline30", "statusonline", "status_online_30"],
    "loading": ["loading"],
    "verified": ["verified", "verified_new", "verify"],
    "questicon": ["questicon3d17", "questicon", "quest_icon"],
    "statusmajor": ["statusmajor", "status_major"],
}

EMOJI_FALLBACK = {
    "animated": "📊",
    "moon2": "🌙",
    "statusonline30": "🟢",
    "loading": "⏳",
    "verified": "✅",
    "questicon": "🎮",
    "statusmajor": "⏹️",
}

def load_emojis():
    loaded = 0
    for emoji in bot.emojis:
        emoji_lower = emoji.name.lower()
        for key, aliases in EMOJI_NAME_MAP.items():
            if EMOJI_CACHE[key] is not None:
                continue
            alias_lower = [a.lower() for a in aliases]
            if emoji_lower in alias_lower:
                EMOJI_CACHE[key] = emoji
                log(f"Emoji loaded: {key} -> {emoji}", "INFO")
                loaded += 1
                break

    for key in EMOJI_NAME_MAP.keys():
        if EMOJI_CACHE.get(key) is None:
            log(f"Emoji MISSING: {key} (using fallback {EMOJI_FALLBACK[key]})", "WARN")

    log(f"Total emojis loaded: {loaded}/{len(EMOJI_NAME_MAP)}", "INFO")
    return loaded


def emoji(key: str, fallback: str = "") -> str:
    e = EMOJI_CACHE.get(key)
    if e:
        return str(e)
    return fallback or EMOJI_FALLBACK.get(key, "")


def resolve_button_emoji(key: str):
    e = EMOJI_CACHE.get(key)
    if e:
        return discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)
    return EMOJI_FALLBACK.get(key, "❓")


def build_bot_activity():
    act_type = BOT_ACTIVITY_TYPE.lower()
    type_map = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }
    activity_type = type_map.get(act_type, discord.ActivityType.playing)
    return discord.Activity(type=activity_type, name=BOT_ACTIVITY_NAME)

# ===== SESSION =====
class QuestSession:
    def __init__(self, owner, user_token):
        self.owner = owner
        self.token = user_token
        self.running = True
        self.started = time.time()
        self.quests = {}
        self.done_count = 0
        self.total = 0
        self.status_message = None
        self.intro_message = None
        self.intro_lines = []
        self.current_quest = "Initializing"

    def progress_bar(self, pct, width=8):
        if pct >= 100:
            return "█" * width
        if pct <= 0:
            return "░" * width
        fill = int(pct / 100 * width)
        return "█" * fill + "░" * (width - fill)

    def get_sorted_quests(self):
        completed, ongoing = [], []
        for name, q in self.quests.items():
            (completed if q["done"] else ongoing).append((name, q))
        ongoing.sort(key=lambda x: x[1]["pct"], reverse=True)
        return completed + ongoing

    def get_quest_text(self):
        """Full quest log with complete indicators (for DM)."""
        lines = []
        loading_em = emoji("loading", "⏳")
        done_em = emoji("verified", "✅")
        for name, q in self.get_sorted_quests():
            pct = q["pct"]
            if q["done"]:
                lines.append(
                    f"{done_em} {self.progress_bar(100)} `100%` — {name}"
                )
            else:
                lines.append(
                    f"{loading_em} {self.progress_bar(pct)} `{pct:3}%` — {name}"
                )
        return "\n".join(lines) if lines else "*No quests available*"

    def elapsed_str(self):
        e = int(time.time() - self.started)
        m, s = divmod(e, 60)
        return f"{m}m {s}s"

    async def status_layout(self):
        """DM status card — full quest log with ✅ indicators."""
        elapsed = time.time() - self.started
        mins, secs = int(elapsed // 60), int(elapsed % 60)

        if self.done_count == self.total and self.total > 0:
            color = discord.Colour(0x57F287)
        elif self.done_count > 0:
            color = discord.Colour(0xFFA500)
        else:
            color = discord.Colour(0x4E5058)

        status_em = emoji("statusonline30", "🟢")
        state = "Active" if self.running else "Complete"

        text = (
            "## Quest Session Status\n\n"
            f"**Progress:** `{self.done_count}/{self.total}` quests completed\n"
            f"{status_em} **{state}** · `{mins}m {secs}s` elapsed\n\n"
            f"### Quest Log\n"
            f"{self.get_quest_text()}"
        )
        if len(text) > 3900:
            text = text[:3890] + "\n..."

        class V(discord.ui.LayoutView):
            pass
        view = V()
        c = discord.ui.Container(accent_colour=color)
        c.add_item(discord.ui.TextDisplay(text))
        view.add_item(c)
        return view

    async def safe_send(self, content):
        try:
            await self.owner.send(content)
            return True
        except discord.Forbidden:
            log(f"Cannot DM {self.owner} (DMs disabled)", "WARN")
        except Exception as e:
            log(f"DM error: {e}", "ERROR")
        return False

    async def send_intro_line(self, key, text):
        prefix = emoji(key, "")
        if prefix:
            self.intro_lines.append(f"{prefix}  {text}")
        else:
            self.intro_lines.append(text)

        class V(discord.ui.LayoutView):
            pass
        view = V()
        c = discord.ui.Container(accent_colour=discord.Colour(0x4E5058))
        c.add_item(discord.ui.TextDisplay("\n".join(self.intro_lines)))
        view.add_item(c)

        try:
            if self.intro_message is None:
                self.intro_message = await self.owner.send(view=view)
            else:
                try:
                    await self.intro_message.edit(view=view)
                except discord.NotFound:
                    self.intro_message = await self.owner.send(view=view)
        except Exception as e:
            log(f"Intro error: {e}", "ERROR")

    async def update_embed(self):
        try:
            view = await self.status_layout()
            if self.status_message is None:
                self.status_message = await self.owner.send(view=view)
            else:
                try:
                    await self.status_message.edit(view=view)
                except discord.NotFound:
                    self.status_message = await self.owner.send(view=view)
        except Exception as e:
            log(f"Status update error: {e}", "ERROR")

    async def run(self):
        async with aiohttp.ClientSession() as session:
            try:
                me = await uapi(session, "GET", f"{API}/users/@me", self.token,
                                "verify", log_response=True)
                if not me:
                    await self.safe_send(
                        "**Session Failed**\n"
                        "Invalid token detected. Use `!removetoken` then `!settoken` again."
                    )
                    self.running = False
                    return
                await self.send_intro_line(
                    "statusonline30",
                    f"Authenticated as **{me.get('username', 'Unknown')}**"
                )
            except QuestError as e:
                await self.safe_send(f"**Token Error:** {e}")
                self.running = False
                return

            await self.send_intro_line("loading", "Retrieving quest list from Discord...")
            quests = await fetch_quests(session, self.token)
            if not quests:
                await self.safe_send("**Failed:** Unable to fetch quests from Discord API.")
                self.running = False
                return

            total_q = len(quests)
            plan, claim_only = build_plan(quests)
            if not plan and not claim_only:
                await self.safe_send(
                    f"**No actionable quests** — Found `{total_q}` total, "
                    f"but none are automatable or claimable."
                )
                self.running = False
                return

            self.total = len(plan) + len(claim_only)
            for p in plan:
                self.quests[p["name"]] = {"pct": 0, "done": False}
            for c in claim_only:
                self.quests[c["name"]] = {"pct": 100, "done": True}

            await self.send_intro_line(
                "animated",
                f"Detected **{total_q}** total quests · "
                f"**{self.total}** actionable. Starting automation."
            )
            await self.update_embed()
            await asyncio.sleep(1)

            for c in claim_only:
                self.current_quest = c["name"]
                await self.claim(session, c)
                self.done_count += 1
                await self.update_embed()

            for i, p in enumerate(plan):
                if not self.running:
                    break
                self.current_quest = p["name"]
                log(f"Processing quest {i+1}/{len(plan)}: {p['name']}", "INFO")
                try:
                    await self.run_one(session, p)
                except QuestError as e:
                    log(f"Quest error: {e}", "ERROR")
                    await self.update_embed()
                if i < len(plan) - 1:
                    await asyncio.sleep(random.uniform(2, 5))

            self.current_quest = "Complete"
            self.running = False
            await self.update_embed()
            await self.safe_send("**All quests completed successfully.**")

    async def run_one(self, session, p):
        if p["task_type"] == "WATCH_VIDEO":
            await self.run_video_quest(session, p)
        else:
            await self.run_play_quest(session, p)

    async def run_video_quest(self, session, p):
        qid = p["quest_id"]
        target = p["target_seconds"]
        url = f"{API}/quests/{qid}/video-progress"
        log(f"VIDEO: {p['name']}", "INFO")
        done = 0
        maxs = target + 5
        while self.running and done < target:
            inc = min(target // 8 + random.randint(0, max(0, target // 10)),
                     target - done)
            inc = max(1, inc)
            done = min(done + inc, maxs)
            data = await uapi(session, "POST", url, self.token, "vp",
                              data={"timestamp": done})
            if data:
                pct = min(99, int(done / target * 100))
                self.quests[p["name"]]["pct"] = pct
                await self.update_embed()
            if done >= target:
                break
            await asyncio.sleep(random.uniform(12, 18))
        if self.running:
            await self.mark_done(session, p)

    async def run_play_quest(self, session, p):
        qid = p["quest_id"]
        aid = p["app_id"]
        target = p["target_seconds"]
        url = f"{API}/quests/{qid}/heartbeat"
        log(f"PLAY: {p['name']}", "INFO")
        hb = 0
        last_val = None
        stuck = 0
        start = time.time()
        deadline = start + target + 120

        while self.running and time.time() < deadline:
            hb += 1
            elapsed = time.time() - start
            data = await uapi(session, "POST", url, self.token, f"hb{hb}",
                              data={"application_id": aid, "terminal": False})
            if data:
                prog = data.get("progress") or {}
                if prog.get("completed_at"):
                    await self.mark_done(session, p)
                    return
                val = 0
                for k in ["PLAY_ON_DESKTOP", "PLAY_ACTIVITY", "value"]:
                    if k in prog:
                        val = prog[k].get("value") if isinstance(prog[k], dict) else prog[k]
                        break
                val = val or 0
                pct = min(99, int(val / max(1, target) * 100))
                self.quests[p["name"]]["pct"] = pct
                await self.update_embed()
                if val == last_val and val > 0:
                    stuck += 1
                    if stuck >= 5:
                        if val >= target:
                            await self.mark_done(session, p)
                            return
                        break
                else:
                    stuck = 0
                    last_val = val
            if elapsed >= target:
                break
            await asyncio.sleep(28 + random.uniform(0, 6))

        for g in range(6):
            if not self.running:
                return
            try:
                data = await uapi(session, "POST", url, self.token, f"g{g}",
                                  data={"application_id": aid, "terminal": True})
                if data and (data.get("progress") or {}).get("completed_at"):
                    await self.mark_done(session, p)
                    return
            except Exception:
                pass
            await asyncio.sleep(20)
        await self.mark_done(session, p)

    async def mark_done(self, session, p):
        self.quests[p["name"]]["pct"] = 100
        self.quests[p["name"]]["done"] = True
        self.done_count += 1
        await self.claim(session, p)
        await self.update_embed()

    async def claim(self, session, p):
        for ep in ["complete", "claim"]:
            try:
                result = await uapi(session, "POST",
                                    f"{API}/quests/{p['quest_id']}/{ep}",
                                    self.token, f"claim-{ep}")
                if result and (result.get("claimed_at") or result.get("completed_at")):
                    log(f"Claimed: {p['name']}", "INFO")
                    return True
            except Exception:
                pass
        log(f"Not claimed: {p['name']}", "INFO")
        return False

# ===== DM CLEANUP =====
async def purge_dm_messages(channel, limit=200, concurrency=3):
    try:
        messages = [m async for m in channel.history(limit=limit)
                    if m.author.id == bot.user.id]
    except Exception as e:
        log(f"History error: {e}", "ERROR")
        return 0
    sem = asyncio.Semaphore(concurrency)
    deleted = 0
    async def _del(msg):
        nonlocal deleted
        async with sem:
            try:
                await msg.delete()
                deleted += 1
            except discord.HTTPException:
                pass
    await asyncio.gather(*(_del(m) for m in messages))
    return deleted

# ===== ADMIN BOARD =====
class AdminBoard:
    """Live-updating admin dashboard — per-user overall progress."""

    def __init__(self, channel):
        self.channel = channel
        self.message = None
        self.running = True
        self.started = time.time()

    async def start(self):
        await self.update()
        asyncio.create_task(self._loop())

    async def _loop(self):
        while self.running:
            await asyncio.sleep(ADMIN_BOARD_REFRESH)
            try:
                await self.update()
            except Exception as e:
                log(f"Admin board update error: {e}", "ERROR")

    async def stop(self):
        self.running = False

    def _build_view(self):
        class V(discord.ui.LayoutView):
            pass
        view = V(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour(0x4E5058))
        container.add_item(discord.ui.TextDisplay(self._build_text()))
        view.add_item(container)
        return view

    def _build_text(self):
        now = datetime.now().strftime("%H:%M:%S")
        elapsed = int(time.time() - self.started)
        m, s = divmod(elapsed, 60)

        board_em = emoji("animated", "📊")
        moon_em = emoji("moon2", "🌙")
        online_em = emoji("statusonline30", "🟢")
        loading_em = emoji("loading", "⏳")
        verified_em = emoji("verified", "✅")

        lines = [
            f"## {board_em} Admin Control Board",
            f"**Active Sessions:** `{len(ACTIVE_SESSIONS)}/{MAX_CONCURRENT_SESSIONS}` · "
            f"**Uptime:** `{m}m {s}s` · **Updated:** `{now}`",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # ─── PER-USER OVERALL PROGRESS (no aggregate) ─────
        if not ACTIVE_SESSIONS:
            lines.append(f"{moon_em} *No active sessions at this time.*")
            lines.append("")
            lines.append("Users may click **Start Quest** on the panel to begin.")
        else:
            lines.append(f"### {moon_em} Active Sessions")
            lines.append("")
            for idx, (uid, sess) in enumerate(list(ACTIVE_SESSIONS.items())[:15]):
                status_marker = emoji("statusonline30", "🟢") if sess.running else "🔴"

                # Per-user OVERALL progress (aggregate of their quests)
                user_pct = int(sess.done_count / sess.total * 100) if sess.total else 0
                user_bar = sess.progress_bar(user_pct, width=16)

                meta = get_user_meta(uid)
                uname = meta.get("username", "Unknown")

                # Status text
                in_progress = sess.total - sess.done_count
                if sess.done_count == sess.total and sess.total > 0:
                    state_text = f"{verified_em} Complete"
                elif in_progress > 0:
                    state_text = f"{loading_em} {in_progress} remaining"
                else:
                    state_text = "Initializing"

                lines.append(f"{status_marker} **{uname}** · `{sess.elapsed_str()}`")
                lines.append(
                    f"     `{user_bar}` **{user_pct}%** · "
                    f"`{sess.done_count}/{sess.total}`"
                )
                lines.append(f"     {state_text}")
                lines.append("")

            if len(ACTIVE_SESSIONS) > 15:
                lines.append(f"*...and {len(ACTIVE_SESSIONS) - 15} more sessions*")
                lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        lines.append(f"### {online_em} Statistics")
        lines.append("")
        lines.append(f"**Registered users:** `{len(load_tokens())}`")

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3890] + "\n...\n*Session limit reached — use `!sessions` for full list*"
        return text

    async def update(self):
        view = self._build_view()
        if self.message is None:
            self.message = await self.channel.send(view=view)
        else:
            try:
                await self.message.edit(view=view)
            except discord.NotFound:
                self.message = await self.channel.send(view=view)
            except discord.HTTPException as e:
                log(f"Admin board edit failed: {e}", "WARN")

# ===== UI =====
ACCENT_GREY = discord.Colour(0x4E5058)

class PanelRow(discord.ui.ActionRow):
    def __init__(self):
        super().__init__()
        self.children[0].emoji = resolve_button_emoji("questicon")
        self.children[1].emoji = resolve_button_emoji("statusonline30")
        self.children[2].emoji = resolve_button_emoji("statusmajor")

    @discord.ui.button(label="Start Quest", style=discord.ButtonStyle.secondary,
                       custom_id="quest:start")
    async def start(self, itx, button):
        uid = itx.user.id

        if uid in ACTIVE_SESSIONS and ACTIVE_SESSIONS[uid].running:
            await itx.response.send_message(
                "You already have an active session running.",
                ephemeral=True
            )
            return

        user_token = get_user_token(uid)
        if not user_token:
            await itx.response.send_message(
                "**No token registered.**\n\n"
                "**Setup Instructions:**\n"
                "1. Send a direct message to this bot\n"
                "2. Type: `!settoken <your_discord_token>`\n"
                "3. Return here and click **Start Quest** again\n\n"
                "**How to find your token:**\n"
                "Open Discord in your browser → Press `F12` → "
                "**Application** tab → **Local Storage** → `discord.com` → "
                "find the `token` key.\n\n"
                "**⚠️ Never share your token with anyone.**",
                ephemeral=True
            )
            return

        if len(ACTIVE_SESSIONS) >= MAX_CONCURRENT_SESSIONS:
            await itx.response.send_message(
                f"**Server at capacity.** Currently `{MAX_CONCURRENT_SESSIONS}` "
                f"sessions are running. Please try again in a few minutes.",
                ephemeral=True
            )
            return

        session = QuestSession(itx.user, user_token)
        ACTIVE_SESSIONS[uid] = session

        log(f"Session started: {itx.user} (ID: {uid}) — "
            f"active: {len(ACTIVE_SESSIONS)}", "INFO")

        await itx.response.send_message(
            f"Session initialized. Active sessions: "
            f"`{len(ACTIVE_SESSIONS)}/{MAX_CONCURRENT_SESSIONS}`.\n"
            f"Check your direct messages for progress.",
            ephemeral=True
        )

        asyncio.create_task(self._run_session_with_cleanup(uid, session))

    @staticmethod
    async def _run_session_with_cleanup(uid, session):
        try:
            await session.run()
        except Exception as e:
            log(f"Session error for {uid}: {e}", "ERROR")
        finally:
            if uid in ACTIVE_SESSIONS:
                del ACTIVE_SESSIONS[uid]
            log(f"Session ended: user {uid} — active: {len(ACTIVE_SESSIONS)}", "INFO")

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary,
                       custom_id="quest:status")
    async def status(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s:
            await itx.response.send_message(
                "No active session found for your account.",
                ephemeral=True
            )
            return
        await itx.response.send_message(view=await s.status_layout(), ephemeral=True)

    @discord.ui.button(label="Stop Quest", style=discord.ButtonStyle.secondary,
                       custom_id="quest:stop")
    async def stop(self, itx, button):
        s = ACTIVE_SESSIONS.get(itx.user.id)
        if not s or not s.running:
            await itx.response.send_message(
                "No active session to stop.",
                ephemeral=True
            )
            return
        s.running = False
        await itx.response.send_message(
            "Session termination requested. Finishing current task...",
            ephemeral=True
        )


class QuestPanel(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=ACCENT_GREY)
        c.add_item(discord.ui.TextDisplay(
            "## Quest Auto-Completer\n\n"
            "**Available Actions:**\n"
            "• **Start Quest** — Begin automated quest completion\n"
            "• **Status** — View your current session progress\n"
            "• **Stop Quest** — Terminate your active session\n\n"
            "**First Time Users:**\n"
            "Send `!settoken <your_token>` via direct message to register."
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(PanelRow())
        self.add_item(c)

# ===== BOT =====
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_ready_done = False

@bot.event
async def on_ready():
    global _ready_done, SESSION_SEMAPHORE
    log(f"Bot ready: {bot.user}", "INFO")
    if _ready_done:
        return
    _ready_done = True

    SESSION_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)

    load_emojis()
    bot.add_view(QuestPanel())

    saved = len(load_tokens())
    activity = build_bot_activity()
    await bot.change_presence(activity=activity)

    log(f"Bot activity: {BOT_ACTIVITY_TYPE.capitalize()} \"{BOT_ACTIVITY_NAME}\"", "INFO")
    log(f"Configuration: max_concurrent={MAX_CONCURRENT_SESSIONS}", "INFO")
    log(f"Registered users: {saved}", "INFO")

@bot.event
async def on_disconnect():
    log("Bot disconnecting — terminating all sessions...", "WARN")
    for uid, s in list(ACTIVE_SESSIONS.items()):
        s.running = False
    global ACTIVE_ADMIN_BOARD
    if ACTIVE_ADMIN_BOARD:
        await ACTIVE_ADMIN_BOARD.stop()
    log(f"Terminated {len(ACTIVE_SESSIONS)} session(s)", "WARN")

# ===== COMMANDS =====
@bot.command(name="questpanel")
@commands.has_permissions(manage_guild=True)
async def questpanel(ctx):
    try:
        await ctx.send(view=QuestPanel())
        log(f"Panel posted in #{ctx.channel}", "INFO")
    except discord.HTTPException as e:
        log(f"Panel post failed: {e}", "ERROR")
        await ctx.send(f"Failed to post panel: `{e}`")

@bot.command(name="adminboard")
@commands.has_permissions(manage_guild=True)
async def adminboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if ACTIVE_ADMIN_BOARD:
        await ACTIVE_ADMIN_BOARD.stop()

    board = AdminBoard(ctx.channel)
    ACTIVE_ADMIN_BOARD = board
    await board.start()
    log(f"Admin board started in #{ctx.channel}", "INFO")

@bot.command(name="stopboard")
@commands.has_permissions(manage_guild=True)
async def stopboard(ctx):
    global ACTIVE_ADMIN_BOARD
    if not ACTIVE_ADMIN_BOARD:
        await ctx.reply("No active admin board to stop.")
        return
    await ACTIVE_ADMIN_BOARD.stop()
    ACTIVE_ADMIN_BOARD = None
    await ctx.reply("Admin board stopped.")

@bot.command(name="settoken")
async def settoken_cmd(ctx, token: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.author.send(
            "**Privacy Notice:** Token registration must be done via direct message."
        )
        return

    if not token:
        await ctx.reply("**Usage:** `!settoken <your_discord_token>`")
        return

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{API}/users/@me", headers=uheaders(token),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    await ctx.reply(
                        f"**Invalid token.** Discord API returned HTTP `{r.status}`. "
                        f"Verify your token and try again."
                    )
                    try:
                        await ctx.message.delete()
                    except Exception:
                        pass
                    return
                user_data = await r.json()
        except Exception as e:
            await ctx.reply(f"**Network error:** `{e}`")
            return

    username = user_data.get("username", "Unknown")
    set_user_token(ctx.author.id, token, username=username)
    log(f"Token registered: {ctx.author.id} ({username})", "INFO")

    try:
        await ctx.message.delete()
    except Exception:
        pass

    await ctx.author.send(
        f"**Token Registered Successfully**\n\n"
        f"**Account:** `{username}`\n\n"
        f"You may now use the **Start Quest** button on the panel.\n"
        f"To remove your token: `!removetoken`"
    )

@bot.command(name="removetoken")
async def removetoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.reply("Use this command in a direct message.")
        return
    if not has_user_token(ctx.author.id):
        await ctx.reply("No token is currently registered to your account.")
        return
    remove_user_token(ctx.author.id)
    log(f"Token removed: {ctx.author.id}", "INFO")
    await ctx.reply("**Token removed successfully.**")

@bot.command(name="mytoken")
async def mytoken_cmd(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if has_user_token(ctx.author.id):
        meta = get_user_meta(ctx.author.id)
        await ctx.reply(
            f"**Registered:** Yes\n"
            f"**Account:** `{meta.get('username', 'Unknown')}`"
        )
    else:
        await ctx.reply(
            "**Registered:** No\n\n"
            "Use `!settoken <token>` to register."
        )

@bot.command(name="sessions")
@commands.has_permissions(manage_guild=True)
async def sessions_cmd(ctx):
    if not ACTIVE_SESSIONS:
        await ctx.reply("No active sessions.")
        return

    lines = [f"**Active Sessions:** `{len(ACTIVE_SESSIONS)}/{MAX_CONCURRENT_SESSIONS}`\n"]
    for uid, s in ACTIVE_SESSIONS.items():
        marker = "🟢" if s.running else "🔴"
        meta = get_user_meta(uid)
        uname = meta.get("username", "Unknown")
        lines.append(
            f"{marker} **{uname}** (ID: `{uid}`)\n"
            f"     `{s.done_count}/{s.total}` quests · "
            f"`{s.elapsed_str()}` elapsed\n"
            f"     Current: *{s.current_quest}*\n"
        )

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n..."
    await ctx.reply(text)

@bot.command(name="stopall")
@commands.has_permissions(manage_guild=True)
async def stopall_cmd(ctx):
    count = len(ACTIVE_SESSIONS)
    if count == 0:
        await ctx.reply("No active sessions to terminate.")
        return
    for uid, s in list(ACTIVE_SESSIONS.items()):
        s.running = False
    log(f"Admin terminated {count} session(s)", "INFO")
    await ctx.reply(f"Terminating `{count}` session(s)...")

@bot.command(name="stopuser")
@commands.has_permissions(manage_guild=True)
async def stopuser_cmd(ctx, user: discord.User = None):
    if not user:
        await ctx.reply("**Usage:** `!stopuser @user`")
        return
    s = ACTIVE_SESSIONS.get(user.id)
    if not s:
        await ctx.reply(f"No active session found for {user.mention}.")
        return
    s.running = False
    log(f"Admin terminated session for {user} (ID: {user.id})", "INFO")
    await ctx.reply(f"Terminating session for {user.mention}...")

@bot.command(name="clear")
async def clear_dm(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.reply("Use this command in a direct message.")
        return
    notice = await ctx.send("Clearing messages...")
    deleted = await purge_dm_messages(ctx.channel)
    try:
        await notice.delete()
    except discord.HTTPException:
        pass
    log(f"Cleared {deleted} DM messages for {ctx.author}", "INFO")
    confirm = await ctx.send(f"Cleared `{deleted}` message(s).")
    await asyncio.sleep(3)
    try:
        await confirm.delete()
    except discord.HTTPException:
        pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("You need the **Manage Server** permission.")
        return
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await ctx.reply("User not found.")
        return
    log(f"Command error in '{ctx.command}': {error!r}", "ERROR")
    try:
        await ctx.reply(f"**Error:** `{error}`")
    except Exception:
        pass

# ===== MAIN =====
if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  DISCORD QUEST AUTO-COMPLETER")
    print("=" * 70)
    print(f"  Log file:         {LOG_FILE}")
    print(f"  Tokens file:      {TOKENS_FILE}")
    print(f"  Max concurrent:   {MAX_CONCURRENT_SESSIONS} sessions")
    print(f"  Board refresh:    every {ADMIN_BOARD_REFRESH}s")
    print(f"  Emoji source:     loaded from server (case-insensitive)")
    print(f"  Bot activity:     {BOT_ACTIVITY_TYPE.capitalize()} \"{BOT_ACTIVITY_NAME}\"")
    print("=" * 70)
    print("  USER COMMANDS (Direct Message):")
    print("    !settoken <token>    Register your token")
    print("    !mytoken             Check registration status")
    print("    !removetoken         Remove your token")
    print("    !clear               Clear bot messages")
    print("")
    print("  ADMIN COMMANDS (Manage Server):")
    print("    !questpanel          Post user panel")
    print("    !adminboard          Start live dashboard")
    print("    !stopboard           Stop dashboard")
    print("    !sessions            Text-based session list")
    print("    !stopall             Terminate all sessions")
    print("    !stopuser @user      Terminate specific session")
    print("=" * 70 + "\n")

    try:
        bot.run(BOT_TOKEN)
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        log(f"Fatal: {e}", "FATAL")