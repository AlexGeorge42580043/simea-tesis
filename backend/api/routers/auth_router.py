from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import OAuth2PasswordRequestForm

from auth import autenticar_usuario, crear_token, usuario_actual

router = APIRouter(
    prefix="/api",
    tags=["Autenticación"]
)

# -----------------------------
# LOGIN
# -----------------------------

@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Recibe usuario y contraseña, devuelve un token JWT.
    El form usa los campos estándar 'username' y 'password' de OAuth2.
    """
    user = autenticar_usuario(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos"
        )

    token = crear_token(form_data.username)

    return {
        "access_token": token,
        "token_type": "bearer",
        "usuario": user["nombre"],
        "rol": user["rol"]
    }

# -----------------------------
# VERIFICAR TOKEN
# -----------------------------

@router.get("/me")
async def info_usuario_actual(user: dict = Depends(usuario_actual)):
    """
    Devuelve la información del usuario actual a partir del token.
    Sirve para verificar si el token sigue siendo válido y obtener datos del usuario.
    """
    return user