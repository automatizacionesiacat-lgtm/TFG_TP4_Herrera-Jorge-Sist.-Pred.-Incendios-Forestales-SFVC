import json
import logging

import config
from db import conectar, ahora, iso, horas_desde, get_umbral, ultima_ingesta_ok

log = logging.getLogger(__name__)
ID_T = config.TERRITORIO["id_territorio"]


def _limite_geojson(conn) -> dict:
    fila = conn.execute("SELECT limite_geojson FROM Territorio WHERE id_territorio = ?", (ID_T,)).fetchone()
    if fila and fila["limite_geojson"]:
        return json.loads(fila["limite_geojson"])
    o, s, e, n = config.TERRITORIO["bbox"]
    # VERIFICAR: rectángulo aproximado; cargar el límite oficial con base_de_datos.cargar_limite_geojson()
    return {"type": "Feature", "properties": {"aproximado": True},
            "geometry": {"type": "Polygon", "coordinates": [[[o, s], [e, s], [e, n], [o, n], [o, s]]]}}


def listar_alertas(conn, desde: str = None, estado: str = None, organismo: str = None, limite: int = 50) -> list:
    """Historial de alertas con el estado de confirmación de cada organismo (HU-14)."""
    sql = ("SELECT a.id_alerta, a.fecha_envio, a.umbral_aplicado, a.estado_global, r.indice_riesgo, r.fecha_objetivo, r.demo "
           "FROM Alerta a JOIN Riesgo r ON r.id_riesgo = a.id_riesgo WHERE 1=1")
    args = []
    if desde:
        sql += " AND substr(a.fecha_envio,1,10) >= ?"
        args.append(desde)
    if estado:
        sql += " AND a.estado_global = ?"
        args.append(estado)
    if organismo:
        sql += " AND EXISTS (SELECT 1 FROM Aviso_Organismo v WHERE v.id_alerta = a.id_alerta AND v.organismo = ?)"
        args.append(organismo)
    sql += " ORDER BY a.fecha_envio DESC LIMIT ?"
    args.append(max(1, min(int(limite), 500)))
    salida = []
    for a in conn.execute(sql, args).fetchall():
        avisos = [dict(v) for v in conn.execute(
            "SELECT organismo, estado, fecha_envio, fecha_confirmacion, reenviado FROM Aviso_Organismo WHERE id_alerta = ? ORDER BY organismo",
            (a["id_alerta"],))]
        salida.append({**dict(a), "avisos": avisos})
    return salida


def construir_datos(conn=None) -> dict:
    propia = conn is None
    if propia:
        ctx = conectar()
        conn = ctx.__enter__()
    try:
        focos = [dict(f) for f in conn.execute(
            "SELECT latitud AS lat, longitud AS lon, fecha_hora_deteccion AS fecha_hora, frp_mw AS frp, confianza "
            "FROM Foco_Calor WHERE id_territorio = ? AND datetime(fecha_hora_deteccion) >= datetime('now','-7 days') "
            "ORDER BY fecha_hora_deteccion DESC LIMIT 2000", (ID_T,))]
        vig = conn.execute(
            "SELECT r.*, v.algoritmo FROM Riesgo r LEFT JOIN Version_Modelo v ON v.id_modelo = r.id_modelo "
            "WHERE r.id_territorio = ? ORDER BY r.fecha_emision DESC, r.demo DESC, r.id_riesgo DESC LIMIT 1", (ID_T,)).fetchone()
        umbral = get_umbral(conn)
        riesgo_vigente = None
        if vig:
            riesgo_vigente = {"indice": vig["indice_riesgo"], "fecha_emision": vig["fecha_emision"], "fecha_objetivo": vig["fecha_objetivo"],
                              "horizonte_h": vig["horizonte_h"], "estado_datos": vig["estado_datos"], "demo": bool(vig["demo"]),
                              "modelo": vig["algoritmo"], "nivel": None if vig["indice_riesgo"] is None else ("ALTO" if vig["indice_riesgo"] >= umbral else "BAJO/MEDIO")}
        riesgos = [dict(r) for r in conn.execute(
            "SELECT fecha_objetivo, indice_riesgo AS indice, demo FROM Riesgo WHERE id_territorio = ? AND indice_riesgo IS NOT NULL "
            "AND fecha_emision >= date('now','-30 days','localtime') ORDER BY fecha_objetivo ASC, demo ASC", (ID_T,))]
        c = conn.execute(
            "SELECT temperatura_c, humedad_pct, viento_kmh, fuente, fecha_hora_obs FROM Clima WHERE id_territorio = ? "
            "ORDER BY fecha_hora_obs DESC LIMIT 1", (ID_T,)).fetchone()
        clima = None
        if c:
            # el viento puede faltar: se informa None y el dashboard muestra «Sin información»
            clima = {"temperatura_c": c["temperatura_c"], "humedad_pct": c["humedad_pct"], "viento_kmh": c["viento_kmh"],
                     "fuente": c["fuente"], "fecha_hora_obs": c["fecha_hora_obs"]}
        t = config.TERRITORIO
        return {
            "generado": iso(ahora()),
            "territorio": {"nombre": t["nombre"], "lat": t["lat_centro"], "lon": t["lon_centro"]},
            "limite": _limite_geojson(conn),
            "umbral": umbral,
            "focos": focos,
            "riesgo_vigente": riesgo_vigente,
            "riesgos": riesgos,
            "clima": clima,
            "antiguedad_horas": {fuente: horas_desde(ultima_ingesta_ok(conn, fuente)) for fuente in ("FIRMS", "Open-Meteo", "OpenWeather")},
            "alertas": listar_alertas(conn, limite=5),
        }
    finally:
        if propia:
            ctx.__exit__(None, None, None)


def generar_json(ruta="datos.json") -> dict:
    datos = construir_datos()
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    print(f"✅ {ruta} actualizado: {len(datos['focos'])} focos, {len(datos['riesgos'])} riesgos, {len(datos['alertas'])} alertas.")
    return datos


if __name__ == "__main__":
    from base_de_datos import inicializar_base_de_datos

    inicializar_base_de_datos()
    generar_json()
