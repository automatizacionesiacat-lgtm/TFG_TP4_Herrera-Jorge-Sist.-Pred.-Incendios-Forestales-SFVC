import json
import logging
import sys
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, precision_score, recall_score, confusion_matrix
from sklearn.model_selection import TimeSeriesSplit

import config
from db import conectar, iso, ahora, horas_desde, ultima_ingesta_ok, set_umbral

log = logging.getLogger(__name__)
FEATURES = ["temp_c", "hum_pct", "viento_kmh", "focos_72h"]
ID_T = config.TERRITORIO["id_territorio"]


# ======================================================================= Datos
def _focos_por_dia(conn) -> pd.Series:
    """Focos por día LOCAL (GMT-3). La detección se guarda en UTC."""
    filas = conn.execute(
        "SELECT substr(datetime(fecha_hora_deteccion, '-3 hours'), 1, 10) AS d, COUNT(*) AS n "
        "FROM Foco_Calor WHERE id_territorio = ? GROUP BY d", (ID_T,)).fetchall()
    return pd.Series({r["d"]: r["n"] for r in filas}, dtype=float)


def construir_dataset(conn) -> pd.DataFrame:
    """Una fila por día t con las entradas de t y la etiqueta de t+1."""
    filas = conn.execute(
        "SELECT substr(fecha_hora_obs,1,10) AS dia, temperatura_c, humedad_pct, viento_kmh "
        "FROM Clima WHERE id_territorio = ? AND fuente = 'Open-Meteo' ORDER BY dia", (ID_T,)).fetchall()
    if not filas:
        return pd.DataFrame(columns=["fecha", *FEATURES, "y"])
    df = pd.DataFrame([dict(f) for f in filas])
    df["fecha"] = pd.to_datetime(df["dia"])
    df = df.set_index("fecha").drop(columns="dia")
    df = df.reindex(pd.date_range(df.index.min(), df.index.max(), freq="D"))  # huecos -> NaN
    df.index.name = "fecha"
    df = df.rename(columns={"temperatura_c": "temp_c", "humedad_pct": "hum_pct", "viento_kmh": "viento_kmh"})

    focos = _focos_por_dia(conn)
    por_dia = pd.Series({pd.Timestamp(k): v for k, v in focos.items()}, dtype=float)
    por_dia = por_dia.reindex(df.index, fill_value=0.0)
    # focos de los 3 días calendario PREVIOS a t (t-3, t-2, t-1): nunca se usa el día t ni el objetivo.
    df["focos_72h"] = por_dia.shift(1).rolling(3, min_periods=1).sum().fillna(0.0)

    regla = (df["temp_c"] > config.REGLA_TEMP_C) & (df["hum_pct"] < config.REGLA_HUM_PCT)
    regla = regla.where(df["temp_c"].notna() & df["hum_pct"].notna())  # NaN si falta algún dato
    df["y"] = regla.shift(-1)  # etiqueta del día siguiente
    df = df.dropna(subset=["temp_c", "hum_pct", "y"]).copy()
    df["y"] = df["y"].astype(int)
    # El viento ausente se deja como nulo: Random Forest (scikit-learn >= 1.4) y XGBoost lo admiten, sin inventar el valor.
    return df.reset_index()


def cortes_cronologicos(df: pd.DataFrame):
    n = len(df)
    i1, i2 = int(n * 0.70), int(n * 0.85)
    return df.iloc[:i1], df.iloc[i1:i2], df.iloc[i2:]


# ======================================================================= Modelos
def _crear_modelos(y_train) -> dict:
    """El desbalance se trata únicamente con los datos de entrenamiento (ponderación de clases)."""
    modelos = {
        "RandomForest": lambda: RandomForestClassifier(
            n_estimators=300, min_samples_leaf=2, class_weight="balanced_subsample", random_state=42, n_jobs=-1),
    }
    try:
        from xgboost import XGBClassifier

        pos = max(int(y_train.sum()), 1)
        neg = max(int((y_train == 0).sum()), 1)
        modelos["XGBoost"] = lambda: XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8, eval_metric="logloss",
            scale_pos_weight=neg / pos, random_state=42, n_jobs=1)
    except ImportError:
        log.warning("xgboost no está instalado: se entrena solo Random Forest (el TP4 prevé la comparación).")
    return modelos


