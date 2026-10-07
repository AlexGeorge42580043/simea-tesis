"""
Orquestador de aprendizaje para la cinta transportadora.

Este script genera automáticamente un patrón realista de operación industrial
durante ~90 minutos, produciendo datos ricos y bien distribuidos para que el
sistema SIMEA pueda aprender un modelo de excelente calidad.

FUNDAMENTO METODOLÓGICO
───────────────────────
Un modelo aprendido de excelente calidad requiere:
- Suficientes muestras de cada estado operativo (mínimo ~100 por estado)
- Transiciones limpias entre estados (para que K-Means separe bien los clusters)
- Ciclos productivos completos y repetibles (para que se construya una curva
  patrón representativa)
- Suficiente variabilidad natural (para calibrar bien Isolation Forest)

PATRÓN DE OPERACIÓN
───────────────────
Simulamos una cinta transportadora en un proceso industrial real, donde los
tiempos DENTRO del ciclo productivo están definidos por el proceso, pero el
tiempo APAGADO entre ciclos varía aleatoriamente (30s a 5min), reflejando
que en la vida real los turnos, pausas y arranques no ocurren en horarios
exactos.

Cada ciclo productivo dura ~15 minutos y cubre todos los estados operativos:
    apagado → vacío → ligera → media → plena → media → ligera → vacío → apagado

En 90 minutos totales se completan ~4-5 ciclos productivos, más que suficiente
para que el sistema detecte comportamiento cíclico (mínimo 3 ciclos) y
construya la curva patrón.

USO
───
1. Iniciá sesión de aprendizaje desde la interfaz web (para la cinta).
2. Corré este script:  python orquestador_cinta.py [--duracion 90]
3. Esperá a que termine (informa progreso periódicamente).
4. Finalizá la sesión desde la interfaz web.
5. El sistema entrenará automáticamente el modelo.
6. Activá el modelo desde Modelos entrenados.
"""

import argparse
import json
import random
import sys
import os
import time
from datetime import datetime, timezone
import numpy as np
import paho.mqtt.client as mqtt

# Importar módulos del generador de la cinta (reutilizamos su modelo físico)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cinta'))
from motor_modelo import estado_motor
from transiciones import calcular_duracion_transicion, valor_en_transicion

# -----------------------------
# CONFIGURACIÓN
# -----------------------------

MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = 1883
MQTT_TOPIC_BASE = "planta"
EQUIPO_ID = "motor_cinta_1"
INTERVALO_MUESTRAS = 1.0  # segundos entre publicaciones (a tiempo real)

# Cargas de cada estado operativo (fracción de carga nominal del motor)
CARGA_APAGADO = 0.00
CARGA_VACIO   = 0.20   # cinta girando sin carga (solo fricción propia)
CARGA_LIGERA  = 0.45   # con carga baja
CARGA_MEDIA   = 0.70   # con carga media
CARGA_PLENA   = 0.95   # cerca de nominal

# Rango de pausa aleatoria entre ciclos productivos
PAUSA_MIN_S = 30
PAUSA_MAX_S = 300  # 5 minutos

# Estructura del ciclo productivo (fase, carga_objetivo, duracion_en_estado_s)
# El "duracion_en_estado_s" es el tiempo QUE PASA en ese estado una vez que
# terminó la transición. Las transiciones se calculan aparte.
CICLO_PRODUCTIVO = [
    ("arranque",         CARGA_VACIO,   60),   # apagado→vacío + 60s en vacío
    ("carga_ligera",     CARGA_LIGERA,  120),  # vacío→ligera + 120s en ligera
    ("aumento_a_media",  CARGA_MEDIA,   180),
    ("pico_plena",       CARGA_PLENA,   120),
    ("descenso_a_media", CARGA_MEDIA,   120),
    ("baja_a_ligera",    CARGA_LIGERA,  120),
    ("descarga",         CARGA_VACIO,   60),
    ("parada",           CARGA_APAGADO, 60),   # vacío→apagado + 60s apagado
]

# -----------------------------
# ESTADO INTERNO DEL ORQUESTADOR
# -----------------------------

