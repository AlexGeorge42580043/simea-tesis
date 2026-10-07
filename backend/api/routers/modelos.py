from fastapi import APIRouter, Depends, HTTPException
from auth import usuario_actual
from database import get_pg_cursor
from datetime import datetime, timezone
import os

router = APIRouter(
    prefix="/api/equipos/{equipo_id}/modelos",
    tags=["Modelos"]
)

# -----------------------------
# LISTAR MODELOS
# -----------------------------

@router.get("")
async def listar_modelos(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """Lista todos los modelos entrenados para el equipo."""
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, k_optimo, fecha_entrenamiento,
                   fecha_inicio_datos, fecha_fin_datos,
                   estado, descripcion, umbral_iforest
            FROM modelos
            WHERE equipo_id = %s
            ORDER BY fecha_entrenamiento DESC
        """, (equipo_id,))
        modelos = cur.fetchall()

    return {
        "modelos": [
            {
                "id": m["id"],
                "k_optimo": m["k_optimo"],
                "fecha_entrenamiento": m["fecha_entrenamiento"].isoformat(),
                "fecha_inicio_datos": m["fecha_inicio_datos"].isoformat(),
                "fecha_fin_datos": m["fecha_fin_datos"].isoformat(),
                "estado": m["estado"],
                "descripcion": m["descripcion"],
                "umbral_iforest": float(m["umbral_iforest"]) if m["umbral_iforest"] else None,
            }
            for m in modelos
        ]
    }

# -----------------------------
# ACTIVAR MODELO
# -----------------------------

@router.post("/{id_modelo}/activar")
async def activar_modelo(
    equipo_id: str,
    id_modelo: int,
    user: dict = Depends(usuario_actual)
):
    """Activa un modelo específico y desactiva el actualmente activo de este equipo."""
    with get_pg_cursor() as cur:
        # Verificar que el modelo existe Y pertenece al equipo
        cur.execute("SELECT id FROM modelos WHERE id = %s AND equipo_id = %s", (id_modelo, equipo_id))
        if not cur.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"Modelo {id_modelo} no encontrado para el equipo '{equipo_id}'"
            )

        # Verificar que no haya monitoreo activo en ESTE equipo
        cur.execute("""
            SELECT id FROM sesiones_modo
            WHERE modo = 'monitoreo' AND activa = TRUE AND equipo_id = %s
            LIMIT 1
        """, (equipo_id,))
        if cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail="No se puede cambiar el modelo mientras el monitoreo está activo. Deténgalo primero."
            )

        # Desactivar solo el modelo activo de ESTE equipo
        cur.execute("UPDATE modelos SET estado = 'inactivo' WHERE estado = 'activo' AND equipo_id = %s", (equipo_id,))
        cur.execute("UPDATE modelos SET estado = 'activo' WHERE id = %s", (id_modelo,))

    return {
        "mensaje": "Modelo activado correctamente",
        "id_modelo": id_modelo,
        "usuario": user["nombre"],
        "fecha_activacion": datetime.now(timezone.utc).isoformat()
    }

# -----------------------------
# ACTUALIZAR DESCRIPCIÓN
# -----------------------------

from pydantic import BaseModel, Field
from typing import Optional

class ActualizarDescripcion(BaseModel):
    descripcion: Optional[str] = Field(default=None, max_length=500)

@router.patch("/{id_modelo}")
async def actualizar_descripcion(
    equipo_id: str,
    id_modelo: int,
    datos: ActualizarDescripcion,
    user: dict = Depends(usuario_actual)
):
    """Actualiza la descripción de un modelo."""
    with get_pg_cursor() as cur:
        cur.execute("SELECT id FROM modelos WHERE id = %s AND equipo_id = %s", (id_modelo, equipo_id))
        if not cur.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"Modelo {id_modelo} no encontrado para el equipo '{equipo_id}'"
            )

        cur.execute(
            "UPDATE modelos SET descripcion = %s WHERE id = %s",
            (datos.descripcion, id_modelo)
        )

    return {
        "mensaje": "Descripción actualizada",
        "id_modelo": id_modelo,
        "descripcion": datos.descripcion
    }

# -----------------------------
# ELIMINAR MODELO
# -----------------------------

@router.delete("/{id_modelo}")
async def eliminar_modelo(
    equipo_id: str,
    id_modelo: int,
    user: dict = Depends(usuario_actual)
):
    """
    Elimina un modelo. Se permite eliminar cualquier modelo (activo o inactivo)
    siempre que no haya una sesión de monitoreo en curso para el equipo.
    Si se elimina el modelo activo y no queda ningún otro, el equipo quedará
    sin modelo y requerirá una nueva sesión de aprendizaje para poder
    reanudar el monitoreo.
    """
    with get_pg_cursor() as cur:
        cur.execute(
            "SELECT estado, ruta_kmeans, ruta_isolation_forest FROM modelos WHERE id = %s AND equipo_id = %s",
            (id_modelo, equipo_id)
        )
        modelo = cur.fetchone()

        if not modelo:
            raise HTTPException(
                status_code=404,
                detail=f"Modelo {id_modelo} no encontrado para el equipo '{equipo_id}'"
            )

        # Bloquear el borrado si el equipo tiene una sesión de monitoreo activa.
        # Sin este chequeo, borrar el modelo activo dejaría al monitor evaluando
        # datos contra un modelo que ya no existe.
        cur.execute("""
            SELECT id FROM sesiones_modo
            WHERE modo = 'monitoreo' AND activa = TRUE AND equipo_id = %s
            LIMIT 1
        """, (equipo_id,))
        if cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail="No se puede eliminar el modelo mientras el monitoreo está activo. Deténgalo primero."
            )

        # Borrar dependencias en cascada
        cur.execute("DELETE FROM bandas_tolerancia WHERE id_modelo = %s", (id_modelo,))
        cur.execute("DELETE FROM curva_patron WHERE id_modelo = %s", (id_modelo,))
        cur.execute("DELETE FROM modelos WHERE id = %s", (id_modelo,))

    # Borrar los archivos .pkl asociados
    for ruta in [modelo["ruta_kmeans"], modelo["ruta_isolation_forest"]]:
        if ruta and os.path.exists(ruta):
            try:
                os.remove(ruta)
            except Exception as e:
                print(f"No se pudo borrar {ruta}: {e}")

    return {
        "mensaje": "Modelo eliminado correctamente",
        "id_modelo": id_modelo,
        "era_activo": modelo["estado"] == "activo",
        "usuario": user["nombre"]
    }