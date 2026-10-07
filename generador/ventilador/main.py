"""
Generador de datos para simular un ventilador extractor axial industrial.

CONTEXTO FÍSICO:
Ventilador axial industrial de gran caudal, empleado para extracción de aire
en naves industriales grandes o sistemas de ventilación mayores. Accionamiento
directo con motor asíncrono trifásico WEG W22 de 7.5 kW, el mismo modelo
utilizado en la cinta transportadora. La reutilización del mismo modelo de
motor para dos aplicaciones distintas es una decisión de diseño deliberada:
demuestra que el sistema SIMEA aprende comportamientos operativos independiente
del hardware eléctrico subyacente.

COMPORTAMIENTO:
- El ventilador tiene solo dos estados operativos: apagado o en marcha.
- Cuando arranca, el rodete tiene alta inercia: se genera un pico de corriente
  de 6-8x la nominal por 3-5 segundos, absorbido por la lógica de supresión
  de transiciones del monitor (SUPRESION_TRANSICION=8s).
- En régimen estable opera al 73% de la carga nominal del motor (5.5 kW /
  10.5 A / FP 0.88), punto de dimensionamiento típico con margen de seguridad.
- Se ofrece adicionalmente el comando 'carga X' para pruebas con carga variable
  (simula un variador de frecuencia hipotético), aunque en operación real este
  tipo de ventilador no tiene regulación.

Comandos disponibles:
    marcha                                - arrancar (transición al 73%)
    apagar                                - detener (transición al 0%)
    carga X                               - carga específica (0-100, para pruebas)
    normal                                - quitar anomalía activa
    sobrecarga X                          - inyectar sobrecarga (X = 0.0-1.0)
    falla X                               - inyectar falla eléctrica
    subtension X                          - inyectar subtensión de red
    degradacion X                         - inyectar degradación mecánica
    equipo <id>                           - cambiar el equipo simulado
    equipos                               - listar equipos disponibles
    status                                - ver estado actual
    q                                     - salir
"""

import json
import time
import threading
import sys
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()
import numpy as np
import paho.mqtt.client as mqtt
import psycopg2

# Importar módulos del generador de la cinta (mismo motor físico).
# Ajustar el path para encontrar el paquete del generador original.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cinta'))
from motor_modelo import estado_motor
from transiciones import calcular_duracion_transicion, valor_en_transicion
from anomalias import aplicar_anomalia

# Módulo propio del ventilador: curvas de arranque físicamente realistas.
# Se importa desde el mismo directorio de este main.py.
from curvas_arranque import (
    estado_ventilador_en_arranque,
    DURACION_ARRANQUE_S,
    CARGA_REGIMEN as CARGA_REGIMEN_ARRANQUE,
)

# -----------------------------
# CONFIGURACION
# -----------------------------

MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC_BASE = "planta"

INTERVALO_MUESTRAS = 1.0  # segundos entre publicaciones

# Equipo por defecto al arrancar este generador
EQUIPO_INICIAL = "ventilador_extraccion_1"

# Carga en régimen de operación del ventilador (73% de la nominal del motor).
# Corresponde a un dimensionamiento típico donde se aplica un margen de
# seguridad del ~25% respecto a la potencia nominal del motor.
CARGA_REGIMEN = 0.73

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

    # Estado operativo (el ventilador solo puede estar apagado o en marcha,
    # pero internamente usamos carga [0.0-1.0] para reutilizar toda la
    # infraestructura de transiciones y modelo del motor).
    "carga_actual": 0.0,
    "carga_objetivo": 0.0,
    "carga_inicial": 0.0,
    "en_transicion": False,
    "tiempo_inicio_transicion": None,
    "duracion_transicion": 0.0,
    "nombre_estado": "apagado",

    # Arranque físico realista: cuando se ejecuta el comando "marcha" desde
    # el motor apagado, entramos en un arranque real de 10 segundos con
    # curvas específicas por variable (P, I, FP) según física del ventilador.
    # Este flag se activa solo para el arranque real, no para cambios de carga
    # con el comando `carga X` (esos usan la transición genérica de la cinta).
    "en_arranque": False,
    "tiempo_inicio_arranque": None,

    # Anomalía activa
    "anomalia_activa": None,
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
    activos disponibles. Se usa para validar el comando 'equipo'.
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


