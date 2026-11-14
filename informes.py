import argparse
import logging
import math
import os
import re
import time
from datetime import date, timedelta

import config
from db import conectar, ahora, iso, get_umbral

log = logging.getLogger(__name__)
ID_T = config.TERRITORIO["id_territorio"]
NOTA_TECNICA = (
    "NOTA TÉCNICA: los focos de NASA FIRMS son anomalías térmicas y pueden ser falsos positivos (zonas urbanas o industriales); "
    "no equivalen a incendios confirmados. La puntuación de riesgo es una salida del modelo, no una probabilidad calibrada, y su "
    "evaluación es exploratoria. Consultar el dashboard para validación."
)


# ======================================================================= RAG (ChromaDB)
EMBEDDING_OVERRIDE = None  # solo lo reemplazan los tests para no descargar el modelo de embeddings


def _coleccion():
    import chromadb

    cliente = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    kwargs = {"embedding_function": EMBEDDING_OVERRIDE} if EMBEDDING_OVERRIDE else {}  # por defecto: embedding de Chroma
    return cliente.get_or_create_collection(name="corpus_rag", metadata={"hnsw:space": "cosine"}, **kwargs)


def _leer_corpus() -> list:
    docs = []
    for ruta in sorted(config.CORPUS_DIR.glob("*.txt")):
        if ruta.name.upper().startswith("README"):
            continue
        cabecera, cuerpo, en_cuerpo = {}, [], False
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if not en_cuerpo and linea.strip() == "---":
                en_cuerpo = True
            elif not en_cuerpo and ":" in linea:
                k, v = linea.split(":", 1)
                cabecera[k.strip().upper()] = v.strip()
            else:
                cuerpo.append(linea)
        for i, parrafo in enumerate(p.strip() for p in "\n".join(cuerpo).split("\n\n") if p.strip()):
            if parrafo.startswith("NO IMPLEMENTADO"):
                continue  # avisos para el autor, no son contenido del corpus
            docs.append({"id": f"{ruta.stem}-{i}", "texto": parrafo, "documento": cabecera.get("DOCUMENTO", ruta.stem),
                         "version": cabecera.get("VERSION", "sin versión")})
    return docs


def indexar_corpus() -> int:
    docs = _leer_corpus()
    if not docs:
        print("⚠️ El corpus está vacío (carpeta corpus/).")
        return 0
    col = _coleccion()
    col.upsert(ids=[d["id"] for d in docs], documents=[d["texto"] for d in docs],
               metadatas=[{"documento": d["documento"], "version_documento": d["version"]} for d in docs])
    print(f"📚 Corpus indexado: {len(docs)} fragmentos.")
    return len(docs)


def recuperar_fragmentos(consulta: str, k: int = 2) -> list:
    col = _coleccion()
    if col.count() == 0:
        indexar_corpus()
    if col.count() == 0:
        return []
    r = col.query(query_texts=[consulta], n_results=min(k, col.count()))
    return [{"texto": t, "documento": m["documento"], "version": m["version_documento"]}
            for t, m in zip(r["documents"][0], r["metadatas"][0])]


