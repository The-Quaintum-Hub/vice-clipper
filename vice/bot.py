"""Bot de Telegram (long polling, solo stdlib). Solo obedece al chat autorizado (TELEGRAM_CHAT_ID).

Flujo: clip nuevo -> te llega con ✅ aprobar / ❌ rechazar / ✏️ título.
Aprobar -> archivo + texto listo para copiar + cuenta y hora sugeridas por red.
Respondés a ese mensaje con el enlace publicado -> queda registrado (red y cuenta salen solas).
Comandos: /vistas <enlace> <número>  (TikTok/Instagram a mano) · /reporte · /estado
Vistas de YouTube: API oficial (YOUTUBE_API_KEY en .env), `vice views` cada 6 h."""
import datetime as dt
import json
import logging
import os
import pathlib
import re
import time
import urllib.parse
import urllib.request
import uuid

from . import config, db

log = logging.getLogger("vice.bot")
IN_FLIGHT = 4  # clips esperando tu respuesta a la vez (para no inundarte)
# Horas sugeridas (hora de Chile = ET + 2 h en nov.). Desde el 17-nov las dos marcas apuntan a EE. UU.;
# horarios desfasados para que las dos cuentas no publiquen a la misma hora.
def slots(marca):
    """Horarios de publicación (hora local): la 1ª cuenta 13/19/22 h y cada cuenta siguiente 1 h después (no chocan)."""
    i = list(config.cuentas()).index(marca)
    return [f"{(h + i) % 24:02d}:00" for h in (13, 19, 22)]
RED_OFFSET = {"tiktok": 0, "youtube": 20, "instagram": 40, "x": 60}  # minutos entre redes del mismo clip
URL_RE = re.compile(r"https?://[^\s<>\"]+")


def _red(url):
    """Red del enlace por su dominio EXACTO (o subdominio): "evil-tiktok.com" o "tiktok.com.otro.net" no cuentan."""
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    for red, dominios in (("tiktok", ("tiktok.com",)), ("youtube", ("youtube.com", "youtu.be")),
                          ("instagram", ("instagram.com",)), ("x", ("x.com", "twitter.com"))):
        if any(h == d or h.endswith("." + d) for d in dominios):
            return red
    return None


def _yt_id(url):
    m = re.search(r"(?:shorts/|watch\?v=|youtu\.be/)([\w-]{11})", url)
    return m.group(1) if m else None


