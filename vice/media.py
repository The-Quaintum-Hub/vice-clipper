"""Descargas (yt-dlp + segmentos HLS directos) y utilidades de ffmpeg/ffprobe."""
import concurrent.futures as cf
import csv
import functools
import http.client
import ipaddress
import json
import os
import pathlib
import socket
import subprocess
import urllib.parse
import urllib.request

import yt_dlp

from . import config

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
YDL = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True, "noplaylist": True}
# Directo/VOD de YouTube con restricción de edad (18-nov): sin cuenta yt-dlp no entra.
PERFIL_YT = None   # None = el perfil de Chrome de tu 1ª cuenta (o VICE_YT_COOKIES)


def perfil_yt():
    return PERFIL_YT or pathlib.Path(os.environ.get("VICE_YT_COOKIES") or
                                     f"~/.config/vice-chrome/{next(iter(config.cuentas()))}/Default").expanduser()


def ydl(opts, hacer):
    """hacer(YoutubeDL) sin cuenta; si YouTube pide confirmar la edad, de nuevo con la sesión de YouTube del Chrome de
    tu 1ª cuenta (cookies leídas en local, no salen de la Pavilion). Solo entonces: usar una cuenta con yt-dlp puede
    hacer que YouTube la marque. Si devuelve un dict, lleva vice_edad=True."""
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            return hacer(y)
    except yt_dlp.utils.DownloadError as e:
        if "confirm your age" not in str(e) or not perfil_yt().exists():
            raise
    # con cuenta, YouTube exige resolver su reto en JavaScript (node, como en ~/.config/yt-dlp/config del CLI)
    with yt_dlp.YoutubeDL({**opts, "cookiesfrombrowser": ("chrome", str(perfil_yt()), None, None),
                           "js_runtimes": {"node": {}}}) as y:
        r = hacer(y)
    return {**r, "vice_edad": True} if isinstance(r, dict) else r


def galletas():
    """Lo mismo para el yt-dlp de línea de comandos (chat en vivo)."""
    return ["--cookies-from-browser", f"chrome:{perfil_yt()}"]


def info(url):
    """Metadatos. Material de marca entregado como archivo en la Pavilion: se usa tal cual. Grabación de un directo
    (carpeta que deja live.guardar, `<carpeta>#vivo-<unix>`): se une en un mp4 la primera vez y trae sus datos
    (canal, hora de inicio, chat) del .json de al lado."""
    path = pathlib.Path(str(url).split("#vivo-")[0]).expanduser()
    if path.is_dir():
        path = unir(path)
    if path.is_file():
        meta = path.with_suffix(".json")
        extra = json.loads(meta.read_text()) if meta.exists() else {}
        return {"title": path.stem, "uploader": "", **extra, "webpage_url": str(path), "id": path.stem, "local": True,
                "duration": probe(path)["duracion"]}
    return ydl(YDL, lambda y: y.extract_info(url, download=False))


def unir(d):
    """Trozos de 10 s de una grabación (lista.csv del segmentador de ffmpeg) -> <carpeta>/<carpeta>.mp4, sin
    recodificar, y se borran los trozos. Si ya está unida, no hace nada."""
    out = d / f"{d.name}.mp4"
    if out.exists():
        return out
    trozos = [d / r[0] for r in csv.reader((d / "lista.csv").read_text().splitlines()) if r and (d / r[0]).exists()]
    lst, tmp = d / "unir.txt", d / f"{d.name}.parte.mp4"
    lst.write_text("".join(f"file '{t.name}'\n" for t in trozos))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(tmp)],
                   check=True, capture_output=True, timeout=3600)
    tmp.rename(out)
    for t in trozos + [lst]:
        t.unlink()
    return out


def list_channel(url, n=6):
    """Últimos `n` videos de un canal (sin detalles). Devuelve [{url,title,duration}]."""
    u = url.rstrip("/")
    if "twitch.tv/" in u and "/videos" not in u:
        u += "/videos?filter=archives&sort=time"
    elif "kick.com/" in u and "/videos" not in u:
        u += "/videos"
    elif ("youtube.com/@" in u or "/channel/" in u) and not u.endswith(("/streams", "/videos")):
        u += "/streams"
    with yt_dlp.YoutubeDL({**YDL, "extract_flat": "in_playlist", "playlistend": n}) as y:
        pl = y.extract_info(u, download=False)
    out = []
    for e in (pl.get("entries") or [])[:n]:
        link = e.get("url") or e.get("webpage_url")
        if link and not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={link}"
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue  # todavía en vivo: se toma cuando termine
        out.append({"url": link, "title": e.get("title"), "duration": e.get("duration")})
    return out