def validacion_cruzada_temporal(fabrica, X, y, n_splits=4) -> dict:
    f1s, recs = [], []
    for tr, va in TimeSeriesSplit(n_splits=n_splits).split(X):
        if y.iloc[tr].sum() == 0 or y.iloc[va].sum() == 0:  # sin positivos no hay F1/Recall definidos
            continue
        m = fabrica().fit(X.iloc[tr], y.iloc[tr])
        p = m.predict(X.iloc[va])
        f1s.append(f1_score(y.iloc[va], p, zero_division=0))
        recs.append(recall_score(y.iloc[va], p, zero_division=0))
    return {"f1": float(np.mean(f1s)) if f1s else float("nan"),
            "recall": float(np.mean(recs)) if recs else float("nan"), "folds_usables": len(f1s)}


def calibrar_umbral(y_true, proba) -> dict:
    """Máximo F1 sujeto a Recall >= 0,80 en el conjunto de AJUSTE. Si no existe, queda 0,80 (valor inicial del TP4)."""
    mejor = None
    if y_true.sum() > 0:
        for u in np.arange(0.05, 0.96, 0.01):
            pred = (proba >= u).astype(int)
            rec = recall_score(y_true, pred, zero_division=0)
            if rec >= config.ACEPTACION_RECALL:
                f1 = f1_score(y_true, pred, zero_division=0)
                if mejor is None or f1 > mejor[1]:
                    mejor = (round(float(u), 2), f1)
    if mejor is None:
        return {"umbral": config.UMBRAL_DEFECTO, "calibrado": False}
    return {"umbral": mejor[0], "calibrado": True, "f1_ajuste": mejor[1]}


def entrenar() -> dict:
    print("🧠 Construyendo dataset desde la base (Open-Meteo + FIRMS)...")
    with conectar() as conn:
        df = construir_dataset(conn)
    if len(df) < 200:
        raise RuntimeError(
            f"Solo hay {len(df)} días utilizables; el TP4 prevé ~1.096. Ejecutá `python ingesta.py --historico` primero.")
    train, ajuste, prueba = cortes_cronologicos(df)
    print(f"   días: entrenamiento={len(train)} ({train.fecha.min().date()}..{train.fecha.max().date()}), "
          f"ajuste={len(ajuste)}, prueba={len(prueba)} ({prueba.fecha.min().date()}..{prueba.fecha.max().date()})")
    print(f"   positivos: train={int(train.y.sum())}, ajuste={int(ajuste.y.sum())}, prueba={int(prueba.y.sum())}")
    if train.y.sum() < 10 or prueba.y.sum() < 5:
        print("   ⚠️ Muy pocos positivos: las métricas serán inestables (declararlo en el TP4).")

    X_tr, y_tr = train[FEATURES], train["y"]
    fabricas = _crear_modelos(y_tr)

    cv = {nombre: validacion_cruzada_temporal(f, X_tr, y_tr) for nombre, f in fabricas.items()}
    for n, r in cv.items():
        print(f"   CV temporal {n}: F1={r['f1']:.3f} Recall={r['recall']:.3f} (folds usables: {r['folds_usables']})")
    # Selección por F1 y luego Recall (los NaN pierden)
    elegido = max(cv, key=lambda k: (np.nan_to_num(cv[k]["f1"], nan=-1), np.nan_to_num(cv[k]["recall"], nan=-1)))
    print(f"   -> modelo elegido: {elegido}")

    modelo = fabricas[elegido]().fit(X_tr, y_tr)
    cal = calibrar_umbral(ajuste["y"], modelo.predict_proba(ajuste[FEATURES])[:, 1])
    umbral = cal["umbral"]
    print(f"   umbral (ajuste): {umbral:.2f} {'(calibrado)' if cal['calibrado'] else '(valor inicial: no hubo umbral con Recall>=0,80)'}")

    proba_te = modelo.predict_proba(prueba[FEATURES])[:, 1]
    pred_te = (proba_te >= umbral).astype(int)
    f1 = f1_score(prueba["y"], pred_te, zero_division=0)
    rec = recall_score(prueba["y"], pred_te, zero_division=0)
    prec = precision_score(prueba["y"], pred_te, zero_division=0)
    cumple = bool(f1 >= config.ACEPTACION_F1 and rec >= config.ACEPTACION_RECALL)
    print(f"   PRUEBA: F1={f1:.3f} Recall={rec:.3f} Precisión={prec:.3f} -> "
          f"{'CUMPLE' if cumple else 'NO CUMPLE'} el protocolo de aceptación (evaluación EXPLORATORIA)")

    # Focos observados el día siguiente en días clasificados de riesgo alto
    prueba = prueba.assign(pred=pred_te)
    focos_sig = _focos_dia_siguiente(prueba)
    control = {"focos_dia_siguiente_en_alto": float(focos_sig[prueba.pred == 1].mean()) if (prueba.pred == 1).any() else None,
               "focos_dia_siguiente_en_no_alto": float(focos_sig[prueba.pred == 0].mean()) if (prueba.pred == 0).any() else None}
    print(f"   control descriptivo (focos del día siguiente, promedio): {control}")

    metricas = {"algoritmo": elegido, "cv": cv, "umbral": cal, "prueba": {
        "f1": f1, "recall": rec, "precision": prec, "n": len(prueba), "positivos": int(prueba.y.sum()),
        "matriz_confusion": confusion_matrix(prueba["y"], pred_te, labels=[0, 1]).tolist(), "cumple_aceptacion": cumple},
        "control_descriptivo": control}

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ruta = config.MODELS_DIR / f"modelo_{elegido}_{ahora():%Y%m%d_%H%M%S}.joblib"
    joblib.dump({"modelo": modelo, "features": FEATURES, "algoritmo": elegido}, ruta)
    with conectar() as conn:
        conn.execute("UPDATE Version_Modelo SET activo = 0")
        cur = conn.execute(
            "INSERT INTO Version_Modelo (algoritmo, fecha_entrenamiento, f1_prueba, recall_prueba, umbral_sugerido, "
            "metricas_json, artefacto, activo) VALUES (?,?,?,?,?,?,?,1)",
            (elegido, iso(ahora()), f1, rec, umbral, json.dumps(metricas), str(ruta)))
        metricas["id_modelo"] = cur.lastrowid
        # 0,80 es el valor inicial "hasta completar la calibración"; calibrado, pasa a ser el umbral vigente
        # (dentro del rango 0,50-0,95). El Administrador puede cambiarlo después; queda registrado en Config_Log.
        if cal["calibrado"]:
            set_umbral(conn, float(min(max(umbral, config.UMBRAL_MIN), config.UMBRAL_MAX)), "calibracion_modelo")
    return metricas