class EstadoOrquestador:
    """Encapsula el estado del generador durante una fase del ciclo."""
    def __init__(self):
        self.carga_actual = 0.0
        self.nombre_fase = "preparacion"

    def transicion_a(self, carga_final, nombre_fase):
        """
        Ejecuta una transición desde la carga actual hasta la carga objetivo,
        publicando muestras en tiempo real durante toda la transición.

        Devuelve al finalizar (para que el llamador continúe con la próxima fase).
        """
        carga_inicial = self.carga_actual
        duracion = calcular_duracion_transicion(carga_inicial, carga_final)

        if abs(carga_final - carga_inicial) < 0.001:
            return  # No hay nada que hacer

        t_inicio = time.time()
        while True:
            t = time.time() - t_inicio
            if t >= duracion:
                break
            carga_ahora = valor_en_transicion(t, duracion, carga_inicial, carga_final)
            publicar_muestra(carga_ahora, f"transicion_{nombre_fase}")
            time.sleep(INTERVALO_MUESTRAS)

        self.carga_actual = carga_final
        self.nombre_fase = nombre_fase

    def mantener(self, segundos):
        """Publica muestras estables durante N segundos con la carga actual."""
        t_inicio = time.time()
        while time.time() - t_inicio < segundos:
            publicar_muestra(self.carga_actual, self.nombre_fase)
            time.sleep(INTERVALO_MUESTRAS)


# -----------------------------
# PUBLICACIÓN MQTT
# -----------------------------

cliente_mqtt = None
muestras_publicadas = 0


def publicar_muestra(carga_efectiva, nombre_estado):
    """Calcula P, I, FP con ruido y publica al broker MQTT."""
    global muestras_publicadas

    if carga_efectiva <= 0.001:
        p, i, fp = 0.0, 0.0, 0.0
    else:
        estado = estado_motor(carga_efectiva)
        p_base = estado["potencia_kw"]
        i_base = estado["corriente_a"]
        fp_base = estado["factor_potencia"]

        # Ruido gaussiano realista (mismo perfil que el generador manual)
        ruido_p = np.random.normal(0, max(0.015 * abs(p_base), 0.003))
        ruido_i = np.random.normal(0, max(0.015 * abs(i_base), 0.005))
        ruido_fp = np.random.normal(0, 0.005)

        p = p_base + ruido_p
        i = i_base + ruido_i
        fp = max(0.0, min(1.0, fp_base + ruido_fp))

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equipo_id": EQUIPO_ID,
        "potencia_kw": round(p, 4),
        "corriente_a": round(i, 4),
        "factor_potencia": round(fp, 4),
        "carga_actual": round(carga_efectiva, 4),
        "estado": nombre_estado,
        "anomalia": None,
    }

    topic = f"{MQTT_TOPIC_BASE}/{EQUIPO_ID}/mediciones"
    cliente_mqtt.publish(topic, json.dumps(payload))
    muestras_publicadas += 1


# -----------------------------
# ORQUESTACIÓN
# -----------------------------

def ejecutar_ciclo_productivo(num_ciclo, estado):
    """Ejecuta un ciclo productivo completo y devuelve al terminar."""
    print(f"\n{'═'*60}")
    print(f"CICLO PRODUCTIVO #{num_ciclo}")
    print(f"{'═'*60}")

    for nombre_fase, carga_objetivo, duracion_en_estado in CICLO_PRODUCTIVO:
        print(f"  → Fase: {nombre_fase} (carga objetivo {carga_objetivo*100:.0f}%, "
              f"mantener {duracion_en_estado}s)")
        estado.transicion_a(carga_objetivo, nombre_fase)
        estado.mantener(duracion_en_estado)


def ejecutar_pausa_entre_ciclos():
    """Espera un tiempo aleatorio entre ciclos, publicando muestras de apagado."""
    duracion = random.randint(PAUSA_MIN_S, PAUSA_MAX_S)
    print(f"\n  ⏸  Pausa entre ciclos: {duracion}s (aleatorio, motor apagado)")

    t_inicio = time.time()
    while time.time() - t_inicio < duracion:
        publicar_muestra(0.0, "pausa_entre_ciclos")
        time.sleep(INTERVALO_MUESTRAS)


