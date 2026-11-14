import logging
import time

import schedule

import backup
import pipeline
from base_de_datos import inicializar_base_de_datos

log = logging.getLogger("scheduler")


def _seguro(fn):
    def envoltura():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001  (SystemExit del respaldo incluido)
            log.exception("Tarea programada falló: %s", e)
    return envoltura


schedule.every(3).hours.do(_seguro(pipeline.focos))
schedule.every().day.at("07:00").do(_seguro(pipeline.diario))
schedule.every().monday.at("08:00").do(_seguro(pipeline.semanal))
schedule.every().day.at("23:00").do(_seguro(backup.crear_respaldo))

if __name__ == "__main__":
    inicializar_base_de_datos()
    log.info("Programadores en marcha. Próximas tareas: %s", [str(j.next_run) for j in schedule.get_jobs()])
    while True:
        schedule.run_pending()
        time.sleep(30)
