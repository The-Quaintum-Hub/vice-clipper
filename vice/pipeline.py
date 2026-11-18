"""Cadena para UN video de UNA campaña:
señales (chat por personas + audio [+ escenas]) -> picos -> el juez IA lee cada uno y decide cortes y nota
-> solo los aprobados: tramo de video -> whisper -> render -> reglas."""
import json
import logging
import os
import shutil
import time

import numpy as np

from . import chat, config, db, detect, judge, media, render, texts, transcribe, viral, vision

log = logging.getLogger("vice")
MAX_CLIPS = int(os.environ.get("VICE_MAX_CLIPS", "8"))
CANDIDATOS = int(os.environ.get("VICE_CANDIDATOS", "20"))  # picos que lee el juez (la mayoría no sale)
LEER = (95, 65)      # segundos antes / después del pico que lee el juez


class Retry(Exception):
    """El video todavía no se puede procesar (p.ej. sigue en vivo): vuelve a la cola."""


def allowed_ranges(inf, camp, src=None):
    """Tramos del VOD que se pueden usar: los del juego pedido, recortados al `tramo` de la fuente si lo tiene."""
    dur = float(inf.get("duration") or 0)
    f = camp.get("juego_filtro")
    chapters = inf.get("chapters") or []
    ext = (inf.get("extractor_key") or "").lower()
    if f and chapters and "twitch" in ext:  # Twitch: capítulos por juego
        out = [(c["start_time"], c["end_time"]) for c in chapters if config.coincide(camp, c.get("title"))]
    elif f and ("kick" in ext or "youtube" in ext):  # Kick: categoría; YouTube: el título (p.ej. solo GTA VI)
        cats = " ".join(inf.get("categories") or []) + " " + (inf.get("title") or "")
        out = [(0, dur)] if config.coincide(camp, cats) else []
    else:
        out = [(0, dur)]  # archivo o Twitch sin capítulos: no hay de dónde saber el juego; se confía en la fuente
    if src and src.get("tramo"):  # p.ej. solo la parte del VOD que pide la campaña
        a, b = config.tramo(src["tramo"])
        b = dur if b is None else min(b, dur)
        out = [(max(x, a), min(y, b)) for x, y in out if min(y, b) > max(x, a)]
    return out


def signals(inf, camp, ranges, work):
    """Puntaje por segundo de todo el VOD. Baja solo audio (o video liviano si es material de marca)."""
    n = int(inf["duration"]) + 1
    aud = np.full(n, np.nan)
    sce = np.zeros(n) if camp["tipo"] == "marca" else None
    for i, (a, b) in enumerate(ranges):
        f = work / f"audio_{i}.mp4"
        # marca: video liviano (sirve para escenas y audio); streamer: solo audio
        off = media.section(inf, a, b, f, audio_only=sce is None, max_h=360)
        aud = np.fmax(aud, detect.audio_signal(f, off, n))
        if sce is not None:
            sce += detect.scene_signal(f, off, n)
        f.unlink(missing_ok=True)
    msgs = chat.get(inf, min(a for a, _ in ranges), max(b for _, b in ranges)) if camp["tipo"] == "streamer" else []
    log.info("señales: %d mensajes de chat, %d s de audio", len(msgs), int(np.isfinite(aud).sum()))
    ch = detect.chat_signal(msgs, n) if msgs else None
    sc = detect.score(n, ch, aud, sce)
    return sc, detect.muted(aud), len(msgs), ch


def juego_en(inf, t, camp=None):
    """Qué se jugaba en el segundo t: capítulo de Twitch, si no la categoría del video, si no el `juego` de la
    campaña (material de marca: el archivo no lo dice), si no nada."""
    for c in inf.get("chapters") or []:
        if c.get("start_time", 0) <= t < c.get("end_time", 0):
            return c.get("title") or ""
    return (inf.get("categories") or [""])[0] or (camp or {}).get("juego") or ""


