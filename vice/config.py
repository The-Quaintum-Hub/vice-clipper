"""campaigns.yaml: carga, validación y las reglas verificables que un clip debe cumplir."""
import os
import pathlib
import re
import zlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("VICE_DATA", pathlib.Path.home() / "vice-data"))
CAMPAIGNS = pathlib.Path(os.environ.get("VICE_CAMPAIGNS", ROOT / "campaigns.yaml"))

PAGOS = {"content_rewards", "vyro", "discord", "clipster", "organico"}  # organico = sin pago, para crecer la cuenta
TIPOS = {"streamer", "marca"}


def cuentas():
    """Tus marcas = tus cuentas: {nombre: @usuario} de VICE_CUENTAS en .env ("nombre=@usuario,..."; el usuario es el
    mismo en TikTok/YouTube/Instagram), en orden: la 1ª es la principal. Se lee al usarlo, no al importar, así anda
    igual desde los servicios y desde la terminal. Sin configurar: las del ejemplo."""
    v = os.environ.get("VICE_CUENTAS", "").replace(" ", "").strip()
    return dict(x.split("=", 1) for x in v.split(",") if "=" in x) or {"cuenta_1": "@tu_cuenta_1", "cuenta_2": "@tu_cuenta_2"}


def idioma(marca):
    """Idioma de títulos y subtítulos. ponytail: uno para todas las marcas (VICE_IDIOMA); uno por marca si hace falta."""
    return os.environ.get("VICE_IDIOMA", "en")

REDES = {"tiktok", "youtube", "instagram", "x"}


def load_env(path=ROOT / ".env"):
    """Lee .env (KEY=valor) a os.environ sin pisar lo que ya exista."""
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def ruta(p):
    """Rutas relativas de campaigns.yaml (p.ej. assets/overlay.png) = relativas al repo."""
    return str(p if pathlib.Path(p).is_absolute() else ROOT / p)


def tramo(s):
    """'00:45:50-fin' -> (2750.0, None) · '1:06:07-3:03:10' -> (3967.0, 10990.0). Segundos del VOD."""
    a, _, b = str(s).partition("-")

    def seg(t):
        return sum(float(x) * 60 ** i for i, x in enumerate(reversed(t.strip().split(":"))))
    return seg(a), (None if b.strip().lower() in ("fin", "") else seg(b))


def coincide(camp, texto):
    """¿`texto` (capítulo, categoría o título) es del juego de la campaña? `juego_filtro` = texto o lista; sin filtro, sí."""
    f = camp.get("juego_filtro") or []
    return not f or any(x.lower() in (texto or "").lower() for x in ([f] if isinstance(f, str) else f))


def adelanto(camp):
    """Máximo de segundos del momento pico que abren el clip ("primero qué pasó, después por qué"). 17-nov:
    en TODOS los clips de streamers; hasta 5 s salvo que la campaña diga otra cosa (adelanto: 0 lo apaga). Tráilers: no.
    El adelanto real es la frase del remate completa (pipeline.ventana_adelanto), de 2 s a este máximo."""
    return camp["requisitos"].get("adelanto", 5 if camp["tipo"] == "streamer" else 0) or 0


def _rect_ok(r):
    try:
        x, y, w, h = (float(r[k]) for k in "xywh")
    except (TypeError, KeyError, ValueError):
        return False
    return 0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001


