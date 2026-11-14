"""Usuarios, contraseñas con hash (bcrypt) y roles (RBAC, HU-15)."""
import bcrypt

from db import conectar

ROLES = ("administrador", "defensa_civil", "bomberos", "autoridad")
_HASH_FALSO = bcrypt.hashpw(b"relleno-para-igualar-tiempos", bcrypt.gensalt())


def hashear_clave(clave: str) -> str:
    datos = clave.encode("utf-8")
    if len(clave) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres")
    if len(datos) > 72:  # límite de bcrypt
        raise ValueError("La contraseña no puede superar 72 bytes")
    return bcrypt.hashpw(datos, bcrypt.gensalt()).decode()


def verificar_clave(clave: str, hash_guardado) -> bool:
    try:
        return bcrypt.checkpw(clave.encode("utf-8")[:72], (hash_guardado or _HASH_FALSO.decode()).encode())
    except ValueError:
        return False


def crear_usuario(nombre: str, rol: str, clave: str):
    if rol not in ROLES:
        raise ValueError(f"Rol inválido. Opciones: {', '.join(ROLES)}")
    if not nombre.strip():
        raise ValueError("Nombre vacío")
    with conectar() as conn:
        conn.execute("INSERT INTO Usuario (nombre, rol, hash_clave) VALUES (?,?,?)", (nombre.strip(), rol, hashear_clave(clave)))


def autenticar(nombre: str, clave: str):
    """Devuelve (nombre, rol) o None. Tarda lo mismo exista o no el usuario."""
    with conectar() as conn:
        fila = conn.execute("SELECT nombre, rol, hash_clave FROM Usuario WHERE nombre = ?", (nombre,)).fetchone()
    ok = verificar_clave(clave, fila["hash_clave"] if fila else None)
    return (fila["nombre"], fila["rol"]) if (fila and ok) else None