# ======================================================================= Datos de los últimos 7 días
def reunir_datos(fin: date = None, incluir_demo: bool = False) -> dict:
    """Resume los 7 días previos (lunes a domingo de la semana anterior) tomados de la base local."""
    hoy = ahora().date()
    fin = fin or (hoy - timedelta(days=hoy.weekday() + 1))  # último domingo ya terminado
    ini = fin - timedelta(days=6)
    demo = 1 if incluir_demo else 0
    with conectar() as conn:
        riesgos = conn.execute(
            "SELECT indice_riesgo, fecha_objetivo, id_modelo FROM Riesgo WHERE id_territorio=? AND demo=? AND indice_riesgo IS NOT NULL "
            "AND fecha_emision BETWEEN ? AND ?", (ID_T, demo, ini.isoformat(), fin.isoformat())).fetchall()
        focos = conn.execute(
            "SELECT confianza, COUNT(*) n FROM Foco_Calor WHERE id_territorio=? AND "
            "substr(datetime(fecha_hora_deteccion,'-3 hours'),1,10) BETWEEN ? AND ? GROUP BY confianza",
            (ID_T, ini.isoformat(), fin.isoformat())).fetchall()
        clima = conn.execute(
            "SELECT MAX(temperatura_c) t, MIN(humedad_pct) h, MAX(viento_kmh) v FROM Clima WHERE id_territorio=? AND fuente='Open-Meteo' "
            "AND substr(fecha_hora_obs,1,10) BETWEEN ? AND ?", (ID_T, ini.isoformat(), fin.isoformat())).fetchone()
        alertas = []
        for a in conn.execute(
                "SELECT a.id_alerta, a.fecha_envio, a.umbral_aplicado FROM Alerta a JOIN Riesgo r ON r.id_riesgo=a.id_riesgo "
                "WHERE r.id_territorio=? AND r.demo=? AND substr(a.fecha_envio,1,10) BETWEEN ? AND ? ORDER BY a.fecha_envio",
                (ID_T, demo, ini.isoformat(), fin.isoformat())):
            avisos = [{"organismo": v["organismo"], "estado": v["estado"],
                       "hora": v["fecha_confirmacion"][11:16] if v["fecha_confirmacion"] else None}
                      for v in conn.execute("SELECT organismo, estado, fecha_confirmacion FROM Aviso_Organismo WHERE id_alerta=?", (a["id_alerta"],))]
            alertas.append({"fecha": a["fecha_envio"][:10], "umbral": a["umbral_aplicado"], "avisos": avisos})
        modelo = riesgos[0]["id_modelo"] if riesgos else None
        umbral = get_umbral(conn)
    valores = [r["indice_riesgo"] for r in riesgos]
    mejor = max(riesgos, key=lambda r: r["indice_riesgo"]) if riesgos else None
    return {
        "periodo_inicio": ini.isoformat(), "periodo_fin": fin.isoformat(), "fecha_emision": ahora().date().isoformat(),
        "riesgo_max": {"indice": mejor["indice_riesgo"], "fecha_objetivo": mejor["fecha_objetivo"]} if mejor else None,
        "riesgo_prom": round(sum(valores) / len(valores), 2) if valores else None, "n_riesgos": len(valores),
        "focos_total": sum(f["n"] for f in focos), "focos_confianza": {(f["confianza"] or "sin dato"): f["n"] for f in focos},
        "alertas": alertas, "temp_max": clima["t"], "hum_min": clima["h"], "viento_max": clima["v"],
        "id_modelo": modelo, "demo": incluir_demo, "umbral": umbral,
    }


_ESTADO_TXT = {"confirmado": "confirmó", "enviado": "pendiente", "sin_confirmar": "sin confirmar"}


def _fmt(x, nd=2):
    return "sin información" if x is None else f"{x:.{nd}f}".replace(".", ",")


def _nivel(indice, umbral) -> str:
    return "sin información" if indice is None else ("Alto" if indice >= umbral else "Bajo/Medio")


def datos_a_texto(d: dict) -> str:
    lin = [f"Período evaluado: {d['periodo_inicio']} al {d['periodo_fin']} (7 días). Fecha de emisión: {d['fecha_emision']}."]
    rm = d["riesgo_max"]
    lin.append(f"Nivel máximo de riesgo del período: {_fmt(rm['indice']) + ' - ' + _nivel(rm['indice'], d['umbral']) if rm else 'sin información'}"
               + (f" (válido para el {rm['fecha_objetivo']})." if rm else "."))
    lin.append(f"Puntuación promedio del período: {_fmt(d['riesgo_prom'])}"
               + (f" - {_nivel(d['riesgo_prom'], d['umbral'])}." if d["riesgo_prom"] is not None else "."))
    conf = ", ".join(f"{k}: {v}" for k, v in d["focos_confianza"].items()) or "sin detecciones"
    lin.append(f"Focos observados (NASA FIRMS, VIIRS NOAA-20): {d['focos_total']} ({conf}).")
    lin.append(f"Alertas emitidas: {len(d['alertas'])}.")
    for a in d["alertas"]:
        est = "; ".join(f"{v['organismo']} {_ESTADO_TXT[v['estado']]}" + (f" a las {v['hora']}" if v["hora"] else "") for v in a["avisos"])
        lin.append(f"  Alerta del {a['fecha']} (umbral {a['umbral']:.2f}): {est}.")
    lin.append(f"Temperatura máxima: {_fmt(d['temp_max'], 1)} °C; humedad mínima: {_fmt(d['hum_min'], 0)} %; viento máximo: "
               + (f"{_fmt(d['viento_max'], 0)} km/h." if d["viento_max"] is not None else "sin información."))
    return "\n".join(lin)