def validate(c):
    """Lista de errores de UNA campaña (vacía = válida)."""
    e = []
    req = c.get("requisitos") or {}
    if not str(c.get("id", "")).strip() or " " in str(c.get("id")):
        e.append("id vacío o con espacios")
    if not isinstance(c.get("prioridad", 0), int):
        e.append("prioridad: un número entero (más alto = sale primero en los turnos de Ghost; GTA = 10)")
    if c.get("plataforma_pago") not in PAGOS:
        e.append(f"plataforma_pago debe ser una de {sorted(PAGOS)}")
    if c.get("tipo") not in TIPOS:
        e.append(f"tipo debe ser {sorted(TIPOS)}")
    if c.get("marca") not in cuentas():
        e.append(f"marca debe ser una de tus cuentas {list(cuentas())} (VICE_CUENTAS en .env)")
    if not isinstance(c.get("activa"), bool):
        e.append("activa debe ser true o false")
    fuentes = c.get("fuentes") or []
    if not fuentes:
        e.append("sin fuentes")
    for i, f in enumerate(fuentes):
        if not isinstance(f, dict) or not f.get("url"):
            e.append(f"fuente {i}: falta url")
            continue
        for k in ("camara", "juego"):
            if f.get(k) is not None and not _rect_ok(f[k]):
                e.append(f"fuente {i}: {k} debe ser {{x,y,w,h}} en fracciones 0-1 dentro del cuadro")
        if f.get("tramo") is not None:
            try:
                a, b = tramo(f["tramo"])
                assert b is None or a < b
            except (ValueError, AssertionError):
                e.append(f"fuente {i}: tramo debe ser 'HH:MM:SS-HH:MM:SS' o 'HH:MM:SS-fin'")
    for k in ("minimo_vistas", "tope_por_clip"):
        if not isinstance(c.get(k), (int, float)) or c[k] < 0:
            e.append(f"{k} debe ser un número >= 0")
    redes = set(c.get("redes") or [])
    if not redes or redes - REDES:
        e.append(f"redes debe ser un subconjunto de {sorted(REDES)}")
    t = c.get("tarifa_por_mil")  # un número, o uno por red: {tiktok: 2.0, instagram: 1.5}
    if not (isinstance(t, (int, float)) and t >= 0 or isinstance(t, dict) and set(t) <= redes
            and all(isinstance(v, (int, float)) and v >= 0 for v in t.values())):
        e.append("tarifa_por_mil: número >= 0, o {red: número} solo con redes de la campaña")
    dmin, dmax = req.get("duracion_min"), req.get("duracion_max")
    if not (isinstance(dmin, (int, float)) and isinstance(dmax, (int, float)) and 3 <= dmin < dmax <= 180):
        e.append("requisitos.duracion_min/duracion_max: números con 3 <= min < max <= 180")
    for k, pre in (("hashtags", "#"), ("menciones", "@")):
        v = req.get(k) or []
        if not isinstance(v, list) or any(not str(x).startswith(pre) or " " in str(x) for x in v):
            e.append(f"requisitos.{k}: lista de textos que empiezan con '{pre}' y sin espacios")
    if not str(req.get("texto") or "").strip():
        e.append("requisitos.texto: pega el texto original de la campaña")
    ad = req.get("adelanto", 5 if c.get("tipo") == "streamer" else 0)
    if not (isinstance(ad, (int, float)) and 0 <= ad <= 5 and isinstance(dmax, (int, float))
            and isinstance(dmin, (int, float)) and dmax - ad > dmin):
        e.append("requisitos.adelanto: segundos 0-5, y duracion_max - adelanto > duracion_min")
    for k in ("candidatos", "max_clips"):
        if c.get(k) is not None and not (isinstance(c[k], int) and c[k] > 0):
            e.append(f"{k}: entero > 0")
    if c.get("vivo") is not None and not isinstance(c["vivo"], bool):
        e.append("vivo: true o false")
    if (c.get("plataforma_pago") == "organico") != str(c.get("id", "")).startswith("organico"):
        e.append("las campañas sin pago van con plataforma_pago: organico e id que empiece con organico")
    div = req.get("divulgacion")
    if div is not None and not (isinstance(div, str) and div.startswith("#") and " " not in div):
        e.append("requisitos.divulgacion: un hashtag, p.ej. '#Ad'")
    hmax = req.get("hashtags_max")
    if hmax is not None and not (isinstance(hmax, int) and hmax >= len(req.get("hashtags") or [])):
        e.append("requisitos.hashtags_max: entero >= cantidad de hashtags obligatorios")
    for k in ("texto_obligatorio", "recordatorios"):
        if not isinstance(req.get(k) or [], list):
            e.append(f"requisitos.{k}: lista de textos")
    rot = req.get("hashtags_rotar")
    if rot is not None and not (isinstance(rot, list) and rot and all(str(x).startswith("#") and " " not in str(x)
                                                                     for x in rot)):
        e.append("requisitos.hashtags_rotar: lista de hashtags (se suman 2 por clip, rotando)")
    if req.get("cta_final") is not None and not (isinstance(req["cta_final"], str) and req["cta_final"].strip()):
        e.append("requisitos.cta_final: texto que aparece al final del clip")
    av = req.get("aviso_vistas")
    if av is not None and not (isinstance(av, dict) and isinstance(av.get("umbral"), int) and av["umbral"] > 0
                               and isinstance(av.get("texto"), str) and av["texto"].strip()):
        e.append("requisitos.aviso_vistas: {umbral: entero > 0, texto: qué hacer al pasarlo}")
    jf = c.get("juego_filtro")
    if jf is not None and not (isinstance(jf, str) and jf or isinstance(jf, list) and jf and all(
            isinstance(x, str) and x for x in jf)):
        e.append("juego_filtro: texto o lista de textos (capítulo, categoría o título que debe contenerlo)")
    wm = str(req.get("marca_de_agua") or "")
    if wm.lower().endswith(".png") and not pathlib.Path(ruta(wm)).exists():
        e.append(f"requisitos.marca_de_agua: no existe {ruta(wm)}")
    return e


