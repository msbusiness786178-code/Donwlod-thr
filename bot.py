import asyncio
import math
import os
import re
import shutil
import threading
import time
from urllib.parse import urljoin

import aiohttp
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA}
QUALITIES = {"480": 480, "720": 720}
SPLIT_LIMIT = int(1.9 * 1024 * 1024 * 1024)  # Telegram 2GB limit se neeche
WORKDIR = "/tmp/thor"
os.makedirs(WORKDIR, exist_ok=True)

# ---------------- Flask (Render web service ke liye port) ----------------
web = Flask(__name__)


@web.route("/")
def home():
    return "ThorDownloader bot is running", 200


@web.route("/health")
def health():
    return "ok", 200


def run_web():
    web.run(host="0.0.0.0", port=PORT)


# ---------------- Bot ----------------
app = Client("thordownloader", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, workdir=WORKDIR)

pending = {}   # user_id -> url (quality ka wait)
busy = set()   # user_id jinka job chal raha hai


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_master(text, base):
    """Master playlist se variants + audio groups nikalta hai."""
    variants, audios = [], {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
            gid = re.search(r'GROUP-ID="([^"]*)"', line)
            uri = re.search(r'URI="([^"]*)"', line)
            if gid and uri and gid.group(1) not in audios:
                audios[gid.group(1)] = urljoin(base, uri.group(1))
        elif line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            aud = re.search(r'AUDIO="([^"]*)"', line)
            uri = next((l for l in lines[i + 1:] if not l.startswith("#")), None)
            if uri:
                variants.append({
                    "url": urljoin(base, uri),
                    "height": int(res.group(2)) if res else 0,
                    "bw": int(bw.group(1)) if bw else 0,
                    "audio": aud.group(1) if aud else None,
                })
    return variants, audios


def pick_variant(variants, target):
    with_h = [v for v in variants if v["height"]]
    pool = with_h or variants
    if with_h:
        return min(pool, key=lambda v: (abs(v["height"] - target), v["height"]))
    # resolution nahi mili to bandwidth se: 480 -> low-mid, 720 -> high
    pool = sorted(pool, key=lambda v: v["bw"])
    return pool[len(pool) // 2] if target <= 480 else pool[-1]


async def resolve(url, target):
    """(video_url, audio_url|None, height) return karta hai."""
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"Link open nahi hua (HTTP {r.status}). Token expire ya Referer/403 issue ho sakta hai.")
            text = await r.text()
    if "#EXTM3U" not in text:
        raise RuntimeError("Ye valid m3u8 playlist nahi hai.")
    variants, audios = parse_master(text, url)
    if not variants:  # direct media playlist
        return url, None, 0
    v = pick_variant(variants, target)
    return v["url"], audios.get(v["audio"]) if v["audio"] else None, v["height"]


async def run(cmd):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return p


async def probe(path):
    p = await run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height:format=duration",
                   "-of", "default=nw=1", path])
    out, _ = await p.communicate()
    info = dict(l.split("=", 1) for l in out.decode().split() if "=" in l)
    try:
        dur = int(float(info.get("duration", 0)))
    except ValueError:
        dur = 0
    return dur, int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)


async def download(video_url, audio_url, out, status):
    hdr = f"User-Agent: {UA}\r\n"
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
           "-headers", hdr, "-i", video_url]
    if audio_url:
        cmd += ["-headers", hdr, "-i", audio_url, "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
    cmd += ["-c", "copy", "-movflags", "+faststart", out]
    p = await run(cmd)
    err_task = asyncio.create_task(p.stderr.read())
    last = 0
    while p.returncode is None:
        try:
            await asyncio.wait_for(p.wait(), timeout=5)
        except asyncio.TimeoutError:
            size = os.path.getsize(out) if os.path.exists(out) else 0
            if size != last:
                last = size
                try:
                    await status.edit(f"⬇️ Downloading... {human(size)}")
                except Exception:
                    pass
    err = (await err_task).decode(errors="ignore")
    if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError("ffmpeg fail hua:\n" + err[-500:])


async def split_file(path, dur):
    size = os.path.getsize(path)
    parts = math.ceil(size / SPLIT_LIMIT)
    seg = max(int(dur / parts) + 1, 1)
    base = path[:-4]
    p = await run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", path,
                   "-c", "copy", "-map", "0", "-f", "segment",
                   "-segment_time", str(seg), "-reset_timestamps", "1",
                   f"{base}_part%03d.mp4"])
    await p.communicate()
    d = os.path.dirname(path)
    files = sorted(os.path.join(d, f) for f in os.listdir(d)
                   if f.startswith(os.path.basename(base) + "_part"))
    os.remove(path)
    return files


