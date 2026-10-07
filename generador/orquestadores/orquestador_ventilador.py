"""
Orquestador de aprendizaje para el ventilador axial industrial.

Este script genera automáticamente un patrón realista de operación del
ventilador durante ~45 minutos, incluyendo el arranque físico realista, un
período largo de régimen continuo, y una parada final. El objetivo es que
el sistema SIMEA aprenda un modelo de excelente calidad que reconozca tanto
el estado de marcha como el estado apagado.

DECISIÓN METODOLÓGICA
─────────────────────
El ventilador es una carga de operación CONTINUA por naturaleza. A diferencia
de la cinta transportadora, no tiene un patrón cíclico natural. Por eso, el
orquestador ejecuta UN SOLO ciclo apagado→marcha→apagado durante la sesión:

  1. Motor apagado inicial              → captura estado "cero"
  2. Arranque físico realista (10 s)     → curva característica de arranque
                                            (P cúbica, I exponencial, FP sigmoide)
  3. Régimen continuo (40 minutos)       → captura operación estable
  4. Parada eléctrica instantánea         → transición limpia a apagado
  5. Motor apagado final                 → refuerza estado "cero"

Con este patrón, el sistema detecta 1 solo ciclo (menor al umbral de 3
requerido para clasificar como cíclico), y correctamente identifica el modelo
como NO CÍCLICO. NO construye curva patrón, coherente con la naturaleza
del ventilador.

Los períodos de motor apagado (al principio y al final) son necesarios para
que K-Means identifique el cluster "apagado" como un estado operativo válido
del equipo, y no lo interprete como anomalía cuando el ventilador esté
detenido durante el monitoreo.

USO
───
1. Iniciá sesión de aprendizaje desde la interfaz web (para el ventilador).
2. Corré este script:  python orquestador_ventilador.py [--duracion 45]
3. Esperá a que termine (informa progreso periódicamente).
4. Finalizá la sesión desde la interfaz web.
5. El sistema entrenará automáticamente el modelo.
6. Activá el modelo desde Modelos entrenados.
"""

import argparse
import json
import sys
import os
import time
from datetime import datetime, timezone
import numpy as np
import paho.mqtt.client as mqtt

# Importar módulos del generador de la cinta (mismo motor físico)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cinta'))
from motor_modelo import estado_motor

# Importar curvas de arranque específicas del ventilador
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'ventilador'))
from curvas_arranque import (
    estado_ventilador_en_arranque,
    DURACION_ARRANQUE_S,
    CARGA_REGIMEN,
)

# -----------------------------
# CONFIGURACIÓN
# -----------------------------

MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = 1883
MQTT_TOPIC_BASE = "planta"
EQUIPO_ID = "ventilador_extraccion_1"
INTERVALO_MUESTRAS = 1.0  # segundos entre publicaciones (a tiempo real)

# Duración por defecto de las fases (segundos)
DURACION_APAGADO_INICIAL_S = 60      # 1 minuto de motor apagado antes de arrancar
DURACION_APAGADO_FINAL_S = 240       # 4 minutos apagado al final

# La duración del régimen se calcula para completar la duración total pedida.

# -----------------------------
# PUBLICACIÓN MQTT
# -----------------------------

cliente_mqtt = None
muestras_publicadas = 0


def publicar_muestra(p, i, fp, carga, nombre_estado):
    """Publica una muestra al broker MQTT."""
    global muestras_publicadas

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equipo_id": EQUIPO_ID,
        "potencia_kw": round(p, 4),
        "corriente_a": round(i, 4),
        "factor_potencia": round(fp, 4),
        "carga_actual": round(carga, 4),
        "estado": nombre_estado,
        "anomalia": None,
    }

    topic = f"{MQTT_TOPIC_BASE}/{EQUIPO_ID}/mediciones"
    cliente_mqtt.publish(topic, json.dumps(payload))
    muestras_publicadas += 1


def publicar_muestra_apagado(nombre_fase):
    """Publica una muestra con motor apagado (todos los valores en cero)."""
    publicar_muestra(0.0, 0.0, 0.0, 0.0, nombre_fase)


def publicar_muestra_regimen(nombre_fase):
    """
    Publica una muestra en régimen estable del ventilador.
    Usa el modelo del motor con la carga de régimen del ventilador (73%),
    y agrega ruido gaussiano realista.
    """
    estado = estado_motor(CARGA_REGIMEN)
    p_base = estado["potencia_kw"]
    i_base = estado["corriente_a"]
    fp_base = estado["factor_potencia"]

    # Ruido más bajo que la cinta porque el régimen del ventilador es más estable
    ruido_p = np.random.normal(0, max(0.010 * abs(p_base), 0.003))
    ruido_i = np.random.normal(0, max(0.010 * abs(i_base), 0.005))
    ruido_fp = np.random.normal(0, 0.004)

    p = p_base + ruido_p
    i = i_base + ruido_i
    fp = max(0.0, min(1.0, fp_base + ruido_fp))

    publicar_muestra(p, i, fp, CARGA_REGIMEN, nombre_fase)