def leer_pico(inf, peak, camp, streamer, juego, work, leer=None, chat_x=None, paciencia=900):
    """El juez lee la ventana alrededor de `peak` (segundos del video). Devuelve su veredicto con tiempos del
    video si aprueba (nota >= judge.MIN), o None."""
    leer = leer or LEER
    lang, req = config.idioma(camp["marca"]), camp["requisitos"]
    f = work / f"lee_{peak}.mp4"
    try:
        a = max(peak - leer[0], 0)
        off = media.section(inf, a, peak + leer[1], f, audio_only=True)
        base = off + max(a - off, 0)   # archivo local (marca, vivo): se lee solo la ventana, no el archivo entero
        ws, _ = transcribe.words(f, lang, start=base - off, dur=sum(leer), model=transcribe.LECTURA,
                                 vocab=req.get("vocabulario"))
        ad = config.adelanto(camp)  # el adelanto suma segundos: el juez corta con ese margen
        reglas = (req.get("reglas_juez") or "") + (f"\n- Names, spelled exactly: {req['vocabulario']}"
                                                    if req.get("vocabulario") else "")
        try:  # ejemplos reales de clips virales (solo streamers; si la biblioteca falla, se juzga igual)
            ej = viral.ejemplos() if camp["tipo"] == "streamer" else None
        except Exception as e:
            log.warning("biblioteca viral no disponible: %s", str(e)[:100])
            ej = None
        v = judge.juzgar(ws, peak - base, req["duracion_min"], req["duracion_max"] - ad, lang, streamer, juego,
                         camp["tipo"], limite=peak + leer[1] - base, reglas=reglas.strip() or None, adelanto=bool(ad),
                         chat_x=chat_x, ejemplos=ej, paciencia=paciencia)
    finally:
        f.unlink(missing_ok=True)
    if not v or v["nota"] < judge.MIN:
        log.info("pico %d: no sale (%s)", peak, f"{v['nota']} — {v['motivo']}" if v else "sin clip")
        return None
    log.info("pico %d: %d/100 [%s] %s", peak, v["nota"], v["modelo"], v["motivo"])
    v = v | {"inicio": base + v["inicio"], "fin": base + v["fin"], "remate_t": base + v["remate_t"],
             "remate_fin": base + v["remate_fin"], "peak": peak}
    if camp["tipo"] != "streamer":   # tráileres: el juez de texto alcanza (no hay streamer que reaccione)
        return v
    return mirar_pico(inf, v, a, peak + leer[1], camp, streamer, juego, work, reglas.strip() or None)


def mirar_pico(inf, v, a, b, camp, streamer, juego, work, reglas=None):
    """Gemini MIRA [a, b] (segundos del video) y corrige el veredicto del juez de texto: None si el momento no se ve
    en el tramo; el mismo `v` si no hay ojos (sin clave, sin cupo, error): nunca se pierde un clip por eso."""
    if not vision.clave():
        return v
    req, ad = camp["requisitos"], config.adelanto(camp)
    fv, chico = work / f"ve_{v['peak']}.mp4", work / f"ve_{v['peak']}_360.mp4"
    try:
        off = media.section(inf, a, b, fv, max_h=360)
        vision.preparar(fv, a - off, b - a, chico)
        # ponytail: el adelanto NO va en la propuesta: es justo lo que el texto adivina mal (clip 101), lo eligen los ojos
        propuesta = f'"{v["titulo"] or v["motivo"]}", seconds {v["inicio"] - a:.0f}-{v["fin"] - a:.0f}'
        g = vision.mirar(chico, b - a, v["peak"] - a, propuesta, req["duracion_min"],
                         req["duracion_max"] - ad, ad or 5, streamer, juego, config.idioma(camp["marca"]), reglas)
    except Exception as e:
        log.warning("ojos: no pude preparar el tramo: %s", str(e)[:150])
        g = None
    finally:
        fv.unlink(missing_ok=True)
        chico.unlink(missing_ok=True)
    if g is None:
        return v
    if not g["se_ve"]:
        log.info("pico %d: 👁️ no sale — %s", v["peak"], g["motivo"])
        return None
    log.info("pico %d: 👁️ %s", v["peak"], g["motivo"])
    return v | {"inicio": a + g["inicio"], "fin": a + g["fin"], "remate_t": a + g["remate_ini"],
                "remate_fin": a + g["remate_fin"], "titulo": g["titulo"] or v["titulo"],
                "texto_pantalla": g["texto_pantalla"] or v["texto_pantalla"], "motivo": "👁️ " + g["motivo"],
                "visto": True, "desnudez": [(a + x, a + y) for x, y in g["desnudez"]]}


