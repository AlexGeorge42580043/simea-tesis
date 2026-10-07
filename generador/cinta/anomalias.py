"""
Modelos físicos de las anomalías que puede sufrir un motor asíncrono trifásico.

Cada anomalía se define como una modificación de los valores de operación normal.
Las modificaciones siguen el comportamiento físico real del fenómeno.

Referencias:
- WEG W22 7.5 kW: I_nominal = 13.9 A, P_nominal = 7.5 kW, FP_nominal = 0.84
"""


def aplicar_sobrecarga(p_normal, i_normal, fp_normal, intensidad):
    """
    Sobrecarga mecánica: el motor está trabajando por encima de su carga plena normal.

    Causa física: aumento de la carga mecánica que fuerza al motor a operar
    por encima de su capacidad habitual (cinta atascada, material atrapado,
    obstrucción mecánica, exceso de material transportado).

    Manifestación:
    - P por encima del máximo normal aprendido
    - I por encima del máximo normal aprendido
    - FP se mantiene relativamente alto (el motor sigue haciendo trabajo mecánico)

    Modelo: la sobrecarga lleva al motor a operar por encima de su carga plena
    normal, independientemente del estado desde el cual se aplica.

    - intensidad 0.0 → equivalente a plena carga (95% mecanico)
    - intensidad 0.5 → 117.5% mecánico (17.5% por encima)
    - intensidad 1.0 → 140% mecánico (40% por encima)

    Se usa el modelo físico del motor para calcular los valores eléctricos
    correctos, respetando el rendimiento y las curvas del datasheet.
    """
    from motor_modelo import estado_motor

    # Carga mecánica efectiva: desde 95% (equivalente a plena) hasta 140%
    carga_efectiva = 0.95 + 0.45 * intensidad

    # Calcular los valores eléctricos correctos usando el modelo del motor
    estado_sobrecargado = estado_motor(carga_efectiva)

    p_efectiva = estado_sobrecargado["potencia_kw"]
    i_efectiva = estado_sobrecargado["corriente_a"]
    fp_efectivo = estado_sobrecargado["factor_potencia"]

    # Corrección adicional: bajo sobrecarga el rendimiento cae más rápido
    # de lo que el modelo predice, por lo que la P eléctrica sube un poco extra
    if intensidad > 0.1:
        p_efectiva *= (1.0 + 0.03 * intensidad)  # hasta 3% extra por peores condiciones

    return p_efectiva, i_efectiva, fp_efectivo


def aplicar_falla_electrica(p_normal, i_normal, fp_normal, intensidad):
    """
    Falla eléctrica por cortocircuito parcial entre espiras del bobinado.

    Causa física: aislamiento degradado entre espiras genera corrientes
    parasitas que aumentan las perdidas Joule pero no el trabajo mecánico.

    Manifestación:
    - I aumenta significativamente (corrientes parasitas del cortocircuito)
    - P aumenta muy poco (solo por las perdidas adicionales)
    - FP baja fuertemente (aumenta la componente reactiva)

    Modelo: aumento absoluto en I (independiente del punto de operación,
    porque el cortocircuito genera la misma corriente parasita), aumento
    pequeño en P, caída notable en FP.

    Se manifiesta en cualquier estado con corriente circulando.
    """
    # I sube por corrientes parásitas: aumento absoluto proporcional al I nominal
    I_NOMINAL = 13.9
    i_extra = intensidad * I_NOMINAL * 0.50
    i_efectiva = i_normal + i_extra

    # P sube muy poco (solo pérdidas Joule adicionales)
    p_extra = intensidad * 0.20  # kW extra por perdidas
    p_efectiva = p_normal + p_extra

    # FP baja fuerte: multiplicamos por (1 - factor)
    fp_efectivo = fp_normal * (1.0 - 0.40 * intensidad)
    fp_efectivo = max(fp_efectivo, 0.05)  # no puede ser negativo

    return p_efectiva, i_efectiva, fp_efectivo


