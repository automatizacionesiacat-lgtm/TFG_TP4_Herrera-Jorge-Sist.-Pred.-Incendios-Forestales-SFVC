import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
import logging

import config

log = logging.getLogger(__name__)


@contextmanager
def conectar():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ahora() -> datetime:
    """Hora local de Catamarca (GMT-3), con zona horaria."""
    return datetime.now(config.TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(config.TZ).isoformat(timespec="seconds")


def parse_iso(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt


def horas_desde(texto_iso, ref: datetime = None):
    """Antigüedad en horas de un instante ISO; None si no hay dato."""
    if not texto_iso:
        return None
    ref = ref or ahora()
    return round((ref - parse_iso(texto_iso)).total_seconds() / 3600, 1)


def get_config(conn, clave, defecto=None):
    fila = conn.execute("SELECT valor FROM Config WHERE clave = ?", (clave,)).fetchone()
    return fila["valor"] if fila else defecto


def get_umbral(conn) -> float:
    return float(get_config(conn, "umbral_riesgo", config.UMBRAL_DEFECTO))


def set_umbral(conn, valor: float, usuario: str):
    """HU-08: solo 0,50-0,95; registra usuario y fecha del cambio."""
    if not (config.UMBRAL_MIN <= valor <= config.UMBRAL_MAX):
        raise ValueError(f"El umbral debe estar entre {config.UMBRAL_MIN:.2f} y {config.UMBRAL_MAX:.2f}")
    anterior = get_config(conn, "umbral_riesgo")
    ts = iso(ahora())
    conn.execute(
        "INSERT INTO Config (clave, valor, actualizado_por, fecha) VALUES ('umbral_riesgo', ?, ?, ?) "
        "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, actualizado_por=excluded.actualizado_por, fecha=excluded.fecha",
        (str(valor), usuario, ts),
    )
    conn.execute(
        "INSERT INTO Config_Log (clave, valor_anterior, valor_nuevo, usuario, fecha) VALUES ('umbral_riesgo', ?, ?, ?, ?)",
        (anterior, str(valor), usuario, ts),
    )


def reintentar(funcion, intentos=3, espera=2.0, excepciones=(Exception,)):
    """Reintento con espera creciente (Seguridad: 'reintenta la conexión automáticamente')."""
    ultimo = None
    for n in range(1, intentos + 1):
        try:
            return funcion()
        except excepciones as e:  # noqa: PERF203
            ultimo = e
            log.warning("Intento %s/%s falló: %s", n, intentos, e)
            if n < intentos:
                time.sleep(espera * n)
    raise ultimo


def registrar_ingesta(conn, fuente: str, ok: bool, detalle: str = "", registros: int = 0):
    conn.execute(
        "INSERT INTO Ingesta_Log (fuente, fecha_hora, ok, detalle, registros) VALUES (?,?,?,?,?)",
        (fuente, iso(ahora()), 1 if ok else 0, detalle[:500], registros),
    )


def ultima_ingesta_ok(conn, fuente: str):
    fila = conn.execute(
        "SELECT fecha_hora FROM Ingesta_Log WHERE fuente = ? AND ok = 1 ORDER BY id_ingesta DESC LIMIT 1", (fuente,)
    ).fetchone()
    return fila["fecha_hora"] if fila else None