def pick_format(inf, audio_only=False, max_h=1080):
    """audio_only=True: solo audio (o el video más chico con audio). max_h: tope de altura del video."""
    fmts = inf.get("formats") or []
    if audio_only:
        aud = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
        if aud:
            return max(aud, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        vids = [f for f in fmts if f.get("acodec") not in (None, "none")]
        return min(vids, key=lambda f: f.get("height") or 9999) if vids else None
    both = [f for f in fmts if (f.get("height") or 0) <= max_h and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")]
    if both:
        return max(both, key=lambda f: ((f.get("height") or 0), f.get("fps") or 0, f.get("tbr") or 0))
    return None


@functools.lru_cache(maxsize=64)
def _host_publico(host):
    """¿El host resuelve solo a IPs públicas? (una vez por host: los trozos HLS comparten el mismo CDN)"""
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(ips) and all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips)


def _url_ok(url):
    """La lista HLS es dato de afuera: solo http(s) hacia internet (nada de file://, localhost ni la red de la casa).
    ponytail: el DNS se mira antes de pedir, no en la conexión; alcanza para listas de Twitch/Kick/YouTube."""
    u = urllib.parse.urlsplit(url)
    return u.scheme in ("http", "https") and bool(u.hostname) and _host_publico(u.hostname)


def _par_publico(sock):
    """Al conectar (antes de mandar nada): la IP real del otro lado debe ser de internet. Cierra el agujero de un DNS que
    responde distinto entre el chequeo y la conexión."""
    ip = sock.getpeername()[0]
    if not ipaddress.ip_address(ip.split("%")[0]).is_global:
        sock.close()
        raise ValueError(f"conexión a una IP no pública: {ip}")


class _Conexion(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _par_publico(self.sock)


class _ConexionS(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _par_publico(self.sock)


class _Http(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Conexion, req)


class _Https(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_ConexionS, req, context=self._context)


class _SoloPublico(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_ok(newurl):
            raise ValueError(f"redirección no permitida: {newurl[:80]}")
        nuevo = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nuevo and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            nuevo.headers.pop("Authorization", None)   # la clave (p.ej. de Apify) no sigue a otro sitio
            nuevo.unredirected_hdrs.pop("Authorization", None)
        return nuevo


_ABRIR = urllib.request.build_opener(_SoloPublico, _Http, _Https)


def bajar(url, out, maximo=300 * 2**20, headers=None, timeout=120):
    """Descarga `url` (de afuera) a `out` con las mismas reglas que _get y un tope de tamaño."""
    if not _url_ok(url):
        raise ValueError(f"URL no permitida: {url[:80]}")
    n = 0
    with _ABRIR.open(urllib.request.Request(url, headers=headers or UA), timeout=timeout) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            n += len(chunk)
            if n > maximo:
                raise ValueError(f"descarga de más de {maximo >> 20} MB: corto")
            f.write(chunk)


def _get(url, tries=4):
    if not _url_ok(url):
        raise ValueError(f"URL no permitida: {url[:80]}")
    for i in range(tries):
        try:
            with _ABRIR.open(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise


def hls_segments(m3u8_url):
    """[(inicio, dur, url)] y la url del init (fMP4) si existe."""
    text = _get(m3u8_url).decode()
    segs, t, dur, init = [], 0.0, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            uri = line.split('URI="', 1)[1].split('"', 1)[0]
            init = urllib.parse.urljoin(m3u8_url, uri)
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((t, dur, urllib.parse.urljoin(m3u8_url, line)))
            t += dur
            dur = None
    return segs, init


def hls_section(m3u8_url, start, end, out, threads=16):
    """Baja SOLO los segmentos que cubren [start, end]. Devuelve el instante real donde empieza el archivo."""
    segs, init = hls_segments(m3u8_url)
    pick = [s for s in segs if s[0] + s[1] > start and s[0] < end]
    if not pick:
        raise ValueError(f"tramo {start}-{end} fuera del video ({segs[-1][0] + segs[-1][1] if segs else 0:.0f}s)")
    out = pathlib.Path(out)
    with cf.ThreadPoolExecutor(threads) as ex, out.open("wb") as f:
        if init:
            f.write(_get(init))
        for b in ex.map(_get, [s[2] for s in pick]):  # en orden, escribiendo a medida que llegan
            f.write(b)
    return pick[0][0]


def section(inf, start, end, out, audio_only=False, max_h=1080):
    """Tramo [start,end] del video. HLS (Twitch/Kick): segmentos directos. Otros: yt-dlp por rangos.
    Devuelve el desfase: segundo del VOD donde empieza el archivo bajado."""
    if inf.get("local"):  # archivo local: nada que bajar
        out = pathlib.Path(out)
        out.unlink(missing_ok=True)
        out.symlink_to(pathlib.Path(inf["webpage_url"]).resolve())
        return 0.0
    fmt = pick_format(inf, audio_only=audio_only, max_h=max_h)
    if fmt and "m3u8" in (fmt.get("protocol") or ""):
        return hls_section(fmt["url"], start, end, out)
    out = pathlib.Path(out)
    # YouTube y otros (DASH): el mejor video <= max_h va separado del audio; yt-dlp los junta.
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": str(out.with_suffix("")) + ".%(ext)s",
            "format": "bestaudio/worst" if audio_only else
                      f"bestvideo[height<={max_h}][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<={max_h}]+bestaudio/best",
            "merge_output_format": "mp4"}
    whole = start <= 0 and end >= (inf.get("duration") or 0) - 1
    if not whole:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
    else:
        start = 0
    ydl(opts, lambda y: y.download([inf["webpage_url"]]))
    got = sorted(out.parent.glob(out.stem + ".*"))
    if not got:
        raise RuntimeError("yt-dlp no dejó archivo")
    if got[0] != out:
        got[0].rename(out)
    return start  # yt-dlp recorta con ffmpeg: el archivo empieza (aprox.) en start


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,start_time:stream=codec_type,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    dur, t0 = float(j["format"].get("duration") or 0), float(j["format"].get("start_time") or 0)
    if t0 > 60 and dur > t0:  # trozos HLS de Twitch con video: el reloj arranca en el segundo del VOD y ffprobe
        dur -= t0             # da el FIN; en los de solo audio da la duración real (medido 17-nov: quedaba negativa)
    return {"duracion": dur, "ancho": v.get("width"), "alto": v.get("height"),
            "audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def pcm(path, rate=8000, start=None, dur=None):
    """Audio mono int16 como bytes (para medir volumen)."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def frame(path, t, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(path), "-frames:v", "1", str(out)],
                   check=True)
