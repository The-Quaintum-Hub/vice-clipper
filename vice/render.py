"""Render 9:16 1080x1920 con ffmpeg: juego arriba, cámara abajo (o juego sobre fondo difuminado),
subtítulos palabra a palabra en la zona segura, gancho opcional y marca de agua si la campaña la pide.
Cada clip sale con un estilo distinto (fuente, color, proporción, tamaño, agrupación de palabras)."""
import hashlib
import logging
import os
import pathlib
import random
import re
import struct
import subprocess

W, H = 1080, 1920
# Zona segura común a TikTok / Shorts / Reels: arriba ~230 px de pestañas, abajo ~470 px de descripción
# y botones, a la derecha ~150 px de la columna de íconos. El texto se centra en x=495 (no en 540).
SAFE_X, SAFE_TOP, SAFE_BOTTOM = 495, 230, H - 470
FONTS = ["Montserrat Black", "Montserrat ExtraBold", "Roboto Black", "Open Sans Extrabold", "Ubuntu", "DejaVu Sans"]
COLORS = ["&H0000E6FF", "&H006EFF3D", "&H00FFE629", "&H00D84FFF", "&H001F9AFF"]  # amarillo, verde, cian, rosa, naranjo
VAAPI_DEV = "/dev/dri/renderD128"
# Quick Sync (VAAPI) medido ~3x más rápido que libx264 en la Pavilion (fase 0); libx264 queda de respaldo.
ENCODER = os.environ.get("VICE_ENCODER", "vaapi" if os.path.exists(VAAPI_DEV) else "x264")


def style_for(key):
    """Estilo determinista por clip (mismo clip = mismo estilo; clips distintos = estilos distintos)."""
    r = random.Random(int(hashlib.sha256(str(key).encode()).hexdigest()[:12], 16))
    return {"font": r.choice(FONTS), "size": r.choice([76, 82, 88, 94]), "color": r.choice(COLORS),
            "words": r.choice([2, 3, 4]), "upper": r.random() < 0.6,
            "pop": r.random() < 0.6, "split": r.choice([0.56, 0.6, 0.63, 0.66]), "sep": r.choice([0, 0, 6, 10]),
            "sep_color": r.choice(["white", "black", "0xFFE600", "0x29E6FF"]), "hook": r.random() < 0.55,
            "zoom": r.choice([1.0, 1.12, 1.25]), "sub_at": r.choice(["seam", "seam", "upper"])}


def _ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(s):
    # el texto viene de la transcripción (dato no confiable): fuera llaves y barras = sin tags ASS inyectados
    s = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]", "", s)  # libass no dibuja emojis a color
    return s.replace("{", "").replace("}", "").replace("\\", "").replace("\n", " ").strip()


def _png_wh(path):
    """Ancho y alto de un PNG (cabecera IHDR), sin librerías."""
    with open(path, "rb") as f:
        return struct.unpack(">II", f.read(24)[16:24])


def ass(words, dur, st, sub_y, hook=None, watermark_text=None, hook_y=SAFE_TOP + 90, cta=None):
    """Subtítulos .ass. words = [(ini, fin, palabra)] relativos al inicio del clip."""
    # sin rectángulo de fondo: borde negro grueso + sombra (el look elegido, 17-nov)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{st['font']},{st['size']},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,"
        "1,6,2,5,60,150,0,1",
        f"Style: Hook,{st['font']},58,&H00000000,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,"
        "70,160,0,1",
        "Style: Marca,DejaVu Sans,34,&H60FFFFFF,&H000000FF,&H80000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,7,"
        "60,150,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ws = [(a, b, _clean(w)) for a, b, w in words if b > 0 and a < dur and _clean(w)]
    n = st["words"]
    for c in range(0, len(ws), n):
        chunk = ws[c:c + n]
        nxt = ws[c + n][0] if c + n < len(ws) else dur
        for i, (a, b, _) in enumerate(chunk):
            end = chunk[i + 1][0] if i + 1 < len(chunk) else min(b + 0.35, nxt, dur)
            parts = []
            for j, (_, _, w) in enumerate(chunk):
                w = w.upper() if st["upper"] else w
                if j == i:
                    pop = r"\fscx118\fscy118\t(0,90,\fscx100\fscy100)" if st["pop"] else ""
                    parts.append(f"{{\\c{st['color']}{pop}}}{w}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(w)
            lines.append(f"Dialogue: 1,{_ass_time(a)},{_ass_time(max(end, a + 0.08))},Sub,,0,0,0,,"
                         f"{{\\pos({SAFE_X},{sub_y})}}" + " ".join(parts))
    if hook:
        lines.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(min(2.8, dur))},Hook,,0,0,0,,"
                     f"{{\\pos({SAFE_X},{hook_y})\\fad(0,250)}}{_clean(hook)}")
    if cta:  # llamado final que pide la campaña (p.ej. "Subscribe on YouTube"), los últimos 2,5 s
        lines.append(f"Dialogue: 2,{_ass_time(max(dur - 2.5, 0))},{_ass_time(dur)},Hook,,0,0,0,,"
                     f"{{\\pos({SAFE_X},{hook_y})\\fad(200,0)}}{_clean(cta)}")
    if watermark_text:
        lines.append(f"Dialogue: 3,{_ass_time(0)},{_ass_time(dur)},Marca,,0,0,0,,"
                     f"{{\\pos(60,{SAFE_TOP - 30})}}{_clean(watermark_text)}")
    return "\n".join(lines) + "\n"


