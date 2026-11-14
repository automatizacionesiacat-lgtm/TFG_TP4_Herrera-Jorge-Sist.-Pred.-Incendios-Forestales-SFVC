import argparse
import io
import json
import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

import config
from db import conectar, iso, ahora, reintentar, registrar_ingesta

log = logging.getLogger(__name__)
TIMEOUT = 30
ID_T = config.TERRITORIO["id_territorio"]
MAPA_CONFIANZA = {"l": "baja", "n": "nominal", "h": "alta", "low": "baja", "nominal": "nominal", "high": "alta"}


# ======================================================================= NASA FIRMS
def _url_firms(fuente: str, dias: int, fecha: date = None) -> str:
    o, s, e, n = config.TERRITORIO["bbox"]
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{config.NASA_KEY}/{fuente}/{o},{s},{e},{n}/{dias}"
    return url + (f"/{fecha.isoformat()}" if fecha else "")


def obtener_focos(dias: int = 5, fecha: date = None, fuente: str = "VIIRS_NOAA20_NRT") -> pd.DataFrame:
    """Devuelve los focos del recuadro municipal. Lanza excepción si la API falla (no inventa focos)."""
    if not config.NASA_KEY:
        raise RuntimeError("Falta NASA_FIRMS_API_KEY en el .env")
    res = requests.get(_url_firms(fuente, dias, fecha), timeout=TIMEOUT)
    res.raise_for_status()
    texto = res.text.strip()
    if not texto.startswith("latitude"):
        raise RuntimeError(f"Respuesta inesperada de FIRMS: {texto[:120]}")
    df = pd.read_csv(io.StringIO(texto))
    return df


