import argparse
import logging
from datetime import timedelta

import requests

import config
from db import conectar, ahora, iso, parse_iso, get_umbral

log = logging.getLogger(__name__)
ID_T = config.TERRITORIO["id_territorio"]


class TelegramError(Exception):
    pass


def tg(metodo: str, **payload):
    """Llama a la API de Telegram y valida la respuesta"""
    if not config.TG_TOKEN:
        raise TelegramError("Falta TELEGRAM_BOT_TOKEN en el .env")
    r = requests.post(f"https://api.telegram.org/bot{config.TG_TOKEN}/{metodo}", json=payload, timeout=40)
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"Respuesta no JSON de Telegram (HTTP {r.status_code})")
    if not data.get("ok"):
        raise TelegramError(data.get("description", r.text[:120]))
    return data["result"]


def notificar_admin(texto: str):
    chat = config.chat_id_admin()
    if not chat:
        log.warning("Sin TELEGRAM_CHAT_ID_ADMIN; no se pudo avisar al Administrador: %s", texto)
        return
    try:
        tg("sendMessage", chat_id=chat, text=texto)
    except TelegramError as e:
        log.error("No se pudo avisar al Administrador: %s", e)


def _texto_alerta(riesgo, umbral: float, hora: str, demo: bool, reenvio: bool = False) -> str:
    etiqueta = " [DEMO]" if demo else ""
    return (
        f"⚠️ {'REENVÍO - ' if reenvio else ''}ALERTA DE RIESGO ALTO{etiqueta}\n"
        f"Territorio: {config.TERRITORIO['nombre']}\n"
        f"Nivel: ALTO\n"
        f"Puntuación del modelo: {riesgo['indice_riesgo']:.2f} (umbral vigente {umbral:.2f})\n"
        f"Válida para: {riesgo['fecha_objetivo']} (horizonte {riesgo['horizonte_h']} h)\n"
        f"Emitida: {hora}\n"
        f"Confirmá la recepción con el botón. Recibir este mensaje no equivale a confirmación."
    )


