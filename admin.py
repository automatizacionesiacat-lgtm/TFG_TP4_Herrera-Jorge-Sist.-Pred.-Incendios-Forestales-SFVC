import argparse
import getpass

import seguridad
from base_de_datos import inicializar_base_de_datos, cargar_limite_geojson
from db import conectar, set_umbral, get_umbral


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crear-usuario")
    c.add_argument("nombre")
    c.add_argument("rol", choices=seguridad.ROLES)
    sub.add_parser("listar-usuarios")
    u = sub.add_parser("umbral")
    u.add_argument("valor", type=float, nargs="?")
    u.add_argument("--usuario", default="consola")
    li = sub.add_parser("limite")
    li.add_argument("geojson")
    a = ap.parse_args()
    inicializar_base_de_datos()

    if a.cmd == "crear-usuario":
        clave = getpass.getpass("Contraseña (mín. 10 caracteres): ")
        if clave != getpass.getpass("Repetir contraseña: "):
            raise SystemExit("Las contraseñas no coinciden")
        seguridad.crear_usuario(a.nombre, a.rol, clave)
        print(f"Usuario {a.nombre} ({a.rol}) creado.")
    elif a.cmd == "listar-usuarios":
        with conectar() as conn:
            for r in conn.execute("SELECT nombre, rol FROM Usuario ORDER BY nombre"):
                print(f"{r['nombre']:20} {r['rol']}")
    elif a.cmd == "umbral":
        with conectar() as conn:
            if a.valor is None:
                print(f"Umbral vigente: {get_umbral(conn):.2f}")
            else:
                set_umbral(conn, a.valor, a.usuario)
                print(f"Umbral cambiado a {a.valor:.2f}")
    elif a.cmd == "limite":
        cargar_limite_geojson(a.geojson)


if __name__ == "__main__":
    main()
