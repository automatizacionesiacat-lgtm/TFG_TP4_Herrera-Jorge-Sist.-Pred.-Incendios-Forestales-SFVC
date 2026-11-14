import logging
import time

import alertas
import config
from base_de_datos import inicializar_base_de_datos

log = logging.getLogger("bot")


def procesar_actualizacion(upd: dict):
    cb = upd.get("callback_query")
    if not cb:
        return
    try:
        respuesta = alertas.manejar_callback(cb)
    except Exception as e:  # que un error no tumbe el bot
        log.exception("Error procesando callback: %s", e)
        respuesta = "Error interno al procesar la acción."
    try:
        alertas.tg("answerCallbackQuery", callback_query_id=cb["id"], text=respuesta[:190])
        msg = cb.get("message")
        if msg and respuesta.startswith(("Recepción confirmada", "Alerta confirmada")):
            alertas.tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                       reply_markup={"inline_keyboard": []})
    except alertas.TelegramError as e:
        log.warning("No se pudo responder el callback: %s", e)


def main():
    inicializar_base_de_datos()
    offset = None
    ultima_revision = 0.0
    log.info("Bot de confirmaciones en marcha.")
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": ["callback_query"]}
            if offset:
                params["offset"] = offset
            for upd in alertas.tg("getUpdates", **params):
                offset = upd["update_id"] + 1
                procesar_actualizacion(upd)
        except Exception as e:
            log.error("Error de polling: %s (reintento en 10 s)", e)
            time.sleep(10)
        if time.time() - ultima_revision > 60:
            try:
                alertas.revisar_pendientes()
            except Exception as e:
                log.exception("Error revisando pendientes: %s", e)
            ultima_revision = time.time()


if __name__ == "__main__":
    main()