def ventana_adelanto(remate_t, remate_fin, maximo, minimo=2.0):
    """(inicio, fin) del adelanto: la frase del remate COMPLETA, con un poco de aire, sin silencio delante ni corte a
    media palabra. Si la frase es más larga que `maximo`, se queda con su final (ahí está el golpe).
    17-nov: la ventana fija de 3 s (1,5 s antes del remate) traía silencio y cortaba el grito: "suena acelerado"."""
    a, b = remate_t - 0.3, remate_fin + 0.5
    if b - a > maximo:
        a = b - maximo
    if b - a < minimo:
        a = b - minimo
    return round(a, 2), round(b, 2)


def ventana_vista(ws, a, b, maximo, minimo=None):
    """Adelanto que eligió Gemini MIRANDO (empieza un poco antes de que pase la cosa): se respeta; solo se corre lo
    justo para no cortar palabras a la mitad. ws en segundos del video."""
    for x, y, _ in ws:
        if x < a < y:
            a = x - 0.05
    b = min(b, a + maximo)
    for x, y, _ in ws:
        if x < b < y:
            b = y + 0.05 if y + 0.05 - a <= maximo else x - 0.05
    return round(a, 2), round(b, 2)


def ventana_frase(ws, remate_t, remate_fin, maximo, minimo=2.0):
    """(inicio, fin) del adelanto con la transcripción FINAL (la de los subtítulos, que sí pone puntos): la ÚLTIMA
    oración completa que toca el remate del juez, con aire sin pisar palabras vecinas; si queda corta, más la
    oración anterior ENTERA. ws en segundos del video. 18-nov: la transcripción rápida del juez no puntuaba
    ("nice thanks god oh my god ...") y el adelanto abría con la cola de otra frase: "suena acelerado"."""
    ors = judge.oraciones(ws)
    toca = [k for k, o in enumerate(ors) if o[1] > remate_t and o[0] < remate_fin]
    if not toca:
        return ventana_adelanto(remate_t, remate_fin, maximo, minimo)
    i = j = toca[-1]
    if ors[j][1] - ors[j][0] + 0.8 < minimo and i > 0 and ors[j][1] - ors[i - 1][0] + 0.8 <= maximo:
        i -= 1
    antes = [w[1] for w in ws if w[1] <= ors[i][0]]
    despues = [w[0] for w in ws if w[0] >= ors[j][1]]
    a = min(ors[i][0], max(ors[i][0] - 0.3, antes[-1] + 0.05)) if antes else ors[i][0] - 0.3
    b = max(ors[j][1], min(ors[j][1] + 0.5, despues[0] - 0.05)) if despues else ors[j][1] + 0.5
    if b - a > maximo:  # una oración larguísima (oraciones ya las parte en <= LARGO_MAX s): queda su final
        a = b - maximo
    return round(a, 2), round(b, 2)


