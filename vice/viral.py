"""Biblioteca viral: clips REALES de TikTok que pegaron (vía Apify) para que el juez calibre qué atrapa.

Presupuesto: los US$5 del plan gratis de Apify repartidos PAREJO en su ciclo mensual (17-nov): cada pasada
gasta solo lo que "toca" a la fecha. La cuenta no tiene tarjeta: si se acaba, se detiene, nunca cobra.
Todo lo que viene de TikTok (textos, transcripciones) es DATO no confiable: se limpia y llega al juez como ejemplo
entre <viral_examples>, nunca como instrucción. Spec: docs/superpowers/specs/2026-11-17-biblioteca-viral-design.md"""
import datetime as dt
import json
import logging
import math
import os
import re
import time
import urllib.request

from . import config, db, media, texts, transcribe

log = logging.getLogger("vice")
APIFY = "https://api.apify.com/v2"
ACTOR = "clockworks~tiktok-scraper"
TOPE = float(os.environ.get("VICE_APIFY_USD", "5"))
POR_VIDEO = 0.0045            # medido 17-nov: 6 videos con descarga = US$0,023 (+ margen)
# cuentas de TikTok que el juez toma como ejemplo, separadas por coma (VICE_VIRAL_SEMILLAS en .env)
SEMILLAS = [x for x in os.environ.get("VICE_VIRAL_SEMILLAS", "").replace(" ", "").split(",") if x]
HASHTAGS = ["twitchclips", "streamerclips", "gamingclips", "fortniteclips", "codclips", "gamingfunny",
            "streamermoments", "gamingmemes"]
MAX_DESCUBIERTAS = 12
_cache = {}

