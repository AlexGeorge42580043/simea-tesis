from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from datetime import datetime
from typing import Optional, Annotated

from auth import requiere_supervisor, hash_password
from database import get_pg_cursor

router = APIRouter(
    prefix="/api/usuarios",
    tags=["Usuarios"]
)

# -----------------------------
# MODELOS DE DATOS
# -----------------------------

class NuevoUsuario(BaseModel):
    """Datos requeridos al crear un nuevo usuario."""
    usuario: Annotated[str, StringConstraints(min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_.-]+$')] = Field(
        ..., description="Nombre de usuario (solo letras, números, guiones y puntos)"
    )
    nombre: Annotated[str, StringConstraints(min_length=1, max_length=100)] = Field(
        ..., description="Nombre completo o descriptivo del usuario"
    )
    password: Annotated[str, StringConstraints(min_length=6, max_length=100)] = Field(
        ..., description="Contraseña (mínimo 6 caracteres)"
    )
    rol: str = Field(..., description="Rol del usuario: 'supervisor' u 'operador'")

class EditarUsuario(BaseModel):
    """Datos para editar un usuario existente. Todos los campos son opcionales."""
    nombre: Optional[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = None
    rol: Optional[str] = None
    password: Optional[Annotated[str, StringConstraints(min_length=6, max_length=100)]] = Field(
        None, description="Nueva contraseña (opcional). Si se omite, no se cambia."
    )

# -----------------------------
# HELPERS
# -----------------------------

def _validar_rol(rol: str):
    """Valida que el rol sea uno de los permitidos por el sistema."""
    if rol not in ("supervisor", "operador"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El rol debe ser 'supervisor' u 'operador'"
        )

def _obtener_usuario_por_id(cur, usuario_id: int):
    """Devuelve el usuario activo por su ID, o lanza 404."""
    cur.execute("""
        SELECT id, usuario, nombre, rol, fecha_creacion, activo
        FROM usuarios
        WHERE id = %s AND activo = TRUE
    """, (usuario_id,))
    user = cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuario con id {usuario_id} no encontrado"
        )
    return user

def _contar_supervisores_activos(cur) -> int:
    """Cuenta cuántos supervisores activos hay en el sistema."""
    cur.execute("SELECT COUNT(*) as total FROM usuarios WHERE rol = 'supervisor' AND activo = TRUE")
    resultado = cur.fetchone()
    return resultado["total"] if resultado else 0

# -----------------------------
# LISTAR USUARIOS
# -----------------------------

