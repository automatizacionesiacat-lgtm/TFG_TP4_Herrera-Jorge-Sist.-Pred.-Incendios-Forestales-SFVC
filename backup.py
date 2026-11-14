import io
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

import config

log = logging.getLogger("backup")
CONSERVAR = 14  # cantidad de respaldos locales


def crear_respaldo() -> Path:
    clave = os.getenv("BACKUP_KEY")
    if not clave:
        raise SystemExit("Falta BACKUP_KEY en el .env (python backup.py --generar-clave). El respaldo debe ir cifrado.")
    fernet = Fernet(clave.encode())
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copia_db = Path(tmp) / "sistema_incendios.db"
        origen = sqlite3.connect(config.DB_PATH)
        destino = sqlite3.connect(copia_db)
        origen.backup(destino)  # copia consistente aunque el sistema esté escribiendo
        destino.close()
        origen.close()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(copia_db, arcname="sistema_incendios.db")
            env = config.BASE_DIR / ".env"
            if env.exists():
                tar.add(env, arcname=".env")
            if config.CORPUS_DIR.exists():
                tar.add(config.CORPUS_DIR, arcname="corpus")
    from db import ahora

    ruta = config.BACKUP_DIR / f"respaldo_{ahora():%Y%m%d_%H%M%S}.tar.gz.enc"
    ruta.write_bytes(fernet.encrypt(buf.getvalue()))
    log.info("Respaldo creado: %s", ruta)

    for viejo in sorted(config.BACKUP_DIR.glob("respaldo_*.tar.gz.enc"))[:-CONSERVAR]:
        viejo.unlink()
    sync = os.getenv("BACKUP_SYNC_DIR")
    if sync:
        try:
            Path(sync).mkdir(parents=True, exist_ok=True)
            shutil.copy2(ruta, Path(sync) / ruta.name)
            log.info("Copiado a la carpeta sincronizada: %s", sync)
        except OSError as e:
            log.error("No se pudo copiar a BACKUP_SYNC_DIR: %s", e)
    return ruta


if __name__ == "__main__":
    if "--generar-clave" in sys.argv:
        print(Fernet.generate_key().decode())
    else:
        print(crear_respaldo())
