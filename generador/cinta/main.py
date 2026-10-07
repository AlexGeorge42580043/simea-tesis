"""
Generador de datos de motor asíncrono trifásico con estados operativos
comandados manualmente y anomalías físicamente coherentes.

Soporta simular datos para distintos equipos usando el comando 'equipo <id>'.
Todos los datos generados se etiquetan con el equipo_id actual y se publican
en el topic MQTT correspondiente.

Comandos disponibles:
    apagado, vacio, ligera, media, plena  - cambiar a ese estado
    carga X                               - carga específica (0-100)
    normal                                - quitar anomalía activa
    sobrecarga X                          - inyectar sobrecarga (X = 0.0-1.0)
    falla X                               - inyectar falla eléctrica
    subtension X                          - inyectar subtensión de red
    degradacion X                         - inyectar degradación mecánica
    equipo <id>                           - cambiar el equipo que se está simulando
    status                                - ver estado actual
    q                                     - salir
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()
import numpy as np
import paho.mqtt.client as mqtt
import psycopg2

from motor_modelo import estado_motor
from estados import ESTADOS, obtener_carga_por_nombre, nombre_estado_por_carga
from transiciones import calcular_duracion_transicion, valor_en_transicion
from anomalias import aplicar_anomalia

# -----------------------------
# CONFIGURACION
# -----------------------------

MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC_BASE = "planta"  # el topic completo se arma dinámicamente con el equipo_id

INTERVALO_MUESTRAS = 1.0  # segundos entre publicaciones

# Equipo por defecto al arrancar
EQUIPO_INICIAL = "motor_cinta_1"

# Configuración de PostgreSQL para validar equipos existentes
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_USER = os.getenv("POSTGRES_USER")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD")
PG_DB = os.getenv("POSTGRES_DB")

# -----------------------------
# ESTADO GLOBAL DEL GENERADOR
# -----------------------------

estado_actual = {
    # Identidad del equipo que se está simulando
    "equipo_id": EQUIPO_INICIAL,

    # Estado operativo
    "carga_actual": 0.0,          # carga real que está aplicando ahora
    "carga_objetivo": 0.0,        # carga a la que estamos transitando (si aplica)
    "carga_inicial": 0.0,         # carga desde donde inició la transición
    "en_transicion": False,
    "tiempo_inicio_transicion": None,
    "duracion_transicion": 0.0,
    "nombre_estado": "apagado",   # etiqueta descriptiva

    # Anomalía activa
    "anomalia_activa": None,      # None o "sobrecarga"/"falla"/"subtension"/"degradacion"
    "intensidad_anomalia": 0.0,
}

lock = threading.Lock()

def obtener_topic_actual():
    """Devuelve el topic MQTT actual basado en el equipo activo."""
    with lock:
        equipo = estado_actual["equipo_id"]
    return f"{MQTT_TOPIC_BASE}/{equipo}/mediciones"


def obtener_equipos_validos():
    """
    Consulta PostgreSQL y devuelve un dict {id: nombre} con los equipos
    activos disponibles en el sistema. Se usa para validar el comando 'equipo'.
    """
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
        )
        cur = conn.cursor()
        cur.execute("SELECT id, nombre FROM equipos WHERE activo = TRUE ORDER BY id")
        resultado = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()
        conn.close()
        return resultado
    except Exception as e:
        print(f"Error consultando equipos en PostgreSQL: {e}")
        return {}

# -----------------------------
# FUNCIONES DE COMANDO
# -----------------------------

def cambiar_equipo(nuevo_equipo_id):
    """
    Cambia el equipo que se está simulando. Los próximos datos publicados
    se etiquetan con este nuevo equipo_id y van al topic correspondiente.
    Solo se aceptan equipos registrados y activos en el sistema.
    """
    nuevo_equipo_id = nuevo_equipo_id.strip().lower()
    if not nuevo_equipo_id:
        print("El equipo_id no puede estar vacío.")
        return

    # Validar contra la base: el equipo debe existir y estar activo
    equipos_validos = obtener_equipos_validos()
    if not equipos_validos:
        print("No se pudo consultar la lista de equipos. ¿PostgreSQL está corriendo?")
        return

    if nuevo_equipo_id not in equipos_validos:
        print(f"El equipo '{nuevo_equipo_id}' no existe o está dado de baja.")
        print("Equipos disponibles:")
        for eid, nombre in equipos_validos.items():
            print(f"  - {eid}  ({nombre})")
        return

    with lock:
        anterior = estado_actual["equipo_id"]
        estado_actual["equipo_id"] = nuevo_equipo_id

    print(f"Equipo cambiado: {anterior} → {nuevo_equipo_id}  ({equipos_validos[nuevo_equipo_id]})")
    print(f"Nuevo topic MQTT: {MQTT_TOPIC_BASE}/{nuevo_equipo_id}/mediciones")


def cambiar_a_estado(nombre_o_carga):
    """
    Inicia una transición hacia un estado nuevo.
    Puede ser un nombre ('vacio', 'plena', etc.) o un porcentaje 0-100.
    """
    with lock:
        if isinstance(nombre_o_carga, str):
            carga_nueva = obtener_carga_por_nombre(nombre_o_carga)
            if carga_nueva is None:
                print(f"Estado desconocido: {nombre_o_carga}")
                return
            nombre = nombre_o_carga
        else:
            # Es un valor numérico entre 0 y 100
            porcentaje = max(0, min(100, nombre_o_carga))
            carga_nueva = porcentaje / 100.0
            nombre = f"carga_{porcentaje:.0f}%"

        carga_actual_real = estado_actual["carga_actual"]

        if abs(carga_nueva - carga_actual_real) < 0.001:
            print(f"Ya estás en ese estado ({nombre})")
            return

        duracion = calcular_duracion_transicion(carga_actual_real, carga_nueva)

        estado_actual["carga_inicial"] = carga_actual_real
        estado_actual["carga_objetivo"] = carga_nueva
        estado_actual["en_transicion"] = True
        estado_actual["tiempo_inicio_transicion"] = time.time()
        estado_actual["duracion_transicion"] = duracion
        estado_actual["nombre_estado"] = nombre

        print(f"Iniciando transición: {carga_actual_real*100:.0f}% → {carga_nueva*100:.0f}% "
              f"(duración: {duracion:.1f}s)")


def inyectar_anomalia(tipo, intensidad):
    """Activa una anomalía sobre el estado actual del motor."""
    if tipo not in ["sobrecarga", "falla", "subtension", "degradacion"]:
        print(f"Anomalía desconocida: {tipo}")
        return

    intensidad = max(0.0, min(1.0, intensidad))
    with lock:
        estado_actual["anomalia_activa"] = tipo
        estado_actual["intensidad_anomalia"] = intensidad
    print(f"Anomalía activa: {tipo} con intensidad {intensidad}")


def quitar_anomalia():
    """Vuelve a operación normal."""
    with lock:
        estado_actual["anomalia_activa"] = None
        estado_actual["intensidad_anomalia"] = 0.0
    print("Anomalía desactivada. Operación normal.")


def mostrar_status():
    """Imprime el estado actual del generador."""
    with lock:
        equipo = estado_actual["equipo_id"]
        carga = estado_actual["carga_actual"]
        nombre = estado_actual["nombre_estado"]
        en_trans = estado_actual["en_transicion"]
        anomalia = estado_actual["anomalia_activa"]
        intensidad = estado_actual["intensidad_anomalia"]

    print(f"\n{'='*50}")
    print(f"Equipo simulado: {equipo}")
    print(f"Topic MQTT: {MQTT_TOPIC_BASE}/{equipo}/mediciones")
    print(f"Estado actual: {nombre} ({carga*100:.1f}% de carga)")
    if en_trans:
        objetivo = estado_actual["carga_objetivo"]
        print(f"En transición hacia {objetivo*100:.0f}%")
    if anomalia:
        print(f"Anomalía activa: {anomalia} (intensidad {intensidad})")
    else:
        print("Sin anomalía activa")
    print(f"{'='*50}\n")

# -----------------------------
# CALCULO DE VALORES A PUBLICAR
# -----------------------------

def calcular_valores_actuales():
    """
    Calcula los valores instantáneos de P, I, FP considerando:
    - El estado operativo actual (o transición)
    - La anomalía activa (si hay)
    - Ruido gaussiano realista
    Incluye el equipo_id actual en el payload para que se preserve al
    llegar a InfluxDB como tag.
    """
    with lock:
        # Determinar la carga actual (considerando transición)
        if estado_actual["en_transicion"]:
            t = time.time() - estado_actual["tiempo_inicio_transicion"]
            duracion = estado_actual["duracion_transicion"]
            carga_efectiva = valor_en_transicion(
                t, duracion,
                estado_actual["carga_inicial"],
                estado_actual["carga_objetivo"]
            )
            estado_actual["carga_actual"] = carga_efectiva

            # ¿Terminó la transición?
            if t >= duracion:
                estado_actual["en_transicion"] = False
                estado_actual["carga_actual"] = estado_actual["carga_objetivo"]
                print(f"Transición completada. Ahora en {estado_actual['nombre_estado']} "
                      f"({estado_actual['carga_actual']*100:.0f}%)")
        else:
            carga_efectiva = estado_actual["carga_actual"]

        equipo_id_actual = estado_actual["equipo_id"]
        anomalia = estado_actual["anomalia_activa"]
        intensidad = estado_actual["intensidad_anomalia"]

    # Si el motor está apagado (carga = 0), no hay consumo
    if carga_efectiva <= 0.001:
        p_base = 0.0
        i_base = 0.0
        fp_base = 0.0
    else:
        estado = estado_motor(carga_efectiva)
        p_base = estado["potencia_kw"]
        i_base = estado["corriente_a"]
        fp_base = estado["factor_potencia"]

    # Aplicar anomalía si está activa
    if anomalia is not None:
        p_efectiva, i_efectiva, fp_efectivo = aplicar_anomalia(
            anomalia, p_base, i_base, fp_base, intensidad
        )
    else:
        p_efectiva, i_efectiva, fp_efectivo = p_base, i_base, fp_base

    # Ruido gaussiano realista.
    # Cuando el motor está apagado, no hay ruido (sensor no capta nada)
    if carga_efectiva <= 0.001:
        p_final = 0.0
        i_final = 0.0
        fp_final = 0.0
    else:
        ruido_p = np.random.normal(0, max(0.015 * abs(p_efectiva), 0.003))
        ruido_i = np.random.normal(0, max(0.015 * abs(i_efectiva), 0.005))
        ruido_fp = np.random.normal(0, 0.005)

        p_final = p_efectiva + ruido_p
        i_final = i_efectiva + ruido_i
        fp_final = max(0.0, min(1.0, fp_efectivo + ruido_fp))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equipo_id": equipo_id_actual,   # ← nuevo: identifica al equipo
        "potencia_kw": round(p_final, 4),
        "corriente_a": round(i_final, 4),
        "factor_potencia": round(fp_final, 4),
        "carga_actual": round(carga_efectiva, 4),
        "estado": estado_actual["nombre_estado"],
        "anomalia": anomalia,
    }

# -----------------------------
# HILO DE PUBLICACION MQTT
# -----------------------------

def hilo_publicacion(client):
    """Publica muestras periódicamente al broker MQTT."""
    contador = 0
    while True:
        try:
            datos = calcular_valores_actuales()
            topic = obtener_topic_actual()
            client.publish(topic, json.dumps(datos))

            contador += 1
            # Mostrar información cada 5 muestras
            if contador % 5 == 0:
                estado_str = datos["estado"]
                if datos["anomalia"]:
                    estado_str += f" + {datos['anomalia']}"
                print(f"P={datos['potencia_kw']:.3f}kW  "
                      f"I={datos['corriente_a']:.3f}A  "
                      f"FP={datos['factor_potencia']:.3f}  "
                      f"[{estado_str}]")

            time.sleep(INTERVALO_MUESTRAS)
        except Exception as e:
            print(f"Error en publicación: {e}")
            time.sleep(INTERVALO_MUESTRAS)

# -----------------------------
# BUCLE DE COMANDOS
# -----------------------------

def bucle_comandos():
    """Lee comandos del usuario e invoca las funciones correspondientes."""
    print("\n" + "="*60)
    print("Generador de datos - Simulador multi-equipo")
    print("="*60)
    print("\nComandos de estado:")
    print("  apagado, vacio, ligera, media, plena")
    print("  carga X     (X = 0 a 100, carga porcentual específica)")
    print("\nComandos de anomalías:")
    print("  normal              (quitar anomalía)")
    print("  sobrecarga X        (X = 0.0 a 1.0)")
    print("  falla X")
    print("  subtension X")
    print("  degradacion X")
    print("\nSelección de equipo:")
    print("  equipo <id>         (cambiar el equipo que se simula)")
    print("  equipos             (listar equipos disponibles)")
    print("\nOtros:")
    print("  status              (ver estado)")
    print("  q                   (salir)")
    print("="*60)
    print(f"\nEquipo inicial: {EQUIPO_INICIAL}")
    print(f"Topic MQTT: {MQTT_TOPIC_BASE}/{EQUIPO_INICIAL}/mediciones")
    print("Estado inicial: apagado\n")

    while True:
        try:
            comando = input("> ").strip().lower()
            if not comando:
                continue

            partes = comando.split()
            cmd = partes[0]

            if cmd == "q":
                print("Finalizando generador...")
                break

            elif cmd == "status":
                mostrar_status()

            elif cmd == "normal":
                quitar_anomalia()

            elif cmd == "equipo":
                if len(partes) != 2:
                    print("Uso: equipo <id>")
                    continue
                cambiar_equipo(partes[1])

            elif cmd == "equipos":
                equipos_validos = obtener_equipos_validos()
                if not equipos_validos:
                    print("No se pudo consultar la lista de equipos.")
                else:
                    print("\nEquipos disponibles en el sistema:")
                    for eid, nombre in equipos_validos.items():
                        marca = "  ← actual" if eid == estado_actual["equipo_id"] else ""
                        print(f"  - {eid:35} ({nombre}){marca}")
                    print()

            elif cmd in ESTADOS:
                cambiar_a_estado(cmd)

            elif cmd == "carga" and len(partes) == 2:
                try:
                    valor = float(partes[1])
                    cambiar_a_estado(valor)
                except ValueError:
                    print("Uso: carga <número 0-100>")

            elif cmd in ["sobrecarga", "falla", "subtension", "degradacion"]:
                if len(partes) != 2:
                    print(f"Uso: {cmd} <intensidad 0.0-1.0>")
                    continue
                try:
                    intensidad = float(partes[1])
                    inyectar_anomalia(cmd, intensidad)
                except ValueError:
                    print("Intensidad inválida. Debe ser un número entre 0 y 1.")

            else:
                print(f"Comando desconocido: {cmd}")

        except KeyboardInterrupt:
            print("\nInterrumpido por el usuario.")
            break
        except Exception as e:
            print(f"Error: {e}")

# -----------------------------
# MAIN
# -----------------------------

def main():
    # Conectar al broker MQTT
    client = mqtt.Client()
    try:
        client.connect(MQTT_BROKER, MQTT_PORT)
        print(f"Conectado a broker MQTT en {MQTT_BROKER}:{MQTT_PORT}")
    except Exception as e:
        print(f"Error conectando al broker MQTT: {e}")
        return

    client.loop_start()

    # Lanzar hilo de publicación
    hilo = threading.Thread(target=hilo_publicacion, args=(client,), daemon=True)
    hilo.start()

    # Bucle de comandos (bloqueante)
    bucle_comandos()

    # Cerrar cliente MQTT
    client.loop_stop()
    client.disconnect()
    print("Desconectado del broker MQTT.")


if __name__ == "__main__":
    main()