"""
Definición de estados operativos del motor.

Los estados representan puntos de operación típicos. La carga se expresa
como fracción de la carga nominal del motor (1.0 = nominal, 0.0 = detenido).

También se soporta carga continua mediante el comando 'carga X' donde X
es un porcentaje entre 0 y 100.
"""

ESTADOS = {
    "apagado":  {"carga": 0.00, "descripcion": "Motor detenido"},
    "vacio":    {"carga": 0.10, "descripcion": "Motor girando sin carga"},
    "ligera":   {"carga": 0.40, "descripcion": "Carga ligera"},
    "media":    {"carga": 0.65, "descripcion": "Carga media"},
    "plena":    {"carga": 0.95, "descripcion": "Carga plena"},
}


def obtener_carga_por_nombre(nombre):
    """
    Devuelve la carga (0.0 a 1.0) asociada al nombre del estado.
    Devuelve None si el nombre no es válido.
    """
    if nombre not in ESTADOS:
        return None
    return ESTADOS[nombre]["carga"]


def nombre_estado_por_carga(carga):
    """
    Dado un valor de carga, devuelve el nombre del estado más cercano.
    Útil para mostrar información al usuario.
    """
    # Encontrar el estado más cercano en carga
    mejor = None
    menor_distancia = float("inf")
    for nombre, info in ESTADOS.items():
        distancia = abs(info["carga"] - carga)
        if distancia < menor_distancia:
            menor_distancia = distancia
            mejor = nombre
    return mejor