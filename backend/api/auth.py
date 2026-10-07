from datetime import datetime, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from config import (
    SECRET_KEY, ALGORITHM, TOKEN_EXPIRACION,
    USUARIO_INICIAL, PASSWORD_INICIAL
)
from database import get_pg_cursor

# -----------------------------
# CONFIGURACIÓN DE HASHING
# -----------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

# -----------------------------
# CONSULTAS A LA BASE
# -----------------------------

def obtener_usuario(usuario: str) -> dict | None:
    """
    Busca un usuario activo por su nombre de usuario en la base de datos.
    Devuelve el diccionario con sus datos o None si no existe o está inactivo.
    """
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT usuario, nombre, password_hash, rol
            FROM usuarios
            WHERE usuario = %s AND activo = TRUE
        """, (usuario,))
        return cur.fetchone()

# -----------------------------
# INICIALIZACIÓN
# -----------------------------

def inicializar_usuarios():
    """
    Al arrancar la API:
    1. Verifica que la tabla usuarios exista (la crea si no).
    2. Si la tabla está vacía, inserta el usuario inicial de configuración.
    Esto garantiza que siempre haya al menos un usuario supervisor para iniciar sesión.
    """
    with get_pg_cursor() as cur:
        # Crear tabla si no existe
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) UNIQUE NOT NULL,
                nombre VARCHAR(100) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                rol VARCHAR(20) NOT NULL CHECK (rol IN ('supervisor', 'operador')),
                fecha_creacion TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                activo BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usuarios_usuario ON usuarios(usuario)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usuarios_activo ON usuarios(activo)")

        # Contar cuántos usuarios activos hay
        cur.execute("SELECT COUNT(*) as total FROM usuarios WHERE activo = TRUE")
        resultado = cur.fetchone()
        total = resultado["total"] if resultado else 0

        # Si no hay usuarios, crear el inicial desde la configuración
        if total == 0:
            password_hash = pwd_context.hash(PASSWORD_INICIAL)
            cur.execute("""
                INSERT INTO usuarios (usuario, nombre, password_hash, rol)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (usuario) DO NOTHING
            """, (USUARIO_INICIAL, USUARIO_INICIAL.capitalize(), password_hash, "supervisor"))
            print(f"Usuario inicial '{USUARIO_INICIAL}' creado en la base de datos.")

# -----------------------------
# AUTENTICACIÓN
# -----------------------------

def verificar_password(password_plano: str, password_hash: str) -> bool:
    """Verifica si la contraseña plana coincide con el hash almacenado."""
    return pwd_context.verify(password_plano, password_hash)

def autenticar_usuario(usuario: str, password: str):
    """Verifica las credenciales del usuario contra la base de datos."""
    user = obtener_usuario(usuario)
    if not user or not user["password_hash"]:
        return None
    if not verificar_password(password, user["password_hash"]):
        return None
    return {"usuario": user["usuario"], "nombre": user["nombre"], "rol": user["rol"]}

def crear_token(usuario: str) -> str:
    """Genera un token JWT con el usuario y su tiempo de expiración."""
    expira = datetime.now(timezone.utc) + TOKEN_EXPIRACION
    payload = {
        "sub": usuario,
        "exp": expira
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def hash_password(password: str) -> str:
    """Genera el hash bcrypt de una contraseña. Usado al crear/editar usuarios."""
    return pwd_context.hash(password)

# -----------------------------
# DEPENDENCIA PARA RUTAS PROTEGIDAS
# -----------------------------

def usuario_actual(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Decodifica el token JWT y devuelve los datos del usuario actual.
    Consulta la base de datos para asegurar que el usuario sigue activo.
    """
    excepcion = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        usuario_nombre = payload.get("sub")
        if not usuario_nombre:
            raise excepcion
    except JWTError:
        raise excepcion

    user = obtener_usuario(usuario_nombre)
    if not user:
        # El usuario existía cuando se emitió el token pero fue eliminado/desactivado
        raise excepcion

    return {"usuario": user["usuario"], "nombre": user["nombre"], "rol": user["rol"]}

def requiere_supervisor(user: dict = Depends(usuario_actual)) -> dict:
    """
    Dependencia adicional que además de autenticar exige rol de supervisor.
    Se usa en los endpoints de gestión de usuarios.
    """
    if user["rol"] != "supervisor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requieren permisos de supervisor para esta operación"
        )
    return user