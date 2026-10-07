import os
from datetime import timedelta
from dotenv import load_dotenv
load_dotenv()

# -----------------------------
# INFLUXDB
# -----------------------------
INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUX_ORG = os.getenv("INFLUXDB_ORG")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET")

# -----------------------------
# POSTGRESQL
# -----------------------------
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_USER = os.getenv("POSTGRES_USER")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD")
PG_DB = os.getenv("POSTGRES_DB")

# -----------------------------
# MQTT
# -----------------------------
MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC_BASE = "planta"

# -----------------------------
# AUTENTICACIÓN
# -----------------------------
# Clave secreta para firmar tokens JWT.
# En producción real esto se debe leer de una variable de entorno.
SECRET_KEY = os.getenv("JWT_SECRET")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_EXPIRACION = timedelta(hours=int(os.getenv("JWT_EXPIRATION_HOURS", "8")))

# Usuario único hardcodeado para esta etapa de desarrollo.
# Más adelante se reemplazará por una tabla de usuarios en PostgreSQL.
USUARIOS = {
    os.getenv("ADMIN_USERNAME"): {
        "nombre": "Alex",
        "password_hash": None,  # se calcula al iniciar la API
        "rol": "supervisor"
    }
}
USUARIO_INICIAL = os.getenv("ADMIN_USERNAME")
PASSWORD_INICIAL = os.getenv("ADMIN_PASSWORD")

# -----------------------------
# PARÁMETROS DEL SISTEMA