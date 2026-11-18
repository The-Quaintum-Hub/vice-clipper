"""vice — línea de comandos. Todo corre en la Pavilion.

  vice check                         valida campaigns.yaml
  vice clip URL --campaign ID        un VOD -> clips (fase 1)
  vice watch                         una pasada del vigilante: encola lo nuevo de las campañas activas
  vice work [--once]                 procesa la cola (servicio)
  vice frame URL [--t SEG]           foto de un VOD
  vice camara URL                    sugiere la línea `camara:` de campaigns.yaml para ese canal
  vice list [--estado revision]      clips producidos
  vice bot | views | report          fases 3 y 4 (Telegram, vistas, reporte semanal)
  vice viral [--ver]                 biblioteca viral: una pasada diaria de TikTok vía Apify (--ver: ejemplos)
"""
import argparse
import logging
import pathlib
import re
import sys
import time

from . import config, db, media

log = logging.getLogger("vice")
FIRST_SIGHT = 1  # al ver una fuente por primera vez, solo su último VOD (no todo el historial)


def is_single(url):
    return bool(re.search(r"/videos/\w|watch\?v=|youtu\.be/|/shorts/|/live/|drive\.google|\.(mp4|mkv|mov|webm)$",
                          url)) or not url.startswith("http")


def watch(con):
    nuevos = 0
    for camp in config.active():
        for fuente in camp["fuentes"]:
            url = fuente["url"]
            try:
                carpeta = pathlib.Path(url).expanduser()
                if not url.startswith("http") and carpeta.is_dir():  # material de marca que bajas a mano
                    for f in sorted(carpeta.iterdir()):
                        # > 2 min sin cambios: que no tome un archivo a medio copiar
                        if f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and time.time() - f.stat().st_mtime > 120:
                            nuevos += db.enqueue(con, camp["id"], str(f))
                    continue
                if is_single(url):
                    nuevos += db.enqueue(con, camp["id"], url)
                    continue
                key = f"visto:{camp['id']}:{url}"
                vids = media.list_channel(url, n=6)
                if db.kv_get(con, key) is None:  # primera vez: el historial queda registrado, no en cola
                    for v in vids[FIRST_SIGHT:]:
                        if db.enqueue(con, camp["id"], v["url"], v.get("title"), v.get("duration")):
                            con.execute("UPDATE videos SET estado='descartado', nota='historial previo a la campaña' "
                                        "WHERE campaign=? AND url=?", (camp["id"], v["url"]))
                    vids = vids[:FIRST_SIGHT]
                    db.kv_set(con, key, time.time())
                for v in vids:
                    if db.enqueue(con, camp["id"], v["url"], v.get("title"), v.get("duration")):
                        nuevos += 1
                        log.info("encolado [%s] %s — %s", camp["id"], v["url"], (v.get("title") or "")[:60])
            except Exception as e:
                log.warning("fuente %s falló: %s", url, str(e)[:200])
    return nuevos


def work_once(con):
    from . import pipeline
    v = db.claim(con)
    if not v:
        return False
    try:
        camp = config.get(v["campaign"], must_be_active=False)
        if not camp["activa"]:
            db.set_video(con, v["id"], "descartado", "campaña inactiva")
            return True
        log.info("procesando video %s [%s] %s", v["id"], camp["id"], v["url"])
        pipeline.process(con, v, camp)
    except pipeline.Retry as e:
        db.set_video(con, v["id"], "pendiente", str(e))
    except Exception as e:
        log.exception("video %s falló", v["id"])
        # v["intentos"] es el valor previo a este intento: 3 intentos fallidos = error definitivo
        db.set_video(con, v["id"], "error" if v["intentos"] + 1 >= 3 else "pendiente", str(e)[:500])
    return True


