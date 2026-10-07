from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional
import subprocess
import sys
import os

from auth import usuario_actual
from database import get_pg_cursor

router = APIRouter(
    prefix="/api/equipos/{equipo_id}",
    tags=["Control de modos"]
)

def validar_equipo(equipo_id: str):
    """Valida que el equipo exista y esté activo. Devuelve sus datos."""
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, nombre, tipo_operacion, descripcion, activo
            FROM equipos
            WHERE id = %s
        """, (equipo_id,))
        equipo = cur.fetchone()
        if not equipo:
            raise HTTPException(
                status_code=404,
                detail=f"Equipo '{equipo_id}' no encontrado"
            )
        if not equipo["activo"]:
            raise HTTPException(
                status_code=400,
                detail=f"Equipo '{equipo_id}' está dado de baja"
            )
        return dict(equipo)

# -----------------------------
# MODELOS DE DATOS
# -----------------------------

class IniciarSesion(BaseModel):
    """Datos opcionales al iniciar una sesión de modo."""
    notas: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Notas opcionales del operador sobre esta sesión"
    )

# -----------------------------
# UTILIDADES
# -----------------------------

def obtener_sesion_activa(equipo_id: str) -> Optional[dict]:
    """Devuelve la sesión activa actual del equipo, si existe."""
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, modo, fecha_inicio, usuario, notas
            FROM sesiones_modo
            WHERE equipo_id = %s AND activa = TRUE
            LIMIT 1
        """, (equipo_id,))
        return cur.fetchone()

def cerrar_sesion_activa(equipo_id: str):
    """Cierra cualquier sesión activa del equipo."""
    with get_pg_cursor() as cur:
        cur.execute("""
            UPDATE sesiones_modo
            SET activa = FALSE, fecha_fin = NOW()
            WHERE equipo_id = %s AND activa = TRUE
        """, (equipo_id,))

