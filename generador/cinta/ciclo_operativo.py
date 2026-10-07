"""
Define el ciclo operativo del equipo monitoreado.

Establece la secuencia de fases por las que atraviesa el motor durante
un ciclo de operación normal, con sus duraciones y niveles de carga
correspondientes. Las transiciones entre fases son suaves (sigmoidales)
para reflejar el comportamiento real de un proceso industrial continuo.
"""

import numpy as np


# ════════════════════════════════════════════════════════════════════
# ESCALA TEMPORAL
# ════════════════════════════════════════════════════════════════════

# Factor de escala temporal: el ciclo real dura X segundos, pero la
# simulación lo comprime para que sea visible más rápido.

FACTOR_ESCALA = 6


# ════════════════════════════════════════════════════════════════════
# DEFINICIÓN DE FASES
# ════════════════════════════════════════════════════════════════════

# Cada fase tiene: nombre, duración (s simulados), carga inicial, carga final
# Las transiciones suaves entre fases evitan saltos bruscos.

FASES = [
    {"nombre": "reposo_inicial",     "duracion": 300,  "carga_ini": 0.0,  "carga_fin": 0.0},
    {"nombre": "arranque",           "duracion": 30,   "carga_ini": 0.0,  "carga_fin": 0.10, "transitorio": True},
    {"nombre": "vacio",              "duracion": 900,  "carga_ini": 0.10, "carga_fin": 0.10},
    {"nombre": "subida_ligera",      "duracion": 300,  "carga_ini": 0.10, "carga_fin": 0.40},
    {"nombre": "carga_ligera",       "duracion": 2700, "carga_ini": 0.40, "carga_fin": 0.40},
    {"nombre": "subida_media",       "duracion": 300,  "carga_ini": 0.40, "carga_fin": 0.65},
    {"nombre": "carga_media",        "duracion": 5400, "carga_ini": 0.65, "carga_fin": 0.65},
    {"nombre": "subida_plena",       "duracion": 300,  "carga_ini": 0.65, "carga_fin": 0.95},
    {"nombre": "carga_plena",        "duracion": 5400, "carga_ini": 0.95, "carga_fin": 0.95},
    {"nombre": "decaimiento",        "duracion": 1800, "carga_ini": 0.95, "carga_fin": 0.10},
    {"nombre": "parada",             "duracion": 30,   "carga_ini": 0.10, "carga_fin": 0.0},
    {"nombre": "reposo_final",       "duracion": 300,  "carga_ini": 0.0,  "carga_fin": 0.0},
]

# Duración total del ciclo en segundos simulados (escala motor real).
DURACION_CICLO_SIMULADO = sum(f["duracion"] for f in FASES)


# ════════════════════════════════════════════════════════════════════
# FUNCIÓN DE TRANSICIÓN SUAVE
# ════════════════════════════════════════════════════════════════════

def _sigmoide(t: float) -> float:
    """
    Curva sigmoidal de transición suave entre 0 y 1.
    Modela la transición física de la carga aplicada sobre el eje.
    """
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 3 * t**2 - 2 * t**3


# ════════════════════════════════════════════════════════════════════
# CÁLCULO DE LA CARGA EN UN INSTANTE DADO
# ════════════════════════════════════════════════════════════════════

def carga_en_tiempo(t_simulado: float) -> dict:
    """
    Devuelve el nivel de carga aplicado al motor en el tiempo t (segundos simulados).

    También devuelve el nombre de la fase actual, lo que facilita el debug
    y permite al generador saber si el motor está parado o no.

    Devuelve:
        dict con keys: 'carga', 'fase', 'motor_encendido'
    """
    # Llevar t al rango del ciclo (módulo)
    t = t_simulado % DURACION_CICLO_SIMULADO

    tiempo_acumulado = 0.0

    for fase in FASES:
        duracion = fase["duracion"]
        if t < tiempo_acumulado + duracion:
            # Estamos en esta fase
            t_local = t - tiempo_acumulado
            progreso = t_local / duracion if duracion > 0 else 1.0

            # Transición sigmoidal entre carga inicial y final
            factor = _sigmoide(progreso)
            carga = fase["carga_ini"] + (fase["carga_fin"] - fase["carga_ini"]) * factor

            motor_encendido = (
                fase["nombre"] not in ("reposo_inicial", "reposo_final")
            )

            return {
                "carga": float(carga),
                "fase": fase["nombre"],
                "motor_encendido": motor_encendido,
                "es_transitorio": fase.get("transitorio", False),
            }

        tiempo_acumulado += duracion

    # No debería pasar, pero por seguridad
    return {
        "carga": 0.0,
        "fase": "indefinida",
        "motor_encendido": False,
        "es_transitorio": False,
    }


# ════════════════════════════════════════════════════════════════════
# AVANCE DEL RELOJ SIMULADO
# ════════════════════════════════════════════════════════════════════

def tiempo_simulado(t_real: float) -> float:
    """
    Convierte tiempo real (segundos reales del reloj) a tiempo simulado
    (segundos en la escala del motor real, comprimido por FACTOR_ESCALA).
    """
    return t_real * FACTOR_ESCALA


# ════════════════════════════════════════════════════════════════════
# AUTO-TEST: VISUALIZACIÓN DEL CICLO
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("Visualización del ciclo operativo de la cinta transportadora")
    print("=" * 70)

    total_duracion = sum(f["duracion"] for f in FASES)
    print(f"\nDuración total del ciclo: {total_duracion}s simulados = {total_duracion/60:.1f} min")
    print(f"Factor de escala temporal: {FACTOR_ESCALA}x")
    print(f"Equivalente en tiempo real: {total_duracion/FACTOR_ESCALA:.1f}s\n")

    print(f"{'Tiempo (s)':>12} {'Fase':<20} {'Carga':>8} {'Estado':<15}")
    print("-" * 65)

    # Muestreo cada 10 segundos a lo largo del ciclo
    for t in range(0, total_duracion + 1, 10):
        info = carga_en_tiempo(t)
        estado = "encendido" if info["motor_encendido"] else "apagado"
        transitorio = " [transitorio]" if info["es_transitorio"] else ""
        print(f"{t:>12} {info['fase']:<20} {info['carga']:>8.3f} {estado:<15}{transitorio}")