def _punto_en_poligono(lon: float, lat: float, anillo: list) -> bool:
    """Ray casting. `anillo` es una lista de [lon, lat] (exterior del polígono)."""
    dentro = False
    j = len(anillo) - 1
    for i in range(len(anillo)):
        xi, yi = anillo[i]
        xj, yj = anillo[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def _anillos_limite(conn) -> list:
    """Polígonos del límite municipal cargados`"""
    fila = conn.execute("SELECT limite_geojson FROM Territorio WHERE id_territorio = ?", (ID_T,)).fetchone()
    if not fila or not fila["limite_geojson"]:
        return []
    g = json.loads(fila["limite_geojson"])
    geoms = [f["geometry"] for f in g["features"]] if g.get("type") == "FeatureCollection" else [g.get("geometry", g)]
    anillos = []
    for geo in geoms:
        if geo["type"] == "Polygon":
            anillos.append(geo["coordinates"][0])
        elif geo["type"] == "MultiPolygon":
            anillos += [p[0] for p in geo["coordinates"]]
    return anillos


def _normalizar_focos(df: pd.DataFrame, anillos: list = None) -> list:
    filas = []
    if df.empty:
        return filas
    o, s, e, n = config.TERRITORIO["bbox"]
    for _, f in df.iterrows():
        lat, lon = float(f["latitude"]), float(f["longitude"])
        if anillos:
            if not any(_punto_en_poligono(lon, lat, a) for a in anillos):
                continue
        # VERIFICAR: sin polígono cargado se usa el recuadro aproximado de config.TERRITORIO["bbox"].
        elif not (s <= lat <= n and o <= lon <= e):
            continue
        hhmm = str(int(f["acq_time"])).zfill(4)
        fecha_hora = f"{f['acq_date']}T{hhmm[:2]}:{hhmm[2:]}:00Z"  # UTC, como indica el diccionario de datos
        confianza = MAPA_CONFIANZA.get(str(f.get("confidence", "")).strip().lower())
        frp = float(f["frp"]) if "frp" in f and pd.notna(f["frp"]) else None
        filas.append((ID_T, lat, lon, fecha_hora, frp, confianza))
    return filas


def guardar_focos(conn, df: pd.DataFrame) -> int:
    """Inserta sin duplicar (UNIQUE) y devuelve la cantidad de focos nuevos."""
    nuevos = 0
    for fila in _normalizar_focos(df, _anillos_limite(conn)):
        cur = conn.execute(
            "INSERT OR IGNORE INTO Foco_Calor (id_territorio, latitud, longitud, fecha_hora_deteccion, frp_mw, confianza) "
            "VALUES (?,?,?,?,?,?)",
            fila,
        )
        nuevos += cur.rowcount
    return nuevos


def ingestar_focos() -> int:
    """HU-01. Si la API no responde: reintenta, deja registro del fallo y NO crea focos."""
    print("🛰️ NASA FIRMS (últimos 5 días)...")
    with conectar() as conn:
        try:
            df = reintentar(lambda: obtener_focos(5))
            n = guardar_focos(conn, df)
            registrar_ingesta(conn, "FIRMS", True, f"{len(df)} filas recibidas", n)
            print(f"   ✅ {len(df)} detecciones recibidas, {n} nuevas guardadas.")
            return n
        except Exception as e:
            log.error("Fallo FIRMS: %s", e)
            registrar_ingesta(conn, "FIRMS", False, str(e))
            print(f"   ❌ Error NASA FIRMS: {e} (el dashboard mostrará la antigüedad del último dato)")
            return 0


def ingestar_focos_historico(inicio: str = None, fin: str = None):
    """Carga histórica para construir la variable 'focos en las 72 h previas' del entrenamiento.
    VERIFICAR: NRT solo conserva los últimos meses; para fechas viejas se usa el producto de procesamiento
    estándar (VIIRS_NOAA20_SP). Confirmar disponibilidad y límites de transacciones en la documentación de FIRMS."""
    d = date.fromisoformat(inicio or config.ENTRENAMIENTO_INICIO)
    hasta = date.fromisoformat(fin or config.ENTRENAMIENTO_FIN)
    total = 0
    with conectar() as conn:
        while d <= hasta:
            fuente = "VIIRS_NOAA20_SP" if d < date.today() - timedelta(days=90) else "VIIRS_NOAA20_NRT"
            try:
                df = reintentar(lambda: obtener_focos(5, d, fuente))
                total += guardar_focos(conn, df)
                conn.commit()
            except Exception as e:
                log.error("Histórico FIRMS %s falló: %s", d, e)
                registrar_ingesta(conn, "FIRMS-historico", False, f"{d}: {e}")
            d += timedelta(days=5)
            time.sleep(0.4)  # cortesía con el límite de la API
        registrar_ingesta(conn, "FIRMS-historico", True, f"{inicio}..{fin}", total)
    print(f"   ✅ Histórico de focos: {total} nuevos.")


# ======================================================================= Open-Meteo
_HOURLY = "temperature_2m,relative_humidity_2m,wind_speed_10m"


def _agregar_diario(payload: dict, hasta: date) -> list:
    """Horario -> diario: temperatura máxima, humedad mínima y viento máximo (peor caso de peligro).
    AJUSTAR TEXTO: el TP4 dice 'registros horarios agregados a una observación diaria'; acá se explicita la agregación."""
    h = payload["hourly"]
    df = pd.DataFrame({"t": pd.to_datetime(h["time"]), "temp": h["temperature_2m"],
                       "hum": h["relative_humidity_2m"], "viento": h["wind_speed_10m"]})
    df["dia"] = df["t"].dt.date
    hoy = date.today()
    filas = []
    for dia, g in df.groupby("dia"):
        if dia > hasta:
            continue
        if dia < hoy and g[["temp", "hum"]].dropna().shape[0] < 20:  # día pasado incompleto
            continue
        temp, hum, vto = g["temp"].max(), g["hum"].min(), g["viento"].max()
        temp = float(temp) if pd.notna(temp) and -20 <= temp <= 55 else None
        hum = float(hum) if pd.notna(hum) and 0 <= hum <= 100 else None
        vto = float(vto) if pd.notna(vto) and vto >= 0 else None
        filas.append((dia.isoformat(), temp, hum, vto))
    return filas


def obtener_clima_openmeteo(inicio: date, fin: date) -> list:
    """Serie histórica (archive) hasta ~5 días atrás + ventana reciente (forecast con past_days)."""
    lat, lon = config.TERRITORIO["lat_centro"], config.TERRITORIO["lon_centro"]
    base = {"latitude": lat, "longitude": lon, "hourly": _HOURLY, "timezone": "America/Argentina/Catamarca"}
    filas = {}
    corte_archivo = date.today() - timedelta(days=6)
    if inicio <= corte_archivo:  # histórico
        r = requests.get("https://archive-api.open-meteo.com/v1/archive", timeout=TIMEOUT,
                         params={**base, "start_date": inicio.isoformat(), "end_date": min(fin, corte_archivo).isoformat()})
        r.raise_for_status()
        for f in _agregar_diario(r.json(), min(fin, corte_archivo)):
            filas[f[0]] = f
    if fin > corte_archivo:  # ventana reciente: contexto del informe y día t del cálculo
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=TIMEOUT,
                         params={**base, "past_days": 7, "forecast_days": 1})
        r.raise_for_status()
        for f in _agregar_diario(r.json(), fin):
            if f[0] >= inicio.isoformat():
                filas.setdefault(f[0], f)
    return [filas[k] for k in sorted(filas)]