def _teclado(id_aviso: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Confirmar recepción", "callback_data": f"ack:{id_aviso}"}]]}


def _enviar_aviso(conn, aviso, riesgo, umbral, demo, reenvio=False):
    texto = _texto_alerta(riesgo, umbral, iso(ahora()), demo, reenvio)
    try:
        res = tg("sendMessage", chat_id=aviso["chat_id"], text=texto, reply_markup=_teclado(aviso["id_aviso"]))
        conn.execute("UPDATE Aviso_Organismo SET message_id = ?, detalle = NULL WHERE id_aviso = ?",
                     (res.get("message_id"), aviso["id_aviso"]))
        return True
    except (TelegramError, requests.RequestException) as e:
        log.error("Fallo el envío a %s: %s", aviso["organismo"], e)
        conn.execute("UPDATE Aviso_Organismo SET detalle = ? WHERE id_aviso = ?", (f"error de envío: {e}"[:300], aviso["id_aviso"]))
        return False


def emitir_alerta(demo: bool = False, forzar_duplicado: bool = False):
    """Evalúa el último riesgo del día y, si corresponde, crea la alerta y los avisos por organismo."""
    with conectar() as conn:
        riesgo = conn.execute(
            "SELECT * FROM Riesgo WHERE id_territorio = ? AND demo = ? ORDER BY fecha_emision DESC, id_riesgo DESC LIMIT 1",
            (ID_T, 1 if demo else 0)).fetchone()
        if riesgo is None:
            print("   (sin riesgo calculado; no hay nada que evaluar)")
            return None
        if riesgo["estado_datos"] != "completo" or riesgo["indice_riesgo"] is None:
            print(f"   ⏸️ Datos {riesgo['estado_datos']}: no se emite alerta automática.")
            return None
        umbral = get_umbral(conn)
        print(f"📊 Riesgo {riesgo['indice_riesgo']:.2f} vs umbral {umbral:.2f}")
        if riesgo["indice_riesgo"] < umbral:  # regla única: >= dispara
            print("   Riesgo por debajo del umbral: sin alerta.")
            return None
        if not forzar_duplicado:
            ya = conn.execute(
                "SELECT 1 FROM Alerta a JOIN Riesgo r ON r.id_riesgo = a.id_riesgo "
                "WHERE r.id_territorio = ? AND r.fecha_objetivo = ? AND r.demo = ?",
                (ID_T, riesgo["fecha_objetivo"], riesgo["demo"])).fetchone()
            if ya:
                print("   Ya existe una alerta para ese territorio y fecha objetivo: no se duplica.")
                return None

        chats = {o: config.chat_id_organismo(o) for o in config.ORGANISMOS}
        if len(set(chats.values())) < len(chats) or not all(chats.values()):
            log.warning("Los chats de Defensa Civil y Bomberos faltan o son el mismo: no se puede confirmar por organismo.")
        ts = iso(ahora())
        id_alerta = conn.execute(
            "INSERT INTO Alerta (id_riesgo, fecha_envio, umbral_aplicado, estado_global) VALUES (?,?,?, 'pendiente')",
            (riesgo["id_riesgo"], ts, umbral)).lastrowid
        for org in config.ORGANISMOS:
            id_aviso = conn.execute(
                "INSERT INTO Aviso_Organismo (id_alerta, organismo, fecha_envio, estado, chat_id) VALUES (?,?,?, 'enviado', ?)",
                (id_alerta, org, ts, chats[org])).lastrowid
            aviso = conn.execute("SELECT * FROM Aviso_Organismo WHERE id_aviso = ?", (id_aviso,)).fetchone()
            if not chats[org]:
                conn.execute("UPDATE Aviso_Organismo SET detalle = 'sin chat_id configurado' WHERE id_aviso = ?", (id_aviso,))
                print(f"   ❌ {org}: sin chat configurado")
                continue
            ok = _enviar_aviso(conn, aviso, riesgo, umbral, bool(riesgo["demo"]))
            print(f"   {'✅' if ok else '❌'} Telegram -> {org}")
        return id_alerta


# ----------------------------------------------------------------------- Confirmaciones
def recalcular_estado(conn, id_alerta: int) -> str:
    estados = [r["estado"] for r in conn.execute("SELECT estado FROM Aviso_Organismo WHERE id_alerta = ?", (id_alerta,))]
    if estados and all(e == "confirmado" for e in estados):
        nuevo = "confirmada"
    elif any(e == "sin_confirmar" for e in estados):
        nuevo = "sin_confirmar"
    else:
        nuevo = "pendiente"
    conn.execute("UPDATE Alerta SET estado_global = ? WHERE id_alerta = ?", (nuevo, id_alerta))
    return nuevo


def confirmar_aviso(conn, id_aviso: int, chat_id_origen) -> str:
    """HU-05: registra organismo, alerta y hora. Solo vale si el botón lo presionó el chat del organismo."""
    aviso = conn.execute("SELECT * FROM Aviso_Organismo WHERE id_aviso = ?", (id_aviso,)).fetchone()
    if aviso is None:
        return "Aviso inexistente."
    if str(chat_id_origen) != str(aviso["chat_id"]):
        log.warning("Confirmación rechazada: el chat %s no corresponde al organismo %s", chat_id_origen, aviso["organismo"])
        return "No autorizado para confirmar este aviso."
    if aviso["estado"] == "confirmado":
        return "Ya estaba confirmado."
    conn.execute("UPDATE Aviso_Organismo SET estado = 'confirmado', fecha_confirmacion = ? WHERE id_aviso = ?",
                 (iso(ahora()), id_aviso))
    nuevo = recalcular_estado(conn, aviso["id_alerta"])
    faltan = [r["organismo"] for r in conn.execute(
        "SELECT organismo FROM Aviso_Organismo WHERE id_alerta = ? AND estado != 'confirmado'", (aviso["id_alerta"],))]
    return "Recepción confirmada." + (f" Falta: {', '.join(faltan)}." if faltan else " Alerta confirmada por ambos organismos.") + f" [{nuevo}]"


def manejar_callback(callback: dict) -> str:
    """Procesa un callback_query de Telegram (lo llama bot_telegram.py)."""
    datos = callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    with conectar() as conn:
        if datos.startswith("ack:"):
            return confirmar_aviso(conn, int(datos.split(":")[1]), chat_id)
    if datos.startswith("inf:"):
        # revisión de informes desde Telegram (Figura 6, pantalla 3)
        import informes

        return informes.manejar_revision(datos, chat_id)
    return "Acción desconocida."


def revisar_pendientes(ahora_dt=None):
    """Reenvío a los 30 min y pase a 'sin_confirmar' a los 30 min posteriores. Pensado para ejecutarse cada ~1 min."""
    ahora_dt = ahora_dt or ahora()
    limite = timedelta(minutes=config.CONFIRMACION_MINUTOS)
    with conectar() as conn:
        pendientes = conn.execute(
            "SELECT av.*, a.id_riesgo, a.umbral_aplicado FROM Aviso_Organismo av JOIN Alerta a ON a.id_alerta = av.id_alerta "
            "WHERE av.estado = 'enviado'").fetchall()
        for av in pendientes:
            referencia = parse_iso(av["fecha_reenvio"] or av["fecha_envio"])
            if ahora_dt - referencia < limite:
                continue
            riesgo = conn.execute("SELECT * FROM Riesgo WHERE id_riesgo = ?", (av["id_riesgo"],)).fetchone()
            if not av["reenviado"]:
                if av["chat_id"]:
                    _enviar_aviso(conn, av, riesgo, av["umbral_aplicado"], bool(riesgo["demo"]), reenvio=True)
                conn.execute("UPDATE Aviso_Organismo SET reenviado = 1, fecha_reenvio = ? WHERE id_aviso = ?", (iso(ahora_dt), av["id_aviso"]))
                log.info("Reenviado aviso a %s (alerta %s)", av["organismo"], av["id_alerta"])
            else:
                conn.execute("UPDATE Aviso_Organismo SET estado = 'sin_confirmar' WHERE id_aviso = ?", (av["id_aviso"],))
                recalcular_estado(conn, av["id_alerta"])
                notificar_admin(f"⚠️ Alerta {av['id_alerta']}: {av['organismo']} NO confirmó la recepción tras el reenvío. Estado: Sin confirmar.")


if __name__ == "__main__":
    from base_de_datos import inicializar_base_de_datos
    import modelo_ml

    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="fuerza riesgo 0,85 marcado como DEMO")
    ap.add_argument("--forzar-duplicado", action="store_true")
    a = ap.parse_args()
    inicializar_base_de_datos()
    if a.demo:
        modelo_ml.predecir_hoy(demo_indice=0.85)
    emitir_alerta(demo=a.demo, forzar_duplicado=a.forzar_duplicado)