def load(path=CAMPAIGNS):
    """Todas las campañas. Lanza ValueError con TODOS los errores si alguna es inválida."""
    if not pathlib.Path(path).exists():
        return []
    camps = (yaml.safe_load(pathlib.Path(path).read_text()) or {}).get("campaigns") or []
    errors, seen = [], set()
    for c in camps:
        cid = c.get("id")
        errors += [f"[{cid}] {m}" for m in validate(c)]
        if cid in seen:
            errors.append(f"[{cid}] id repetido")
        seen.add(cid)
    if errors:
        raise ValueError("campaigns.yaml inválido:\n  " + "\n  ".join(errors))
    return camps


def active(path=CAMPAIGNS):
    return [c for c in load(path) if c["activa"]]


def activa(cid):
    """¿Sigue activa? (si la borraron de campaigns.yaml, tampoco)."""
    return any(c["id"] == cid for c in active())


def get(cid, path=CAMPAIGNS, must_be_active=True):
    for c in load(path):
        if c["id"] == cid:
            if must_be_active and not c["activa"]:
                raise ValueError(f"la campaña {cid} no está activa: no se procesa")
            return c
    raise ValueError(f"no existe la campaña {cid} en {path}")


def _canal(u):
    """'youtube.com/@MiCanal/streams' -> '@micanal'; 'twitch.tv/otro_canal' -> 'otro_canal'; un VOD suelto -> None."""
    m = re.search(r"youtube\.com/(@[\w.-]+)|(?:twitch\.tv|kick\.com)/(?!videos/)([\w-]+)", str(u).lower())
    return (m.group(1) or m.group(2)) if m else None


def source_for(camp, url, inf=None):
    """La fuente de la campaña que corresponde a `url` (para el recorte de cámara). Un VOD llega como
    watch?v=... o twitch.tv/videos/<id>: ahí se reconoce por el canal que lo subió (`inf` de yt-dlp)."""
    def norm(u):
        return u.lower().rstrip("/").replace("://www.", "://").replace("://m.", "://")
    for f in camp["fuentes"]:
        if norm(url).startswith(norm(f["url"])) or norm(f["url"]).startswith(norm(url)):
            return f
    dueno = {_canal(url)} | {str((inf or {}).get(k) or "").lower().lstrip("@") for k in ("uploader_id", "channel_id")}
    dueno |= {"@" + d for d in dueno if d}
    for f in camp["fuentes"]:
        if _canal(f["url"]) and _canal(f["url"]) in dueno:
            return f
    return {"url": url, "camara": camp["fuentes"][0].get("camara") if len(camp["fuentes"]) == 1 else None}


