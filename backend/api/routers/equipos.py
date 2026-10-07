from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import re

from auth import usuario_actual
from database import get_pg_cursor

router = APIRouter(
    prefix="/api/equipos",
    tags=["Equipos"]
)

# -----------------------------
# MODELOS DE DATOS
# -----------------------------

class NuevoEquipo(BaseModel):
    """
    Datos requeridos para dar de alta un nuevo equipo.

    Nota: el sistema NO pide `tipo_operacion` al crear el equipo. Este campo
    se determina automáticamente durante el entrenamiento del primer modelo:
    si el aprendizaje detecta ciclos periódicos en los datos, el modelo se
    marca como cíclico (y se construye su curva patrón); en caso contrario,
    se trata como continuo. Esta decisión es propiedad del modelo, no del
    equipo, y queda registrada en el campo `es_ciclico` de la tabla `modelos`.
    """
    id: str = Field(..., min_length=3, max_length=100, description="Identificador único del equipo (ej: motor_cinta_2)")
    nombre: str = Field(..., min_length=3, max_length=200, description="Nombre descriptivo del equipo")
    descripcion: Optional[str] = Field(default=None, max_length=2000, description="Descripción del equipo, proceso donde opera, características técnicas")
    tiempo_minimo_aprendizaje: int = Field(..., ge=5, le=1440, description="Tiempo mínimo recomendado de aprendizaje en minutos")
    tiempo_recomendado_aprendizaje: int = Field(..., ge=5, le=1440, description="Tiempo recomendado de aprendizaje en minutos")


class EditarEquipo(BaseModel):
    """
    Datos para editar un equipo existente. Todos los campos son opcionales:
    solo se actualizan los que se envían con un valor distinto de None.
    El ID nunca se puede modificar (es la clave primaria).

    `tipo_operacion` NO es editable: es propiedad del modelo entrenado
    (`modelos.es_ciclico`), no del equipo.
    """
    nombre: Optional[str] = Field(default=None, min_length=3, max_length=200)
    descripcion: Optional[str] = Field(default=None, max_length=2000)
    tiempo_minimo_aprendizaje: Optional[int] = Field(default=None, ge=5, le=1440)
    tiempo_recomendado_aprendizaje: Optional[int] = Field(default=None, ge=5, le=1440)
    activo: Optional[bool] = Field(default=None, description="True reactiva el equipo, False lo da de baja")

# -----------------------------
# LISTAR EQUIPOS
# -----------------------------

@router.get("")
async def listar_equipos(
    incluir_inactivos: bool = False,
    user: dict = Depends(usuario_actual)
):
    """
    Lista los equipos del sistema.
    Si incluir_inactivos=True, también devuelve los dados de baja.
    Por defecto solo activos (compatibilidad con la interfaz actual).
    """
    with get_pg_cursor() as cur:
        if incluir_inactivos:
            cur.execute("""
                SELECT id, nombre, tipo_operacion, descripcion, activo, fecha_alta
                FROM equipos
                ORDER BY fecha_alta
            """)
        else:
            cur.execute("""
                SELECT id, nombre, tipo_operacion, descripcion, activo, fecha_alta
                FROM equipos
                WHERE activo = TRUE
                ORDER BY fecha_alta
            """)
        equipos = cur.fetchall()

    return {
        "equipos": [
            {
                "id": e["id"],
                "nombre": e["nombre"],
                "tipo_operacion": e["tipo_operacion"],
                "descripcion": e["descripcion"],
                "activo": e["activo"],
                "fecha_alta": e["fecha_alta"].isoformat(),
                "fecha_creacion": e["fecha_alta"].isoformat(),  # alias para consistencia con el frontend
            }
            for e in equipos
        ]
    }

# -----------------------------
# DETALLE DE UN EQUIPO
# -----------------------------

