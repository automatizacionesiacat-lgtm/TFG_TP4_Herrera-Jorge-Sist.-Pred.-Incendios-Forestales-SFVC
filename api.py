import logging
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import config
import seguridad
from base_de_datos import inicializar_base_de_datos
from db import conectar, ahora, get_umbral, set_umbral
from exportar_datos import construir_datos, listar_alertas

log = logging.getLogger("api")
SECRET = os.getenv("API_SECRET", "")
if len(SECRET) < 32 or SECRET.startswith("cambiar"):
    raise RuntimeError("Definí API_SECRET en el .env (al menos 32 caracteres aleatorios). Ver .env.example.")

inicializar_base_de_datos()
app = FastAPI(title="Sistema de predicción de riesgo de incendios - San Fernando del Valle de Catamarca", docs_url=None, redoc_url=None)
esquema = HTTPBearer(auto_error=False)
_fallos = defaultdict(list)  # usuario -> instantes de intentos fallidos


class Login(BaseModel):
    nombre: str
    clave: str


class NuevoUmbral(BaseModel):
    valor: float


class NuevoUsuario(BaseModel):
    nombre: str = Field(min_length=3, max_length=40)
    rol: str
    clave: str


def usuario_actual(cred: HTTPAuthorizationCredentials = Depends(esquema)) -> dict:
    if cred is None:
        raise HTTPException(401, "Falta autenticación")
    try:
        p = jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Sesión inválida o vencida")
    return {"nombre": p["sub"], "rol": p["rol"]}


def solo(*roles):
    def dep(u: dict = Depends(usuario_actual)) -> dict:
        if u["rol"] not in roles:
            raise HTTPException(403, "Tu rol no tiene acceso a esta función")
        return u
    return dep


@app.post("/api/login")
def login(datos: Login):
    ventana = [t for t in _fallos[datos.nombre] if time.time() - t < 300]
    _fallos[datos.nombre] = ventana
    if len(ventana) >= 5:
        raise HTTPException(429, "Demasiados intentos fallidos; esperá 5 minutos")
    res = seguridad.autenticar(datos.nombre, datos.clave)
    if res is None:
        _fallos[datos.nombre].append(time.time())
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    nombre, rol = res
    token = jwt.encode({"sub": nombre, "rol": rol, "exp": ahora() + timedelta(hours=8)}, SECRET, algorithm="HS256")
    return {"token": token, "nombre": nombre, "rol": rol}


@app.get("/api/datos")
def datos(u: dict = Depends(usuario_actual)):
    return construir_datos()


@app.get("/api/alertas")
def alertas(desde: str = None, estado: str = Query(None, pattern="^(pendiente|confirmada|sin_confirmar)$"),
            organismo: str = Query(None, pattern="^(Defensa Civil|Bomberos)$"), limite: int = 50,
            u: dict = Depends(usuario_actual)):
    with conectar() as conn:
        return listar_alertas(conn, desde, estado, organismo, limite)


@app.get("/api/informes")
def lista_informes(u: dict = Depends(usuario_actual)):
    """Informes aprobados con su estado de entrega y de confirmación de recepción (HU-06 / HU-11)."""
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id_informe, periodo_inicio, periodo_fin, fecha_generacion, estado_entrega, confirmado_por, fecha_confirmacion "
            "FROM Informe WHERE estado_revision = 'aprobado' ORDER BY id_informe DESC LIMIT 10")]


@app.post("/api/informes/{id_informe}/confirmar")
def confirmar_informe(id_informe: int, u: dict = Depends(solo("autoridad", "administrador"))):
    import informes

    try:
        return {"estado_entrega": informes.confirmar_recepcion_informe(id_informe, u["nombre"])}
    except LookupError:
        raise HTTPException(404, "Informe inexistente")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/config")
def ver_config(u: dict = Depends(solo("administrador"))):
    with conectar() as conn:
        return {"umbral": get_umbral(conn), "minimo": config.UMBRAL_MIN, "maximo": config.UMBRAL_MAX}


@app.put("/api/umbral")
def cambiar_umbral(nuevo: NuevoUmbral, u: dict = Depends(solo("administrador"))):
    try:
        with conectar() as conn:
            set_umbral(conn, nuevo.valor, u["nombre"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    log.info("Umbral cambiado a %.2f por %s", nuevo.valor, u["nombre"])
    return {"umbral": nuevo.valor}


@app.get("/api/usuarios")
def listar_usuarios(u: dict = Depends(solo("administrador"))):
    with conectar() as conn:
        return [dict(r) for r in conn.execute("SELECT id_usuario, nombre, rol FROM Usuario ORDER BY nombre")]


@app.post("/api/usuarios", status_code=201)
def alta_usuario(n: NuevoUsuario, u: dict = Depends(solo("administrador"))):
    try:
        seguridad.crear_usuario(n.nombre, n.rol, n.clave)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception:
        raise HTTPException(409, "Ese usuario ya existe")
    return {"creado": n.nombre, "rol": n.rol}


@app.get("/api/health")
def health():
    with conectar() as conn:
        conn.execute("SELECT 1")
    return {"estado": "ok", "hora": ahora().isoformat(timespec="seconds")}


@app.get("/")
def inicio():
    return FileResponse(Path(__file__).with_name("dashboard.html"))