class TG:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat = str(chat_id)

    def call(self, method, files=None, timeout=70, **params):
        if files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in params.items():
                v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, path in files.items():
                p = pathlib.Path(path)
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{p.name}\"\r\n"
                         "Content-Type: application/octet-stream\r\n\r\n").encode() + p.read_bytes() + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(self.base + method, body,
                                         {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(self.base + method, json.dumps(params).encode(),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout if not files else 600) as r:
            j = json.load(r)
        if not j.get("ok"):
            raise RuntimeError(j)
        return j["result"]

    def send(self, text, **kw):
        return self.call("sendMessage", chat_id=self.chat, text=text[:4000], **kw)


def _tg():
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    return TG(tok, chat) if tok and chat else None


MOTIVOS = {"aburrido": "😴 Aburrido", "cortado": "✂️ Cortado", "contexto": "❓ Sin contexto",
           "visual": "🖼️ Se ve mal", "repetido": "🔁 Repetido", "otro": "🤷 Otro"}


def cupo():
    """Posts por día por cuenta y red con Ghost (VICE_CUPO). Cuentas nuevas: 1-3 al día, con horas entre medio."""
    return int(os.environ.get("VICE_CUPO", "2"))


def turnos(marca, red, dia):
    """Cuándo publica Ghost esa cuenta y red ese día: los últimos `cupo()` horarios de la marca (los de la tarde-noche
    de EE. UU.) + el desfase de la red."""
    return [dt.datetime.combine(dia, dt.time(*map(int, s.split(":")))) + dt.timedelta(minutes=RED_OFFSET[red])
            for s in slots(marca)[-cupo():]]


def ghost():
    """VICE_GHOST=1 en .env: Ghost publica lo aprobado y manda el link a Content Rewards (vice/ghost.py)."""
    return os.environ.get("VICE_GHOST") == "1"


def cuando(con, clip, red):
    """Hora de publicar: el n-ésimo horario FUTURO de esa marca (n = aprobados hoy) + desfase por red. En vivo: ya."""
    ahora = dt.datetime.now().replace(microsecond=0)
    if json.loads(clip["estilo"] or "{}").get("vivo"):
        return ahora
    hoy = dt.date.today()
    n = con.execute("SELECT COUNT(*) FROM kv WHERE k LIKE ?", (f"slot:{hoy.isoformat()}:{clip['marca']}:%",)).fetchone()[0]
    libres = [x for x in (dt.datetime.combine(hoy + dt.timedelta(days=d), dt.time(*map(int, s.split(":"))))
                          for d in range(5) for s in slots(clip["marca"])) if x > ahora]
    return libres[min(n, len(libres) - 1)] + dt.timedelta(minutes=RED_OFFSET[red])


def suggest(con, clip, red):
    if json.loads(clip["estilo"] or "{}").get("vivo"):
        return "AHORA (es del directo en curso)"
    return cuando(con, clip, red).strftime("%a %d %H:%M")


def review_text(clip):
    est = json.loads(clip["estilo"] or "{}")
    juez = est.get("juez")
    nota = f"🧠 {clip['puntaje']:.0f}/100 — {juez['motivo']}" if juez else f"puntaje {clip['puntaje']}"
    vivo = "🔴 EN VIVO — mientras antes lo subas, mejor\n" if est.get("vivo") else ""
    if clip["campaign"].startswith("organico"):
        vivo += "🌱 ORGÁNICO — sin pago, para crecer la cuenta\n"
    return (f"{vivo}🎬 Clip #{clip['id']} · {clip['marca']} · campaña {clip['campaign']}\n"
            f"{clip['fin'] - clip['inicio']:.0f}s · {nota}\n\n{clip['caption']}")


def push_new(con, tg):
    """Manda a revisión los clips nuevos: máx IN_FLIGHT sin responder. Los del directo y los de campañas con
    `prioridad` (GTA) NO hacen fila: si esperan a que respondas los viejos, el momento se enfría (18-nov: 9 clips del
    directo quedaron trancados detrás de 4 sin responder)."""
    camps = {c["id"]: c for c in config.active()}  # clips de campañas apagadas esperan, no se borran
    marcas = ",".join("?" * len(camps))
    # solo cuentan los de campañas activas: los de pruebas viejas sin responder no tapan la fila
    waiting = con.execute(f"SELECT COUNT(*) FROM clips WHERE estado='enviado' AND campaign IN ({marcas})",
                          list(camps)).fetchone()[0]
    prio = lambda c: camps[c["campaign"]].get("prioridad", 0)  # noqa: E731
    vivo = lambda c: bool(json.loads(c["estilo"] or "{}").get("vivo"))  # noqa: E731
    pend = con.execute(f"SELECT * FROM clips WHERE estado='revision' AND campaign IN ({marcas})", list(camps)).fetchall()
    pend.sort(key=lambda c: (vivo(c) or prio(c) > 0, prio(c), vivo(c), not c["campaign"].startswith("organico"),
                             c["puntaje"] or 0), reverse=True)
    for clip in pend:
        if waiting >= IN_FLIGHT and not (vivo(clip) or prio(clip) > 0):
            break
        if not pathlib.Path(clip["archivo"]).exists():
            con.execute("UPDATE clips SET estado='rechazado' WHERE id=?", (clip["id"],))
            continue
        kb = {"inline_keyboard": [[{"text": "✅ Aprobar", "callback_data": f"a:{clip['id']}"},
                                   {"text": "❌ Rechazar", "callback_data": f"r:{clip['id']}"},
                                   {"text": "✏️ Título", "callback_data": f"e:{clip['id']}"}]]}
        # sin width/height Telegram muestra la vista previa cuadrada (aplanada)
        msg = tg.call("sendVideo", files={"video": clip["archivo"]}, chat_id=tg.chat, caption=review_text(clip)[:1000],
                      width=1080, height=1920, duration=round(clip["fin"] - clip["inicio"]),
                      supports_streaming="true", reply_markup=kb)
        con.execute("UPDATE clips SET estado='enviado', tg_msg=? WHERE id=?", (msg["message_id"], clip["id"]))
        db.kv_set(con, f"msg:{msg['message_id']}", clip["id"])
        waiting += 1


GHOST_REDES = {"tiktok", "instagram", "youtube"}  # las que Ghost sabe publicar (ghost.FLUJOS); X sigue a mano


def ghost_redes():
    """Las que Ghost publica HOY: VICE_GHOST_REDES en .env (por defecto todas las que sabe). 18-nov: TikTok fuera;
    lo publicado por el navegador quedaba en 0 vistas y lo subido desde el celular despegaba."""
    v = os.environ.get("VICE_GHOST_REDES")
    return GHOST_REDES & set(v.replace(" ", "").split(",")) if v is not None else GHOST_REDES


def redes_clip(con, clip, camp):
    """Redes del clip: todas las de la campaña, o UNA rotando (según los aprobados antes) si prohíbe repetirlo."""
    if not camp["requisitos"].get("una_red_por_clip"):
        return camp["redes"]
    n = con.execute("SELECT COUNT(*) FROM clips WHERE campaign=? AND estado='aprobado' AND id<?",
                    (camp["id"], clip["id"])).fetchone()[0]
    return [camp["redes"][n % len(camp["redes"])]]


def programar(con, clip, redes):
    """Toma el siguiente horario de la marca. Con Ghost encendido deja una tarea por red. -> líneas para Telegram."""
    cuenta, lines = config.cuentas()[clip["marca"]], []
    for red in redes:
        if ghost() and red in ghost_redes():  # si ya lo publicaste tú, Ghost se salta esa red
            con.execute("INSERT OR IGNORE INTO tareas(tipo,clip_id,marca,red,cuando) VALUES('publicar',?,?,?,?)",
                        (clip["id"], clip["marca"], red, dt.datetime.now().replace(microsecond=0).isoformat(" ")))
            horas = " y ".join(f"{x:%H:%M}" for x in turnos(clip["marca"], red, dt.date.today()))
            lines.append(f"• {red}: {cuenta} — 🤖 Ghost, en el próximo turno libre ({horas}; GTA primero)")
        else:
            lines.append(f"• {red}: {cuenta} — {'a mano, ' if ghost() else ''}sugerido {suggest(con, clip, red)}")
    db.kv_set(con, f"slot:{dt.date.today().isoformat()}:{clip['marca']}:{clip['id']}", 1)
    return lines


def approve(con, tg, clip):
    camp = config.get(clip["campaign"], must_be_active=False)
    cuenta = config.cuentas()[clip["marca"]]
    req = camp["requisitos"]
    lines = [f"✅ Clip #{clip['id']} aprobado — marca {clip['marca']} (cuenta {cuenta})", ""]
    redes = redes_clip(con, clip, camp)
    if req.get("una_red_por_clip"):  # "spam posting: the same video on multiple accounts": cada red, otro clip
        lines.append(f"⚠️ Esta campaña prohíbe el mismo video en varias cuentas: este clip va SOLO a {redes[0]}.")
    lines += programar(con, clip, redes)
    lines += [f"📋 {r}" for r in req.get("recordatorios") or []]
    con.execute("UPDATE clips SET estado='aprobado' WHERE id=?", (clip["id"],))
    if ghost():
        lines += ["", "Si prefieres publicarlo tú, responde a ESTE mensaje con el enlace y Ghost se salta esa red."]
        m = tg.send("\n".join(lines))
        return db.kv_set(con, f"msg:{m['message_id']}", clip["id"])
    lines += ["", "Texto para copiar 👇"]
    doc = tg.call("sendDocument", files={"document": clip["archivo"]}, chat_id=tg.chat,
                  caption=f"#{clip['id']} {clip['marca']} — NO subir a la otra marca")
    db.kv_set(con, f"msg:{doc['message_id']}", clip["id"])
    m1 = tg.send("\n".join(lines))
    m2 = tg.send(clip["caption"])
    m3 = tg.send("Cuando lo publiques, RESPONDE a este mensaje con el enlace (uno por red).")
    for m in (m1, m2, m3):
        db.kv_set(con, f"msg:{m['message_id']}", clip["id"])


def register_link(con, clip, url):
    """Registra una publicación. Devuelve texto de respuesta."""
    red = _red(url)
    camp = config.get(clip["campaign"], must_be_active=False)
    if not red:
        return f"No reconozco la red de {url} (solo TikTok, YouTube, Instagram, X)."
    if red not in camp["redes"]:
        return f"La campaña {camp['id']} no acepta {red}. No lo registré."
    if clip["estado"] != "aprobado":
        return f"El clip #{clip['id']} no está aprobado ({clip['estado']})."
    req = camp["requisitos"]
    if req.get("una_red_por_clip") and con.execute("SELECT 1 FROM publicaciones WHERE clip_id=?", (clip["id"],)).fetchone():
        return f"La campaña {camp['id']} prohíbe el mismo video en varias cuentas: #{clip['id']} ya está publicado."
    otro = con.execute("SELECT clip_id FROM publicaciones WHERE url=?", (url,)).fetchone()
    if otro:
        return f"Ese enlace ya está registrado para el clip #{otro[0]}."
    try:
        con.execute("INSERT INTO publicaciones(clip_id,red,cuenta,url) VALUES(?,?,?,?)",
                    (clip["id"], red, config.cuentas()[clip["marca"]], url))
    except Exception:
        return f"El clip #{clip['id']} ya tiene un enlace de {red}. Cada clip va una vez por red."
    ok = f"📌 Registrado #{clip['id']} en {red} ({config.cuentas()[clip['marca']]})."
    if ghost() and camp["plataforma_pago"] == "content_rewards" and camp.get("enlace"):  # publicado por Ghost o a mano
        con.execute("INSERT OR IGNORE INTO tareas(tipo,clip_id,marca,red,url,cuando) VALUES('enviar',?,?,?,?,?)",
                    (clip["id"], clip["marca"], red, url, dt.datetime.now().replace(microsecond=0).isoformat(" ")))
        ok += "\n💰 Ghost lo manda a Content Rewards ahora."
    elif req.get("enviar_link_min"):  # p.ej. el link va a Content Rewards antes de 30 min
        hasta = dt.datetime.now() + dt.timedelta(minutes=req["enviar_link_min"])
        ok += f"\n⏰ Mándalo a {camp['plataforma_pago']} ANTES de las {hasta:%H:%M} (regla de la campaña)."
    return ok


def set_views(con, url, n, fuente="manual"):
    cur = con.execute("UPDATE publicaciones SET vistas=?, vistas_fuente=?, vistas_actualizado=datetime('now') "
                      "WHERE url=?", (int(n), fuente, url))
    return cur.rowcount


def avisos_vistas(con, tg):
    """UN aviso cuando una publicación pasa el umbral de su campaña (p.ej. 2.000 vistas -> mandar la captura de
    audiencia por país). Lo llaman /vistas y la actualización automática de YouTube. Devuelve cuántos mandó."""
    camps = {c["id"]: c for c in config.load()}
    n = 0
    for p in con.execute("SELECT p.id, p.url, p.red, p.vistas, c.id AS clip, c.campaign FROM publicaciones p "
                         "JOIN clips c ON c.id = p.clip_id").fetchall():
        av = (camps.get(p["campaign"]) or {}).get("requisitos", {}).get("aviso_vistas")
        if av and p["vistas"] >= av["umbral"] and db.kv_get(con, f"aviso_vistas:{p['id']}") is None:
            tg.send(f"📈 El clip #{p['clip']} pasó {av['umbral']:,} vistas en {p['red']}: {p['url']}\n📋 {av['texto']}")
            db.kv_set(con, f"aviso_vistas:{p['id']}", 1)
            n += 1
    return n


def handle(con, tg, upd):
    if "callback_query" in upd:
        q = upd["callback_query"]
        if str(q["message"]["chat"]["id"]) != tg.chat:
            return
        accion, cid, *extra = q["data"].split(":")
        clip = con.execute("SELECT * FROM clips WHERE id=?", (int(cid),)).fetchone()
        try:  # solo apaga el relojito del botón; si el bot estaba subiendo un video y contesta tarde, Telegram
            tg.call("answerCallbackQuery", callback_query_id=q["id"])  # da 400 "query is too old": la acción igual va
        except Exception as e:
            log.info("answerCallbackQuery tarde: %s", str(e)[:80])
        if accion == "m" and extra and extra[0] in MOTIVOS:  # por qué se rechazó: materia prima de la automejora
            db.kv_set(con, f"rechazo:{cid}", extra[0])
            return tg.send(f"Anotado #{cid}: {MOTIVOS[extra[0]]}")
        if not clip or clip["estado"] not in ("enviado", "revision"):
            return tg.send(f"El clip #{cid} ya fue resuelto ({clip['estado'] if clip else 'no existe'}).")
        if accion == "a":
            approve(con, tg, clip)
        elif accion == "r":
            con.execute("UPDATE clips SET estado='rechazado' WHERE id=?", (clip["id"],))
            pathlib.Path(clip["archivo"]).unlink(missing_ok=True)
            tg.send(f"❌ Clip #{clip['id']} rechazado y borrado. ¿Por qué? (así el juez aprende)",
                    reply_markup={"inline_keyboard": [[{"text": v, "callback_data": f"m:{clip['id']}:{k}"}
                                                       for k, v in list(MOTIVOS.items())[i:i + 3]] for i in (0, 3)]})
        elif accion == "e":
            db.kv_set(con, "editando", clip["id"])
            tg.send(f"✏️ Escribe el nuevo título para el clip #{clip['id']}:", reply_markup={"force_reply": True})
        return
    msg = upd.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != tg.chat or "text" not in msg:
        return
    text = msg["text"].strip()
    if text.startswith("/estado"):
        q = dict(con.execute("SELECT estado, COUNT(*) FROM videos GROUP BY estado").fetchall())
        c = dict(con.execute("SELECT estado, COUNT(*) FROM clips GROUP BY estado").fetchall())
        return tg.send(f"Videos: {q}\nClips: {c}")
    if text.startswith("/reporte"):
        return tg.send(report_text(con))
    if text.startswith("/vistas"):
        m = re.match(r"/vistas\s+(\S+)\s+([\d.,kKmM]+)", text)
        if not m:
            return tg.send("Uso: /vistas <enlace> <número>   ej: /vistas https://tiktok.com/... 12500")
        raw = m.group(2).lower()
        if raw[-1] in "km":  # 12,5k / 1.2m: la coma o el punto son decimales
            n = int(float(raw[:-1].replace(",", ".")) * (1000 if raw[-1] == "k" else 1_000_000))
        else:  # 12.500 / 12,500: separadores de miles
            n = int(raw.replace(",", "").replace(".", ""))
        ok = set_views(con, m.group(1), n)
        tg.send(f"👀 {n} vistas guardadas." if ok else "No encontré ese enlace registrado.")
        return avisos_vistas(con, tg)
    editando = db.kv_get(con, "editando")
    if editando and not text.startswith("/"):
        clip = con.execute("SELECT * FROM clips WHERE id=?", (int(editando),)).fetchone()
        camp = config.get(clip["campaign"], must_be_active=False)
        titulo = text[:100]
        cap = config.caption(camp, titulo, clip["descripcion"], (clip["hashtags"] or "").split())
        con.execute("UPDATE clips SET titulo=?, caption=? WHERE id=?", (titulo, cap, clip["id"]))
        con.execute("DELETE FROM kv WHERE k='editando'")
        return tg.send(f"Título actualizado (#{clip['id']}):\n\n{cap}")
    urls = URL_RE.findall(text)
    if urls:
        reply = msg.get("reply_to_message", {}).get("message_id")
        cid = db.kv_get(con, f"msg:{reply}") if reply else None
        m = re.search(r"#(\d+)", text)
        cid = cid or (m.group(1) if m else None)
        if not cid:
            return tg.send("¿De qué clip es? Responde al mensaje del clip, o agrega #<número> al enlace.")
        clip = con.execute("SELECT * FROM clips WHERE id=?", (int(cid),)).fetchone()
        if not clip:
            return tg.send(f"No existe el clip #{cid}.")
        return tg.send("\n".join(register_link(con, clip, u) for u in urls))


def run(con):
    tg = _tg()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok and not tg:  # ayuda de instalación: mostrar el chat id de quien le escribió al bot
        for u in TG(tok, 0).call("getUpdates", timeout=5):
            m = u.get("message") or {}
            print("chat id:", m.get("chat", {}).get("id"), "de", m.get("from", {}).get("username"))
        raise SystemExit("Escríbele /start al bot, corre `vice bot` de nuevo y pon ese número en TELEGRAM_CHAT_ID")
    if not tg:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en ~/vice/.env")
    offset = int(db.kv_get(con, "tg_offset", 0))
    while True:
        try:
            push_new(con, tg)
            for upd in tg.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"]):
                offset = upd["update_id"] + 1
                db.kv_set(con, "tg_offset", offset)
                try:
                    handle(con, tg, upd)
                except Exception as e:
                    log.exception("update falló")
                    tg.send(f"⚠️ Error: {str(e)[:300]}")
        except Exception as e:
            log.warning("telegram: %s", str(e)[:200])
            time.sleep(15)


def update_views(con):
    """YouTube: API oficial (gratis, 1 unidad por cada 50 videos). TikTok/Instagram: /vistas en el bot."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("Sin YOUTUBE_API_KEY en .env: vistas de YouTube no se actualizan.")
        return
    pubs = con.execute("SELECT url FROM publicaciones WHERE red='youtube'").fetchall()
    ids = {_yt_id(p["url"]): p["url"] for p in pubs if _yt_id(p["url"])}
    items = list(ids)
    for i in range(0, len(items), 50):
        q = urllib.parse.urlencode({"part": "statistics", "id": ",".join(items[i:i + 50]), "key": key})
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/videos?{q}", timeout=30) as r:
            for it in json.load(r).get("items", []):
                set_views(con, ids[it["id"]], int(it["statistics"].get("viewCount", 0)), "youtube_api")
    print(f"vistas de YouTube actualizadas: {len(items)} videos")
    tg = _tg()
    if tg:
        avisos_vistas(con, tg)


def report_text(con, days=7):
    camps = {c["id"]: c for c in config.load()}
    rows = con.execute("SELECT p.*, c.campaign, c.marca FROM publicaciones p JOIN clips c ON c.id=p.clip_id "
                       "WHERE p.publicado >= datetime('now', ?)", (f"-{days} days",)).fetchall()
    total_v = total_usd = 0
    por = {}
    for r in rows:
        usd = config.earnings(camps[r["campaign"]], r["vistas"], r["red"]) if r["campaign"] in camps else 0
        k = (r["marca"], r["campaign"], r["red"])
        a = por.setdefault(k, [0, 0, 0.0])
        a[0] += 1
        a[1] += r["vistas"]
        a[2] += usd
        total_v += r["vistas"]
        total_usd += usd
    clips = con.execute("SELECT estado, COUNT(*) FROM clips WHERE creado >= datetime('now', ?) GROUP BY estado",
                        (f"-{days} days",)).fetchall()
    lines = [f"📊 Reporte últimos {days} días", f"Clips producidos: {dict(clips)}",
             f"Publicaciones: {len(rows)} · vistas {total_v:,} · ganancia estimada US${total_usd:,.2f}", ""]
    for (marca, camp, red), (n, v, usd) in sorted(por.items()):
        lines.append(f"• {marca} / {camp} / {red}: {n} pub · {v:,} vistas · US${usd:,.2f}")
    sin = con.execute("SELECT COUNT(*) FROM publicaciones WHERE red!='youtube' AND (vistas_actualizado IS NULL OR "
                      "vistas_actualizado < datetime('now','-3 days'))").fetchone()[0]
    if sin:
        lines.append(f"\n⚠️ {sin} publicaciones de TikTok/Instagram sin vistas recientes: /vistas <enlace> <n>")
    return "\n".join(lines)


def weekly_report(con):
    text = report_text(con)
    tg = _tg()
    if tg:
        tg.send(text)
    print(text)
