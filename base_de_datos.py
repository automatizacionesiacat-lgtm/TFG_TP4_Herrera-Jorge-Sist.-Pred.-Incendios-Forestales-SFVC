import json
import sqlite3

import config
from db import conectar, iso, ahora

ESQUEMA = """
CREATE TABLE IF NOT EXISTS Territorio (
    id_territorio INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    lat_centro REAL NOT NULL,
    lon_centro REAL NOT NULL,
    limite_geojson TEXT
);

CREATE TABLE IF NOT EXISTS Clima (
    id_clima INTEGER PRIMARY KEY AUTOINCREMENT,
    id_territorio INTEGER NOT NULL REFERENCES Territorio(id_territorio),
    fecha_hora_obs TEXT NOT NULL,            -- ISO 8601, GMT-3 (diario: 'AAAA-MM-DDT00:00:00-03:00')
    temperatura_c REAL,                      -- °C
    humedad_pct REAL,                        -- %
    viento_kmh REAL,                         -- km/h; NULL = sin información
    fuente TEXT NOT NULL CHECK (fuente IN ('Open-Meteo','OpenWeather')),
    UNIQUE (id_territorio, fuente, fecha_hora_obs)
);

CREATE TABLE IF NOT EXISTS Foco_Calor (
    id_foco INTEGER PRIMARY KEY AUTOINCREMENT,
    id_territorio INTEGER NOT NULL REFERENCES Territorio(id_territorio),
    latitud REAL NOT NULL,
    longitud REAL NOT NULL,
    fecha_hora_deteccion TEXT NOT NULL,      -- ISO 8601, UTC ('...Z')
    frp_mw REAL,
    confianza TEXT CHECK (confianza IN ('baja','nominal','alta')),
    UNIQUE (id_territorio, latitud, longitud, fecha_hora_deteccion)   --no duplicar
);

CREATE TABLE IF NOT EXISTS Version_Modelo (
    id_modelo INTEGER PRIMARY KEY AUTOINCREMENT,
    algoritmo TEXT NOT NULL,
    fecha_entrenamiento TEXT NOT NULL,
    f1_prueba REAL,
    recall_prueba REAL,
    umbral_sugerido REAL,
    metricas_json TEXT,
    artefacto TEXT,
    activo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS Riesgo (
    id_riesgo INTEGER PRIMARY KEY AUTOINCREMENT,
    id_territorio INTEGER NOT NULL REFERENCES Territorio(id_territorio),
    id_modelo INTEGER REFERENCES Version_Modelo(id_modelo),
    id_clima INTEGER REFERENCES Clima(id_clima),
    fecha_emision TEXT NOT NULL,             -- AAAA-MM-DD (día t)
    fecha_objetivo TEXT NOT NULL,            -- emisión + 1 día
    horizonte_h INTEGER NOT NULL DEFAULT 24,
    indice_riesgo REAL CHECK (indice_riesgo IS NULL OR (indice_riesgo >= 0 AND indice_riesgo <= 1)),
    estado_datos TEXT NOT NULL CHECK (estado_datos IN ('completo','clima_ausente','foco_ausente','desactualizado')),
    demo INTEGER NOT NULL DEFAULT 0,
    UNIQUE (id_territorio, fecha_emision, demo)
);

CREATE TABLE IF NOT EXISTS Alerta (
    id_alerta INTEGER PRIMARY KEY AUTOINCREMENT,
    id_riesgo INTEGER NOT NULL REFERENCES Riesgo(id_riesgo),
    fecha_envio TEXT NOT NULL,
    umbral_aplicado REAL NOT NULL CHECK (umbral_aplicado >= 0.50 AND umbral_aplicado <= 0.95),
    estado_global TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado_global IN ('pendiente','confirmada','sin_confirmar'))
);

CREATE TABLE IF NOT EXISTS Aviso_Organismo (
    id_aviso INTEGER PRIMARY KEY AUTOINCREMENT,
    id_alerta INTEGER NOT NULL REFERENCES Alerta(id_alerta),
    organismo TEXT NOT NULL CHECK (organismo IN ('Defensa Civil','Bomberos')),
    fecha_envio TEXT NOT NULL,
    fecha_confirmacion TEXT,
    estado TEXT NOT NULL DEFAULT 'enviado' CHECK (estado IN ('enviado','confirmado','sin_confirmar')),
    chat_id TEXT,
    message_id INTEGER,
    reenviado INTEGER NOT NULL DEFAULT 0,
    fecha_reenvio TEXT,
    detalle TEXT,
    UNIQUE (id_alerta, organismo)
);

CREATE TABLE IF NOT EXISTS Informe (
    id_informe INTEGER PRIMARY KEY AUTOINCREMENT,
    id_territorio INTEGER NOT NULL REFERENCES Territorio(id_territorio),
    id_modelo INTEGER REFERENCES Version_Modelo(id_modelo),
    periodo_inicio TEXT NOT NULL,
    periodo_fin TEXT NOT NULL,
    fecha_generacion TEXT NOT NULL,
    contenido_texto TEXT NOT NULL,
    estado_revision TEXT NOT NULL CHECK (estado_revision IN ('borrador','aprobado','rechazado')),
    -- AJUSTAR TEXTO: se agrega 'rebotado' al dominio de la Tabla 18 (lo menciona: "aceptado, entregado o rebotado").
    estado_entrega TEXT NOT NULL DEFAULT 'no_enviado'
        CHECK (estado_entrega IN ('no_enviado','aceptado_por_proveedor','entregado','rebotado','confirmado_por_destinatario')),
    resend_id TEXT,
    modelo_llm TEXT,
    motivo TEXT,
    latencia_s REAL,
    confirmado_por TEXT,                     -- HQuién confirmó la recepción (rol Autoridad)
    fecha_confirmacion TEXT
);

CREATE TABLE IF NOT EXISTS Informe_Fuente (
    id_fuente INTEGER PRIMARY KEY AUTOINCREMENT,
    id_informe INTEGER NOT NULL REFERENCES Informe(id_informe),
    tipo TEXT NOT NULL CHECK (tipo IN ('datos','documento')),
    referencia TEXT NOT NULL,
    texto TEXT,
    version_documento TEXT
);

CREATE TABLE IF NOT EXISTS Usuario (
    id_usuario INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL UNIQUE,
    rol TEXT NOT NULL CHECK (rol IN ('administrador','defensa_civil','bomberos','autoridad')),
    hash_clave TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Config (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    actualizado_por TEXT,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS Config_Log (
    id_log INTEGER PRIMARY KEY AUTOINCREMENT,
    clave TEXT NOT NULL,
    valor_anterior TEXT,
    valor_nuevo TEXT NOT NULL,
    usuario TEXT NOT NULL,
    fecha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Ingesta_Log (
    id_ingesta INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente TEXT NOT NULL,
    fecha_hora TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detalle TEXT,
    registros INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_foco_fecha ON Foco_Calor (fecha_hora_deteccion);
CREATE INDEX IF NOT EXISTS idx_clima_fecha ON Clima (fecha_hora_obs);
"""