def _fragmentos_a_texto(frags: list) -> str:
    return "\n".join(f"[{f['documento']}, versión {f['version']}] {f['texto']}" for f in frags) or "(sin fragmentos recuperados)"


# ======================================================================= Redacción
def plantilla_fija(d: dict, frags: list) -> str:
    """Alternativa de continuidad"""
    rm = d["riesgo_max"]
    s = ["1. Resumen de situación",
         f"   Nivel máximo de riesgo del período: {_fmt(rm['indice']) + ' - ' + _nivel(rm['indice'], d['umbral']) if rm else 'sin información'}"
         + (f", el {rm['fecha_objetivo']}." if rm else "."),
         f"   Puntuación promedio del período: {_fmt(d['riesgo_prom'])}"
         + (f" - {_nivel(d['riesgo_prom'], d['umbral'])}." if d["riesgo_prom"] is not None else "."),
         f"   Focos observados (NASA FIRMS, VIIRS NOAA-20): {d['focos_total']}.",
         "", "2. Alertas emitidas y confirmación"]
    if not d["alertas"]:
        s.append("   No se emitieron alertas en el período (0 alertas).")
    for a in d["alertas"]:
        est = "; ".join(f"{v['organismo']} {_ESTADO_TXT[v['estado']]}" + (f" a las {v['hora']}" if v["hora"] else "") for v in a["avisos"])
        s.append(f"   Alerta del {a['fecha']}: {est}.")
    s += ["", "3. Análisis meteorológico (Open-Meteo y OpenWeather)",
          f"   Temperatura máxima {_fmt(d['temp_max'], 1)} °C; humedad mínima {_fmt(d['hum_min'], 0)} %; viento máximo "
          + (f"{_fmt(d['viento_max'], 0)} km/h." if d["viento_max"] is not None else "sin información."),
          "", "4. Recomendaciones operativas (basadas en RAG)"]
    if frags:
        for f in frags:
            s.append(f"   - {f['texto']} [{f['documento']}, versión {f['version']}]")
    else:
        s.append("   Sin recomendaciones documentales disponibles.")
    s += ["", "5. Fuentes: base local (7 días) y fragmentos del corpus (documento, versión)."]
    return "\n".join(s)


_SISTEMA = (
    "Sos un redactor técnico de un sistema municipal de alerta de incendios (Municipio de San Fernando del Valle de Catamarca). "
    "Escribí un informe semanal breve y directo, en español, con EXACTAMENTE estas secciones: "
    "1. Resumen de situación, 2. Alertas emitidas y confirmación, 3. Análisis meteorológico, "
    "4. Recomendaciones operativas (basadas en RAG), 5. Fuentes. "
    "REGLAS ESTRICTAS: usá únicamente las cifras de DATOS, sin redondear ni convertir (no pases 0,85 a 85 %); no inventes leyes, "
    "organismos, cifras ni citas; en la sección 4 citá cada recomendación con el formato [Documento, versión X] copiado de FRAGMENTOS; "
    "si no hay fragmentos escribí 'Sin recomendaciones documentales disponibles'; mencioná siempre el nivel máximo de riesgo, "
    "la cantidad de focos y cada alerta con el estado de confirmación de Defensa Civil y de Bomberos; "
    "no afirmes que la puntuación es una probabilidad. Sin saludo ni cierre."
)


