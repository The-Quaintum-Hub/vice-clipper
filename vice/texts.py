"""Textos de respaldo (plantillas variadas) y llamada a Ollama local. El título normal lo escribe el juez.
La transcripción es DATO: va entre delimitadores y el resultado se valida/limpia antes de usarse.
El chat NUNCA llega acá."""
import hashlib
import json
import os
import random
import re
import urllib.request


OLLAMA = os.environ.get("VICE_OLLAMA_URL", "http://127.0.0.1:11434")
LLM = os.environ.get("VICE_LLM", "gemma3:4b")  # fase 0: 13 s por título en caliente, mejor español
LLM_TIMEOUT = int(os.environ.get("VICE_LLM_TIMEOUT", "120"))

BASE_TAGS = {"es": ["#clips", "#parati", "#viral", "#streamer"], "en": ["#clips", "#fyp", "#viral", "#streamer"]}
TEMPLATES = {
    "es": ["“{frase}” 💀", "{streamer} no se lo esperaba 😳", "Esto pasó en el directo de {streamer} 🤯",
           "Lo más loco que verás hoy en {juego} 😭", "{streamer} y el peor momento en {juego} 💀",
           "¿Cómo terminó así? 😂 {streamer}", "Nadie estaba listo para esto 😳", "{streamer}: “{frase}”"],
    "en": ["“{frase}” 💀", "{streamer} did NOT see this coming 😳", "This happened live on {streamer}'s stream 🤯",
           "The craziest {juego} moment today 😭", "{streamer}'s worst moment in {juego} 💀",
           "How did it end like this? 😂 {streamer}", "Nobody was ready for this 😳", "{streamer}: “{frase}”"]}
DESC = {"es": ["Clip del directo de {streamer} jugando {juego}.", "{streamer} en {juego}, momento del stream.",
               "Momentazo de {streamer} en {juego}."],
        "en": ["Clip from {streamer}'s stream playing {juego}.", "{streamer} in {juego}, straight from the stream.",
               "A wild {juego} moment from {streamer}."]}
HOOKS = {"es": ["ESPERA EL FINAL 😳", "NO PUEDE SER 💀", "MIRA ESTO 👀", "ESTO SE DESCONTROLÓ 🤯"],
         "en": ["WAIT FOR IT 😳", "NO WAY 💀", "WATCH THIS 👀", "THIS GOT OUT OF HAND 🤯"]}


def _rng(key):
    return random.Random(int(hashlib.sha256(str(key).encode()).hexdigest()[:12], 16))


def _frase(transcript, r):
    """Frase corta (3-9 palabras) de la transcripción para usar como cita."""
    partes = [p.strip(" ,.¿?¡!") for p in re.split(r"[.?!¡¿]+", transcript) if 3 <= len(p.split()) <= 9]
    return r.choice(partes) if partes else ""


def _tag(t):
    t = re.sub(r"[^\w]", "", str(t).lstrip("#"), flags=re.UNICODE)
    return f"#{t.lower()}" if 1 < len(t) <= 30 else None


def clean_title(t, n=90):
    t = re.sub(r"\s+", " ", re.sub(r"https?://\S+|www\.\S+|@\w+|#\w+", "", str(t))).strip(" \"'")
    return t[:n].rstrip()


def template(transcript, lang, streamer, juego, key):
    r = _rng(key)
    frase = _frase(transcript, r)
    opts = [t for t in TEMPLATES[lang] if frase or "{frase}" not in t]
    f = {"streamer": streamer or ("el streamer" if lang == "es" else "the streamer"),
         "juego": juego or ("el directo" if lang == "es" else "the stream"), "frase": frase}
    return {"titulo": clean_title(r.choice(opts).format(**f)), "descripcion": r.choice(DESC[lang]).format(**f),
            "hashtags": BASE_TAGS[lang] + [x for x in [_tag(juego or "")] if x], "gancho": r.choice(HOOKS[lang]), "fuente_texto": "plantilla",
            "score": None}


def _ollama(prompt, num_predict=160, timeout=None):
    body = json.dumps({"model": LLM, "prompt": prompt, "stream": False, "format": "json", "keep_alive": "30m",
                       "options": {"num_predict": num_predict, "temperature": 0.8, "num_thread": os.cpu_count()}})
    req = urllib.request.Request(f"{OLLAMA}/api/generate", body.encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or LLM_TIMEOUT) as r:
        return json.loads(json.load(r)["response"])


if __name__ == "__main__":
    t = template("no no no se me fue el carro al mar. ayúdenme por favor que me siguen", "es", "MiCanal", "GTA V", 1)
    assert t["titulo"] and len(t["titulo"]) <= 90 and "#gtav" in t["hashtags"], t
    assert template("x", "es", "Y", "", 3)["hashtags"] == BASE_TAGS["es"]  # sin juego: etiquetas genéricas
    assert len({template("x", "en", "Streamer", "GTA V", k)["titulo"] for k in range(12)}) >= 4  # varía
    assert clean_title("Mira esto https://evil.com @alguien #ad 😳") == "Mira esto 😳"  # #Ad va en su línea
    assert _tag("#GTA 6!") == "#gta6" and _tag("#") is None
    print("texts OK", t)
