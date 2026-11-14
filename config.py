import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "sistema_incendios.db"))
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
MODELS_DIR = Path(os.getenv("MODELS_DIR", BASE_DIR / "models"))
CORPUS_DIR = Path(os.getenv("CORPUS_DIR", BASE_DIR / "corpus"))
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", BASE_DIR / "chroma_db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", BASE_DIR / "backups"))

TZ = ZoneInfo("America/Argentina/Catamarca")

# ---------------------------------------------------------------- Territorio
# Municipio de San Fernando del Valle de Catamarca.
# VERIFICAR: el rectángulo es APROXIMADO. Reemplazar por el límite oficial (IGN / IDE Catamarca) y
# cargarlo en Territorio.limite_geojson; con el polígono real hay que filtrar los focos con él.
TERRITORIO = {
    "id_territorio": 1,
    "nombre": "Municipio de San Fernando del Valle de Catamarca",
    "lat_centro": -28.4696,
    "lon_centro": -65.7852,
    # (oeste, sur, este, norte)
    "bbox": (-65.90, -28.60, -65.65, -28.35),
}

# ---------------------------------------------------------------- Regla de etiqueta y umbrales
# Etiqueta = peligro meteorológico, no ocurrencia de incendio.
REGLA_TEMP_C = 35.0   # temperatura > 35 °C
REGLA_HUM_PCT = 25.0  # humedad < 25 %

UMBRAL_DEFECTO = 0.80
UMBRAL_MIN = 0.50
UMBRAL_MAX = 0.95
HORIZONTE_H = 24

# Serie de entrenamiento del TP4: 36 meses, 01/10/2023 - 30/09/2026
ENTRENAMIENTO_INICIO = os.getenv("ENTRENAMIENTO_INICIO", "2023-10-01")
ENTRENAMIENTO_FIN = os.getenv("ENTRENAMIENTO_FIN", "2026-09-30")

# Criterios de aceptación 
ACEPTACION_F1 = 0.80
ACEPTACION_RECALL = 0.80

# Confirmación de alertas: 30 min, un reenvío
CONFIRMACION_MINUTOS = 30

# Informe (HU-06): latencia máxima 60 s
INFORME_LATENCIA_MAX_S = 60

# Datos "desactualizados" si el último dato correcto tiene más de 24 h 
DATOS_MAX_HORAS = 24

# ---------------------------------------------------------------- Claves / servicios
NASA_KEY = os.getenv("NASA_FIRMS_API_KEY")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")  # VERIFICAR meta-llama/llama-3-8b-instruct:free
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RESEND_KEY = os.getenv("RESEND_API_KEY")
EMAIL_REMITENTE = os.getenv("EMAIL_REMITENTE", "onboarding@resend.dev")

ORGANISMOS = ("Defensa Civil", "Bomberos")


def chat_id_organismo(organismo: str):
    return {
        "Defensa Civil": os.getenv("TELEGRAM_CHAT_ID_DEFENSA_CIVIL"),
        "Bomberos": os.getenv("TELEGRAM_CHAT_ID_BOMBEROS"),
    }[organismo]


def chat_id_admin():
    return os.getenv("TELEGRAM_CHAT_ID_ADMIN")


def lista_env(nombre: str):
    return [x.strip() for x in os.getenv(nombre, "").split(",") if x.strip()]


# ---------------------------------------------------------------- Logging (Seguridad: "disponibilidad")
def configurar_logging():
    raiz = logging.getLogger()
    if getattr(raiz, "_tfg_configurado", False):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    archivo = RotatingFileHandler(LOG_DIR / "sistema.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    archivo.setFormatter(fmt)
    consola = logging.StreamHandler()
    consola.setFormatter(fmt)
    raiz.setLevel(logging.INFO)
    raiz.addHandler(archivo)
    raiz.addHandler(consola)
    raiz._tfg_configurado = True


configurar_logging()