def abrir_sesion(equipo_id: str, modo: str, usuario: str, notas: Optional[str]) -> int:
    """Abre una nueva sesión del modo indicado y devuelve su ID."""
    with get_pg_cursor() as cur:
        cur.execute("""
            INSERT INTO sesiones_modo (equipo_id, modo, usuario, notas)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (equipo_id, modo, usuario, notas))
        return cur.fetchone()["id"]

# -----------------------------
# CONSULTA DE ESTADO ACTUAL
# -----------------------------

@router.get("/estado")
async def obtener_estado(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Devuelve el modo actual del sistema para este equipo:
    'aprendizaje', 'monitoreo' o 'inactivo'.
    """
    validar_equipo(equipo_id)

    sesion = obtener_sesion_activa(equipo_id)

    if not sesion:
        return {
            "equipo_id": equipo_id,
            "modo": "inactivo",
            "sesion": None
        }

    return {
        "equipo_id": equipo_id,
        "modo": sesion["modo"],
        "sesion": {
            "id": sesion["id"],
            "fecha_inicio": sesion["fecha_inicio"].isoformat(),
            "usuario": sesion["usuario"],
            "notas": sesion["notas"]
        }
    }

# -----------------------------
# MODO APRENDIZAJE
# -----------------------------

@router.post("/aprendizaje/iniciar")
async def iniciar_aprendizaje(
    equipo_id: str,
    datos: IniciarSesion = IniciarSesion(),
    user: dict = Depends(usuario_actual)
):
    """
    Inicia el modo aprendizaje. Si ya hay una sesión activa, devuelve error.
    A partir de este momento, los datos que lleguen se considerarán
    parte del período de aprendizaje.
    """
    validar_equipo(equipo_id)

    sesion_existente = obtener_sesion_activa(equipo_id)
    if sesion_existente:
        raise HTTPException(
            status_code=409,
            detail=f"Ya hay una sesión activa de '{sesion_existente['modo']}'. "
                   f"Finalícela antes de iniciar una nueva."
        )

    id_sesion = abrir_sesion(equipo_id, "aprendizaje", user["usuario"], datos.notas)

    return {
        "mensaje": "Modo aprendizaje iniciado",
        "id_sesion": id_sesion,
        "equipo_id": equipo_id,
        "modo": "aprendizaje",
        "usuario": user["nombre"],
        "fecha_inicio": datetime.now(timezone.utc).isoformat()
    }

@router.post("/aprendizaje/finalizar")
async def finalizar_aprendizaje(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Finaliza el modo aprendizaje y dispara el entrenamiento del modelo
    con los datos recolectados durante la sesión.
    """
    validar_equipo(equipo_id)

    sesion = obtener_sesion_activa(equipo_id)
    if not sesion or sesion["modo"] != "aprendizaje":
        raise HTTPException(
            status_code=400,
            detail="No hay una sesión de aprendizaje activa para finalizar."
        )

    # Cerrar la sesión
    cerrar_sesion_activa(equipo_id)

    # Disparar el entrenamiento en segundo plano
    # Asume que el script está en analizador/entrenar.py relativo a la raíz del proyecto
    script_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "analizador", "entrenar.py")
    )

    # La salida se redirige a un archivo (no a PIPE): con PIPE y sin nada que
    # lea el buffer, entrenar.py se bloquea indefinidamente en cuanto su
    # salida supera el tamaño del buffer del pipe del sistema operativo,
    # lo cual ocurre con datasets grandes o ciclicos (mas lineas impresas).
    log_dir = os.path.join(os.path.dirname(script_path), "logs_entrenamiento")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"{equipo_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
    )
    log_file = open(log_path, "w", encoding="utf-8")

    # PYTHONIOENCODING=utf-8: sin esto, el proceso hijo hereda la codificación
    # de consola por defecto de Windows (cp1252) para su propio sys.stdout,
    # independientemente de que el archivo destino se haya abierto en UTF-8,
    # y revienta con UnicodeEncodeError al imprimir caracteres como "≥".
    env_hijo = dict(os.environ, PYTHONIOENCODING="utf-8")

    proceso = subprocess.Popen(
        [sys.executable, script_path, "--equipo", equipo_id],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env_hijo
    )

    return {
        "mensaje": "Aprendizaje finalizado. Entrenamiento iniciado en segundo plano.",
        "id_sesion": sesion["id"],
        "pid_entrenamiento": proceso.pid,
        "fecha_finalizacion": datetime.now(timezone.utc).isoformat()
    }

# -----------------------------
# MODO MONITOREO
# -----------------------------

@router.post("/monitoreo/iniciar")
async def iniciar_monitoreo(
    equipo_id: str,
    datos: IniciarSesion = IniciarSesion(),
    user: dict = Depends(usuario_actual)
):
    """
    Inicia el modo monitoreo. Requiere que exista un modelo activo
    entrenado previamente.
    """
    validar_equipo(equipo_id)

    sesion_existente = obtener_sesion_activa(equipo_id)
    if sesion_existente:
        raise HTTPException(
            status_code=409,
            detail=f"Ya hay una sesión activa de '{sesion_existente['modo']}'. "
                   f"Finalícela antes de iniciar una nueva."
        )

    # Verificar que exista un modelo activo
    # Verificar que exista un modelo activo para ESTE equipo
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id FROM modelos
            WHERE estado = 'activo' AND equipo_id = %s
            LIMIT 1
        """, (equipo_id,))
        if not cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail=f"No hay modelo activo para el equipo '{equipo_id}'. Debe completar primero una sesión de aprendizaje."
            )

    id_sesion = abrir_sesion(equipo_id, "monitoreo", user["usuario"], datos.notas)

    return {
        "mensaje": "Modo monitoreo iniciado",
        "id_sesion": id_sesion,
        "equipo_id": equipo_id,
        "modo": "monitoreo",
        "usuario": user["nombre"],
        "fecha_inicio": datetime.now(timezone.utc).isoformat()
    }

@router.post("/monitoreo/detener")
async def detener_monitoreo(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Detiene el modo monitoreo. El sistema deja de evaluar datos en tiempo real.
    """
    validar_equipo(equipo_id)

    sesion = obtener_sesion_activa(equipo_id)
    if not sesion or sesion["modo"] != "monitoreo":
        raise HTTPException(
            status_code=400,
            detail="No hay una sesión de monitoreo activa para detener."
        )

    cerrar_sesion_activa(equipo_id)

    return {
        "mensaje": "Monitoreo detenido",
        "id_sesion": sesion["id"],
        "fecha_finalizacion": datetime.now(timezone.utc).isoformat()
    }