def generar_borrador_llm(d: dict, frags: list, observaciones: str = "") -> tuple:
    """LangChain + OpenRouter. Devuelve (texto, segundos). Lanza excepción si falla o excede el tiempo."""
    if not config.OPENROUTER_KEY:
        raise RuntimeError("Falta OPENROUTER_API_KEY")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=config.OPENROUTER_MODEL, api_key=config.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1",
                     timeout=config.INFORME_LATENCIA_MAX_S, max_retries=0, temperature=0.2)
    cadena = ChatPromptTemplate.from_messages([
        ("system", _SISTEMA),
        ("human", "DATOS:\n{datos}\n\nFRAGMENTOS:\n{fragmentos}\n\n{observaciones}")]) | llm
    t0 = time.time()
    resp = cadena.invoke({"datos": datos_a_texto(d), "fragmentos": _fragmentos_a_texto(frags), "observaciones": observaciones})
    seg = time.time() - t0
    if seg > config.INFORME_LATENCIA_MAX_S:
        raise TimeoutError(f"El LLM tardó {seg:.0f} s (> {config.INFORME_LATENCIA_MAX_S} s)")
    texto = (resp.content or "").strip()
    if not texto:
        raise RuntimeError("El LLM devolvió un texto vacío")
    return texto, seg


# ======================================================================= Revisión factual
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_ACRONIMO = re.compile(r"\b[A-ZÁÉÍÓÚÑ]{3,8}\b")
_ACRONIMOS_OK = {"SNMF", "SAEC", "NASA", "FIRMS", "VIIRS", "NOAA", "UTC", "LLM", "RAG", "ALTO", "BAJO", "MEDIO", "RAG"}


def _numeros(texto: str) -> set:
    return {float(x.replace(",", ".")) for x in _NUM.findall(texto)}


def revision_factual(texto: str, d: dict, frags: list) -> dict:
    """Contrasta cifras, leyes, organismos y citas del borrador con los datos y los fragmentos (protocolo de aceptación del TP4).
    Alucinación: cifra, ley, organismo o cita que no coincide. Omisión: falta nivel máximo, focos o alertas con su estado.
    AJUSTAR TEXTO: la revisión es automática y conservadora (puede rechazar un informe correcto); el TP4 la asigna al autor como
    Product Owner, lo que se cubre con REVISION_MANUAL=true (aprobación por Telegram antes del envío)."""
    base_datos = datos_a_texto(d)
    base_frag = _fragmentos_a_texto(frags)
    permitido = _numeros(base_datos) | _numeros(base_frag) | {1, 2, 3, 4, 5, 7, 24, 30, 60, 72}
    permitido |= {round(x * 100) for x in permitido if 0 < x <= 1}  # equivalentes porcentuales
    alucin, omis = [], []

    cuerpo = "\n".join(re.sub(r"^\s*\d+[.)]\s*", "", ln) for ln in texto.splitlines())  # sin numeración de secciones
    for n in sorted(_numeros(cuerpo)):
        if not any(math.isclose(n, p, abs_tol=0.0051) for p in permitido):
            alucin.append(f"cifra no respaldada: {n:g}")

    for m in re.finditer(r"Ley\s+(?:Provincial\s+)?(?:N[°º.]?\s*)?(\d+)", cuerpo):
        if not re.search(r"Ley[^\d]{0,30}" + m.group(1), base_frag):
            alucin.append(f"ley no respaldada: {m.group(0)}")

    ok_acr = _ACRONIMOS_OK | set(_ACRONIMO.findall(base_datos)) | set(_ACRONIMO.findall(base_frag))
    for ln in texto.splitlines():
        if ln.strip() and not ln.strip().isupper():
            for a in _ACRONIMO.findall(ln):
                if a not in ok_acr:
                    alucin.append(f"organismo/sigla no respaldada: {a}")

    for m in re.finditer(r"\[([^\]]+)\]", texto):
        c = m.group(1)
        if re.search(r"versi[oó]n|^Ley|^Protocolo|^Informe", c, re.I):
            if not any(f["documento"] in c for f in frags):
                alucin.append(f"cita no respaldada: [{c}]")

    rm = d["riesgo_max"]
    if rm:
        if not any(math.isclose(n, rm["indice"], abs_tol=0.0051) for n in _numeros(cuerpo)):
            omis.append("nivel máximo de riesgo")
    elif "sin información" not in texto.lower():
        omis.append("nivel máximo de riesgo (indicar 'sin información')")
    if d["focos_total"] not in {int(n) for n in _numeros(cuerpo) if float(n).is_integer()} and not (
            d["focos_total"] == 0 and re.search(r"sin focos|ning[uú]n foco", texto, re.I)):
        omis.append("cantidad de focos observados")
    if d["alertas"]:
        for org in config.ORGANISMOS:
            if org not in texto:
                omis.append(f"estado de confirmación de {org}")
        if not re.search(r"confirm|pendiente", texto, re.I):
            omis.append("estado de confirmación de las alertas")
    elif not re.search(r"sin alertas|no se emitieron|ninguna alerta|\b0 alertas|alertas emitidas: ?0", texto, re.I):
        omis.append("alertas emitidas (indicar que no hubo)")
    return {"aprobado": not alucin and not omis, "alucinaciones": sorted(set(alucin)), "omisiones": omis}


