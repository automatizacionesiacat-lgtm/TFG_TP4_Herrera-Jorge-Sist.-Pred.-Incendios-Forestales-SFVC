import argparse
import logging

import alertas
import exportar_datos
import informes
import ingesta
import modelo_ml
from base_de_datos import inicializar_base_de_datos

log = logging.getLogger("pipeline")


def _paso(nombre, fn, *a, **k):
    """Cada paso falla de forma aislada: un error en uno no impide los siguientes (disponibilidad)."""
    try:
        return fn(*a, **k)
    except Exception as e:
        log.exception("Paso '%s' falló: %s", nombre, e)
        return None


def diario():
    inicializar_base_de_datos()
    _paso("ingesta", ingesta.ejecutar)
    _paso("riesgo", modelo_ml.predecir_hoy)
    _paso("alerta", alertas.emitir_alerta)
    _paso("exportar", exportar_datos.generar_json)


def focos():
    inicializar_base_de_datos()
    _paso("focos", ingesta.ingestar_focos)
    _paso("exportar", exportar_datos.generar_json)


def semanal(demo=False):
    inicializar_base_de_datos()
    _paso("corpus", informes.indexar_corpus)
    _paso("informe", informes.generar_y_enviar_informe, demo=demo)
    _paso("estado_entrega", informes.actualizar_estado_entrega)


def demostracion():
    """Para el video: la corrida queda marcada como [DEMO] en el mensaje, el informe y la base (no se mezcla con datos reales)."""
    inicializar_base_de_datos()
    _paso("ingesta", ingesta.ejecutar)
    modelo_ml.predecir_hoy(demo_indice=0.85)
    alertas.emitir_alerta(demo=True, forzar_duplicado=True)
    _paso("exportar", exportar_datos.generar_json)
    semanal(demo=True)


def inicial():
    inicializar_base_de_datos()
    ingesta.ingestar_clima_historico()
    ingesta.ingestar_focos_historico()
    modelo_ml.entrenar()
    informes.indexar_corpus()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ciclo", choices=["diario", "semanal", "focos", "entrenar", "demo", "inicial"])
    a = ap.parse_args()
    {"diario": diario, "semanal": semanal, "focos": focos, "entrenar": lambda: (inicializar_base_de_datos(), modelo_ml.entrenar()),
     "demo": demostracion, "inicial": inicial}[a.ciclo]()