def hacer_clip(con, inf, p, camp, video_id, src, streamer, juego, work, t0=0.0, vivo=False):
    """Renderiza el veredicto `p` (tiempos de `inf`), chequea las reglas y lo deja en revisión.
    t0 = desfase para guardar los tiempos (vivo: segundos desde que empezó la grabación). Devuelve el archivo o None."""
    marca, req = camp["marca"], camp["requisitos"]
    lang = config.idioma(marca)
    outdir = config.DATA / "clips" / marca / camp["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    name = f"{marca}_{camp['id']}_v{video_id}_{int(t0 + p['inicio'])}"
    out = outdir / f"{name}.mp4"
    if con.execute("SELECT 1 FROM clips WHERE archivo=?", (str(out),)).fetchone():
        return None  # ya existe de una corrida anterior (y quizás ya lo revisaste): no se pisa
    dur = p["fin"] - p["inicio"]
    f = work / f"{name}.mp4"
    try:
        off = media.section(inf, max(p["inicio"] - 1, 0), p["fin"] + 1, f)
        rel = p["inicio"] - off
        ws, _ = transcribe.words(f, lang, start=rel, dur=dur, vocab=req.get("vocabulario"))
        base = texts.template(p["texto"], lang, streamer, juego, name)
        t = {"titulo": p["titulo"] or base["titulo"], "descripcion": p["descripcion"] or base["descripcion"],
             "hashtags": p["hashtags"] or base["hashtags"], "gancho": p["texto_pantalla"] or base["gancho"]}
        st = render.style_for(name)
        wm = req.get("marca_de_agua")
        ad = config.adelanto(camp)
        adelanto = None
        if ad:  # el momento pico primero: la última frase COMPLETA del remate (ver ventana_frase)
            ventana = ventana_vista if p.get("visto") else ventana_frase   # Gemini lo eligió mirando: manda
            a0, b0 = ventana([(x + p["inicio"], y + p["inicio"], w) for x, y, w in ws], p["remate_t"],
                             p.get("remate_fin", p["remate_t"] + 2.5), ad)
            a0 = max(a0, off)
            adelanto = (round(a0 - off, 3), round(b0 - a0, 3))
        tapar = [(x - off, y - off) for x, y in p.get("desnudez") or []]   # desnudez que vieron los ojos: borroso
        wm = render.render(f, rel, dur, ws, src, st, out, hook=t["gancho"], adelanto=adelanto, cta=req.get("cta_final"),
                           tapar=tapar,
                           watermark=config.ruta(wm) if str(wm or "").lower().endswith(".png") else wm)
    except Exception as e:
        log.warning("render falló en %s: %s", name, str(getattr(e, "stderr", b"") or e)[-300:])
        return None
    finally:
        f.unlink(missing_ok=True)
    cap = config.caption(camp, t["titulo"], t["descripcion"], t["hashtags"])
    clip = media.probe(out) | {"marca": marca, "caption": cap, "titulo": t["titulo"], "marca_de_agua": wm,
                               "camara": bool(src.get("camara"))}
    fallas = config.check(clip, camp)
    if fallas:
        log.warning("clip %s NO sale: %s", name, "; ".join(fallas))
        out.unlink(missing_ok=True)
        return None
    juez = {k: p[k] for k in ("modelo", "gancho", "viral", "motivo")}
    con.execute("INSERT INTO clips(video_id,campaign,marca,inicio,fin,puntaje,archivo,titulo,descripcion,"
                "hashtags,caption,transcripcion,estilo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (video_id, camp["id"], marca, t0 + p["inicio"], t0 + p["fin"], p["nota"], str(out), t["titulo"],
                 t["descripcion"], " ".join(t["hashtags"]), cap, " ".join(w for *_, w in ws),
                 json.dumps(st | {"texto": p["modelo"], "juez": juez, "vivo": vivo, "adelanto": adelanto[1] if adelanto else 0,
                                  # para re-renderizar sin volver a preguntarle al juez (y para la automejora)
                                  "peak": p.get("peak"), "remate": [p.get("remate_t"), p.get("remate_fin")],
                                  "desnudez": p.get("desnudez") or []},
                            ensure_ascii=False)))
    log.info("clip listo %s (%.0fs, %d/100) — %s", out.name, clip["duracion"], p["nota"], t["titulo"])
    return out


def _ya_hecho(con, video_id, p, solape=0.25):
    """¿Ese momento ya tiene clip en este video (revisado, aprobado o rechazado)? Así rehacer no te lo repite."""
    for ini, fin in con.execute("SELECT inicio, fin FROM clips WHERE video_id=?", (video_id,)):
        if min(p["fin"], fin) - max(p["inicio"], ini) > solape * min(p["fin"] - p["inicio"], fin - ini):
            return True
    return False