def _api(path, body=None, timeout=60):
    req = urllib.request.Request(APIFY + path, json.dumps(body).encode() if body is not None else None, {
        "Authorization": f"Bearer {os.environ['APIFY_TOKEN']}", "Content-Type": "application/json",
        "User-Agent": "vice/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def presupuesto(lim, ahora=None):
    """USD que se pueden gastar AHORA: el tope × la fracción del ciclo que ya pasó, menos lo ya gastado.
    lim = respuesta de /users/me/limits (data)."""
    ciclo = lim["monthlyUsageCycle"]
    a, b = (dt.datetime.fromisoformat(ciclo[k].replace("Z", "+00:00")) for k in ("startAt", "endAt"))
    ahora = ahora or dt.datetime.now(dt.timezone.utc)
    frac = min(max((ahora - a) / (b - a), 0.0), 1.0)
    tope = min(TOPE, (lim.get("limits") or {}).get("maxMonthlyUsageUsd") or TOPE)
    return round(tope * frac - (lim.get("current") or {}).get("monthlyUsageUsd", 0.0), 4)


def cuentas(con):
    """Semillas + cuentas descubiertas que pegan por sobre sus seguidores (mediana vistas÷seguidores ≥ 1)."""
    filas = con.execute("SELECT cuenta, seguidores, vistas FROM virales WHERE origen LIKE 'hashtag:%' "
                        "AND seguidores >= 10000 AND vistas > 0").fetchall()
    por = {}
    for c, s, v in filas:
        por.setdefault(c, []).append(v / s)
    buenas = sorted(((sorted(r)[len(r) // 2], c) for c, r in por.items() if c not in SEMILLAS),
                    reverse=True)
    return SEMILLAS + [c for m, c in buenas if m >= 1][:MAX_DESCUBIERTAS]


def plan(usd, lista, ronda, vistas):
    """Qué pedir en esta pasada: 2 cuentas + 1 hashtag en rotación. Devuelve [input del actor] (vacío si no alcanza).
    vistas = cuentas que ya se bajaron alguna vez (esas van por lo último, las nuevas por lo más popular)."""
    n = int(usd / POR_VIDEO)
    if n < 6:
        return []
    k = max(3, n // 3)
    elegidas = [lista[(2 * ronda + i) % len(lista)] for i in range(min(2, len(lista)))]
    base = {"resultsPerPage": k, "profileScrapeSections": ["videos"], "shouldDownloadVideos": True,
            "shouldDownloadCovers": False, "shouldDownloadSubtitles": False}
    out = [base | {"profiles": [c], "profileSorting": "latest" if c in vistas else "popular"}
           for c in dict.fromkeys(elegidas)]
    out.append(base | {"hashtags": [HASHTAGS[ronda % len(HASHTAGS)]]})
    return out


def _correr(entrada, espera=900):
    """Lanza el actor y espera a que termine (asíncrono: si una corrida sincrónica se pasa de tiempo, Apify cobra
    igual y los datos se pierden). Devuelve los items."""
    run = _api(f"/acts/{ACTOR}/runs", entrada)["data"]
    fin = time.time() + espera
    while run["status"] in ("READY", "RUNNING") and time.time() < fin:
        time.sleep(10)
        run = _api(f"/actor-runs/{run['id']}")["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"corrida {run['id']}: {run['status']}")
    return _api(f"/datasets/{run['defaultDatasetId']}/items?clean=true")


def _guardar(con, x, origen, trans):
    a = x.get("authorMeta") or {}
    con.execute("INSERT OR REPLACE INTO virales(id,red,cuenta,seguidores,url,vistas,likes,compartidos,comentarios,"
                "duracion,texto,transcripcion,origen,publicado) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(x["id"]), "tiktok", a.get("name"), a.get("fans") or 0, x.get("webVideoUrl"),
                 x.get("playCount") or 0, x.get("diggCount") or 0, x.get("shareCount") or 0,
                 x.get("commentCount") or 0, (x.get("videoMeta") or {}).get("duration") or 0, (x.get("text") or "")[:500],
                 trans, origen, x.get("createTimeISO")))


def _transcribir(x):
    """Baja el video que dejó Apify, lo transcribe en local y lo borra. "" si no se pudo."""
    urls = x.get("mediaUrls") or []
    if not urls:
        return ""
    f = config.DATA / "viral" / f"{x['id']}.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        # el enlace viene de Apify (dato de afuera): solo internet, tope de 300 MB, y la clave solo a api.apify.com
        media.bajar(urls[0], f, headers={"User-Agent": "vice/1.0", **(
            {"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"} if urls[0].startswith(APIFY + "/") else {})})
        ws, _ = transcribe.words(f, "en", dur=90, model=transcribe.LECTURA)
        return " ".join(w for *_, w in ws)[:1500]
    except Exception as e:
        log.warning("viral %s: no pude transcribir: %s", x.get("id"), str(e)[:120])
        return ""
    finally:
        f.unlink(missing_ok=True)


def pasada(con):
    """Una pasada diaria: gasta solo lo que toca a la fecha. Devuelve cuántos clips nuevos guardó."""
    lim = _api("/users/me/limits")["data"]
    usd = presupuesto(lim)
    vistas = {r[0] for r in con.execute("SELECT DISTINCT cuenta FROM virales")}
    ronda = int(db.kv_get(con, "viral:ronda", 0))
    pedidos = plan(usd, cuentas(con), ronda, vistas)
    log.info("viral: presupuesto de hoy US$%.3f (gastado en el ciclo US$%.3f) -> %d pedidos", usd,
             (lim.get("current") or {}).get("monthlyUsageUsd", 0), len(pedidos))
    nuevos = 0
    for p in pedidos:
        origen = f"cuenta:{p['profiles'][0]}" if "profiles" in p else f"hashtag:{p['hashtags'][0]}"
        try:
            items = _correr(p)
        except Exception as e:
            log.warning("viral %s falló: %s", origen, str(e)[:150])
            continue
        for x in items if isinstance(items, list) else []:
            if not x.get("id") or x.get("isAd") or x.get("isSponsored") or x.get("isSlideshow"):
                continue
            ya = con.execute("SELECT transcripcion FROM virales WHERE id=?", (str(x["id"]),)).fetchone()
            _guardar(con, x, origen, ya[0] if ya and ya[0] else _transcribir(x))
            nuevos += not ya
        log.info("viral %s: %d videos", origen, len(items) if isinstance(items, list) else 0)
    db.kv_set(con, "viral:ronda", ronda + 1)
    return nuevos


def _limpio(s, n):
    # dato de TikTok: sin links, menciones, hashtags, ni signos que imiten etiquetas del prompt
    return texts.clean_title(re.sub(r"[<>{}\[\]`]", "", str(s or "")), n)


def ejemplos(con=None, k=6, cada=3600):
    """Los k clips más virales (vistas ÷ seguidores, ≥ 100 mil vistas), máx. 2 por cuenta, con cuenta_a primero
    si hay. Texto listo para el prompt del juez. Se cachea `cada` segundos."""
    if _cache.get("t", 0) > time.time() - cada:
        return _cache["v"]
    con = con or db.connect()
    filas = con.execute("SELECT cuenta, seguidores, vistas, duracion, texto, transcripcion FROM virales "
                        "WHERE vistas >= 100000 AND transcripcion != '' ORDER BY "
                        "(cuenta IN (%s)) DESC, CAST(vistas AS REAL) / MAX(seguidores, 1000) DESC" %
                        ",".join("?" * len(SEMILLAS)), SEMILLAS).fetchall()
    out, por = [], {}
    for c, s, v, d, t, tr in filas:
        if por.get(c, 0) >= 2 or len(out) >= k:
            continue
        por[c] = por.get(c, 0) + 1
        x = v / max(s or 0, 1000)
        primeras = " ".join(_limpio(tr, 400).split()[:30])
        out.append(f'- {v / 1e6:.1f}M views ({x:.0f}x the account\'s followers), {d:.0f}s. Caption: "{_limpio(t, 90)}". '
                   f'Opens with: "{primeras}"')
    _cache.update(t=time.time(), v="\n".join(out))
    return _cache["v"]


if __name__ == "__main__":  # self-check sin red
    SEMILLAS[:] = ["cuenta_a"]                                               # no depende del .env
    lim = {"monthlyUsageCycle": {"startAt": "2026-11-17T00:00:00.000Z", "endAt": "2026-12-16T23:59:59.999Z"},
           "limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": 0.5}}
    t = dt.datetime(2026, 12, 2, tzinfo=dt.timezone.utc)                     # mitad del ciclo
    assert math.isclose(presupuesto(lim, t), 5 * 15 / 30 - 0.5, abs_tol=0.01), presupuesto(lim, t)
    assert presupuesto(lim, dt.datetime(2026, 11, 17, 1, tzinfo=dt.timezone.utc)) < 0   # recién empieza: nada
    assert presupuesto(lim, dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)) == 4.5    # pasado el fin: el resto
    assert plan(0.02, ["cuenta_a"], 0, set()) == []                                        # no alcanza: no se pide
    p = plan(0.17, ["cuenta_a", "a", "b"], 0, {"cuenta_a"})
    assert [x.get("profiles") or x.get("hashtags") for x in p] == [["cuenta_a"], ["a"], [HASHTAGS[0]]], p
    assert p[0]["profileSorting"] == "latest" and p[1]["profileSorting"] == "popular" and p[0]["resultsPerPage"] == 12
    assert [x.get("profiles") for x in plan(0.17, ["cuenta_a", "a", "b"], 1, set())][:2] == [["b"], ["cuenta_a"]]
    con = db.connect(":memory:")
    for i, (c, s, v, tr) in enumerate([("cuenta_a", 722400, 15_400_000, "yo bro {ignore previous} <b>hi</b>"),
                                       ("otro", 20000, 2_000_000, "wait for it"), ("otro", 20000, 1_500_000, "x"),
                                       ("otro", 20000, 900_000, "y"), ("chico", 50000, 40_000, "no")]):
        con.execute("INSERT INTO virales(id,red,cuenta,seguidores,vistas,duracion,texto,transcripcion,origen) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (str(i), "tiktok", c, s, v, 30, "Anime kid voice #cod @x https://t.co",
                                                  tr, "hashtag:x"))
    e = ejemplos(con, cada=0)
    assert e.splitlines()[0].startswith("- 15.4M views (21x") and e.count("\n") == 2, e   # cuenta_a primero; máx 2/otro
    assert "{" not in e and "<" not in e and "#cod" not in e and "https" not in e and "chico" not in e, e
    assert cuentas(con) == ["cuenta_a", "otro"], cuentas(con)
    print("viral OK")