def registrar_equipo_si_no_existe():
    """
    Verifica que el equipo ventilador esté registrado en PostgreSQL. Si no
    existe, lo crea con valores razonables para operación continua.
    Este chequeo es solo un facilitador: normalmente el equipo se registra
    desde la interfaz web antes de iniciar el generador.
    """
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
        )
        cur = conn.cursor()
        cur.execute("SELECT id FROM equipos WHERE id = %s", (EQUIPO_INICIAL,))
        existe = cur.fetchone() is not None
        cur.close()
        conn.close()

        if not existe:
            print(f"⚠️  El equipo '{EQUIPO_INICIAL}' no está registrado en PostgreSQL.")
            print("    Registralo desde la interfaz web (Configuración → Registro de equipos)")
            print("    antes de que el sistema de monitoreo pueda procesar sus datos.")
        else:
            print(f"✓ Equipo '{EQUIPO_INICIAL}' encontrado en PostgreSQL.")
    except Exception as e:
        print(f"No se pudo verificar el equipo en PostgreSQL: {e}")


# -----------------------------
# FUNCIONES DE COMANDO
# -----------------------------

def cambiar_equipo(nuevo_equipo_id):
    """Cambia el equipo que se está simulando (validado contra PostgreSQL)."""
    nuevo_equipo_id = nuevo_equipo_id.strip().lower()
    if not nuevo_equipo_id:
        print("El equipo_id no puede estar vacío.")
        return

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


def cambiar_a_carga(carga_nueva, nombre_estado):
    """
    Inicia una transición hacia una carga nueva. La duración de la transición
    se calcula automáticamente según la diferencia de carga (reutilizando
    la lógica de transiciones del motor de cinta): mayores saltos = arranques
    más largos, absorbiendo el pico de corriente del rodete del ventilador.
    """
    with lock:
        carga_actual_real = estado_actual["carga_actual"]

        if abs(carga_nueva - carga_actual_real) < 0.001:
            print(f"Ya estás en ese estado ({nombre_estado})")
            return

        duracion = calcular_duracion_transicion(carga_actual_real, carga_nueva)

        estado_actual["carga_inicial"] = carga_actual_real
        estado_actual["carga_objetivo"] = carga_nueva
        estado_actual["en_transicion"] = True
        estado_actual["tiempo_inicio_transicion"] = time.time()
        estado_actual["duracion_transicion"] = duracion
        estado_actual["nombre_estado"] = nombre_estado

        print(f"Iniciando transición: {carga_actual_real*100:.0f}% → {carga_nueva*100:.0f}% "
              f"(duración: {duracion:.1f}s)")


def marcha():
    """
    Arranca el ventilador desde apagado, o lo lleva al régimen si ya está
    en marcha con otra carga.

    Si estamos partiendo desde apagado (carga ≈ 0), se activa el modo
    "arranque físico" que reproduce la curva característica real del
    ventilador axial durante los primeros 10 segundos (ley de afinidad,
    pico de corriente, recuperación de FP).

    Si ya estamos en marcha con otra carga (por ejemplo tras usar `carga X`),
    hacemos una transición genérica al régimen sin dispararse el arranque
    físico (ya no hay pico ni fenómenos de rotor trabado).
    """
    with lock:
        carga_actual_real = estado_actual["carga_actual"]

    if carga_actual_real < 0.01:
        # Motor apagado: activar arranque físico realista de 10 segundos
        with lock:
            estado_actual["en_arranque"] = True
            estado_actual["tiempo_inicio_arranque"] = time.time()
            estado_actual["carga_actual"] = CARGA_REGIMEN_ARRANQUE
            estado_actual["carga_objetivo"] = CARGA_REGIMEN_ARRANQUE
            estado_actual["carga_inicial"] = 0.0
            estado_actual["en_transicion"] = False
            estado_actual["nombre_estado"] = "marcha"

        print(f"Arranque físico iniciado ({DURACION_ARRANQUE_S:.0f} segundos):")
        print(f"  - Pico de corriente inicial: ~4x nominal (rotor trabado)")
        print(f"  - Potencia crece cúbicamente (ley de afinidad)")
        print(f"  - FP se recupera desde ~0.20 hasta régimen")
    else:
        # Ya está en marcha con otra carga: transición genérica al régimen
        cambiar_a_carga(CARGA_REGIMEN, "marcha")