def guardar_clima_openmeteo(conn, filas: list) -> int:
    for dia, temp, hum, vto in filas:
        conn.execute(
            "INSERT INTO Clima (id_territorio, fecha_hora_obs, temperatura_c, humedad_pct, viento_kmh, fuente) "
            "VALUES (?,?,?,?,?,'Open-Meteo') "
            "ON CONFLICT(id_territorio, fuente, fecha_hora_obs) DO UPDATE SET "
            "temperatura_c=excluded.temperatura_c, humedad_pct=excluded.humedad_pct, viento_kmh=excluded.viento_kmh",
            (ID_T, f"{dia}T00:00:00-03:00", temp, hum, vto),
        )
    return len(filas)


def ingestar_clima_reciente(dias: int = 5) -> int:
    print("☁️ Open-Meteo (ventana reciente)...")
    hoy = date.today()
    with conectar() as conn:
        try:
            filas = reintentar(lambda: obtener_clima_openmeteo(hoy - timedelta(days=dias), hoy))
            n = guardar_clima_openmeteo(conn, filas)
            registrar_ingesta(conn, "Open-Meteo", True, f"{dias} días", n)
            print(f"   ✅ {n} días de clima.")
            return n
        except Exception as e:
            log.error("Fallo Open-Meteo: %s", e)
            registrar_ingesta(conn, "Open-Meteo", False, str(e))
            print(f"   ❌ Error Open-Meteo: {e}")
            return 0


def ingestar_clima_historico(inicio: str = None, fin: str = None) -> int:
    """Serie de entrenamiento (36 meses). Se pide por tramos anuales."""
    d0 = date.fromisoformat(inicio or config.ENTRENAMIENTO_INICIO)
    d1 = date.fromisoformat(fin or config.ENTRENAMIENTO_FIN)
    total = 0
    with conectar() as conn:
        d = d0
        while d <= d1:
            tramo_fin = min(date(d.year + 1, d.month, d.day) - timedelta(days=1), d1)
            filas = reintentar(lambda: obtener_clima_openmeteo(d, tramo_fin))
            total += guardar_clima_openmeteo(conn, filas)
            d = tramo_fin + timedelta(days=1)
        registrar_ingesta(conn, "Open-Meteo-historico", True, f"{d0}..{d1}", total)
    print(f"   ✅ Histórico de clima: {total} días.")
    return total


# ======================================================================= OpenWeather
def obtener_clima_actual_openweather() -> dict:
    if not config.OPENWEATHER_KEY:
        raise RuntimeError("Falta OPENWEATHER_API_KEY en el .env")
    t = config.TERRITORIO
    r = requests.get("https://api.openweathermap.org/data/2.5/weather", timeout=TIMEOUT,
                     params={"lat": t["lat_centro"], "lon": t["lon_centro"], "appid": config.OPENWEATHER_KEY, "units": "metric"})
    r.raise_for_status()
    d = r.json()
    viento = d.get("wind", {}).get("speed")
    return {
        "temperatura_c": d["main"]["temp"],
        "humedad_pct": d["main"]["humidity"],
        "viento_kmh": round(viento * 3.6, 1) if viento is not None else None,  # m/s -> km/h
    }


def ingestar_clima_actual() -> bool:
    print("🌡️ OpenWeather (clima actual)...")
    with conectar() as conn:
        try:
            c = reintentar(obtener_clima_actual_openweather)
            conn.execute(
                "INSERT OR IGNORE INTO Clima (id_territorio, fecha_hora_obs, temperatura_c, humedad_pct, viento_kmh, fuente) "
                "VALUES (?,?,?,?,?,'OpenWeather')",
                (ID_T, iso(ahora()), c["temperatura_c"], c["humedad_pct"], c["viento_kmh"]),
            )
            registrar_ingesta(conn, "OpenWeather", True, "", 1)
            print(f"   ✅ Temp {c['temperatura_c']} °C, Hum {c['humedad_pct']} %, Viento {c['viento_kmh']} km/h")
            return True
        except Exception as e:
            log.error("Fallo OpenWeather: %s", e)
            registrar_ingesta(conn, "OpenWeather", False, str(e))
            print(f"   ❌ Error OpenWeather: {e}")
            return False


def ejecutar():
    ingestar_focos()
    ingestar_clima_reciente()
    ingestar_clima_actual()


if __name__ == "__main__":
    from base_de_datos import inicializar_base_de_datos

    ap = argparse.ArgumentParser()
    ap.add_argument("--historico", action="store_true", help="carga la serie de entrenamiento (36 meses)")
    args = ap.parse_args()
    inicializar_base_de_datos()
    if args.historico:
        ingestar_clima_historico()
        ingestar_focos_historico()
    ejecutar()
