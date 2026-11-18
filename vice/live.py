"""Ruta en vivo: mientras un canal de una campaña activa transmite, graba los últimos 30 min, escucha el chat y,
cuando hay un pico, el MISMO juez de los VODs decide en caliente. El clip llega a Telegram 🔴 antes de que termine
el directo. Diseño: docs/superpowers/specs/2026-11-17-ruta-en-vivo-design.md

`vice live` = supervisor (servicio): ve qué canales están en vivo y lanza una sesión por canal (máx. VIVO_MAX).
`vice vivo <canal> --campaign <id>` = una sesión (también sirve para probar a mano).
El texto del chat solo se CUENTA (personas por segundo); nunca llega a ningún modelo."""
import csv
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

from . import chat as chatmod, config, db, detect, media, pipeline

log = logging.getLogger("vice")
BIN = pathlib.Path(sys.executable).parent
VIVO_MAX = int(os.environ.get("VICE_VIVO_MAX", "2"))
UMBRAL = float(os.environ.get("VICE_VIVO_UMBRAL", "3.0"))
POR_HORA = int(os.environ.get("VICE_VIVO_POR_HORA", "12"))  # juicios por hora: cuida la cuota diaria de Groq
# s: grabación guardada, línea base, arranque, entre disparos. 18-nov: se guarda el directo ENTERO (antes 30 min):
# YouTube borró el VOD de un directo y con él la pasada final. ~3 GB por hora en 1080p60.
BUFFER, BASE, CALIENTE, ENFRIA = 12 * 3600, 15 * 60, 5 * 60, 90
GRABACIONES = config.DATA / "grabaciones"
GUARDAR_H = float(os.environ.get("VICE_GRABACION_HORAS", "24"))   # cuánto queda cada grabación tras su pasada final
LEER = (95, 45)          # la reacción ya pasó cuando se corta: 45 s después del pico


# ---------- ¿está en vivo? ----------

def estado(url):
    """{'vivo', 'juego' (categoría), 'autor', 'titulo', 'id'} vía Streamlink (sin descargar nada)."""
    r = subprocess.run([str(BIN / "streamlink"), "--json", url], capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    if not j.get("streams") and "youtube.com" in url:
        # Streamlink no entra a un directo con restricción de edad (y lo dice igual que "no está en vivo"):
        # yt-dlp, con la sesión de Chrome de tu 1ª cuenta solo si YouTube la pide (media.ydl)
        try:
            i = media.info(url)
        except Exception:
            i = {}
        hls = [f for f in i.get("formats") or [] if "m3u8" in (f.get("protocol") or "") and f.get("acodec") != "none"
               and f.get("vcodec") != "none" and (f.get("height") or 0) <= 1080]
        if i.get("live_status") == "is_live" and hls:
            # ponytail: la lista HLS firmada vence a las ~6 h; ahí la grabación corta y el supervisor abre otra sesión
            mejor = max(hls, key=lambda f: (f.get("height") or 0, f.get("fps") or 0))
            return {"vivo": True, "juego": "", "autor": i.get("uploader") or i.get("channel") or "", "titulo": i.get("title") or "",
                    "id": i.get("id") or "", "hls": mejor["url"], "edad": bool(i.get("vice_edad"))}
    meta = j.get("metadata") or {}
    return {"vivo": bool(j.get("streams")), "juego": meta.get("category") or "", "autor": meta.get("author") or "",
            "titulo": meta.get("title") or "", "id": meta.get("id") or ""}


def juego_ok(camp, juego, titulo=""):
    """Twitch/Kick: la categoría. YouTube: la categoría es "Gaming", así que manda el título del directo."""
    return config.coincide(camp, f"{juego} {titulo}")


def url_vivo(url):
    """Canal de YouTube (…/@x/streams o …/@x/videos) -> …/@x/live, que es donde aparece el directo."""
    m = re.match(r"https?://(?:www\.|m\.)?youtube\.com/(@[\w.-]+)", url)
    return f"https://www.youtube.com/{m.group(1)}/live" if m else url


def canal(url):
    m = re.search(r"youtube\.com/@([\w.-]+)", url)
    return (m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]).lower()


# ---------- grabación (Streamlink -> ffmpeg en trozos de 10 s) ----------