def calcular_ciclos_a_ejecutar(duracion_total_min):
    """
    Estima cuántos ciclos productivos entran en la duración total configurada.
    Cada ciclo dura aproximadamente:
        - Transiciones (variables según el salto de carga): ~1-8s cada una
        - Tiempo en cada estado: suma de duracion_en_estado del CICLO_PRODUCTIVO
        - Pausa aleatoria promedio: (PAUSA_MIN_S + PAUSA_MAX_S) / 2
    En la práctica un ciclo completo con pausa promedio es ~19-22 min.
    """
    tiempo_estados = sum(d for _, _, d in CICLO_PRODUCTIVO)  # ≈ 840s = 14 min
    tiempo_transiciones = 8 * 5  # ~5s promedio × 8 transiciones = 40s
    pausa_promedio = (PAUSA_MIN_S + PAUSA_MAX_S) / 2  # ~165s = 2.75 min

    tiempo_por_ciclo_con_pausa = tiempo_estados + tiempo_transiciones + pausa_promedio
    duracion_total_s = duracion_total_min * 60
    num_ciclos = max(1, int(round(duracion_total_s / tiempo_por_ciclo_con_pausa)))
    return num_ciclos


# -----------------------------
# MAIN
# -----------------------------

def main():
    global cliente_mqtt

    parser = argparse.ArgumentParser(
        description="Orquestador de aprendizaje para la cinta transportadora"
    )
    parser.add_argument(
        "--duracion", type=int, default=90,
        help="Duración total del aprendizaje en minutos (default: 90)"
    )
    args = parser.parse_args()

    num_ciclos = calcular_ciclos_a_ejecutar(args.duracion)

    # Aviso previo al usuario
    print("=" * 60)
    print("ORQUESTADOR DE APRENDIZAJE - CINTA TRANSPORTADORA")
    print("=" * 60)
    print(f"\nEquipo: {EQUIPO_ID}")
    print(f"Topic MQTT: {MQTT_TOPIC_BASE}/{EQUIPO_ID}/mediciones")
    print(f"Duración configurada: {args.duracion} minutos")
    print(f"Ciclos productivos a ejecutar: {num_ciclos}")
    print(f"\nPatrón por ciclo:")
    for nombre_fase, carga, duracion in CICLO_PRODUCTIVO:
        print(f"  - {nombre_fase:20s} carga {carga*100:3.0f}%  duración {duracion:3d}s")
    print(f"\nPausa entre ciclos: aleatoria entre {PAUSA_MIN_S}s y {PAUSA_MAX_S}s")
    print(f"\n{'─'*60}")
    print("IMPORTANTE: antes de continuar, verificá que:")
    print(f"  1. La sesión de APRENDIZAJE está iniciada en la interfaz web")
    print(f"     para el equipo '{EQUIPO_ID}'.")
    print(f"  2. El broker MQTT y Node-RED están corriendo.")
    print(f"{'─'*60}\n")

    input("Presioná ENTER para arrancar (o Ctrl+C para cancelar)... ")

    # Conectar a MQTT
    cliente_mqtt = mqtt.Client()
    try:
        cliente_mqtt.connect(MQTT_BROKER, MQTT_PORT)
        print(f"\n✓ Conectado a broker MQTT ({MQTT_BROKER}:{MQTT_PORT})")
    except Exception as e:
        print(f"\n✗ Error conectando al broker MQTT: {e}")
        return

    cliente_mqtt.loop_start()

    # Ejecutar los ciclos
    estado = EstadoOrquestador()
    t_arranque = time.time()

    try:
        for i in range(1, num_ciclos + 1):
            ejecutar_ciclo_productivo(i, estado)

            # Pausa aleatoria SALVO después del último ciclo
            if i < num_ciclos:
                ejecutar_pausa_entre_ciclos()

            transcurrido = (time.time() - t_arranque) / 60
            print(f"\n  Progreso: {i}/{num_ciclos} ciclos completados "
                  f"({transcurrido:.1f} min transcurridos, "
                  f"{muestras_publicadas} muestras publicadas)")

    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario.")

    finally:
        cliente_mqtt.loop_stop()
        cliente_mqtt.disconnect()

        transcurrido = (time.time() - t_arranque) / 60
        print(f"\n{'═'*60}")
        print("EJECUCIÓN COMPLETADA")
        print(f"{'═'*60}")
        print(f"  Tiempo total transcurrido: {transcurrido:.1f} minutos")
        print(f"  Muestras publicadas: {muestras_publicadas}")
        print(f"  Ciclos productivos completados: {num_ciclos}")
        print(f"\nRECORDÁ:")
        print(f"  1. Finalizá la sesión de APRENDIZAJE desde la interfaz web.")
        print(f"  2. El sistema entrenará el modelo automáticamente.")
        print(f"  3. Activá el modelo desde 'Modelos entrenados'.")
        print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