def caption(camp, titulo, descripcion, hashtags):
    """Texto final para pegar en la red: título, descripción, menciones y textos obligatorios; después la
    divulgación (#Ad) sola en su línea y como PRIMER hashtag (regla FTC de las campañas de marca); al final
    los hashtags (obligatorios primero, hasta `hashtags_max`)."""
    req = camp["requisitos"]
    div = req.get("divulgacion")
    tags, low = [], {div.lower()} if div else set()
    rot = list(req.get("hashtags_rotar") or [])
    if rot:  # p.ej. "#canal #gta6 + 1-2 de la lista, rotando": 2 seguidos, distintos según el título
        k = zlib.crc32(titulo.encode()) % len(rot)
        rot = (rot * 2)[k:k + min(2, len(rot))]
    for t in list(req.get("hashtags") or []) + rot + list(hashtags):
        t = "#" + str(t).lstrip("#").replace(" ", "")
        if len(t) > 1 and t.lower() not in low:
            tags.append(t)
            low.add(t.lower())
    if req.get("hashtags_max") is not None:
        tags = tags[:req["hashtags_max"]]
    if camp["tipo"] == "marca":
        descripcion = ""  # marca: una línea genuina basta; más texto = más riesgo de "datos inventados"
    titulo = re.sub(r"\s+", " ", re.sub(r"#\w+", "", titulo)).strip()  # hashtags solo en su línea (p.ej. si lo editaste a mano)
    menciones = " ".join(req.get("menciones") or [])
    hay = f"{titulo} {descripcion}".lower()
    extra = [x for x in req.get("texto_obligatorio") or [] if x.lower() not in hay]
    return "\n".join(x for x in (titulo, descripcion, menciones, *extra, div, " ".join(tags)) if x).strip()


def check(clip, camp):
    """Reglas verificables. Devuelve la lista de fallas; vacía = el clip puede salir."""
    req, fallas = camp["requisitos"], []
    if not camp.get("activa"):
        fallas.append("la campaña ya no está activa")
    if clip.get("marca") != camp["marca"]:
        fallas.append(f"marca del clip {clip.get('marca')} != marca de la campaña {camp['marca']}")
    d = clip.get("duracion") or 0
    if not req["duracion_min"] <= d <= req["duracion_max"]:
        fallas.append(f"duración {d:.1f}s fuera de [{req['duracion_min']}, {req['duracion_max']}]")
    if (clip.get("ancho"), clip.get("alto")) != (1080, 1920):
        fallas.append(f"resolución {clip.get('ancho')}x{clip.get('alto')} != 1080x1920")
    if not clip.get("audio"):
        fallas.append("el clip no tiene audio")
    if req.get("facecam") and not clip.get("camara"):
        fallas.append("la campaña exige la cámara del streamer visible y el clip no la tiene")
    texto = (clip.get("caption") or "").lower()
    for x in list(req.get("hashtags") or []) + list(req.get("menciones") or []) + list(req.get("texto_obligatorio") or []):
        if x.lower() not in texto:
            fallas.append(f"falta {x} en el texto")
    tags = re.findall(r"#\w+", texto)
    div = (req.get("divulgacion") or "").lower()
    if div and (not tags or tags[0] != div or div not in texto.splitlines()):
        fallas.append(f"{req['divulgacion']} debe ser el primer hashtag y estar sola en su línea")
    if req.get("hashtags_max") is not None and len([t for t in tags if t != div]) > req["hashtags_max"]:
        fallas.append(f"más de {req['hashtags_max']} hashtags además de la divulgación")
    if req.get("marca_de_agua") and not clip.get("marca_de_agua"):
        fallas.append("la campaña pide marca de agua y el render no la aplicó")
    if not (clip.get("titulo") or "").strip():
        fallas.append("sin título")
    return fallas


def earnings(camp, vistas, red=None):
    """USD estimados: 0 bajo el mínimo; tarifa por mil (la de esa red si es por red); nunca sobre el tope."""
    t = camp["tarifa_por_mil"]
    t = t.get(red, 0) if isinstance(t, dict) else t
    if vistas < camp["minimo_vistas"]:
        return 0.0
    return round(min(vistas / 1000 * t, camp["tope_por_clip"]), 2)