@router.get("")
async def listar_usuarios(user: dict = Depends(requiere_supervisor)):
    """
    Lista todos los usuarios activos del sistema.
    Solo accesible por supervisores.
    """
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, usuario, nombre, rol, fecha_creacion
            FROM usuarios
            WHERE activo = TRUE
            ORDER BY fecha_creacion ASC
        """)
        usuarios = cur.fetchall()

    return {
        "usuarios": [
            {
                "id": u["id"],
                "usuario": u["usuario"],
                "nombre": u["nombre"],
                "rol": u["rol"],
                "fecha_creacion": u["fecha_creacion"].isoformat(),
                "es_actual": u["usuario"] == user["usuario"],
            }
            for u in usuarios
        ]
    }

# -----------------------------
# CREAR USUARIO
# -----------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
async def crear_usuario(
    datos: NuevoUsuario,
    user: dict = Depends(requiere_supervisor)
):
    """
    Crea un nuevo usuario en el sistema. Solo accesible por supervisores.
    La contraseña se hashea antes de almacenarse; nunca se guarda en texto plano.
    """
    _validar_rol(datos.rol)

    with get_pg_cursor() as cur:
        # Verificar que el nombre de usuario no exista (ni activo ni inactivo)
        cur.execute("SELECT id, activo FROM usuarios WHERE usuario = %s", (datos.usuario,))
        existente = cur.fetchone()
        if existente:
            if existente["activo"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Ya existe un usuario con el nombre '{datos.usuario}'"
                )
            else:
                # El usuario existió antes y fue eliminado lógicamente.
                # Podemos reactivarlo con los nuevos datos.
                password_hash = hash_password(datos.password)
                cur.execute("""
                    UPDATE usuarios
                    SET nombre = %s,
                        password_hash = %s,
                        rol = %s,
                        activo = TRUE,
                        fecha_creacion = NOW()
                    WHERE id = %s
                    RETURNING id, usuario, nombre, rol, fecha_creacion
                """, (datos.nombre, password_hash, datos.rol, existente["id"]))
                creado = cur.fetchone()
                return {
                    "id": creado["id"],
                    "usuario": creado["usuario"],
                    "nombre": creado["nombre"],
                    "rol": creado["rol"],
                    "fecha_creacion": creado["fecha_creacion"].isoformat(),
                    "mensaje": f"Usuario '{creado['usuario']}' creado exitosamente"
                }

        # Insertar el nuevo usuario
        password_hash = hash_password(datos.password)
        cur.execute("""
            INSERT INTO usuarios (usuario, nombre, password_hash, rol)
            VALUES (%s, %s, %s, %s)
            RETURNING id, usuario, nombre, rol, fecha_creacion
        """, (datos.usuario, datos.nombre, password_hash, datos.rol))
        creado = cur.fetchone()

    return {
        "id": creado["id"],
        "usuario": creado["usuario"],
        "nombre": creado["nombre"],
        "rol": creado["rol"],
        "fecha_creacion": creado["fecha_creacion"].isoformat(),
        "mensaje": f"Usuario '{creado['usuario']}' creado exitosamente"
    }

# -----------------------------
# EDITAR USUARIO
# -----------------------------

@router.put("/{usuario_id}")
async def editar_usuario(
    usuario_id: int,
    datos: EditarUsuario,
    user: dict = Depends(requiere_supervisor)
):
    """
    Edita los datos de un usuario existente. Solo accesible por supervisores.
    Se pueden modificar nombre, rol y/o contraseña (parcialmente).
    Un supervisor no puede degradarse a sí mismo si es el último supervisor.
    """
    with get_pg_cursor() as cur:
        objetivo = _obtener_usuario_por_id(cur, usuario_id)

        # Validaciones sobre cambio de rol
        nuevo_rol = datos.rol
        if nuevo_rol is not None:
            _validar_rol(nuevo_rol)
            # Si el usuario actual se está intentando degradar a sí mismo,
            # verificar que no sea el único supervisor.
            if objetivo["usuario"] == user["usuario"] and nuevo_rol != "supervisor":
                total_supervisores = _contar_supervisores_activos(cur)
                if total_supervisores <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No podés cambiar tu propio rol: sos el único supervisor del sistema"
                    )
            # Si se está degradando a otro supervisor, verificar que no sea el último.
            elif objetivo["rol"] == "supervisor" and nuevo_rol != "supervisor":
                total_supervisores = _contar_supervisores_activos(cur)
                if total_supervisores <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No se puede degradar al último supervisor del sistema"
                    )

        # Construir la actualización dinámicamente según qué campos se pasaron
        campos = []
        valores = []
        if datos.nombre is not None:
            campos.append("nombre = %s")
            valores.append(datos.nombre)
        if nuevo_rol is not None:
            campos.append("rol = %s")
            valores.append(nuevo_rol)
        if datos.password is not None:
            campos.append("password_hash = %s")
            valores.append(hash_password(datos.password))

        if not campos:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se enviaron cambios"
            )

        valores.append(usuario_id)
        cur.execute(
            f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s RETURNING id, usuario, nombre, rol",
            tuple(valores)
        )
        actualizado = cur.fetchone()

    return {
        "id": actualizado["id"],
        "usuario": actualizado["usuario"],
        "nombre": actualizado["nombre"],
        "rol": actualizado["rol"],
        "mensaje": f"Usuario '{actualizado['usuario']}' actualizado exitosamente"
    }

# -----------------------------
# ELIMINAR USUARIO (LÓGICO)
# -----------------------------

@router.delete("/{usuario_id}")
async def eliminar_usuario(
    usuario_id: int,
    user: dict = Depends(requiere_supervisor)
):
    """
    Elimina un usuario del sistema (baja lógica: activo = FALSE).
    Solo accesible por supervisores.
    Un usuario no puede eliminarse a sí mismo.
    No se puede eliminar al último supervisor del sistema.
    """
    with get_pg_cursor() as cur:
        objetivo = _obtener_usuario_por_id(cur, usuario_id)

        # Restricción: no se puede eliminar a sí mismo
        if objetivo["usuario"] == user["usuario"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No podés eliminar tu propio usuario"
            )

        # Restricción: no se puede eliminar al último supervisor
        if objetivo["rol"] == "supervisor":
            total_supervisores = _contar_supervisores_activos(cur)
            if total_supervisores <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No se puede eliminar al último supervisor del sistema"
                )

        cur.execute("UPDATE usuarios SET activo = FALSE WHERE id = %s", (usuario_id,))

    return {
        "mensaje": f"Usuario '{objetivo['usuario']}' eliminado exitosamente",
        "id": usuario_id
    }