class Grabacion:
    def __init__(self, url, carpeta, hls=None):
        self.dir = pathlib.Path(carpeta)
        self.dir.mkdir(parents=True, exist_ok=True)
        fuente = "hls://" + hls if hls else url
        if "kick.com" in url:  # el plugin de Kick de Streamlink pide un navegador; yt-dlp saca la lista HLS
            fuente = "hls://" + subprocess.run([str(BIN / "yt-dlp"), "-g", "-f", "best", url], capture_output=True,
                                               text=True, timeout=120, check=True).stdout.split()[0]
        self.sl = subprocess.Popen([str(BIN / "streamlink"), "--stdout", "--retry-open", "3", fuente,
                                    "1080p60,1080p,best"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0",
                                    "-c", "copy", "-f", "segment", "-segment_time", "10", "-reset_timestamps", "1",
                                    "-segment_list", str(self.dir / "lista.csv"), "-segment_list_type", "csv",
                                    str(self.dir / "s%06d.ts")], stdin=self.sl.stdout, stderr=subprocess.DEVNULL)
        self.sl.stdout.close()
        self.t0 = None   # hora real (unix) del segundo 0 de la grabación

    def segmentos(self):
        """[(archivo, inicio, fin)] en segundos desde el comienzo de la grabación (solo los ya cerrados)."""
        f = self.dir / "lista.csv"
        if not f.exists():
            return []
        rows = [r for r in csv.reader(f.read_text().splitlines()) if len(r) >= 3]
        if not rows:
            return []
        s0 = float(rows[0][1])
        segs = [(self.dir / r[0], float(r[1]) - s0, float(r[2]) - s0) for r in rows]
        if self.t0 is None and segs[0][0].exists():  # el primer trozo se cerró al terminar sus ~10 s
            self.t0 = segs[0][0].stat().st_mtime - (segs[0][2] - segs[0][1])
        return [s for s in segs if s[0].exists()]

    def podar(self):
        segs = self.segmentos()
        if segs:
            for f, a, b in segs:
                if b < segs[-1][2] - BUFFER:
                    f.unlink(missing_ok=True)

    def cortar(self, a, b, out):
        """Une los trozos que cubren [a, b] en un mp4 (sin recodificar). Devuelve el segundo donde empieza."""
        segs = [s for s in self.segmentos() if s[2] > a and s[1] < b]
        if not segs:
            raise RuntimeError(f"sin grabación para {a:.0f}-{b:.0f}")
        lst = pathlib.Path(out).with_suffix(".txt")
        lst.write_text("".join(f"file '{f}'\n" for f, *_ in segs))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        str(out)], check=True, capture_output=True, timeout=300)
        lst.unlink(missing_ok=True)
        return segs[0][1]

    def viva(self):
        return self.ff.poll() is None

    def parar(self):
        for p in (self.sl, self.ff):
            if p.poll() is None:
                p.terminate()
        for p in (self.sl, self.ff):
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------- chat en vivo: [(hora_unix, texto, autor)] ----------

