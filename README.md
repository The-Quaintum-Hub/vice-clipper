# Vice: clipper automático de streams (GTA 6 y más)

Vice mira los directos y VODs de los streamers que eliges y encuentra los mejores momentos. Los corta en vertical
(9:16) con la cámara del streamer, subtítulos y gancho, y te los manda por Telegram para que los apruebes.
Corre en un PC normal, **solo con CPU**, y usa servicios de IA **gratis** (sin tarjeta).

```
Directo o VOD (Twitch, Kick, YouTube)
  → el chat y el volumen dicen DÓNDE pasó algo (~20 picos)
  → un juez IA lee lo que dijo el streamer y decide si hay un momento completo (nota 0-100)
  → (opcional) Gemini MIRA el momento: descarta lo que no se ve y desenfoca desnudez
  → render vertical: juego arriba, cara del streamer abajo, subtítulos, el remate abre el clip
  → te llega a Telegram con ✅ Aprobar / ❌ Rechazar / ✏️ Título
  → lo publicas tú (o Ghost lo publica por ti)
```

Nada se publica sin tu ✅. Vice no simula vistas ni likes.

---

## Lo que necesitas

| Qué | Detalle |
|-----|---------|
| Un PC con **Ubuntu 24.04** | o Windows 10/11 con **WSL2** (Ubuntu). No necesita tarjeta gráfica. Con Intel Quick Sync renderiza 3 veces más rápido |
| ~20 GB libres | + ~3 GB por cada hora de directo grabado (se borran solos a las 24 h) |
| Cuenta de **Groq** (gratis) | el juez IA: https://console.groq.com/keys |
| Un **bot de Telegram** (gratis) | donde te llegan los clips: créalo hablando con @BotFather |
| *(opcional)* clave de **Gemini** (gratis) | para que el juez vea el video: https://aistudio.google.com/apikey |

---

## La forma más fácil: que un agente de código lo instale

Si no quieres usar la terminal tú mismo, un **agente de código** lo instala por ti: lee este README, ejecuta los
comandos y te pregunta lo que falte. Sirve cualquiera de estos:

| Agente | Precio |
|--------|--------|
| [OpenCode](https://opencode.ai) | gratis (con modelos gratis) |
| [Claude Code](https://claude.com/claude-code) | con suscripción de Claude |
| [Codex](https://openai.com/codex) | con suscripción de ChatGPT |

En Windows, primero instala Ubuntu con `wsl --install` (en PowerShell como administrador) y abre el agente
**dentro de Ubuntu**. Después pégale esto:

```text
Instala Vice en este computador siguiendo el README de https://github.com/The-Quaintum-Hub/vice-clipper
(clónalo en ~/vice). Instala todo lo que pida. Pregúntame mis claves (Groq, Telegram, Gemini si quiero ojos) y
mis cuentas en vez de inventarlas, y guárdalas solo en ~/vice/.env con chmod 600. Ayúdame a crear mi primera
campaña para el streamer que te diga. Al final corre `vice check` y las pruebas, y dime qué falta.
```

Tus claves quedan solo en tu computador. Nunca las pegues en un chat público ni las subas a internet.

## Instalación paso a paso (a mano)

Abre una terminal y copia cada bloque tal cual.

**1. Programas del sistema** (ffmpeg corta el video; Node lo necesita YouTube):

```bash
sudo apt update && sudo apt install -y git python3-venv ffmpeg curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
```

**2. Descarga Vice** (tiene que quedar en `~/vice`):

```bash
git clone https://github.com/The-Quaintum-Hub/vice-clipper.git ~/vice
cd ~/vice
```

**3. Instala sus librerías** en un entorno aparte (tarda unos minutos):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**4. Pon tus claves y tus cuentas.** Copia el ejemplo y complétalo: Groq, Telegram y tus cuentas en
`VICE_CUENTAS` (ponles el nombre que quieras, por ejemplo `VICE_CUENTAS=clips=@miusuario,cortes=@otrousuario`):

```bash
cp .env.example .env && chmod 600 .env
nano .env        # guarda con Ctrl+O, Enter, y sal con Ctrl+X
```

`chmod 600` hace que solo tu usuario pueda leer ese archivo. **Nunca** lo subas a internet.

**5. Crea tu primera campaña.** Una "campaña" es: qué streamer, qué juego y qué reglas debe cumplir el clip.

```bash
cp campaigns.example.yaml campaigns.yaml
nano campaigns.yaml
.venv/bin/python -m vice check      # te dice si falta algo
```

Para GTA 6, lo mínimo es esto (y `activa: true`):

```yaml
  - id: organico-gta6-mi-streamer   # las campañas sin pago empiezan con "organico"
    activa: true
    plataforma_pago: organico         # o content_rewards, vyro... si es una campaña pagada
    tarifa_por_mil: 0                 # USD por 1.000 vistas (en una pagada, lo que diga la campaña)
    minimo_vistas: 0
    tope_por_clip: 0
    tipo: streamer
    marca: cuenta_1                   # una de tus cuentas, como la llamaste en VICE_CUENTAS
    fuentes:
      - url: https://www.twitch.tv/CANAL
        camara: {x: 0.74, y: 0.66, w: 0.26, h: 0.34}   # dónde está la cámara (ver paso 6)
    juego_filtro: [gta, grand theft auto]             # solo partes donde juega GTA
    redes: [tiktok, youtube, instagram]
    requisitos:
      texto: |
        (pega acá las reglas de la campaña)
      duracion_min: 15
      duracion_max: 60
      hashtags: ["#gta6"]
      adelanto: 5                     # el clip abre con el momento cumbre (hasta 5 s)
```

**6. Ubica la cámara del streamer** (Vice te sugiere la línea `camara:` y deja una foto para revisarla):

```bash
.venv/bin/python -m vice camara https://www.twitch.tv/videos/NUMERO_DE_UN_VOD
```

En cada clip, Vice además busca la cara cerca de esa zona y la centra, porque los streamers mueven la cámara.

**7. Prueba con un VOD:**

```bash
.venv/bin/python -m vice clip https://www.twitch.tv/videos/NUMERO --campaign organico-gta6-mi-streamer
```

Los clips quedan en `~/vice-data/clips/`. Para recibirlos en Telegram, abre otra terminal y corre
`.venv/bin/python -m vice bot`.

---

## Dejarlo funcionando solo (24/7)

```bash
bash ~/vice/systemd/install.sh
```

Esto instala el comando `vice` y estos servicios, que arrancan solos aunque reinicies el PC:

| Servicio | Qué hace |
|----------|----------|
| `vice-watch` | cada 15 min revisa si tus streamers subieron VODs nuevos |
| `vice-work` | procesa la cola de VODs (detectar, juzgar, cortar) |
| `vice-live` | graba los directos de tus campañas y saca clips **mientras transmiten** (~3 min después del momento). Guarda el directo entero 24 h y al terminar le hace una pasada final, por si el VOD desaparece |
| `vice-bot` | el bot de Telegram para aprobar |
| `vice-views` / `vice-report` | vistas de tus Shorts y reporte semanal |

Ver cómo va: `systemctl --user status 'vice-*'` · ver lo que hace: `journalctl --user -u vice-work -f`

### Comandos útiles

```bash
vice check                                        # valida campaigns.yaml
vice clip URL_DEL_VOD --campaign ID               # un VOD → clips
vice clip URL_DEL_VOD --campaign ID --rehacer     # repetir (borra solo clips aún sin revisar)
vice vivo URL_DEL_CANAL --campaign ID             # grabar un directo a mano
vice list --estado revision                       # clips esperando tu ✅
vice frame URL_DEL_VOD --t 3600                   # foto de un segundo del VOD
```

---

## Opcionales

**Ojos (Gemini).** Con `GEMINI_API_KEY` en `.env`, después del juez de texto Gemini mira el momento (imagen y
audio). Así descarta clips donde el streamer *habla* de algo que no se ve, elige el instante exacto que abre el
clip y desenfoca la parte del juego si aparece desnudez. Si Gemini falla o se queda sin cupo, el clip sale igual
con el juez de texto.

**Ghost (publicar automático).** Con `VICE_GHOST=1`, Vice abre un Chrome real por cada cuenta (necesita escritorio y
Google Chrome instalado). Inicias sesión **una vez a mano** en TikTok, YouTube e Instagram, y después Ghost publica
lo que apruebas en horarios fijos. Si la campaña es de Content Rewards, también manda el link. Sin Ghost, publicas a
mano y le pegas el link al bot.

**Respaldo local (Ollama).** Si instalas [Ollama](https://ollama.com) con `gemma3:4b`, el juez sigue funcionando
aunque Groq no responda.

---

## Problemas comunes

| Síntoma | Solución |
|---------|----------|
| `Sign in to confirm your age` en YouTube | el directo tiene restricción de edad: inicia sesión en YouTube en un perfil de Chrome y apunta `VICE_YT_COOKIES` a ese perfil (por defecto, el Chrome de Ghost de tu primera cuenta) |
| El render falla con `vaapi` | tu PC no tiene Intel Quick Sync: pon `VICE_ENCODER=x264` en `.env` |
| `HTTP 429` del juez | se acabó el cupo gratis del minuto/día: Vice espera solo y sigue, no cobra nada |
| No llegan clips a Telegram | ¿está `vice-bot` activo? ¿escribiste `/start` a tu bot? ¿`TELEGRAM_CHAT_ID` es el tuyo? |

---

## Cómo está hecho

| Archivo | Qué hace |
|---------|----------|
| `vice/detect.py` | picos por chat (personas distintas, no mensajes) y volumen |
| `vice/judge.py` | juez IA de texto (Groq): ¿hay historia completa?, inicio, remate y fin por oraciones |
| `vice/vision.py` | ojos del juez (Gemini): ¿se ve?, instante del adelanto, desnudez |
| `vice/transcribe.py` | subtítulos con faster-whisper (CPU) |
| `vice/render.py` | vertical 9:16: cámara + juego, subtítulos palabra por palabra, gancho, desenfoque |
| `vice/live.py` | ruta en vivo: Streamlink graba, el chat se lee en vivo, cortes en caliente |
| `vice/pipeline.py` | une todo para un VOD |
| `vice/bot.py` | bot de Telegram: aprobar, rechazar, links, vistas |
| `vice/ghost.py` | publicar con Chrome real (Playwright por CDP) |
| `vice/config.py` | campañas y sus reglas (`vice check`) |

Pruebas: `.venv/bin/python tests/test_core.py` y `python -m vice.render` (también `judge`, `live`, `vision`).

## Reglas de uso

- Usa Vice con streamers y campañas que **permiten** clipear su contenido, y cumple las reglas de cada campaña.
- Nada de vistas, likes ni cuentas falsas: las plataformas de pago por vistas lo detectan y no pagan.
- Un mismo archivo nunca va a dos marcas distintas: cada clip pertenece a una sola cuenta.

Hecho por [The Quaintum Hub](https://thequaintumhub.com).