# ======================================================================= Envío (Resend)
def _encabezado(d: dict, estado: str, id_modelo, modelo_llm: str) -> str:
    return (f"INFORME SEMANAL DE RIESGO DE INCENDIOS - {config.TERRITORIO['nombre'].upper()}\n"
            f"Fecha de emisión: {d['fecha_emision']} | Período evaluado: {d['periodo_inicio']} al {d['periodo_fin']}\n"
            f"Estado de revisión: {estado} | Versión del modelo de riesgo: {id_modelo if id_modelo else 'sin dato'} | Redacción: {modelo_llm}"
            + ("\n*** INFORME DE DEMOSTRACIÓN ***" if d.get("demo") else "") + "\n\n")


def enviar_email(asunto: str, texto: str) -> str:
    import resend

    if not config.RESEND_KEY:
        raise RuntimeError("Falta RESEND_API_KEY")
    para = config.lista_env("EMAIL_AUTORIDADES")
    cc = config.lista_env("EMAIL_DEFENSA_CIVIL") + config.lista_env("EMAIL_BOMBEROS")
    if not para:
        raise RuntimeError("Falta EMAIL_AUTORIDADES")
    if config.EMAIL_REMITENTE.endswith("@resend.dev"):
        log.warning("Remitente de prueba de Resend: solo entrega al correo dueño de la cuenta. Verificar un dominio propio.")
    resend.api_key = config.RESEND_KEY
    r = resend.Emails.send({"from": config.EMAIL_REMITENTE, "to": para, "cc": cc, "subject": asunto, "text": texto})
    return r["id"] if isinstance(r, dict) else getattr(r, "id")


def enviar_informe_guardado(id_informe: int) -> bool:
    """Envía un informe aprobado y guarda el estado devuelto por el proveedor (aceptado != leído)."""
    with conectar() as conn:
        inf = conn.execute("SELECT * FROM Informe WHERE id_informe = ?", (id_informe,)).fetchone()
        if inf["estado_revision"] != "aprobado":
            raise ValueError("Solo se envían informes aprobados")
        try:
            rid = enviar_email(f"INFORME SEMANAL - Riesgo de Incendios {config.TERRITORIO['nombre']}", inf["contenido_texto"])
            conn.execute("UPDATE Informe SET estado_entrega='aceptado_por_proveedor', resend_id=? WHERE id_informe=?", (rid, id_informe))
            print("   -> ✅ Email aceptado por el proveedor (no implica lectura del destinatario).")
            return True
        except Exception as e:
            log.error("Fallo el envío del informe: %s", e)
            conn.execute("UPDATE Informe SET motivo = COALESCE(motivo,'') || ' | envío fallido: ' || ? WHERE id_informe=?", (str(e)[:200], id_informe))
            print(f"   -> ❌ Error Email: {e}")
            return False


def confirmar_recepcion_informe(id_informe: int, usuario: str) -> str:
    """HU-06 (criterio 3): la Autoridad confirma la recepción desde el dashboard. Es distinto de 'entregado' (HU-11)."""
    with conectar() as conn:
        inf = conn.execute("SELECT estado_entrega FROM Informe WHERE id_informe = ?", (id_informe,)).fetchone()
        if inf is None:
            raise LookupError("Informe inexistente")
        if inf["estado_entrega"] in ("no_enviado", "rebotado"):
            raise ValueError("El informe no fue entregado: no hay nada que confirmar")
        conn.execute("UPDATE Informe SET estado_entrega='confirmado_por_destinatario', confirmado_por=?, fecha_confirmacion=? WHERE id_informe=?",
                     (usuario, iso(ahora()), id_informe))
    return "confirmado_por_destinatario"