class Chat(threading.Thread):
    def __init__(self, url, edad=False):
        super().__init__(daemon=True)
        self.url, self.msgs, self.fin, self.edad = url, [], threading.Event(), edad

    def run(self):
        if self.edad and "youtube.com" in self.url:
            # 18-nov: con restricción de edad YouTube no muestra el chat a una cuenta sin la edad verificada ("El chat
            # no está disponible"). Reintentar cada 15 s con la cuenta solo la expondría: los picos salen del audio.
            log.warning("chat en vivo: directo con restricción de edad, sin chat; los picos salen solo del audio")
            return
        while not self.fin.is_set():
            try:
                if "twitch.tv" in self.url:
                    self._twitch()
                elif "kick.com" in self.url:
                    self._kick()
                elif "youtube.com" in self.url:
                    self._youtube()
                else:
                    return
            except Exception as e:
                log.warning("chat en vivo: %s (reintento en 15 s)", str(e)[:150])
                self.fin.wait(15)

    def _twitch(self):
        """IRC anónimo de Twitch (usuario justinfan*: solo lectura, sin cuenta)."""
        s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=300)
        s.sendall(f"NICK justinfan{int(time.time()) % 100000}\r\nJOIN #{canal(self.url)}\r\n".encode())
        buf = b""
        while not self.fin.is_set():
            data = s.recv(65536)
            if not data:
                raise ConnectionError("IRC cerró")
            buf += data
            *lineas, buf = buf.split(b"\r\n")
            for ln in lineas:
                ln = ln.decode(errors="replace")
                if ln.startswith("PING"):
                    s.sendall(b"PONG :tmi.twitch.tv\r\n")
                m = re.match(r":(\w+)!\S+ PRIVMSG #\S+ :(.*)", ln)
                if m:
                    self.msgs.append((time.time(), m.group(2), m.group(1)))

    def _kick(self):
        """API web de Kick (la misma de los VODs), cada 3 s; los mensajes repetidos se saltan por id."""
        from curl_cffi import requests
        cid = requests.get(f"https://kick.com/api/v2/channels/{canal(self.url)}", impersonate="chrome",
                           timeout=20).json()["id"]
        vistos = set()
        while not self.fin.is_set():
            r = requests.get(f"https://kick.com/api/v2/channels/{cid}/messages", impersonate="chrome", timeout=20)
            for m in (r.json().get("data") or {}).get("messages") or []:
                if m.get("id") not in vistos:
                    vistos.add(m.get("id"))
                    self.msgs.append((time.time(), m.get("content") or "", (m.get("sender") or {}).get("username")))
            self.fin.wait(3)


    def _youtube(self):
        """yt-dlp va escribiendo el chat en vivo (una línea JSON cada ~10 s, con la hora real de cada mensaje).
        Al reconectar trae de nuevo los últimos mensajes: se saltan por id."""
        d = pathlib.Path(tempfile.mkdtemp(prefix="vice-chat-"))
        p = subprocess.Popen([str(BIN / "yt-dlp"), *(media.galletas() if self.edad else []), "--skip-download",
                              "--write-subs", "--sub-langs", "live_chat",
                              "-o", str(d / "c"), self.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vistos, pos = getattr(self, "_vistos", set()), 0
        self._vistos = vistos
        try:
            while not self.fin.is_set():
                f = d / "c.live_chat.json.part"
                if f.exists() and f.stat().st_size > pos:
                    with open(f, "rb") as fh:
                        fh.seek(pos)
                        data = fh.read()
                    cut = data.rfind(b"\n") + 1      # solo líneas completas
                    pos += cut
                    for ln in data[:cut].splitlines():
                        for _, ts, txt, who, mid in chatmod.yt_linea(ln):
                            if ts and mid not in vistos:
                                vistos.add(mid)
                                self.msgs.append((ts, txt, who))
                if p.poll() is not None:
                    raise ConnectionError("el chat de YouTube se cortó")
                self.fin.wait(3)
        finally:
            if p.poll() is None:
                p.terminate()
            shutil.rmtree(d, ignore_errors=True)


def avisar(texto):
    """Mensaje simple a Telegram (p.ej. "<canal> está en vivo"). Si falla, solo queda en el log."""
    try:
        from .bot import _tg
        tg = _tg()
        if tg:
            tg.send(texto)
    except Exception as e:
        log.warning("aviso por Telegram falló: %s", str(e)[:150])


# ---------- disparo ----------

def pico(sc, desde, hasta, ultimo, disparos, ahora):
    """Mejor segundo en [desde, hasta] que pase el umbral, a ENFRIA s del último disparo y dentro del cupo por hora
    (pasado el cupo solo entra algo muy fuerte). Devuelve el segundo o None."""
    desde, hasta = int(desde), int(hasta)
    if hasta <= desde:
        return None
    p = desde + int(np.argmax(sc[desde:hasta]))
    umbral = UMBRAL * (1.5 if sum(1 for d in disparos if d > ahora - 3600) >= POR_HORA else 1)
    if sc[p] < umbral or (ultimo is not None and p - ultimo < ENFRIA):
        return None
    return p


def sesion(con, camp, url):
    """Graba y vigila UN directo hasta que termine (o la campaña se apague, o cambie de juego)."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # al parar el servicio: igual limpia (finally)
    st = estado(url)
    streamer = st["autor"] or canal(url)
    yt = "youtube.com" in url
    juego = (camp.get("juego") or st["juego"]) if yt else st["juego"]   # YouTube: la categoría dice solo "Gaming"
    trabajo = config.DATA / "vivo" / f"{camp['id']}_{canal(url)}"
    shutil.rmtree(trabajo, ignore_errors=True)
    rec, chat = Grabacion(url, trabajo / "rec", st.get("hls")), Chat(url, st.get("edad"))
    chat.start()
    vid = None
    aud = np.full(12 * 3600, np.nan)   # volumen por segundo de la grabación (12 h alcanza para cualquier directo)
    hechos, ultimo, disparos, revisado, ult_log = set(), None, [], time.time(), 0
    try:
        while rec.viva():
            time.sleep(5)
            segs = rec.segmentos()
            if not segs or rec.t0 is None:
                continue
            if vid is None:  # el directo existe desde ahora en la DB, con su hora de inicio en la URL
                vid = con.execute("INSERT INTO videos(campaign,url,titulo,estado,nota) VALUES(?,?,?,'vivo',?)",
                                  (camp["id"], f"{url.rstrip('/')}#vivo-{rec.t0:.0f}", st["titulo"] or juego,
                                   "grabando en vivo")).lastrowid
                log.info("🔴 %s en vivo (%s): grabando", streamer, st["titulo"] or juego)
                ver = f"https://www.youtube.com/watch?v={st['id']}" if yt and st["id"] else url
                avisar(f"🔴 {streamer} está en vivo [{camp['id']}]: {st['titulo'] or juego}\n{ver}\n"
                       "Lo estoy grabando: los clips llegan acá a medida que pase algo.")
            for f, a, b in segs:   # volumen de cada trozo nuevo
                if f not in hechos and int(b) < len(aud):
                    aud = np.fmax(aud, detect.audio_signal(f, a, len(aud)))
                    hechos.add(f)
            rec.podar()
            ahora = segs[-1][2]
            if time.time() - revisado > 300:   # cada 5 min: ¿sigue activa la campaña y en el juego pedido?
                revisado = time.time()
                st2 = estado(url)
                if not config.activa(camp["id"]) or (st2["vivo"] and not juego_ok(camp, st2["juego"], st2["titulo"])):
                    log.info("🔴 %s: la campaña se apagó o cambió de juego (%s): corto", streamer,
                             st2["titulo"] or st2["juego"])
                    break
            if ahora < CALIENTE:
                continue
            a0 = max(int(ahora - BASE), 0)
            n = int(ahora) + 1
            msgs = [(t - rec.t0, txt, who) for t, txt, who in list(chat.msgs) if t - rec.t0 >= a0]
            sc = np.full(n, -np.inf)
            ch = detect.chat_signal([(t - a0, x, w) for t, x, w in msgs], n - a0) if msgs else None
            sc[a0:] = detect.score(n - a0, ch, aud[a0:n])
            if ahora - ult_log >= 300:   # cada 5 min: para calibrar el umbral
                ult_log = ahora
                log.info("🔴 %s: %d min grabados, chat %d msgs, puntaje máx. últimos 5 min %.1f (umbral %.1f)",
                         streamer, ahora // 60, len(chat.msgs), np.max(sc[max(n - 300, a0):n]), UMBRAL)
            desde = max(a0, (ultimo or 0) + ENFRIA, CALIENTE - LEER[1])
            p = pico(sc, desde, ahora - LEER[1], ultimo, disparos, time.time())
            if p is None:
                continue
            ultimo = p
            disparos.append(time.time())
            log.info("🔴 pico en %ds (%.1f): el juez lo lee", p, sc[p])
            _juzgar(con, rec, camp, url, vid, p, streamer, juego, trabajo, detect.chat_x(ch, p - a0))
    finally:
        chat.fin.set()
        rec.parar()
        if vid:
            n = con.execute("SELECT COUNT(*) FROM clips WHERE video_id=?", (vid,)).fetchone()[0]
            db.set_video(con, vid, "listo", f"en vivo: {n} clips, {len(disparos)} picos leídos")
            try:
                guardar(con, camp, url, rec, st["titulo"] or juego, streamer, juego, list(chat.msgs))
            except Exception as e:
                log.warning("🔴 no pude guardar la grabación: %s", str(e)[:200])
        shutil.rmtree(trabajo, ignore_errors=True)
        log.info("🔴 %s: sesión terminada", streamer)


def _juzgar(con, rec, camp, url, vid, p, streamer, juego, trabajo, chat_x=None):
    t = time.time()
    f = trabajo / f"v{p}.mp4"
    try:
        ini = rec.cortar(p - LEER[0] - 2, p + LEER[1] + 2, f)
        inf = media.info(str(f))
        # en vivo manda la rapidez: si el mejor modelo no tiene cuota, se baja al siguiente al minuto
        v = pipeline.leer_pico(inf, p - ini, camp, streamer, juego, trabajo, leer=LEER, chat_x=chat_x, paciencia=65)
        out = v and pipeline.hacer_clip(con, inf, v, camp, vid, config.source_for(camp, url), streamer, juego,
                                        trabajo, t0=ini, vivo=True)
        if out:
            log.info("🔴 clip en vivo listo en %.0f s desde el corte", time.time() - t)
    except Exception as e:
        log.warning("🔴 pico %d falló: %s", p, str(e)[:200])
    finally:
        f.unlink(missing_ok=True)


def guardar(con, camp, url, rec, titulo, streamer, juego, msgs):
    """Deja la grabación entera para la pasada final (`vice work` la procesa como un VOD, sin depender de que el
    sitio lo guarde). Mover la carpeta es instantáneo (cabe en los 90 s de un `systemctl stop`); unirla en un mp4 lo
    hace el worker (media.info). Las horas: `#vivo-<unix>` = mismo reloj que los clips en vivo, así no se repiten."""
    segs = rec.segmentos()
    if rec.t0 is None or not segs or segs[-1][2] - segs[0][1] < CALIENTE:
        return
    t0 = rec.t0 + segs[0][1]   # si se podó el comienzo (>12 h), el archivo empieza ahí
    d = GRABACIONES / f"{camp['id']}_{canal(url)}_{t0:.0f}"
    GRABACIONES.mkdir(parents=True, exist_ok=True)
    rec.dir.rename(d)
    (d / f"{d.name}.json").write_text(json.dumps({
        "title": titulo, "uploader": streamer, "uploader_id": canal(url), "timestamp": t0,
        "categories": [juego] if juego else [], "chat": [(t - t0, x, w) for t, x, w in msgs if t >= t0]},
        ensure_ascii=False))
    con.execute("INSERT INTO videos(campaign,url,titulo,estado,nota) VALUES(?,?,?,'pendiente',?)",
                (camp["id"], f"{d}#vivo-{t0:.0f}", f"{titulo} (grabación completa)", "pasada final de la grabación"))
    log.info("🔴 grabación guardada (%.0f min) para la pasada final: %s", (segs[-1][2] - segs[0][1]) / 60, d.name)


def limpiar_grabaciones(con, horas=GUARDAR_H):
    """Borra las grabaciones de más de `horas` cuya pasada final ya terminó. Una pendiente nunca se borra."""
    for j in GRABACIONES.glob("*/*.json"):
        if time.time() - j.stat().st_mtime < horas * 3600:
            continue
        fila = con.execute("SELECT estado FROM videos WHERE url LIKE ?", (f"{j.parent}#vivo-%",)).fetchone()
        if not fila or fila[0] in ("listo", "descartado", "error"):
            shutil.rmtree(j.parent, ignore_errors=True)
            log.info("grabación borrada (%.0f h): %s", horas, j.parent.name)


# ---------- supervisor ----------

def fuentes_vivo():
    """[(campaña, url_canal)] de campañas activas tipo streamer (las URLs de VODs sueltos no cuentan)."""
    from .__main__ import is_single
    out = [(c, url_vivo(f["url"])) for c in config.active() if c["tipo"] == "streamer" and c.get("vivo", True)
           for f in c["fuentes"] if not is_single(f["url"])]   # vivo: false = solo VODs (p.ej. orgánicas)
    return list({(c["id"], u): (c, u) for c, u in out}.values())   # YouTube: /streams y /videos = el mismo /live


def rescatar(con):
    """Sesiones que murieron sin despedirse (reinicio del servicio, corte de luz): su grabación pasa igual a la pasada
    final, sin el chat (vivía en memoria). 18-nov: un reinicio en pleno directo borró 106 min de grabación."""
    for vid, cid, u, titulo in con.execute("SELECT id, campaign, url, titulo FROM videos WHERE estado='vivo'").fetchall():
        url, _, t0 = u.rpartition("#vivo-")
        rec = Grabacion.__new__(Grabacion)   # sin arrancar Streamlink: solo leer los trozos que quedaron
        rec.dir, rec.t0 = config.DATA / "vivo" / f"{cid}_{canal(url)}" / "rec", float(t0)
        try:
            camp = config.get(cid, must_be_active=False)
            if (rec.dir / "lista.csv").exists():
                guardar(con, camp, url, rec, titulo, canal(url), camp.get("juego") or "", [])
        except Exception as e:
            log.warning("🔴 no pude rescatar la grabación de %s: %s", u, str(e)[:200])
        db.set_video(con, vid, "listo", "sesión en vivo cortada")


def supervisor(con):
    rescatar(con)
    shutil.rmtree(config.DATA / "vivo", ignore_errors=True)   # lo que quedó: trabajo temporal de sesiones cortadas
    corriendo = {}
    while True:
        for url, proc in list(corriendo.items()):
            if proc.poll() is not None:
                del corriendo[url]
        for camp, url in fuentes_vivo():
            if url in corriendo or len(corriendo) >= VIVO_MAX:
                continue
            try:
                st = estado(url)
            except Exception as e:
                log.warning("no pude ver %s: %s", url, str(e)[:100])
                continue
            if st["vivo"] and juego_ok(camp, st["juego"], st["titulo"]):
                log.info("🔴 %s está en vivo con %s: abro sesión", url, st["juego"])
                corriendo[url] = subprocess.Popen([sys.executable, "-m", "vice", "vivo", url, "--campaign", camp["id"]])
        limpiar_grabaciones(con)
        time.sleep(120)


if __name__ == "__main__":  # self-check del disparo, sin red
    sc = np.zeros(1000)
    sc[400], sc[700] = 5.0, 2.0
    assert pico(sc, 300, 600, None, [], 0) == 400
    assert pico(sc, 255.0, 255.4, None, [], 0) is None   # ventana de menos de 1 s (pasó en vivo)
    assert pico(sc, 300, 600, 350, [], 0) is None          # a 50 s del último: enfriando
    assert pico(sc, 600, 900, None, [], 0) is None          # 2.0 < umbral
    assert pico(sc, 300, 600, None, [time.time()] * POR_HORA, time.time()) == 400   # cupo lleno, pero 5 >= 4.5
    sc[400] = 4.0
    assert pico(sc, 300, 600, None, [time.time()] * POR_HORA, time.time()) is None
    assert juego_ok({"juego_filtro": "Grand Theft Auto"}, "Grand Theft Auto VI") and not juego_ok(
        {"juego_filtro": "Grand Theft Auto"}, "Just Chatting") and juego_ok({}, "")
    assert canal("https://www.twitch.tv/MiCanal/") == "micanal"
    # YouTube (17-nov): el canal se vigila en /live; el juego sale del TÍTULO (la categoría dice "Gaming")
    assert url_vivo("https://www.youtube.com/@MiCanal/streams") == "https://www.youtube.com/@MiCanal/live"
    assert url_vivo("https://www.twitch.tv/otrocanal/videos") == "https://www.twitch.tv/otrocanal/videos"
    assert canal("https://www.youtube.com/@MiCanal/live") == "micanal"
    gta = {"juego_filtro": ["gta", "grand theft auto"]}
    assert juego_ok(gta, "Gaming", "Playing GTA 6 For The FIRST TIME!") and not juego_ok(gta, "Gaming", "BACKROOMS")
    from .chat import yt_linea
    ln = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"id": "m1", "timestampUsec": "1794956305338759", "authorExternalChannelId": "u",
                                        "message": {"runs": [{"text": "LOL"}]}}}}}]}})
    assert yt_linea(ln) == [(None, 1794956305.338759, "LOL", "u", "m1")] and yt_linea("basura") == []
    print("live OK")