def main(argv=None):
    config.load_env()
    ap = argparse.ArgumentParser(prog="vice", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("clip")
    p.add_argument("url")
    p.add_argument("--campaign", required=True)
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--rehacer", action="store_true", help="borra los clips sin revisar de este VOD y lo procesa de nuevo")
    sub.add_parser("watch")
    p = sub.add_parser("work")
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("frame")
    p.add_argument("url")
    p.add_argument("--t", type=float, default=None)
    p = sub.add_parser("camara")
    p.add_argument("url")
    p = sub.add_parser("list")
    p.add_argument("--estado", default=None)
    sub.add_parser("bot")
    sub.add_parser("live")
    p = sub.add_parser("vivo")
    p.add_argument("url")
    p.add_argument("--campaign", required=True)
    sub.add_parser("views")
    sub.add_parser("report")
    p = sub.add_parser("ghost", help="publica lo aprobado y manda el link a Content Rewards (ver vice/ghost.py)")
    p.add_argument("que", nargs="?", choices=["sesiones", "chrome", "encolar"])
    p.add_argument("marca", nargs="?", choices=list(config.cuentas()))
    p = sub.add_parser("viral")
    p.add_argument("--ver", action="store_true", help="solo muestra presupuesto y los ejemplos que ve el juez")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # logs al instante en archivos y en journald
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for ruidoso in ("httpx", "faster_whisper", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    con = db.connect() if a.cmd != "check" else None

    if a.cmd == "check":
        camps = config.load()
        for c in camps:
            print(f"{'ACTIVA ' if c['activa'] else 'inactiva'} {c['id']:<28} {c['tipo']:<8} {c['marca']:<10} "
                  f"{c['plataforma_pago']:<15} {len(c['fuentes'])} fuente(s)")
        print(f"OK: {len(camps)} campañas válidas en {config.CAMPAIGNS}")
    elif a.cmd == "clip":
        from . import pipeline
        camp = config.get(a.campaign)  # falla si no existe o no está activa
        db.enqueue(con, camp["id"], a.url)
        v = con.execute("SELECT * FROM videos WHERE campaign=? AND url=?", (camp["id"], a.url)).fetchone()
        if a.rehacer:
            for c in con.execute("SELECT id, archivo FROM clips WHERE video_id=? AND estado='revision'", (v["id"],)):
                pathlib.Path(c["archivo"]).unlink(missing_ok=True)
                con.execute("DELETE FROM clips WHERE id=?", (c["id"],))
        db.set_video(con, v["id"], "procesando")
        n = pipeline.process(con, v, camp, **({"max_clips": a.max} if a.max else {}))
        for c in con.execute("SELECT id,archivo,titulo,inicio,fin,puntaje FROM clips WHERE video_id=? ORDER BY puntaje "
                             "DESC", (v["id"],)):
            print(f"#{c['id']} {c['fin'] - c['inicio']:.0f}s p={c['puntaje']} {c['titulo']}\n    {c['archivo']}")
        print(f"{n} clips nuevos")
    elif a.cmd == "watch":
        print(f"{watch(con)} videos nuevos en cola")
    elif a.cmd == "work":
        # lo que quedó a medias (reinicio, corte de luz) vuelve a la cola; el chat queda en caché
        n = con.execute("UPDATE videos SET estado='pendiente', actualizado=NULL, nota='retomado tras reinicio' "
                        "WHERE estado='procesando'").rowcount
        if n:
            log.info("%d video(s) a medias vuelven a la cola", n)
        while True:
            if not work_once(con):
                if a.once:
                    break
                time.sleep(60)
    elif a.cmd == "frame":
        inf = media.info(a.url)
        t = a.t if a.t is not None else (inf.get("duration") or 7200) / 2
        out_dir = config.DATA / "frames"
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "tmp.mp4"
        off = media.section(inf, t, t + 12, tmp)
        out = out_dir / f"{inf.get('uploader_id') or inf['id']}_{int(t)}.png"
        media.frame(tmp, max(t - off, 0) + 1, out)
        tmp.unlink(missing_ok=True)
        print(out)
    elif a.cmd == "camara":
        import subprocess

        import numpy as np

        from . import render
        inf = media.info(a.url)
        dur = inf.get("duration") or 3600
        out_dir = config.DATA / "frames"
        out_dir.mkdir(parents=True, exist_ok=True)
        grays, tmp = [], out_dir / "tmp.mp4"
        for k in range(1, 7):  # 6 momentos repartidos en el VOD
            t = dur * k / 7
            off = media.section(inf, t, t + 6, tmp)
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t - off + 1), "-i", str(tmp), "-frames:v", "1",
                                  "-vf", "scale=960:540,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
            if len(raw) == 960 * 540:
                grays.append(np.frombuffer(raw, np.uint8).reshape(540, 960))
            last_t = t - off + 1
        r = render.guess_cam(grays)
        if not r:
            print("No encontré una cara fija: el streamer quizá no usa cámara -> camara: null")
        else:
            png = out_dir / f"camara_{inf.get('uploader_id') or inf['id']}.png"
            box = f"drawbox=x={r['x']}*iw:y={r['y']}*ih:w={r['w']}*iw:h={r['h']}*ih:color=red:t=8"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(last_t), "-i", str(tmp), "-frames:v", "1",
                            "-vf", box, str(png)], check=True)
            print(f"cara en {r['votos']} de {len(grays)} cuadros. Pegá esto en la fuente de campaigns.yaml:")
            print(f"        camara: {{x: {r['x']}, y: {r['y']}, w: {r['w']}, h: {r['h']}}}")
            print(f"Verificá el recuadro rojo en: {png}")
        tmp.unlink(missing_ok=True)
    elif a.cmd == "list":
        q = "SELECT id,campaign,marca,estado,titulo,archivo FROM clips" + (" WHERE estado=?" if a.estado else "")
        for c in con.execute(q + " ORDER BY id", (a.estado,) if a.estado else ()):
            print(f"#{c['id']} [{c['estado']}] {c['marca']}/{c['campaign']} {c['titulo']}\n    {c['archivo']}")
    elif a.cmd == "live":
        from . import live
        live.supervisor(con)
    elif a.cmd == "vivo":
        from . import live
        live.sesion(con, config.get(a.campaign), a.url)  # falla si la campaña no existe o no está activa
    elif a.cmd in ("bot", "views", "report"):
        from . import bot
        {"bot": bot.run, "views": bot.update_views, "report": bot.weekly_report}[a.cmd](con)
    elif a.cmd == "ghost":
        from . import ghost
        if a.que == "chrome":
            ghost.chrome(a.marca)
        elif a.que == "encolar":
            from . import bot
            if not bot.ghost():
                raise SystemExit("Ghost está apagado: falta VICE_GHOST=1 en ~/vice/.env")
            for cid, lineas in ghost.encolar(con):
                print(f"#{cid}", *lineas, sep="\n  ")
        elif a.que == "sesiones":
            for m in [a.marca] if a.marca else config.cuentas():
                try:
                    print(m, ghost.sesiones(m))
                except ghost.Falla as e:
                    print(m, e)
        else:
            ghost.servicio(con)
    elif a.cmd == "viral":
        from . import viral
        if not a.ver:
            print(f"{viral.pasada(con)} clips virales nuevos en la biblioteca")
        lim = viral._api("/users/me/limits")["data"]
        print(f"Apify: gastado US${lim['current']['monthlyUsageUsd']:.3f} de US${viral.TOPE:.0f} · "
              f"disponible hoy US${max(viral.presupuesto(lim), 0):.3f} · "
              f"{con.execute('SELECT COUNT(*) FROM virales').fetchone()[0]} clips en la biblioteca")
        print(viral.ejemplos(con, cada=0) or "(todavía sin ejemplos con 100 mil+ vistas)")


if __name__ == "__main__":
    main()