def aplicar_subtension(p_normal, i_normal, fp_normal, intensidad):
    """
    Subtensión de red: la tensión de alimentación cae por debajo del nominal.

    Causa física: caída sostenida de tensión en la red (por sobrecargas en
    la instalación, problemas del transformador, cableado insuficiente).

    Manifestación:
    - V baja
    - Para mantener P mecanica (torque), I debe subir en proporción inversa
    - FP baja porque el motor trabaja fuera de su punto óptimo
    - P se mantiene aproximadamente igual (mismo trabajo mecánico)

    Modelo: I sube proporcional al I actual (compensación), FP baja,
    P se mantiene.

    Se manifiesta en cualquier estado donde el motor este operando.
    """
    # I sube para compensar la caída de tensión
    # Si V baja 15% * intensidad, I debe subir aproximadamente eso para mantener P
    factor_i = 1.0 + 0.18 * intensidad
    i_efectiva = i_normal * factor_i

    # P se mantiene aproximadamente (mismo trabajo mecánico)
    p_efectiva = p_normal * (1.0 + 0.02 * intensidad)  # ligero aumento por pérdidas

    # FP baja por operación fuera del punto óptimo
    fp_efectivo = fp_normal * (1.0 - 0.25 * intensidad)
    fp_efectivo = max(fp_efectivo, 0.10)

    return p_efectiva, i_efectiva, fp_efectivo


def aplicar_degradacion(p_normal, i_normal, fp_normal, intensidad):
    """
    Degradación mecánica: desgaste de rodamientos y fricción interna.

    Causa física: rodamientos con desgaste, falta de lubricación, alineación
    imperfecta. En la realidad este fenómeno es muy lento (meses o años),
    aquí se simula acelerado para poder demostrarlo.

    Manifestación:
    - I aumenta ligeramente (mayor fricción interna a vencer)
    - P aumenta ligeramente (más pérdidas por fricción)
    - FP se mantiene prácticamente igual
    - El efecto es una combinación de fricción constante + fricción proporcional a carga

    Modelo: componente constante (fricción interna) + componente proporcional
    (mayor carga = más pérdidas en rodamientos degradados).
    """
    # Componente constante: aumento absoluto pequeño
    i_extra_constante = intensidad * 0.30  # A extras por fricción
    p_extra_constante = intensidad * 0.15  # kW extras

    # Componente proporcional a la carga actual: mayor carga = más fricción
    i_extra_proporcional = i_normal * intensidad * 0.05
    p_extra_proporcional = p_normal * intensidad * 0.05

    i_efectiva = i_normal + i_extra_constante + i_extra_proporcional
    p_efectiva = p_normal + p_extra_constante + p_extra_proporcional
    fp_efectivo = fp_normal  # casi no cambia

    return p_efectiva, i_efectiva, fp_efectivo


# -----------------------------
# DESPACHADOR DE ANOMALIAS
# -----------------------------

def aplicar_anomalia(tipo, p_normal, i_normal, fp_normal, intensidad):
    """
    Punto único de entrada para aplicar cualquier anomalía.

    Parámetros:
        tipo: uno de {"sobrecarga", "falla", "subtension", "degradacion"}
        p_normal, i_normal, fp_normal: valores del motor en operación normal
        intensidad: entre 0.0 y 1.0

    Devuelve: (p_efectiva, i_efectiva, fp_efectivo)
    """
    if tipo == "sobrecarga":
        return aplicar_sobrecarga(p_normal, i_normal, fp_normal, intensidad)
    elif tipo == "falla":
        return aplicar_falla_electrica(p_normal, i_normal, fp_normal, intensidad)
    elif tipo == "subtension":
        return aplicar_subtension(p_normal, i_normal, fp_normal, intensidad)
    elif tipo == "degradacion":
        return aplicar_degradacion(p_normal, i_normal, fp_normal, intensidad)
    else:
        # tipo desconocido, no aplicamos anomalía
        return p_normal, i_normal, fp_normal