def apagar():
    """
    Detiene el ventilador. Corta la alimentación al motor: los valores
    eléctricos (P, I, FP) caen a cero en la próxima muestra.

    Físicamente, el rodete sigue girando por inercia durante varios minutos
    después de cortar la energía, pero desde el punto de vista del monitoreo
    eléctrico el motor está desenergizado y no consume. Por eso NO hay
    transición: la caída es inmediata.
    """
    with lock:
        # Cortar cualquier arranque o transición en curso
        estado_actual["en_arranque"] = False
        estado_actual["en_transicion"] = False
        estado_actual["carga_actual"] = 0.0
        estado_actual["carga_objetivo"] = 0.0
        estado_actual["nombre_estado"] = "apagado"
    print("Ventilador detenido. El motor queda desenergizado.")
    print("  (El rodete sigue girando por inercia por varios minutos,")
    print("  pero eléctricamente el consumo pasa a cero en la próxima muestra.)")


def carga_manual(porcentaje):
    """Carga específica en porcentaje 0-100. Para pruebas con carga variable."""
    porcentaje = max(0.0, min(100.0, porcentaje))
    carga_nueva = porcentaje / 100.0
    nombre = f"carga_{porcentaje:.0f}%"
    cambiar_a_carga(carga_nueva, nombre)


def inyectar_anomalia(tipo, intensidad):
    """Activa una anomalía sobre el estado actual del ventilador."""
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
    """Muestra el estado actual del generador."""
    with lock:
        equipo = estado_actual["equipo_id"]
        carga = estado_actual["carga_actual"]
        nombre = estado_actual["nombre_estado"]
        anomalia = estado_actual["anomalia_activa"]
        intensidad = estado_actual["intensidad_anomalia"]
        en_trans = estado_actual["en_transicion"]

    print(f"\n--- Estado del generador ---")
    print(f"Equipo simulado: {equipo}")
    print(f"Topic MQTT: {MQTT_TOPIC_BASE}/{equipo}/mediciones")
    print(f"Estado operativo: {nombre}  (carga: {carga*100:.1f}%)")
    if en_trans:
        print(f"  → En transición")
    if anomalia:
        print(f"Anomalía activa: {anomalia} (intensidad: {intensidad})")
    else:
        print(f"Anomalía activa: ninguna")
    print()


# -----------------------------
# CÁLCULO DE VALORES ELÉCTRICOS
# -----------------------------

