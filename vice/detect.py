"""Detección de momentos: picos de chat, de volumen y de cambios de escena -> candidatos [inicio, fin]."""
import re
import subprocess

import numpy as np

from . import media

# Reacciones típicas de chat (ES/EN). Pesan más que un mensaje normal. Solo se usan para contar.
HYPE = re.compile(r"(ja){2,}|j[sa]j[sa]|x+d+\b|\blo+l\b|lma+o|kekw|omegalul|\blul\b|pog|\bclip|wtf|omg|"
                  r"no{3,}|\?{3,}|!{3,}|ksks|😂|🤣|💀|😭|🔥", re.I)
CHAT_LAG = 8  # el chat reacciona ~8 s después de lo que pasa en pantalla


# no son reacciones (un VOD, 17-nov: los picos más grandes eran "!enter" de sorteos, votos "1"/"2"
# y su bot): comandos, números sueltos y bots no cuentan. Es un filtro local; el texto nunca va al juez.
NO_REACCION = re.compile(r"^\s*(!|\d{1,3}\s*$)")
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "soundalerts", "botrixoficial",
        "kofistreambot", "sery_bot", "pokemoncommunitygame"}


def es_bot(autor):
    a = (autor or "").lower()
    return a in BOTS or a.endswith("bot")


def chat_signal(msgs, n, ventana=10):
    """Mensajes por segundo, pero cada PERSONA cuenta una vez por ventana de 10 s: así el spam de emotes de
    unos pocos no parece un momento (idea de stream-clip-finder). Tuplas sin autor (caché vieja) cuentan todas."""
    s, visto = np.zeros(n), set()
    for t, txt, *autor in msgs:
        i = int(t)
        if not 0 <= i < n or NO_REACCION.match(txt or "") or (autor and es_bot(autor[0])):
            continue
        if autor and autor[0]:
            k = (autor[0], i // ventana)
            if k in visto:
                continue
            visto.add(k)
        s[i] += 1 + (1.5 if HYPE.search(txt or "") else 0)
    return s


def chat_x(chat, t, win=20):
    """Cuántas veces más gente escribió en los ~20 s después del momento t (el chat reacciona con atraso) que en un
    tramo normal del stream. Es un NÚMERO: el texto del chat nunca llega al juez. None si no hay chat."""
    if chat is None or chat.sum() <= 20:
        return None
    s = np.convolve(chat, np.ones(win), "full")[:len(chat)]   # s[i] = gente en los win s que terminan en i
    normal = max(float(np.median(s[s > 0])), 1.0)
    a, b = int(t) + win // 2, int(t) + CHAT_LAG + win + win // 2
    return round(float(s[a:b].max()) / normal, 1) if a < len(s) else None


def audio_signal(path, offset, n, rate=8000):
    """Volumen (dB RMS) por segundo, ubicado en la línea de tiempo del VOD."""
    pcm = np.frombuffer(media.pcm(path, rate), dtype=np.int16).astype(np.float32)
    secs = len(pcm) // rate
    rms = np.sqrt((pcm[:secs * rate].reshape(secs, rate) ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms / 32768 + 1e-9)
    s = np.full(n, np.nan)
    a = int(offset)
    s[a:a + secs] = db[:max(0, n - a)]
    return s


def scene_signal(path, offset, n, thr=0.35):
    """Cambios de escena por segundo (ffmpeg, a 320 px para que sea barato)."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf",
                        f"scale=320:-2,select='gt(scene,{thr})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    s = np.zeros(n)
    for m in re.finditer(r"pts_time:([\d.]+)", r.stderr):
        i = int(offset + float(m.group(1)))
        if 0 <= i < n:
            s[i] += 1
    return s


def _z(x, win):
    """Suaviza (media móvil) y normaliza robusto (mediana/MAD). NaN = sin dato = 0."""
    x = np.nan_to_num(np.asarray(x, dtype=float), nan=np.nanmedian(x) if np.isfinite(x).any() else 0)
    if win > 1:
        x = np.convolve(x, np.ones(win) / win, mode="same")
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826 or (x.std() or 1)
    return (x - med) / mad


def muted(audio):
    """Segundos en silencio digital (< -75 dB): VOD silenciado por copyright o sin audio."""
    return np.nan_to_num(audio, nan=0) < -75


def score(n, chat=None, audio=None, scenes=None):
    total, w = np.zeros(n), 0
    if chat is not None and chat.sum() > 20:
        # 17-nov: lo que manda es CUÁNTA gente escribe; el volumen del audio solo desempata
        total += 0.75 * np.roll(_z(chat, 12), -CHAT_LAG)
        w += 0.75
    if audio is not None and np.isfinite(audio).any():
        a = 0.25 if w else 0.7
        total += a * _z(audio, 4)
        w += a
    if scenes is not None and scenes.sum() > 0:
        total += 0.4 * _z(scenes, 8)
        w += 0.4
    return total / (w or 1)


def candidates(sc, k, allowed=None, pre=28, post=14, gap=None, margin=60, mute=None):
    """Top-k picos separados al menos `gap` s. allowed=[(a,b)] limita a esos tramos (p.ej. capítulo de GTA).
    mute = máscara de segundos sin sonido (Twitch silencia música con copyright): esas ventanas no salen.
    Devuelve [(pico, puntaje, inicio, fin)] ordenado por puntaje."""
    n = len(sc)
    sc = sc.copy()
    ok = np.zeros(n, bool)
    for a, b in allowed or [(0, n)]:
        m = int(min(margin, (b - a) / 20))  # VOD largo: fuera intro/cierre; material corto: casi nada
        ok[max(int(a) + m, 0):min(int(b) - m, n)] = True
    sc[~ok] = -np.inf
    gap = gap or 2 * (pre + post)
    out = []
    while len(out) < k:
        p = int(np.argmax(sc))
        if not np.isfinite(sc[p]):
            break
        if mute is not None and mute[max(p - pre, 0):p + post].mean() > 0.1:
            sc[p] = -np.inf
            continue
        out.append((p, float(sc[p]), max(p - pre, 0), min(p + post, n)))
        sc[max(p - gap, 0):p + gap] = -np.inf
    return out


def window(dmin, dmax):
    """(pre, post) alrededor del pico según la duración que pide la campaña (~2/3 antes, ~1/3 después)."""
    target = min(max(42, dmin), dmax)
    return round(target * 0.67), round(target * 0.33)


if __name__ == "__main__":  # self-check: dos picos sintéticos, el chat atrasado 8 s
    n = 3600
    rng = np.random.default_rng(1)
    chat = rng.poisson(2, n).astype(float)
    chat[1008:1020] += 40          # reacción a algo que pasó en ~1000
    chat[2508:2515] += 25
    aud = rng.normal(-30, 2, n)
    aud[998:1004] += 15
    c = candidates(score(n, chat, aud), k=3, allowed=[(0, n)])
    assert abs(c[0][0] - 1003) <= 6, c
    assert abs(c[1][0] - 2503) <= 8, c
    assert candidates(score(n, chat, aud), k=3, allowed=[(1500, n)])[0][0] > 1500
    assert window(15, 60) == (28, 14) and window(10, 30) == (20, 10)
    short = candidates(score(120, None, rng.normal(-30, 2, 120)), k=3, allowed=[(0, 120)], pre=20, post=10)
    assert len(short) >= 2, short
    aud2 = aud.copy()
    aud2[990:1030] = -91  # el momento cae en un tramo silenciado
    c2 = candidates(score(n, chat, aud2), k=3, allowed=[(0, n)], mute=muted(aud2))
    assert all(abs(p - 1003) > 40 for p, *_ in c2), c2
    spam = [(5.0 + i / 10, "KEKW", "uno") for i in range(20)]
    assert chat_signal(spam, 10)[5] == 2.5                      # una persona spameando = 1 mensaje (con hype)
    assert chat_signal([(5.0, "hola", f"p{i}") for i in range(20)], 10)[5] == 20   # 20 personas = 20
    assert chat_signal([(5.0, "hola")] * 3, 10)[5] == 3        # caché vieja sin autor
    ruido = [(5.0, "!enter", f"p{i}") for i in range(30)] + [(5.0, "7", f"q{i}") for i in range(30)] + \
        [(5.0, "@x you have entered", "canalbot"), (5.0, "hola", "Nightbot")]
    assert chat_signal(ruido, 10)[5] == 0                         # sorteo, votos y bots: no son reacciones
    assert chat_signal([(5.0, "W", "a"), (5.0, "100 IQ play", "b")], 10)[5] >= 2
    assert chat_x(chat, 1003) > 4 * chat_x(chat, 500), (chat_x(chat, 1003), chat_x(chat, 500))   # el pico se nota
    assert chat_x(None, 5) is None and chat_x(np.zeros(100), 5) is None
    print("detect OK", [(p, round(s, 1)) for p, s, *_ in c], "chat_x", chat_x(chat, 1003))
