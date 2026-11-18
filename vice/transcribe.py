"""faster-whisper solo sobre el tramo candidato. El modelo se carga una vez por proceso."""
import os

import numpy as np

from . import media

# Fase 0 (Pavilion, int8): ES medium RTF 0,78 y mejor que large-v3-turbo (0,87, sin puntuación);
# EN small RTF 0,31 con calidad suficiente (medium 0,92 sin mejora visible; turbo 1,55).
MODELS = {"es": os.environ.get("VICE_WHISPER_ES", "medium"), "en": os.environ.get("VICE_WHISPER_EN", "small")}
LECTURA = os.environ.get("VICE_WHISPER_LECTURA", "small")  # rápido: solo para que el juez entienda la ventana
_models = {}


def _model(name):
    if name not in _models:
        from faster_whisper import WhisperModel
        kw = {"device": "cpu", "compute_type": "int8", "cpu_threads": os.cpu_count() or 4}
        try:  # ya descargado: sin red (no depende de que HuggingFace responda)
            _models[name] = WhisperModel(name, local_files_only=True, **kw)
        except Exception:
            _models[name] = WhisperModel(name, **kw)
    return _models[name]


def words(path, lang, start=0.0, dur=None, model=None, vocab=None):
    """[(inicio, fin, palabra)] en segundos RELATIVOS al `start` dentro del archivo, y [(ini, fin)] de frases.
    vocab = nombres propios para que whisper los escriba bien (p.ej. un nombre de personaje raro)."""
    # ponytail: decodificamos con ffmpeg; el decode PyAV de faster-whisper 1.2.1 choca con av>=16
    audio = np.frombuffer(media.pcm(path, 16000, start, dur), np.int16).astype(np.float32) / 32768
    segs, _ = _model(model or MODELS[lang]).transcribe(audio, language=lang, beam_size=1, vad_filter=True,
                                                word_timestamps=True, condition_on_previous_text=False,
                                                initial_prompt=vocab or None)
    ws, frases = [], []
    for s in segs:
        frases.append((s.start, s.end))
        for w in s.words or []:
            ws.append((w.start, w.end, w.word.strip()))
    return ws, frases