async def make_thumb(path, dur):
    thumb = path + ".jpg"
    p = await run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                   "-ss", str(min(3, max(dur // 2, 0))), "-i", path,
                   "-frames:v", "1", "-vf", "scale=320:-1", thumb])
    await p.communicate()
    return thumb if os.path.exists(thumb) else None


def upload_progress_factory(status):
    state = {"t": 0}

    async def cb(cur, total):
        if time.time() - state["t"] < 5:
            return
        state["t"] = time.time()
        try:
            await status.edit(f"⬆️ Uploading... {human(cur)} / {human(total)} ({cur * 100 // total}%)")
        except Exception:
            pass
    return cb


async def process(m: Message, url: str, q: str):
    uid = m.from_user.id
    job_dir = os.path.join(WORKDIR, f"{uid}_{m.id}")
    os.makedirs(job_dir, exist_ok=True)
    status = await m.reply(f"🔍 Playlist check ho rahi hai ({q}p)...")
    try:
        v_url, a_url, h = await resolve(url, QUALITIES[q])
        got = f"{h}p" if h else "default"
        await status.edit(f"⬇️ Downloading... quality: {got}")
        out = os.path.join(job_dir, f"video_{q}p.mp4")
        await download(v_url, a_url, out, status)

        dur, w, hh = await probe(out)
        files = [out]
        if os.path.getsize(out) > SPLIT_LIMIT:
            await status.edit("✂️ File badi hai, parts me split ho rahi hai...")
            files = await split_file(out, dur)

        for i, f in enumerate(files, 1):
            pdur, pw, ph = await probe(f)
            thumb = await make_thumb(f, pdur)
            cap = f"🎬 {got}" + (f" | Part {i}/{len(files)}" if len(files) > 1 else "")
            await m.reply_video(f, caption=cap, duration=pdur, width=pw, height=ph,
                                thumb=thumb, supports_streaming=True,
                                progress=upload_progress_factory(status))
        await status.delete()
    except Exception as e:
        try:
            await status.edit(f"❌ Error: {e}"[:4000])
        except Exception:
            pass
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        busy.discard(uid)


@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    await m.reply(
        "👋 **ThorDownloader**\n\n"
        "Mujhe `.m3u8` link bhejo, fir quality bhejo:\n"
        "`480` → 480p\n`720` → 720p")


@app.on_message(filters.command("cancel") & filters.private)
async def cancel(_, m: Message):
    pending.pop(m.from_user.id, None)
    await m.reply("Cancel ho gaya.")


@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel"]))
async def handler(_, m: Message):
    uid = m.from_user.id
    txt = m.text.strip()

    if txt.lower().startswith("http"):
        if uid in busy:
            return await m.reply("⏳ Pehla download chal raha hai, wait karo.")
        pending[uid] = txt
        return await m.reply("Quality bhejo:\n`480` ya `720`")

    q = txt.lower().replace("p", "").strip()
    if q in QUALITIES and uid in pending:
        if uid in busy:
            return await m.reply("⏳ Pehla download chal raha hai, wait karo.")
        url = pending.pop(uid)
        busy.add(uid)
        asyncio.create_task(process(m, url, q))
        return

    if uid in pending:
        return await m.reply("Sirf `480` ya `720` bhejo.")
    await m.reply("Pehle m3u8 link bhejo.")


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    app.run()