@router.get("/{equipo_id}")
async def detalle_equipo(equipo_id: str, user: dict = Depends(usuario_actual)):
    """Devuelve el detalle de un equipo específico."""
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, nombre, tipo_operacion, descripcion, activo, fecha_alta,
                   tiempo_minimo_aprendizaje, tiempo_recomendado_aprendizaje
            FROM equipos
            WHERE id = %s
        """, (equipo_id,))
        equipo = cur.fetchone()

    if not equipo:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")

    return {
        "id": equipo["id"],
        "nombre": equipo["nombre"],
        "tipo_operacion": equipo["tipo_operacion"],
        "descripcion": equipo["descripcion"],
        "activo": equipo["activo"],
        "fecha_alta": equipo["fecha_alta"].isoformat(),
        "tiempo_minimo_aprendizaje": equipo["tiempo_minimo_aprendizaje"],
        "tiempo_recomendado_aprendizaje": equipo["tiempo_recomendado_aprendizaje"],
    }

# -----------------------------
# CREAR NUEVO EQUIPO
# -----------------------------

@router.post("")
async def crear_equipo(datos: NuevoEquipo, user: dict = Depends(usuario_actual)):
    """
    Crea un nuevo equipo en el sistema. El ID debe ser único, alfanumérico
    con guiones bajos, sin espacios (ej: 'bomba_agua_02').

    Ya no se pide `tipo_operacion` al crear: el sistema lo determina
    automáticamente al entrenar el primer modelo. Se guarda un valor
    neutro ('desconocido') en la columna legacy `tipo_operacion` por
    compatibilidad con la estructura de la tabla; el valor real relevante
    pasa a ser `modelos.es_ciclico` una vez entrenado el modelo.
    """
    # Validar formato del ID
    if not re.match(r'^[a-z0-9_]+$', datos.id):
        raise HTTPException(
            status_code=400,
            detail="El ID debe contener solo letras minúsculas, números y guiones bajos"
        )

    # Validar coherencia de tiempos
    if datos.tiempo_recomendado_aprendizaje < datos.tiempo_minimo_aprendizaje:
        raise HTTPException(
            status_code=400,
            detail="El tiempo recomendado no puede ser menor que el tiempo mínimo"
        )

    # Verificar que el ID no exista
    with get_pg_cursor() as cur:
        cur.execute("SELECT id FROM equipos WHERE id = %s", (datos.id,))
        if cur.fetchone():
            raise HTTPException(
                status_code=409,
                detail=f"Ya existe un equipo con el ID '{datos.id}'"
            )

        # Insertar el nuevo equipo.
        # tipo_operacion se guarda como 'desconocido' porque el sistema
        # detecta automáticamente el tipo real durante el entrenamiento.
        cur.execute("""
            INSERT INTO equipos (id, nombre, tipo_operacion, descripcion,
                                 tiempo_minimo_aprendizaje, tiempo_recomendado_aprendizaje,
                                 activo)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """, (
            datos.id,
            datos.nombre,
            'desconocido',
            datos.descripcion,
            datos.tiempo_minimo_aprendizaje,
            datos.tiempo_recomendado_aprendizaje,
        ))

    return {
        "mensaje": "Equipo creado exitosamente",
        "id": datos.id,
        "nombre": datos.nombre,
        "usuario": user["nombre"],
    }

# -----------------------------
# EDITAR EQUIPO EXISTENTE
# -----------------------------

@router.put("/{equipo_id}")
async def editar_equipo(
    equipo_id: str,
    datos: EditarEquipo,
    user: dict = Depends(usuario_actual)
):
    """
    Edita los campos modificables de un equipo. Solo se actualizan los
    campos enviados (los que sean None se ignoran).

    Reglas de validación:
      - El `id` y la `fecha_alta` nunca se modifican.
      - Cambiar `tipo_operacion` requiere que el equipo NO tenga modelos
        entrenados (los modelos asumen un tipo específico: los cíclicos
        generan curva patrón, los continuos no).
      - Cambiar `activo` (dar de baja o reactivar) requiere que el equipo
        NO tenga sesión de aprendizaje o monitoreo activa.
      - Los modelos existentes NO se tocan al dar de baja: quedan como
        estén, y si el equipo se reactiva vuelven a estar disponibles.
    """
    with get_pg_cursor() as cur:
        # Verificar que el equipo existe
        cur.execute("""
            SELECT id, nombre, activo
            FROM equipos
            WHERE id = %s
        """, (equipo_id,))
        equipo_actual = cur.fetchone()

        if not equipo_actual:
            raise HTTPException(status_code=404, detail="Equipo no encontrado")

        # Validar coherencia de tiempos si vienen ambos
        tiempo_min = datos.tiempo_minimo_aprendizaje
        tiempo_rec = datos.tiempo_recomendado_aprendizaje
        if tiempo_min is not None and tiempo_rec is not None:
            if tiempo_rec < tiempo_min:
                raise HTTPException(
                    status_code=400,
                    detail="El tiempo recomendado no puede ser menor que el tiempo mínimo"
                )
        # Si solo se envía uno, validar contra el actual guardado
        elif tiempo_min is not None or tiempo_rec is not None:
            cur.execute("""
                SELECT tiempo_minimo_aprendizaje, tiempo_recomendado_aprendizaje
                FROM equipos WHERE id = %s
            """, (equipo_id,))
            fila = cur.fetchone()
            tm = tiempo_min if tiempo_min is not None else fila["tiempo_minimo_aprendizaje"]
            tr = tiempo_rec if tiempo_rec is not None else fila["tiempo_recomendado_aprendizaje"]
            if tr < tm:
                raise HTTPException(
                    status_code=400,
                    detail="El tiempo recomendado no puede ser menor que el tiempo mínimo"
                )

        # Validar cambio de estado activo/inactivo
        if datos.activo is not None and datos.activo != equipo_actual["activo"]:
            # Verificar que no haya sesión activa antes de cambiar el estado
            cur.execute("""
                SELECT modo FROM sesiones_modo
                WHERE equipo_id = %s AND activa = TRUE
                LIMIT 1
            """, (equipo_id,))
            sesion = cur.fetchone()
            if sesion:
                modo = sesion["modo"]
                accion = "dar de baja" if not datos.activo else "reactivar"
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede {accion} el equipo: hay una sesión de {modo} activa. Deténgala primero."
                )

        # Construir UPDATE dinámico solo con los campos enviados
        campos = []
        valores = []

        if datos.nombre is not None:
            campos.append("nombre = %s")
            valores.append(datos.nombre)
        if datos.descripcion is not None:
            campos.append("descripcion = %s")
            valores.append(datos.descripcion)
        if datos.tiempo_minimo_aprendizaje is not None:
            campos.append("tiempo_minimo_aprendizaje = %s")
            valores.append(datos.tiempo_minimo_aprendizaje)
        if datos.tiempo_recomendado_aprendizaje is not None:
            campos.append("tiempo_recomendado_aprendizaje = %s")
            valores.append(datos.tiempo_recomendado_aprendizaje)
        if datos.activo is not None:
            campos.append("activo = %s")
            valores.append(datos.activo)

        if not campos:
            raise HTTPException(
                status_code=400,
                detail="No se envió ningún campo para actualizar"
            )

        valores.append(equipo_id)
        cur.execute(
            f"UPDATE equipos SET {', '.join(campos)} WHERE id = %s",
            valores
        )

    return {
        "mensaje": "Equipo actualizado correctamente",
        "id": equipo_id,
        "usuario": user["nombre"],
    }

# -----------------------------
# DAR DE BAJA UN EQUIPO
# -----------------------------

@router.delete("/{equipo_id}")
async def dar_de_baja_equipo(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Da de baja un equipo (borrado lógico: marca activo = false).
    El registro NO se elimina de la base: los modelos, alertas históricas,
    datos y sesiones se preservan. El equipo puede reactivarse más adelante
    con PUT `/api/equipos/{id}` enviando `{activo: true}`.

    No se permite dar de baja si el equipo tiene sesión de aprendizaje
    o monitoreo activa: primero hay que detenerla desde el panel principal.
    """
    with get_pg_cursor() as cur:
        # Verificar que el equipo existe
        cur.execute("SELECT id, nombre, activo FROM equipos WHERE id = %s", (equipo_id,))
        equipo = cur.fetchone()

        if not equipo:
            raise HTTPException(status_code=404, detail="Equipo no encontrado")

        if not equipo["activo"]:
            raise HTTPException(
                status_code=400,
                detail="El equipo ya está dado de baja"
            )

        # Verificar que no haya sesión activa
        cur.execute("""
            SELECT modo FROM sesiones_modo
            WHERE equipo_id = %s AND activa = TRUE
            LIMIT 1
        """, (equipo_id,))
        sesion = cur.fetchone()
        if sesion:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede dar de baja: hay una sesión de {sesion['modo']} activa. Deténgala primero."
            )

        # Borrado lógico
        cur.execute("UPDATE equipos SET activo = FALSE WHERE id = %s", (equipo_id,))

    return {
        "mensaje": "Equipo dado de baja correctamente",
        "id": equipo_id,
        "nombre": equipo["nombre"],
        "usuario": user["nombre"],
    }