def _ya_en_vivo(con, camp, inf, p, solape=0.25):
    """¿Este momento del VOD ya salió como clip en vivo? Los directos se guardan como `<canal>#vivo-<unix>`
    con tiempos desde que empezó la grabación; el VOD trae su hora de inicio (YouTube: `release_timestamp`, porque su
    `timestamp` es de horas después; Twitch: `timestamp`)."""
    ts = inf.get("release_timestamp") or inf.get("timestamp")
    if not ts:
        return False
    a, b = ts + p["inicio"], ts + p["fin"]
    for url, ini, fin in con.execute("SELECT v.url, c.inicio, c.fin FROM clips c JOIN videos v ON v.id=c.video_id "
                                     "WHERE c.campaign=? AND v.url LIKE '%#vivo-%'", (camp["id"],)):
        t0 = float(url.rsplit("#vivo-", 1)[1])
        x, y = t0 + ini, t0 + fin
        if min(b, y) - max(a, x) > solape * min(b - a, y - x):
            log.info("momento %.0f-%.0f ya salió en vivo", p["inicio"], p["fin"])
            return True
    return False


def process(con, video, camp, max_clips=MAX_CLIPS):
    """Devuelve cuántos clips quedaron listos para revisión."""
    t0 = time.time()
    inf = media.info(video["url"])
    if inf.get("live_status") in ("is_live", "post_live") or inf.get("is_live"):
        raise Retry("sigue en vivo (o YouTube todavía procesa el directo que terminó)")
    req = camp["requisitos"]
    src = config.source_for(camp, video["url"], inf)
    ranges = allowed_ranges(inf, camp, src)
    if not ranges:
        db.set_video(con, video["id"], "descartado", f"no es de {camp.get('juego_filtro')} (o fuera del tramo)")
        return 0
    work = config.DATA / "work" / str(video["id"])
    work.mkdir(parents=True, exist_ok=True)
    streamer = inf.get("uploader") or inf.get("channel") or ""
    try:
        sc, mute, n_chat, ch = signals(inf, camp, ranges, work)
        dmin, dmax = req["duracion_min"], req["duracion_max"]
        pre, post = detect.window(dmin, dmax)
        if camp["tipo"] == "marca":
            # tráiler (30-90 s): no hay "picos" que valgan (y el margen de 60 s los borraba todos). El juez lee el
            # tráiler entero y elige el mejor tramo cerca de cada ancla: inicio, medio y final -> hasta 3 clips
            # distintos (sirve para "un clip distinto por red").
            d = float(inf["duration"])
            cands = [(int(d * (2 * i + 1) / 6), 0, 0, 0) for i in range(3)]
        else:
            # orgánicas (sin pago): menos picos por VOD para no comerse la cuota del juez de las pagadas
            cands = detect.candidates(sc, camp.get("candidatos", CANDIDATOS), allowed=ranges, pre=pre, post=post,
                                      gap=90, mute=mute)
        log.info("%d picos en %.0f s", len(cands), time.time() - t0)
        # 1) el juez lee ~2,5 min alrededor de cada pico (solo audio + whisper rápido) y decide
        pool = []
        for peak, score, _, _ in cands:
            if not config.activa(camp["id"]):
                log.warning("campaña %s desactivada a mitad de camino: paro", camp["id"])
                break
            try:
                v = leer_pico(inf, peak, camp, streamer, juego_en(inf, peak, camp), work, chat_x=detect.chat_x(ch, peak))
            except Exception as e:
                log.warning("pico %s falló: %s", peak, str(e)[:200])
                continue
            if v:
                pool.append(v)
        pool = [p for p in judge.dedupe(pool) if not _ya_en_vivo(con, camp, inf, p) and not _ya_hecho(con, video["id"], p)]
        # 2) render de los mejores: video solo del tramo elegido + subtítulos con el whisper bueno
        made, max_clips = 0, camp.get("max_clips", max_clips)
        for p in pool:
            if made >= max_clips:
                break
            if not config.activa(camp["id"]):
                log.warning("campaña %s desactivada a mitad de camino: paro", camp["id"])
                break
            made += bool(hacer_clip(con, inf, p, camp, video["id"], src, streamer, juego_en(inf, p["peak"], camp), work))
        db.set_video(con, video["id"], "listo",
                     f"{made} clips de {len(cands)} picos ({len(pool)} aprobados por el juez), {n_chat} msgs de chat, "
                     f"{time.time() - t0:.0f}s")
        return made
    finally:
        shutil.rmtree(work, ignore_errors=True)
