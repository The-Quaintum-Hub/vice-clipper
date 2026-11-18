"""Repetición del chat -> [(segundo_del_vod, texto, autor)]. El texto es DATO: solo se cuenta, nunca va al modelo.

Twitch: TwitchDownloaderCLI (MIT). Kick: API web de Kick (cursor hacia atrás). YouTube: live_chat de yt-dlp.
Si falla, devuelve [] y la detección sigue solo con audio."""
import concurrent.futures as cf
import datetime as dt
import json
import logging
import pathlib
import subprocess
import tempfile

from .config import DATA

log = logging.getLogger("vice.chat")
TDL = DATA / "bin" / "TwitchDownloaderCLI"


def get(inf, start=0, end=None):
    url = inf.get("webpage_url") or ""
    end = end or inf.get("duration") or 0
    if inf.get("local"):  # grabación propia de un directo: el chat se guardó al grabar (material de marca: no hay)
        return [tuple(m) for m in inf.get("chat") or [] if start <= m[0] <= end]
    cache = DATA / "cache" / f"chat2_{inf.get('extractor_key', 'x')}_{inf.get('id')}_{int(start)}_{int(end)}.json"
    if cache.exists():
        return [tuple(m) for m in json.loads(cache.read_text())]
    msgs = _get(inf, url, start, end)
    if msgs:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(msgs, ensure_ascii=False))
    return msgs


def _get(inf, url, start, end):
    try:
        if "twitch.tv" in url:
            return twitch(inf["id"].lstrip("v"), start, end)
        if "kick.com" in url:
            return kick(inf, start, end)
        if "youtube.com" in url or "youtu.be" in url:
            return youtube(url)
    except Exception as e:  # el chat es un bonus: sin él se detecta por audio
        log.warning("chat no disponible (%s): %s", url, str(e)[:200])
    return []


def twitch(vod_id, start, end):
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "chat.json"
        subprocess.run([str(TDL), "chatdownload", "--id", vod_id, "-b", str(int(start)), "-e", str(int(end) + 1),
                        "-o", str(out), "--bttv", "false", "--ffz", "false", "--stv", "false", "-t", "8",
                        "--collision", "Overwrite", "--banner", "false", "--log-level", "Error",
                        "--temp-path", tmp], check=True, capture_output=True, timeout=3600)
        comments = json.loads(out.read_text())["comments"]
    return [(c["content_offset_seconds"], c["message"]["body"], (c.get("commenter") or {}).get("name") or "")
            for c in comments]


def _kick_chunk(channel_id, t0_unix, a, b):
    """Mensajes entre a y b (segundos del VOD), paginando hacia atrás desde b."""
    from curl_cffi import requests
    out, cursor = [], int((t0_unix + b) * 1e6)
    for _ in range(5000):
        r = requests.get(f"https://kick.com/api/v2/channels/{channel_id}/messages?cursor={cursor}",
                         impersonate="chrome", timeout=20)
        r.raise_for_status()
        d = r.json()["data"]
        msgs = d.get("messages") or []
        if not msgs:
            break
        oldest = b
        for m in msgs:
            t = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp() - t0_unix
            oldest = min(oldest, t)
            if a <= t < b:
                out.append((t, m.get("content") or "", (m.get("sender") or {}).get("username") or ""))
        if oldest < a or not d.get("cursor") or int(d["cursor"]) >= cursor:
            break
        cursor = int(d["cursor"])
    return out


def kick(inf, start, end, chunk=1800):
    cid, t0 = inf.get("channel_id"), inf.get("timestamp")
    if not cid or not t0:
        return []
    spans = [(a, min(a + chunk, end)) for a in range(int(start), int(end), chunk)]
    with cf.ThreadPoolExecutor(4) as ex:
        parts = ex.map(lambda s: _kick_chunk(cid, t0, *s), spans)
    return sorted(m for p in parts for m in p)


def youtube(url):
    from . import media
    with tempfile.TemporaryDirectory() as tmp:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "writesubtitles": True,
                "subtitleslangs": ["live_chat"], "outtmpl": f"{tmp}/c"}
        media.ydl(opts, lambda y: y.download([url]))
        f = next(pathlib.Path(tmp).glob("*.live_chat.json"), None)
        if not f:
            return []
        return [(t, txt, who) for line in f.read_text().splitlines() for t, _, txt, who, _ in yt_linea(line)
                if t is not None]


def yt_linea(line):
    """Una línea del .live_chat.json de yt-dlp -> [(segundo del video | None, hora unix | None, texto, autor, id)].
    VOD: trae el segundo del video. En vivo: no trae segundo, pero sí la hora real de cada mensaje."""
    try:
        act = json.loads(line).get("replayChatItemAction") or {}
    except (ValueError, AttributeError):
        return []
    off = act.get("videoOffsetTimeMsec")
    out = []
    for a in act.get("actions") or []:
        r = ((a.get("addChatItemAction") or {}).get("item") or {}).get("liveChatTextMessageRenderer")
        if r:
            ts = r.get("timestampUsec")
            out.append((int(off) / 1000 if off is not None else None, int(ts) / 1e6 if ts else None,
                        "".join(x.get("text", "") for x in (r.get("message") or {}).get("runs", [])),
                        r.get("authorExternalChannelId") or "", r.get("id")))
    return out