def publicar_muestra_arranque(t_arranque):
    """
    Publica una muestra durante el arranque físico realista.
    Usa las curvas específicas del módulo curvas_arranque.
    """
    estado_arr = estado_ventilador_en_arranque(t_arranque)
    p_base = estado_arr["potencia_kw"]
    i_base = estado_arr["corriente_a"]
    fp_base = estado_arr["factor_potencia"]

    # Ruido bajo durante el arranque para no distorsionar la curva característica
    ruido_p = np.random.normal(0, max(0.005 * abs(p_base), 0.003))
    ruido_i = np.random.normal(0, max(0.005 * abs(i_base), 0.005))
    ruido_fp = np.random.normal(0, 0.004)

    p = p_base + ruido_p
    i = i_base + ruido_i
    fp = max(0.0, min(1.0, fp_base + ruido_fp))

    publicar_muestra(p, i, fp, CARGA_REGIMEN, "arranque_fisico")


# -----------------------------
# FASES DEL ORQUESTADOR
# -----------------------------

def fase_apagado(duracion_s, nombre_fase):
    """Publica muestras de motor apagado durante N segundos."""
    print(f"\n  → Fase: {nombre_fase} ({duracion_s}s con motor apagado)")
    t_inicio = time.time()
    while time.time() - t_inicio < duracion_s:
        publicar_muestra_apagado(nombre_fase)
        time.sleep(INTERVALO_MUESTRAS)


def fase_arranque_fisico():
    """Ejecuta el arranque físico realista de 10 segundos."""
    print(f"\n  → Fase: arranque físico ({DURACION_ARRANQUE_S:.0f}s)")
    print(f"      Pico de corriente inicial, potencia cúbica, FP sigmoide")
    t_inicio = time.time()
    while True:
        t = time.time() - t_inicio
        if t >= DURACION_ARRANQUE_S:
            break
        publicar_muestra_arranque(t)
        time.sleep(INTERVALO_MUESTRAS)


def fase_regimen(duracion_s):
    """Publica muestras del ventilador en régimen estable durante N segundos."""
    duracion_min = duracion_s / 60
    print(f"\n  → Fase: régimen estable ({duracion_min:.1f} min = {duracion_s}s)")
    t_inicio = time.time()
    ultimo_progreso = t_inicio
    while time.time() - t_inicio < duracion_s:
        publicar_muestra_regimen("regimen_continuo")
        time.sleep(INTERVALO_MUESTRAS)

        # Reporte de progreso cada 5 minutos
        if time.time() - ultimo_progreso >= 300:
            transcurrido_min = (time.time() - t_inicio) / 60
            restante_min = (duracion_s - (time.time() - t_inicio)) / 60
            print(f"      Régimen: {transcurrido_min:.1f} min transcurridos, "
                  f"{restante_min:.1f} min restantes...")
            ultimo_progreso = time.time()


# -----------------------------
# MAIN
# -----------------------------

def main():
    global cliente_mqtt

    parser = argparse.ArgumentParser(
        description="Orquestador de aprendizaje para el ventilador axial"
    )
    parser.add_argument(
        "--duracion", type=int, default=45,
        help="Duración total del aprendizaje en minutos (default: 45)"
    )
    args = parser.parse_args()

    # Calcular la duración del régimen para completar la duración total pedida
    duracion_total_s = args.duracion * 60
    duracion_regimen_s = duracion_total_s - (
        DURACION_APAGADO_INICIAL_S + DURACION_ARRANQUE_S + DURACION_APAGADO_FINAL_S
    )

    if duracion_regimen_s < 300:  # Menos de 5 minutos de régimen: muy poco
        print(f"⚠  Duración total muy corta ({args.duracion} min).")
        print(f"   Con las fases fijas (apagado inicial + arranque + apagado final)")
        print(f"   solo quedan {duracion_regimen_s}s para el régimen, insuficiente.")
        print(f"   Se recomienda un mínimo de 15 minutos.")
        return

    # Aviso previo al usuario
    print("=" * 60)
    print("ORQUESTADOR DE APRENDIZAJE - VENTILADOR AXIAL")
    print("=" * 60)
    print(f"\nEquipo: {EQUIPO_ID}")
    print(f"Topic MQTT: {MQTT_TOPIC_BASE}/{EQUIPO_ID}/mediciones")
    print(f"Duración configurada: {args.duracion} minutos")
    print(f"\nPlan de ejecución:")
    print(f"  1. Motor apagado inicial:      {DURACION_APAGADO_INICIAL_S}s")
    print(f"  2. Arranque físico realista:   {DURACION_ARRANQUE_S:.0f}s")
    print(f"  3. Régimen estable:            {duracion_regimen_s}s "
          f"({duracion_regimen_s/60:.1f} min)")
    print(f"  4. Parada + apagado final:     {DURACION_APAGADO_FINAL_S}s")
    print(f"\nEl sistema debería detectar este patrón como NO CÍCLICO")
    print(f"(1 solo ciclo, umbral es 3), coherente con operación continua.")
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
    t_arranque = time.time()

    try:
        # Fase 1: motor apagado inicial
        fase_apagado(DURACION_APAGADO_INICIAL_S, "apagado_inicial")

        # Fase 2: arranque físico realista
        fase_arranque_fisico()

        # Fase 3: régimen estable (la más larga)
        fase_regimen(duracion_regimen_s)

        # Fase 4: parada instantánea + apagado final
        # (la parada eléctrica es instantánea: la siguiente muestra ya es cero)
        fase_apagado(DURACION_APAGADO_FINAL_S, "apagado_final")

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
        print(f"\nRECORDÁ:")
        print(f"  1. Finalizá la sesión de APRENDIZAJE desde la interfaz web.")
        print(f"  2. El sistema entrenará el modelo automáticamente.")
        print(f"  3. El modelo debería clasificarse como NO cíclico.")
        print(f"  4. Activá el modelo desde 'Modelos entrenados'.")
        print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