def actualizar_estado_entrega(id_informe: int = None):
    """Consulta a Resend el estado de los informes enviados (entregado / rebotado).
    VERIFICAR: el nombre del campo `last_event` y sus valores según la versión del SDK de Resend."""
    import resend

    resend.api_key = config.RESEND_KEY
    with conectar() as conn:
        filas = conn.execute("SELECT id_informe, resend_id FROM Informe WHERE resend_id IS NOT NULL AND estado_entrega IN "
                             "('aceptado_por_proveedor')" + (" AND id_informe = ?" if id_informe else ""),
                             (id_informe,) if id_informe else ()).fetchall()
        for f in filas:
            try:
                r = resend.Emails.get(f["resend_id"])
                evento = (r.get("last_event") if isinstance(r, dict) else getattr(r, "last_event", "")) or ""
                nuevo = {"delivered": "entregado", "bounced": "rebotado", "complained": "rebotado"}.get(evento)
                if nuevo:
                    conn.execute("UPDATE Informe SET estado_entrega=? WHERE id_informe=?", (nuevo, f["id_informe"]))
            except Exception as e:
                log.warning("No se pudo consultar el estado del informe %s: %s", f["id_informe"], e)


# ======================================================================= Orquestación
def _guardar_informe(d, frags, texto, revision_estado, modelo_llm, motivo, latencia) -> int:
    with conectar() as conn:
        id_inf = conn.execute(
            "INSERT INTO Informe (id_territorio, id_modelo, periodo_inicio, periodo_fin, fecha_generacion, contenido_texto, "
            "estado_revision, modelo_llm, motivo, latencia_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ID_T, d["id_modelo"], d["periodo_inicio"], d["periodo_fin"], iso(ahora()), texto, revision_estado, modelo_llm, motivo, latencia)).lastrowid
        conn.execute("INSERT INTO Informe_Fuente (id_informe, tipo, referencia, texto) VALUES (?, 'datos', 'base local (7 días)', ?)",
                     (id_inf, datos_a_texto(d)))
        for f in frags:
            conn.execute("INSERT INTO Informe_Fuente (id_informe, tipo, referencia, texto, version_documento) VALUES (?, 'documento', ?, ?, ?)",
                         (id_inf, f["documento"], f["texto"], f["version"]))
    return id_inf


def generar_y_enviar_informe(enviar: bool = True, demo: bool = False, fin: date = None, usar_llm: bool = True) -> dict:
    print("📝 Generando informe (RAG + LLM)...")
    d = reunir_datos(fin, incluir_demo=demo)
    consulta = ("¿Qué hacer si el riesgo es ALTO?" if (d["riesgo_max"] and d["riesgo_max"]["indice"] >= 0.5)
                else "¿Qué hacer si el riesgo es BAJO?")
    try:
        frags = recuperar_fragmentos(consulta)
    except Exception as e:  # RAG caído: el informe sale sin recomendaciones documentales
        log.error("Fallo el RAG: %s", e)
        frags = []

    texto, modelo_llm, motivo, latencia = None, config.OPENROUTER_MODEL, "", None
    if usar_llm:
        observ = ""
        for intento in (1, 2):
            try:
                borrador, latencia = generar_borrador_llm(d, frags, observ)
            except Exception as e:  # LLM caído, límite de uso o > 60 s -> alternativa de continuidad
                motivo = f"LLM no disponible ({e}); se usa plantilla fija"
                log.warning(motivo)
                break
            rev = revision_factual(borrador, d, frags)
            if rev["aprobado"]:
                texto = borrador
                break
            motivo = f"revisión factual rechazó el borrador (intento {intento}): {rev['alucinaciones'] + rev['omisiones']}"
            log.warning(motivo)
            observ = "CORREGÍ estos problemas del borrador anterior: " + "; ".join(rev["alucinaciones"] + rev["omisiones"])
        else:
            # dos borradores rechazados: NO se envía (criterio de aceptación del TP4)
            cuerpo = borrador
            id_inf = _guardar_informe(d, frags, _encabezado(d, "rechazado", d["id_modelo"], modelo_llm) + cuerpo, "rechazado",
                                      modelo_llm, motivo, latencia)
            from alertas import notificar_admin

            notificar_admin(f"⚠️ Informe {id_inf} RECHAZADO por la revisión factual y no se envió. Motivo: {motivo[:300]}")
            print(f"   ❌ Informe rechazado (no se envía): {motivo}")
            return {"id_informe": id_inf, "estado": "rechazado", "motivo": motivo}
    if texto is None:
        texto, modelo_llm = plantilla_fija(d, frags), "plantilla_fija"
        rev = revision_factual(texto, d, frags)
        assert rev["aprobado"], f"La plantilla fija no pasó la revisión: {rev}"

    manual = os.getenv("REVISION_MANUAL", "true").lower() == "true"  # TP4: la revisión la hace el autor como Product Owner
    estado = "borrador" if manual else "aprobado"
    completo = (_encabezado(d, estado, d["id_modelo"], modelo_llm) + texto + "\n\n---\n" + NOTA_TECNICA
                + "\nEste informe fue generado automáticamente y requiere revisión factual antes de su envío.")
    id_inf = _guardar_informe(d, frags, completo, estado, modelo_llm, motivo, latencia)
    print(f"   -> ✅ Informe {id_inf} generado ({modelo_llm}), revisión factual aprobada.")
    resultado = {"id_informe": id_inf, "estado": estado, "motivo": motivo}
    if manual:
        from alertas import tg, notificar_admin, TelegramError

        chat = config.chat_id_admin()
        if chat:
            try:
                tg("sendMessage", chat_id=chat, text=f"Informe {id_inf} para revisión del Product Owner (la revisión automática de apoyo no encontró alucinaciones ni omisiones; "
                                         f"contrastá igualmente cada cifra y cita):\n\n{completo[:3300]}",
                   reply_markup={"inline_keyboard": [[{"text": "✅ Aprobar y enviar", "callback_data": f"inf:ok:{id_inf}"},
                                                      {"text": "❌ Rechazar", "callback_data": f"inf:no:{id_inf}"}]]})
            except TelegramError as e:
                log.error("No se pudo enviar el informe a revisión: %s", e)
        return resultado
    if enviar:
        enviar_informe_guardado(id_inf)
    return resultado


def manejar_revision(datos: str, chat_id) -> str:
    """Callback de Telegram: inf:ok:ID / inf:no:ID (solo el chat del Administrador)."""
    if str(chat_id) != str(config.chat_id_admin()):
        return "No autorizado."
    _, accion, id_inf = datos.split(":")
    with conectar() as conn:
        fila = conn.execute("SELECT estado_revision FROM Informe WHERE id_informe = ?", (int(id_inf),)).fetchone()
        if not fila or fila["estado_revision"] != "borrador":
            return "El informe ya no está en revisión."
        conn.execute("UPDATE Informe SET estado_revision = ? WHERE id_informe = ?", ("aprobado" if accion == "ok" else "rechazado", int(id_inf)))
    if accion == "ok":
        return "Informe aprobado y enviado." if enviar_informe_guardado(int(id_inf)) else "Aprobado, pero el envío falló (ver logs)."
    return "Informe rechazado; no se envía."


if __name__ == "__main__":
    from base_de_datos import inicializar_base_de_datos

    ap = argparse.ArgumentParser()
    ap.add_argument("--indexar", action="store_true")
    ap.add_argument("--sin-enviar", action="store_true")
    ap.add_argument("--demo", action="store_true", help="incluye los riesgos/alertas DEMO")
    ap.add_argument("--sin-llm", action="store_true", help="fuerza la plantilla fija")
    a = ap.parse_args()
    inicializar_base_de_datos()
    if a.indexar:
        indexar_corpus()
    else:
        generar_y_enviar_informe(enviar=not a.sin_enviar, demo=a.demo, usar_llm=not a.sin_llm)