def calcular_valores_actuales():
    """
    Calcula los valores instantáneos de P, I, FP.

    Tres casos:
    1. En arranque físico realista (activado por comando "marcha" desde
       apagado): usa las curvas específicas del módulo curvas_arranque,
       con dinámica correcta por variable (I decae exponencial, P crece
       cúbica, FP crece sigmoide).
    2. En transición genérica (cambio de carga con `carga X` o similar):
       usa la transición sigmoide con overshoot del módulo transiciones
       (compartido con la cinta).
    3. En régimen estable: usa el modelo del motor (estado_motor) con la
       carga actual.
    """
    with lock:
        equipo_id_actual = estado_actual["equipo_id"]
        anomalia = estado_actual["anomalia_activa"]
        intensidad = estado_actual["intensidad_anomalia"]

        # ─── Caso 1: arranque físico realista ───
        if estado_actual["en_arranque"]:
            t_arranque = time.time() - estado_actual["tiempo_inicio_arranque"]

            if t_arranque >= DURACION_ARRANQUE_S:
                # Terminó el arranque: valores en régimen desde ahora
                estado_actual["en_arranque"] = False
                estado_actual["tiempo_inicio_arranque"] = None
                estado_actual["carga_actual"] = CARGA_REGIMEN_ARRANQUE
                print(f"Arranque completado. Ventilador en régimen "
                      f"(carga {CARGA_REGIMEN_ARRANQUE*100:.0f}%).")
                # Continuamos abajo con el cálculo en régimen
                en_arranque_actual = False
            else:
                en_arranque_actual = True
                # Obtenemos la muestra de la curva de arranque
                estado_arr = estado_ventilador_en_arranque(t_arranque)
                p_base = estado_arr["potencia_kw"]
                i_base = estado_arr["corriente_a"]
                fp_base = estado_arr["factor_potencia"]
                # Para el payload seguimos reportando la carga objetivo,
                # ya que la variable "carga" es una abstracción; los
                # valores eléctricos reales los da la curva de arranque.
                carga_efectiva = estado_actual["carga_actual"]
        else:
            en_arranque_actual = False

        # ─── Casos 2 y 3: transición genérica o régimen ───
        if not en_arranque_actual:
            if estado_actual["en_transicion"]:
                t = time.time() - estado_actual["tiempo_inicio_transicion"]
                duracion = estado_actual["duracion_transicion"]
                carga_efectiva = valor_en_transicion(
                    t, duracion,
                    estado_actual["carga_inicial"],
                    estado_actual["carga_objetivo"]
                )
                estado_actual["carga_actual"] = carga_efectiva

                if t >= duracion:
                    estado_actual["en_transicion"] = False
                    estado_actual["carga_actual"] = estado_actual["carga_objetivo"]
                    print(f"Transición completada. Ahora en {estado_actual['nombre_estado']} "
                          f"({estado_actual['carga_actual']*100:.0f}%)")
            else:
                carga_efectiva = estado_actual["carga_actual"]

            # Ventilador apagado: sin consumo (motor desenergizado)
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

    # Ruido gaussiano realista. Durante el arranque no agregamos ruido a
    # los valores muy chicos para que se vean claras las curvas.
    if carga_efectiva <= 0.001 and not en_arranque_actual:
        p_final = 0.0
        i_final = 0.0
        fp_final = 0.0
    else:
        # En arranque, ruido más bajo para no distorsionar la curva característica
        factor_ruido = 0.005 if en_arranque_actual else 0.010
        ruido_p = np.random.normal(0, max(factor_ruido * abs(p_efectiva), 0.003))
        ruido_i = np.random.normal(0, max(factor_ruido * abs(i_efectiva), 0.005))
        ruido_fp = np.random.normal(0, 0.004)

        p_final = p_efectiva + ruido_p
        i_final = i_efectiva + ruido_i
        fp_final = max(0.0, min(1.0, fp_efectivo + ruido_fp))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equipo_id": equipo_id_actual,
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
    print("Generador de datos - Ventilador extractor axial")
    print("="*60)
    print("\nComandos de estado:")
    print("  marcha              (arrancar, punto de diseño 73%)")
    print("  apagar              (detener)")
    print("  carga X             (X = 0 a 100, carga porcentual para pruebas)")
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

            elif cmd == "marcha":
                marcha()

            elif cmd in ["apagar", "apagado", "parar", "off"]:
                apagar()

            elif cmd == "carga" and len(partes) == 2:
                try:
                    valor = float(partes[1])
                    carga_manual(valor)
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
    # Verificar registro del equipo (informativo, no bloquea)
    registrar_equipo_si_no_existe()

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
