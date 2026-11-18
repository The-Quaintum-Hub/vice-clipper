"""SQLite: videos en cola, clips producidos y publicaciones (con vistas)."""
import sqlite3

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  id INTEGER PRIMARY KEY,
  campaign TEXT NOT NULL,
  url TEXT NOT NULL,
  titulo TEXT,
  duracion REAL,
  estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente|procesando|listo|error|descartado
  intentos INTEGER NOT NULL DEFAULT 0,
  nota TEXT,
  creado TEXT NOT NULL DEFAULT (datetime('now')),
  actualizado TEXT,
  UNIQUE(campaign, url));
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY,
  video_id INTEGER REFERENCES videos(id),
  campaign TEXT NOT NULL,
  marca TEXT NOT NULL,
  inicio REAL NOT NULL,
  fin REAL NOT NULL,
  puntaje REAL,
  archivo TEXT NOT NULL UNIQUE,
  titulo TEXT,
  descripcion TEXT,
  hashtags TEXT,
  caption TEXT,
  transcripcion TEXT,
  estilo TEXT,
  estado TEXT NOT NULL DEFAULT 'revision',    -- revision|enviado|aprobado|rechazado
  tg_msg INTEGER,
  creado TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS publicaciones(
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  red TEXT NOT NULL,                          -- tiktok|youtube|instagram
  cuenta TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  publicado TEXT NOT NULL DEFAULT (datetime('now')),
  vistas INTEGER NOT NULL DEFAULT 0,
  vistas_fuente TEXT,                         -- youtube_api|manual
  vistas_actualizado TEXT,
  UNIQUE(clip_id, red));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS tareas(                 -- Ghost (vice/ghost.py): lo que hace en el navegador de la marca
  id INTEGER PRIMARY KEY,
  tipo TEXT NOT NULL,                         -- publicar|enviar (a Content Rewards)
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  marca TEXT NOT NULL,
  red TEXT NOT NULL,
  url TEXT,                                   -- enviar: el enlace publicado
  cuando TEXT NOT NULL,                       -- hora LOCAL 'YYYY-MM-DD HH:MM:SS'
  estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente|hecha|saltada|revisar|error
  intentos INTEGER NOT NULL DEFAULT 0,
  resultado TEXT,
  hecha TEXT,                                 -- hora local
  UNIQUE(tipo, clip_id, red));
CREATE TABLE IF NOT EXISTS virales(                -- biblioteca viral (vice/viral.py): datos de TikTok = NO confiables
  id TEXT PRIMARY KEY,
  red TEXT NOT NULL,
  cuenta TEXT,
  seguidores INTEGER,
  url TEXT,
  vistas INTEGER,
  likes INTEGER,
  compartidos INTEGER,
  comentarios INTEGER,
  duracion REAL,
  texto TEXT,
  transcripcion TEXT,
  origen TEXT,                                -- cuenta:<x> | hashtag:<x>
  publicado TEXT,
  visto TEXT NOT NULL DEFAULT (datetime('now')));
"""


def connect(path=None):
    path = path or DATA / "vice.db"
    if path != ":memory:":
        DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; BEGIN explícito donde importa
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def enqueue(con, campaign, url, titulo=None, duracion=None):
    """True si es nuevo."""
    cur = con.execute("INSERT OR IGNORE INTO videos(campaign,url,titulo,duracion) VALUES(?,?,?,?)",
                      (campaign, url, titulo, duracion))
    return cur.rowcount == 1


def claim(con):
    """Toma el video pendiente más antiguo (atómico, por si corren dos workers)."""
    con.execute("BEGIN IMMEDIATE")
    # pagadas primero; y mientras se graba un directo, los orgánicos esperan (la cuota diaria del juez es del directo)
    row = con.execute("SELECT * FROM videos WHERE estado='pendiente' AND (actualizado IS NULL OR actualizado <= "
                      "datetime('now','-20 minutes')) AND NOT (campaign LIKE 'organico%' AND EXISTS "
                      "(SELECT 1 FROM videos WHERE estado='vivo')) ORDER BY (campaign LIKE 'organico%'), id LIMIT 1"
                      ).fetchone()
    if row:
        con.execute("UPDATE videos SET estado='procesando', intentos=intentos+1, actualizado=datetime('now') "
                    "WHERE id=?", (row["id"],))
    con.execute("COMMIT")
    return row


def set_video(con, vid, estado, nota=None):
    con.execute("UPDATE videos SET estado=?, nota=?, actualizado=datetime('now') WHERE id=?", (estado, nota, vid))


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