def _focos_dia_siguiente(prueba: pd.DataFrame) -> pd.Series:
    with conectar() as conn:
        por_dia = _focos_por_dia(conn)
    return pd.Series([por_dia.get((f + timedelta(days=1)).strftime("%Y-%m-%d"), 0.0) for f in prueba["fecha"]],
                     index=prueba.index)


# ======================================================================= Inferencia
def _cargar_modelo_activo(conn):
    fila = conn.execute("SELECT id_modelo, artefacto FROM Version_Modelo WHERE activo = 1 ORDER BY id_modelo DESC LIMIT 1").fetchone()
    if not fila:
        raise RuntimeError("No hay un modelo entrenado. Ejecutá `python modelo_ml.py entrenar`.")
    return fila["id_modelo"], joblib.load(fila["artefacto"])


def _entradas_del_dia(conn, hoy: date):
    """Entradas del día t. Combina la agregación diaria de Open-Meteo con la lectura actual de OpenWeather
    (el TP4 dice que OpenWeather 'alimenta el cálculo diario'): temperatura = máx., humedad = mín., viento = máx."""
    dia = hoy.isoformat()
    om = conn.execute("SELECT id_clima, temperatura_c, humedad_pct, viento_kmh FROM Clima "
                      "WHERE id_territorio=? AND fuente='Open-Meteo' AND substr(fecha_hora_obs,1,10)=?", (ID_T, dia)).fetchone()
    ow = conn.execute("SELECT id_clima, temperatura_c, humedad_pct, viento_kmh FROM Clima "
                      "WHERE id_territorio=? AND fuente='OpenWeather' AND substr(fecha_hora_obs,1,10)=? "
                      "ORDER BY fecha_hora_obs DESC LIMIT 1", (ID_T, dia)).fetchone()
    obs = [o for o in (om, ow) if o is not None]
    if not obs:
        return None, None
    def _vals(campo):
        return [o[campo] for o in obs if o[campo] is not None]
    t, h, v = _vals("temperatura_c"), _vals("humedad_pct"), _vals("viento_kmh")
    focos = conn.execute(
        "SELECT COUNT(*) FROM Foco_Calor WHERE id_territorio=? AND substr(datetime(fecha_hora_deteccion,'-3 hours'),1,10) "
        "BETWEEN ? AND ?", (ID_T, (hoy - timedelta(days=3)).isoformat(), (hoy - timedelta(days=1)).isoformat())).fetchone()[0]
    entradas = {"temp_c": max(t) if t else None, "hum_pct": min(h) if h else None,
                "viento_kmh": max(v) if v else None, "focos_72h": float(focos)}
    return entradas, (ow or om)["id_clima"]


