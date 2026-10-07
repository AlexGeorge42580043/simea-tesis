from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from auth import inicializar_usuarios
from database import cerrar_influx_client

from routers import auth_router, datos
from routers import auth_router, datos, alertas
from routers import auth_router, datos, alertas, modos
from routers import auth_router, datos, alertas, modos, equipos
from routers import auth_router, datos, alertas, modos, equipos, modelos, usuarios

# -----------------------------
# CICLO DE VIDA DE LA APLICACIÓN
# -----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Se ejecuta al arrancar y al apagar la API.
    Permite inicializar y limpiar recursos correctamente.
    """
    print("Iniciando SIMEA API...")
    inicializar_usuarios()
    print("Usuarios inicializados.")
    yield
    print("Apagando SIMEA API...")
    cerrar_influx_client()
    print("Recursos liberados.")

# -----------------------------
# CREACIÓN DE LA APLICACIÓN
# -----------------------------

app = FastAPI(
    title="SIMEA API",
    description="API del Sistema Integrado de Monitoreo Energético y Anomalías",
    version="1.0.0",
    lifespan=lifespan
)

# -----------------------------
# CORS
# -----------------------------

# Permitir que la interfaz web se comunique con la API desde otro origen.
# Durante el desarrollo aceptamos cualquier origen; en producción se restringe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# ENDPOINT DE PRUEBA
# -----------------------------

@app.get("/")
async def raiz():
    """Endpoint de salud para verificar que la API está corriendo."""
    return {
        "sistema": "SIMEA",
        "estado": "activo",
        "descripcion": "API del Sistema Integrado de Monitoreo Energético y Anomalías"
    }

app.include_router(auth_router.router)
app.include_router(datos.router)
app.include_router(alertas.router)
app.include_router(modos.router)
app.include_router(equipos.router)
app.include_router(modelos.router)
app.include_router(usuarios.router)   # <--- agregar esta línea