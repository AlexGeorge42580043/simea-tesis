"""
Modelo físico del motor trifásico WEG W22 IE3 7.5 kW 4P 380V 50Hz 132M.

Implementa las ecuaciones electromecánicas estándar del motor asíncrono
de inducción, parametrizadas con los datos reales del datasheet del fabricante.

Referencia: Catálogo técnico WEG, sección motores W22 línea premium.
"""

import numpy as np
from scipy.interpolate import CubicSpline

# ════════════════════════════════════════════════════════════════════
# PARÁMETROS NOMINALES (Datasheet WEG W22 IE3 7.5 kW 4P 132M)
# ════════════════════════════════════════════════════════════════════

# Eléctricos
P_NOMINAL = 7500.0          # W (potencia mecánica nominal)
V_NOMINAL = 400.0           # V (tensión nominal de línea)
I_NOMINAL = 13.9            # A (corriente nominal de línea)
FP_NOMINAL = 0.84           # Factor de potencia a plena carga
FRECUENCIA = 50.0           # Hz
POLOS = 4

# Mecánicos
N_SINCRONICA = 1500.0       # rpm
N_NOMINAL = 1465.0          # rpm (velocidad nominal en operación)
SLIP_NOMINAL = (N_SINCRONICA - N_NOMINAL) / N_SINCRONICA  # ≈ 0.0233
T_NOMINAL = 48.93           # N·m (par nominal)
T_ARRANQUE = 2.5 * T_NOMINAL  # N·m (par de arranque, dato datasheet)
T_MAXIMO = 3.4 * T_NOMINAL    # N·m (par máximo o breakdown)
I_ARRANQUE = 8.5 * I_NOMINAL  # A (corriente con rotor trabado)
J_ROTOR = 0.0642            # kg·m² (momento de inercia del rotor)

# Componente magnetizante (estimada en 32% de In, típico en motores 7.5 kW IE3)
I_MAGNETIZANTE = 0.32 * I_NOMINAL


# ════════════════════════════════════════════════════════════════════
# INTERPOLACIONES DESDE EL DATASHEET
# ════════════════════════════════════════════════════════════════════

# El datasheet WEG declara rendimiento y FP a 50%, 75% y 100% de carga.
# Para cargas menores extrapolamos según comportamiento físico estándar:
#   - En vacío, FP es muy bajo (predomina corriente magnetizante).
#   - En vacío, rendimiento es 0 (no entrega potencia útil).

# Carga (fracción), FP, Rendimiento
CARGA_PUNTOS    = np.array([0.00, 0.25, 0.50, 0.75, 1.00, 1.25])
FP_PUNTOS       = np.array([0.10, 0.45, 0.68, 0.79, 0.84, 0.86])
RENDIMIENTO_PTS = np.array([0.00, 0.85, 0.910, 0.915, 0.915, 0.910])

_fp_interp = CubicSpline(CARGA_PUNTOS, FP_PUNTOS, bc_type='natural')
_rendimiento_interp = CubicSpline(CARGA_PUNTOS, RENDIMIENTO_PTS, bc_type='natural')


def factor_potencia(carga: float) -> float:
    """Devuelve el factor de potencia en función del nivel de carga (0 a 1)."""
    carga = np.clip(carga, 0.0, 1.5)
    return float(_fp_interp(carga))


def rendimiento(carga: float) -> float:
    """Devuelve el rendimiento eléctrico-mecánico en función del nivel de carga."""
    carga = np.clip(carga, 0.0, 1.5)
    return float(_rendimiento_interp(carga))


# ════════════════════════════════════════════════════════════════════
# CORRIENTE EN FUNCIÓN DEL NIVEL DE CARGA
# ════════════════════════════════════════════════════════════════════

def corriente(carga: float) -> float:
    """
    Calcula la corriente de línea en función del nivel de carga.

    Modelo: I(carga) = √(I_magnetizante² + (k · carga)²)

    La corriente tiene una componente magnetizante constante (≈ 32% In)
    y una componente activa que crece con la carga. A plena carga,
    la suma cuadrática iguala a I_NOMINAL.

    El coeficiente k se calibra para que I(1.0) = I_NOMINAL.
    """
    k = np.sqrt(I_NOMINAL**2 - I_MAGNETIZANTE**2)
    return float(np.sqrt(I_MAGNETIZANTE**2 + (k * carga)**2))


# ════════════════════════════════════════════════════════════════════
# CURVA PAR-VELOCIDAD (ECUACIÓN DE KLOSS)
# ════════════════════════════════════════════════════════════════════

# Deslizamiento al cual ocurre el par máximo.
# Se despeja de la ecuación de Kloss usando el par de arranque (s=1).
# Relación T_arranque/T_max conocida del datasheet.
#
# Ecuación: T_arranque/T_max = 2 / (1/s_max + s_max)
# Despejando s_max:

def _calcular_smax():
    """Resuelve numéricamente s_max desde los datos del datasheet."""
    relacion = T_ARRANQUE / T_MAXIMO  # ≈ 0.735
    # Despejamos 1/s_max + s_max = 2/relacion
    suma = 2.0 / relacion
    # x² - suma·x + 1 = 0  →  x = (suma ± √(suma² - 4)) / 2
    # Tomamos la raíz menor (s_max < 1)
    discriminante = suma**2 - 4
    s_max = (suma - np.sqrt(discriminante)) / 2
    return float(s_max)

S_MAX = _calcular_smax()  # ≈ 0.32


def par_motor(slip: float) -> float:
    """
    Par electromagnético del motor según la ecuación de Kloss.

        T(s) = T_max · 2 / (s/s_max + s_max/s)

    Donde s es el deslizamiento actual y s_max el deslizamiento al par máximo.
    """
    if slip <= 0:
        return 0.0
    return T_MAXIMO * 2.0 / (slip / S_MAX + S_MAX / slip)


# ════════════════════════════════════════════════════════════════════
# POTENCIA ELÉCTRICA ACTIVA
# ════════════════════════════════════════════════════════════════════

def potencia_electrica(carga: float, tension: float = V_NOMINAL) -> float:
    """
    Calcula la potencia eléctrica activa en W para un nivel de carga dado.

    P_eléctrica = P_mecánica / rendimiento

    Se incluye la dependencia de la tensión por si se simula subtensión.
    Para tensión nominal, este factor es 1.
    """
    p_mecanica = carga * P_NOMINAL
    eta = rendimiento(carga)

    if eta == 0:
        # En vacío, la potencia eléctrica es solo la consumida por pérdidas magnéticas
        # (estimada en ~3% de la nominal para motores IE3)
        return 0.03 * P_NOMINAL

    return p_mecanica / eta


# ════════════════════════════════════════════════════════════════════
# ESTADO COMPLETO DEL MOTOR EN UN PUNTO DE OPERACIÓN
# ════════════════════════════════════════════════════════════════════

def estado_motor(carga: float, tension: float = V_NOMINAL) -> dict:
    """
    Devuelve el estado completo del motor para un nivel de carga dado.

    Esta es la función principal usada por el generador para producir
    cada muestra del motor en régimen estable.

    Parámetros:
        carga: nivel de carga normalizado (0 = vacío, 1 = nominal, >1 sobrecarga)
        tension: tensión de línea aplicada (default = nominal)

    Devuelve:
        Diccionario con potencia_kw, corriente_a, factor_potencia, tension_v
    """
    p_electrica_w = potencia_electrica(carga, tension)
    i = corriente(carga)
    fp = factor_potencia(carga)

    # Si hay desviación de tensión, se ajusta corriente para mantener la
    # potencia mecánica (compensación física real del motor).
    if tension != V_NOMINAL:
        factor_tension = V_NOMINAL / tension
        i = i * factor_tension
        # El FP también se deteriora levemente con la subtensión
        fp = fp * (tension / V_NOMINAL) ** 0.3

    return {
        "potencia_kw": p_electrica_w / 1000.0,
        "corriente_a": i,
        "factor_potencia": fp,
        "tension_v": tension,
    }


# ════════════════════════════════════════════════════════════════════
# VERIFICACIÓN DEL MODELO (auto-test al ejecutar como script)
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("Verificación del modelo del motor WEG W22 7.5 kW")
    print("=" * 60)

    print(f"\nParámetros derivados:")
    print(f"  Slip nominal: {SLIP_NOMINAL:.4f} (datasheet)")
    print(f"  Slip al par máximo (s_max): {S_MAX:.4f}")
    print(f"  Corriente magnetizante: {I_MAGNETIZANTE:.2f} A")

    print(f"\nEstado del motor a distintas cargas:")
    print(f"{'Carga':>8} {'P (kW)':>10} {'I (A)':>10} {'FP':>8} {'η':>8}")
    print("-" * 50)
    for carga in [0.0, 0.25, 0.50, 0.75, 1.00, 1.25]:
        e = estado_motor(carga)
        eta = rendimiento(carga)
        print(f"{carga:>8.2f} {e['potencia_kw']:>10.3f} {e['corriente_a']:>10.3f} "
              f"{e['factor_potencia']:>8.3f} {eta:>8.3f}")

    print(f"\nVerificación a plena carga (datasheet):")
    e = estado_motor(1.0)
    p_calculada = np.sqrt(3) * V_NOMINAL * e["corriente_a"] * e["factor_potencia"]
    print(f"  P eléctrica calculada: {e['potencia_kw']:.3f} kW")
    print(f"  P eléctrica verificada por √3·V·I·cos(φ): {p_calculada/1000:.3f} kW")
    print(f"  Datasheet declara: 7.5 kW de potencia mecánica")