def _estado_datos(conn, entradas) -> str:
    if entradas is None or entradas["temp_c"] is None or entradas["hum_pct"] is None:
        return "clima_ausente"
    foco_ok = ultima_ingesta_ok(conn, "FIRMS")
    if foco_ok is None:
        return "foco_ausente"
    clima_ok = ultima_ingesta_ok(conn, "Open-Meteo") or ultima_ingesta_ok(conn, "OpenWeather")
    if (horas_desde(foco_ok) or 0) > config.DATOS_MAX_HORAS or clima_ok is None or (horas_desde(clima_ok) or 0) > config.DATOS_MAX_HORAS:
        return "desactualizado"
    return "completo"


def predecir_hoy(hoy: date = None, demo_indice: float = None) -> dict:
    """Calcula y guarda el riesgo del día t para t+1. Con datos incompletos guarda el estado y NO produce índice
    (por lo tanto no habrá alerta automática). `demo_indice` fuerza un valor y marca el registro como DEMO."""
    hoy = hoy or ahora().date()
    objetivo = hoy + timedelta(days=1)
    with conectar() as conn:
        if demo_indice is not None:
            cur = conn.execute(
                "INSERT INTO Riesgo (id_territorio, id_modelo, id_clima, fecha_emision, fecha_objetivo, horizonte_h, indice_riesgo, "
                "estado_datos, demo) VALUES (?,NULL,NULL,?,?,?,?,'completo',1) "
                "ON CONFLICT(id_territorio, fecha_emision, demo) DO UPDATE SET indice_riesgo=excluded.indice_riesgo",
                (ID_T, hoy.isoformat(), objetivo.isoformat(), config.HORIZONTE_H, round(demo_indice, 3)))
            fila = conn.execute("SELECT * FROM Riesgo WHERE id_territorio=? AND fecha_emision=? AND demo=1", (ID_T, hoy.isoformat())).fetchone()
            return dict(fila)

        entradas, id_clima = _entradas_del_dia(conn, hoy)
        estado = _estado_datos(conn, entradas)
        indice, id_modelo = None, None
        if estado == "completo":
            id_modelo, art = _cargar_modelo_activo(conn)
            x = {k: (np.nan if v is None else v) for k, v in entradas.items()}
            indice = round(float(art["modelo"].predict_proba(pd.DataFrame([x])[art["features"]])[0, 1]), 3)
        else:
            log.warning("Datos %s: se guarda el estado y no se emite índice ni alerta.", estado)
        conn.execute(
            "INSERT INTO Riesgo (id_territorio, id_modelo, id_clima, fecha_emision, fecha_objetivo, horizonte_h, indice_riesgo, "
            "estado_datos, demo) VALUES (?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(id_territorio, fecha_emision, demo) DO UPDATE SET id_modelo=excluded.id_modelo, id_clima=excluded.id_clima, "
            "indice_riesgo=excluded.indice_riesgo, estado_datos=excluded.estado_datos",
            (ID_T, id_modelo, id_clima, hoy.isoformat(), objetivo.isoformat(), config.HORIZONTE_H, indice, estado))
        fila = conn.execute("SELECT * FROM Riesgo WHERE id_territorio=? AND fecha_emision=? AND demo=0", (ID_T, hoy.isoformat())).fetchone()
    print(f"   -> 🎯 emisión {hoy}, objetivo {objetivo}: puntuación={indice} estado_datos={estado}")
    return dict(fila)


if __name__ == "__main__":
    from base_de_datos import inicializar_base_de_datos

    inicializar_base_de_datos()
    accion = sys.argv[1] if len(sys.argv) > 1 else "entrenar"
    entrenar() if accion == "entrenar" else predecir_hoy()