def _verificar_base_antigua(conn):
    """La base del prototipo viejo (tablas Focos_Calor, Riesgos...) es incompatible con el DER nuevo."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(Clima)")]
    if cols and "id_territorio" not in cols:
        raise SystemExit(
            "La base existente usa el esquema anterior (sin id_territorio). "
            f"Renombrá o borrá {config.DB_PATH} y volvé a ejecutar para crear el esquema del TP4."
        )


def inicializar_base_de_datos():
    with conectar() as conn:
        _verificar_base_antigua(conn)
        conn.executescript(ESQUEMA)
        t = config.TERRITORIO
        conn.execute(
            "INSERT OR IGNORE INTO Territorio (id_territorio, nombre, lat_centro, lon_centro) VALUES (?,?,?,?)",
            (t["id_territorio"], t["nombre"], t["lat_centro"], t["lon_centro"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO Config (clave, valor, actualizado_por, fecha) VALUES ('umbral_riesgo', ?, 'sistema', ?)",
            (str(config.UMBRAL_DEFECTO), iso(ahora())),
        )
    print("✅ Base de datos inicializada correctamente (puede ejecutarse de nuevo sin borrar datos).")


def cargar_limite_geojson(ruta_geojson: str):
    """Carga el polígono oficial del municipio en Territorio.limite_geojson (lo usa el dashboard)."""
    with open(ruta_geojson, encoding="utf-8") as f:
        datos = json.load(f)
    with conectar() as conn:
        conn.execute(
            "UPDATE Territorio SET limite_geojson = ? WHERE id_territorio = ?",
            (json.dumps(datos), config.TERRITORIO["id_territorio"]),
        )
    print("Límite municipal cargado.")


if __name__ == "__main__":
    inicializar_base_de_datos()