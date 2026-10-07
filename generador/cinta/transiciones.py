"""
Modelo de transiciones entre estados operativos.

Cuando el motor cambia de carga, la respuesta física no es instantánea:
- Hay un tiempo de establecimiento proporcional al salto
- Puede haber un sobrepico (overshoot) por la inercia del sistema
- Después se estabiliza en el nuevo valor

Se modela con una respuesta de segundo orden subamortiguada simplificada:
    y(t) = y_final + (y_final - y_inicial) * exp(-t/tau) * cos(omega*t) - (y_final - y_inicial) * exp(-t/tau)

Simplificada como envelope con overshoot inicial.
"""

import math


def calcular_duracion_transicion(carga_inicial, carga_final):
    """
    Devuelve la duración de la transición en segundos.
    Proporcional al salto de carga, con mínimo y máximo razonables.

    Saltos chicos: transiciones más rápidas (2-3 segundos).
    Saltos grandes: transiciones más lentas (hasta 10 segundos).
    """
    salto = abs(carga_final - carga_inicial)
    # Interpolación lineal: 0 salto → 1s, salto máximo (1.0) → 10s
    duracion = 1.0 + salto * 9.0
    return duracion


def calcular_overshoot(carga_inicial, carga_final):
    """
    Devuelve la magnitud del sobrepico como fracción del salto.

    Saltos chicos: overshoot pequeño (~5%).
    Saltos grandes: overshoot mayor (~25%).
    """
    salto = abs(carga_final - carga_inicial)
    # Interpolación: 0 salto → 0%, salto grande (1.0) → 25%
    return salto * 0.25


def valor_en_transicion(t, duracion, carga_inicial, carga_final):
    """
    Devuelve la carga instantánea durante una transición.

    Modelo: exponencial de establecimiento con overshoot.
    - Fase 1 (0 a 30% de duración): sube (o baja) hacia el objetivo + overshoot
    - Fase 2 (30% a 100%): converge exponencialmente al valor final

    Parámetros:
        t: tiempo transcurrido desde inicio de transición (segundos)
        duracion: duración total de la transición (segundos)
        carga_inicial: carga al inicio de la transición
        carga_final: carga objetivo

    Devuelve la carga instantánea (fracción).
    """
    if t >= duracion:
        return carga_final
    if t <= 0:
        return carga_inicial

    # Normalizar el tiempo entre 0 y 1
    tau = t / duracion

    salto = carga_final - carga_inicial
    overshoot = calcular_overshoot(carga_inicial, carga_final)

    # Modelo de respuesta con overshoot:
    # Rampa suave hasta pico (a t/duracion = 0.3), luego decaimiento exponencial hasta valor final
    if tau < 0.3:
        # Fase de subida al pico: sigmoide acelerada
        factor = tau / 0.3
        # Aumenta suavemente hasta 1 + overshoot
        valor_relativo = factor * factor * (1.0 + overshoot) / 1.0
        return carga_inicial + salto * valor_relativo
    else:
        # Fase de estabilización: exponencial decayendo desde el pico hasta 1.0
        tiempo_restante = (tau - 0.3) / 0.7
        # exp decaimiento: empieza en (1 + overshoot), llega a 1.0
        factor_decay = math.exp(-tiempo_restante * 4.0)
        valor_relativo = 1.0 + overshoot * factor_decay
        return carga_inicial + salto * valor_relativo