def _px(rect, sw, sh):
    r = rect or {"x": 0, "y": 0, "w": 1, "h": 1}
    return r["x"] * sw, r["y"] * sh, r["w"] * sw, r["h"] * sh


def _fit(x, y, w, h, aspect, zoom=1.0):
    """Recorta el rect al aspecto pedido (centrado) y aplica zoom. Devuelve crop w:h:x:y en enteros pares."""
    if w / h > aspect:
        nw, nh = h * aspect, h
    else:
        nw, nh = w, w / aspect
    nw, nh = nw / zoom, nh / zoom
    nx, ny = x + (w - nw) / 2, y + (h - nh) / 2
    ev = lambda v: int(v) // 2 * 2  # noqa: E731
    return f"{ev(nw)}:{ev(nh)}:{ev(nx)}:{ev(ny)}"


def _cara_fija(caras, hint_c, minimo=2):
    """caras = [(cx, cy, alto, cuadro)] de varios cuadros. La del streamer se repite en el MISMO lugar; las del juego
    se mueven. -> (cx, cy) del grupo presente en más cuadros distintos (empate: el más cerca de la cámara
    configurada), o None si ninguno aparece en `minimo` cuadros."""
    mejor, clave = None, None
    for cx, cy, _, _ in caras:
        grupo = [c for c in caras if abs(c[0] - cx) < 40 and abs(c[1] - cy) < 40]
        k = (len({c[3] for c in grupo}), -((cx - hint_c[0]) ** 2 + (cy - hint_c[1]) ** 2))
        if clave is None or k > clave:
            mejor, clave = grupo, k
    if not mejor or clave[0] < minimo:
        return None
    xs, ys = sorted(c[0] for c in mejor), sorted(c[1] for c in mejor)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def find_cam(src_file, t0, dur, hint, n=6):
    """Afina el recorte de cámara buscando la CARA del streamer en TODO el cuadro (la que se repite en el mismo
    lugar). 18-nov: un streamer puso su cámara al otro lado y la búsqueda, que solo miraba cerca de la zona configurada,
    agarraba la cara de un personaje de GTA. Devuelve un rect en fracciones o None (=> se usa el recorte fijo)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    fw, fh = 960, 540
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hx, hy, hw, hh = hint["x"] * fw, hint["y"] * fh, hint["w"] * fw, hint["h"] * fh
    caras = []
    for k in range(n):
        t = t0 + dur * (k + 0.5) / n
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src_file), "-frames:v", "1",
                              "-vf", f"scale={fw}:{fh},format=gray", "-f", "rawvideo", "-"],
                             capture_output=True).stdout
        if len(raw) != fw * fh:
            continue
        img = np.frombuffer(raw, np.uint8).reshape(fh, fw)
        caras += [(x + w / 2, y + h / 2, h, k) for x, y, w, h in cascade.detectMultiScale(img, 1.1, 6, minSize=(22, 22))]
    c = _cara_fija(caras, (hx + hw / 2, hy + hh / 2), minimo=max(2, n // 2))
    if not c:
        return None
    # mismo tamaño que la cámara configurada, centrado en la cara (un poco arriba del centro)
    x = min(max(c[0] - hw / 2, 0), fw - hw)
    y = min(max(c[1] - hh * 0.45, 0), fh - hh)
    return {"x": x / fw, "y": y / fh, "w": hint["w"], "h": hint["h"]}


def guess_cam(grays):
    """Sugerencia de recorte de cámara para un canal nuevo. grays = cuadros 960x540 en gris de VARIOS
    momentos del VOD. La cara del streamer se repite en el mismo lugar; las caras del juego no."""
    import cv2
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits = [(x + w / 2, y + h / 2, h, k) for k, g in enumerate(grays)
            for x, y, w, h in cascade.detectMultiScale(g, 1.1, 6, minSize=(22, 22))]
    best, votos = None, 0
    for cx, cy, h, k in hits:  # la cara presente en más cuadros distintos, en el mismo sitio
        cerca = {k2 for x2, y2, _, k2 in hits if abs(x2 - cx) < 40 and abs(y2 - cy) < 40}
        if len(cerca) > votos:
            best, votos = (cx, cy, h), len(cerca)
    if not best or votos < 2:
        return None
    cx, cy, h = best
    bh = min(h * 3.2, 540)
    bw = min(bh * 1.3, 960)
    x = min(max(cx - bw / 2, 0), 960 - bw)
    y = min(max(cy - bh * 0.45, 0), 540 - bh)
    return {"x": round(x / 960, 3), "y": round(y / 540, 3), "w": round(bw / 960, 3), "h": round(bh / 540, 3),
            "votos": votos}


def layout(sw, sh, src, st, tapar=""):
    """Filtro de video (sin subtítulos) + la altura donde van los subtítulos. `tapar` = expresión de ffmpeg con los
    segundos en que se desenfoca el JUEGO (desnudez que marcaron los ojos); la cámara del streamer no se toca."""
    gx, gy, gw, gh = _px(src.get("juego"), sw, sh)
    # yuv420p fijo: con Quick Sync la cadena negocia nv12 hasta acá y boxblur trata mal ese formato (U y V van
    # intercalados): el juego salía verde. Visto en una prueba el 18-nov.
    borroso = f",format=yuv420p,boxblur=30:4:enable='{tapar}'" if tapar else ""
    if src.get("camara"):
        hg = int(H * st["split"]) // 2 * 2
        hc = H - hg
        cx, cy, cw, ch = _px(src["camara"], sw, sh)
        vf = (f"split=2[g0][c0];[g0]crop={_fit(gx, gy, gw, gh, W / hg, st['zoom'] if st['zoom'] < 1.2 else 1.0)},"
              f"scale={W}:{hg}{borroso}[g];[c0]crop={_fit(cx, cy, cw, ch, W / hc)},scale={W}:{hc}[c];[g][c]vstack")
        if st["sep"]:
            vf += f",drawbox=x=0:y={hg - st['sep'] // 2}:w={W}:h={st['sep']}:color={st['sep_color']}:t=fill"
        sub_y = hg if st["sub_at"] == "seam" else int(hg * 0.72)
    else:  # sin cámara: juego al centro sobre el mismo cuadro difuminado (barato: blur a baja resolución)
        aspect = {1.0: 16 / 9, 1.12: 4 / 3, 1.25: 1.0}[st["zoom"]]
        fh = int(W / aspect) // 2 * 2
        y = (H - fh) // 2 - 120
        vf = (f"split=2[b0][f0];[b0]scale=-2:480,crop=270:480,boxblur=10:2,scale={W}:{H},eq=brightness=-0.08[b];"
              f"[f0]crop={_fit(gx, gy, gw, gh, aspect)},scale={W}:{fh}{borroso}[f];[b][f]overlay=0:{y}")
        sub_y = min(y + fh + 110, SAFE_BOTTOM - 60)
    return vf, max(SAFE_TOP + 200, min(sub_y, SAFE_BOTTOM - 60))


def con_adelanto(words, d0, largo):
    """Subtítulos cuando el clip abre con un adelanto: primero las palabras del tramo del adelanto (que empieza
    `d0` s después del inicio del clip), después TODAS las del clip corridas `largo` s."""
    return ([(round(a - d0, 3), round(b - d0, 3), w) for a, b, w in words if a >= d0 and b <= d0 + largo] +
            [(round(a + largo, 3), round(b + largo, 3), w) for a, b, w in words])


def _tapar(tramos, rel_start, dur, ta, tl):
    """Tramos del ARCHIVO a desenfocar -> `enable` de ffmpeg en el tiempo del clip final (el adelanto va primero:
    un mismo tramo puede caer en el adelanto y en el clip)."""
    out = []
    for x, y in tramos or []:
        for ini, largo, corre in ((ta, tl, 0.0), (rel_start, dur, tl)):
            a, b = max(x, ini), min(y, ini + largo)
            if largo and b > a:
                out.append(f"between(t,{a - ini + corre:.2f},{b - ini + corre:.2f})")
    return "+".join(out)


def render(src_file, rel_start, dur, words, src, st, out, hook=None, watermark=None, fonts_dir=None, adelanto=None,
           cta=None, tapar=None):
    """Renderiza el clip. Devuelve True si se aplicó la marca de agua pedida (o no se pidió).
    adelanto = (inicio en el archivo, largo): ese tramo (el remate) va PRIMERO y después el clip entero
    ("open on the payoff, then show how he got there": formato clutch).
    tapar = [(ini, fin)] en segundos del archivo: el juego sale borroso ahí (desnudez, ver vision.py)."""
    out = pathlib.Path(out)
    ta, tl = adelanto or (0.0, 0.0)
    if tl:
        words = con_adelanto(words, ta - rel_start, tl)
    from .media import probe
    p = probe(src_file)
    sw, sh = p["ancho"], p["alto"]
    if src.get("camara"):
        cara = find_cam(src_file, rel_start, dur, src["camara"])
        st["cam"] = "cara" if cara else "fijo"
        src = src | {"camara": cara} if cara else src
    vf, sub_y = layout(sw, sh, src, st, _tapar(tapar, rel_start, dur, ta, tl))
    wm_png = watermark if watermark and str(watermark).lower().endswith(".png") else None
    wm_txt = watermark if watermark and not wm_png else None
    if wm_png and not pathlib.Path(wm_png).exists():
        raise FileNotFoundError(f"marca de agua no encontrada: {wm_png}")
    # PNG ancho (p.ej. el overlay "twitch.tv/..." que exige una campaña) = cartel grande, opaco, arriba al centro
    # de la zona segura; el gancho baja para no pisarlo. PNG chico = logo discreto arriba a la izquierda.
    pw, ph = _png_wh(wm_png) if wm_png else (0, 1)
    banner = pw / ph > 3
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(ass(words, dur + tl, st, sub_y, hook if st["hook"] else None, wm_txt,
                            hook_y=SAFE_TOP + (230 if banner else 90), cta=cta), encoding="utf-8")
    sub = f"subtitles=filename={ass_path}" + (f":fontsdir={fonts_dir}" if fonts_dir else "")
    inputs = ["-ss", f"{rel_start:.3f}", "-t", f"{dur:.3f}", "-i", str(src_file)]
    if tl:  # el adelanto es la entrada 1 y se pega ANTES del clip (mismo archivo: concat sin reescalar)
        inputs += ["-ss", f"{ta:.3f}", "-t", f"{tl:.3f}", "-i", str(src_file)]
        # fundidos de 50-80 ms en los dos cortes de audio: sin "pop" al saltar del remate al inicio del clip
        head = (f"[1:a:0]afade=t=in:d=0.05,afade=t=out:st={max(tl - 0.08, 0):.3f}:d=0.08[ta];"
                "[0:a:0]afade=t=in:d=0.05[ma];[1:v][ta][0:v][ma]concat=n=2:v=1:a=1[cv][ca];[cv]")
    else:
        head = "[0:v]"
    loud = "[ca]" if tl else "[0:a:0]"
    graph = (f"{head}fps=30,{vf}[v0];[v0]{sub},setsar=1[v1];"
             # marcas de tiempo del audio rehechas por muestra: los cortes del directo (trozos TS de 10 s) dejaban
             # paquetes AAC de 0 y 216 muestras; ffmpeg los toca bien, pero Instagram y el reproductor de Telegram
             # silenciaban ese tramo (18-nov: 2 s mudos en el adelanto del clip 95 que subió Ghost)
             f"{loud}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB[aout]")
    last = "[v1]"
    if wm_png:
        inputs += ["-i", str(wm_png)]
        wm = (f"scale=760:-2,format=rgba[wm];{last}[wm]overlay={SAFE_X - 380}:{SAFE_TOP + 10}" if banner else
              f"scale=200:-2,format=rgba,colorchannelmixer=aa=0.8[wm];{last}[wm]overlay=60:{SAFE_TOP - 40}")
        graph += f";[{2 if tl else 1}:v]{wm}[v2]"
        last = "[v2]"
    maxrate = int(min(8e6, 46e6 * 8 / max(dur + tl, 1)))  # < 50 MB: límite de archivos del bot de Telegram
    for enc in ([ENCODER, "x264"] if ENCODER != "x264" else ["x264"]):  # si Quick Sync falla, libx264
        if enc == "vaapi":
            pre = ["-vaapi_device", VAAPI_DEV]
            tail = f";{last}format=nv12,hwupload[vout]"
            venc = ["-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", str(int(maxrate * 0.8)), "-maxrate", str(maxrate)]
        else:
            pre = []
            tail = f";{last}format=yuv420p[vout]"
            venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", str(maxrate),
                    "-bufsize", str(2 * maxrate)]
        cmd = (["ffmpeg", "-v", "error", "-y"] + pre + inputs + ["-filter_complex", graph + tail, "-map", "[vout]",
               "-map", "[aout]", "-ar", "48000"] + venc +
               ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-r", "30", str(out)])
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
            break
        except subprocess.CalledProcessError as e:
            if enc == "x264":
                raise
            logging.getLogger("vice").warning("Quick Sync falló, uso libx264 (3x más lento): %s",
                                              e.stderr.decode(errors="replace")[-200:])
    ass_path.unlink(missing_ok=True)
    return bool(watermark) == bool(wm_png or wm_txt)


if __name__ == "__main__":  # self-check del .ass: sin tags inyectados, palabra activa resaltada, zona segura
    st = style_for("x") | {"words": 2, "upper": False, "pop": False}
    a = ass([(0.0, 0.4, "hola"), (0.5, 0.9, "{\\b1}mundo"), (1.0, 1.4, "que")], 2.0, st, 1100, hook="Gancho")
    c = ass([(0.0, 0.4, "hola")], 10.0, st, 1100, cta="Subscribe on YouTube")
    assert "0:00:07.50,0:00:10.00,Hook" in c and "Subscribe on YouTube" in c, c   # CTA: últimos 2,5 s
    assert "{\\b1}" not in a and "b1mundo" in a, a
    assert a.count("Dialogue: 1,") == 3 and f"\\c{st['color']}}}hola" in a
    assert style_for(1) != style_for(2) and style_for(7) == style_for(7)
    vf, y = layout(1920, 1080, {"camara": {"x": .74, "y": .66, "w": .26, "h": .34}}, style_for(3))
    assert "vstack" in vf and SAFE_TOP < y < SAFE_BOTTOM
    vf, y = layout(1920, 1080, {"camara": None}, style_for(4))
    assert "overlay" in vf and SAFE_TOP < y < SAFE_BOTTOM
    w = [(1.0, 1.5, "go"), (10.0, 10.4, "LETS"), (10.5, 11.0, "GO")]
    assert con_adelanto(w, 9.5, 3.0) == [(0.5, 0.9, "LETS"), (1.0, 1.5, "GO"), (4.0, 4.5, "go"),
                                         (13.0, 13.4, "LETS"), (13.5, 14.0, "GO")]
    assert _tapar([(10, 12)], 5, 20, 9, 3) == "between(t,1.00,3.00)+between(t,8.00,10.00)"   # adelanto y clip
    assert _tapar([(10, 12)], 5, 20, 0, 0) == "between(t,5.00,7.00)" and _tapar([(40, 50)], 5, 20, 0, 0) == ""
    vf, _ = layout(1920, 1080, {"camara": {"x": .74, "y": .66, "w": .26, "h": .34}}, style_for(3), "between(t,1,2)")
    assert "[g0]crop" in vf and ",format=yuv420p,boxblur=30:4:enable='between(t,1,2)'[g]" in vf      # solo el juego
    print("render OK")
