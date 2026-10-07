import pandas as pd
import numpy as np
import pickle
import os
import argparse
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import IsolationForest
import psycopg2
from kneed import KneeLocator

# -----------------------------
# CONFIGURACIÓN
# -----------------------------

INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUX_ORG = os.getenv("INFLUXDB_ORG")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET")

PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_USER = os.getenv("POSTGRES_USER")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD")
PG_DB = os.getenv("POSTGRES_DB")


RUTA_MODELOS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "modelos_entrenados")
)

# -----------------------------
# EXTRACCIÓN DE DATOS
# -----------------------------

def extraer_datos(equipo_id, fecha_inicio, fecha_fin):
    """
    Extrae los datos del bucket dentro del rango de la sesión de aprendizaje,
    filtrando por el equipo específico. Esencial en un entorno multi-equipo:
    sin este filtro se mezclarían datos de distintos equipos y el modelo
    aprendería un patrón sin sentido.
    """
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    inicio_str = fecha_inicio.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fin_str = fecha_fin.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
        |> range(start: time(v: "{inicio_str}"), stop: time(v: "{fin_str}"))
        |> filter(fn: (r) => r["_measurement"] == "mediciones")
        |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
        |> filter(fn: (r) => r["_field"] == "potencia_kw" or r["_field"] == "corriente_a" or r["_field"] == "factor_potencia")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    resultado = query_api.query_data_frame(query)
    client.close()

    if isinstance(resultado, list):
        resultado = pd.concat(resultado)

    resultado = resultado[["_time", "potencia_kw", "corriente_a", "factor_potencia"]]
    resultado.columns = ["tiempo", "potencia_kw", "corriente_a", "factor_potencia"]
    resultado = resultado.dropna().reset_index(drop=True)

    return resultado

# -----------------------------
# OBTENER ÚLTIMA SESIÓN DE APRENDIZAJE
# -----------------------------

def obtener_ultima_sesion_aprendizaje(equipo_id):
    """
    Busca en PostgreSQL la última sesión de aprendizaje cerrada del equipo
    y devuelve sus timestamps de inicio y fin, junto con las notas.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()

    cur.execute("""
        SELECT id, fecha_inicio, fecha_fin, usuario, notas
        FROM sesiones_modo
        WHERE modo = 'aprendizaje' AND activa = FALSE AND equipo_id = %s
        ORDER BY fecha_fin DESC
        LIMIT 1
    """, (equipo_id,))

    sesion = cur.fetchone()
    cur.close()
    conn.close()

    if not sesion:
        raise RuntimeError(
            f"No se encontró ninguna sesión de aprendizaje cerrada para el equipo '{equipo_id}'. "
            "Inicie y finalice una sesión de aprendizaje desde la interfaz."
        )

    id_sesion, fecha_inicio, fecha_fin, usuario, notas = sesion
    print(f"Sesión de aprendizaje encontrada: id={id_sesion}, equipo={equipo_id}, usuario={usuario}")
    print(f"  Inicio: {fecha_inicio}")
    print(f"  Fin: {fecha_fin}")
    if notas:
        print(f"  Notas: {notas}")

    return fecha_inicio, fecha_fin, notas

# -----------------------------
# OBTENER TIPO DE OPERACIÓN DEL EQUIPO
# -----------------------------

def obtener_tipo_operacion(equipo_id: str) -> str:
    """
    DEPRECADO. Se conserva por compatibilidad. Con el nuevo diseño, el sistema
    detecta automáticamente si el modelo es cíclico durante el entrenamiento,
    y guarda el resultado en `modelos.es_ciclico`. Este helper puede
    devolver el valor legacy de `equipos.tipo_operacion` para código antiguo
    que aún lo referencia, pero no se debe usar en lógica nueva.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()
    cur.execute("SELECT tipo_operacion FROM equipos WHERE id = %s", (equipo_id,))
    resultado = cur.fetchone()
    cur.close()
    conn.close()

    if not resultado:
        raise RuntimeError(f"Equipo '{equipo_id}' no encontrado")

    return resultado[0]

# Umbral mínimo de ciclos completos detectados para considerar que un modelo
# aprendido representa un equipo con comportamiento cíclico. Con menos ciclos,
# la curva patrón no sería estadísticamente representativa.
MINIMO_CICLOS_PARA_CICLICO = 3

# -----------------------------
# CÁLCULO DEL K ÓPTIMO MEDIANTE REGLA DEL CODO
# -----------------------------

def calcular_k_optimo(datos, k_min=2, k_max=8):
    """
    Calcula el K óptimo aplicando la regla del codo sobre los datos.
    Entrena K-Means para cada K en el rango y detecta automáticamente el
    punto de inflexión usando KneeLocator.
    """
    X = datos[["potencia_kw", "corriente_a"]].values

    valores_k = list(range(k_min, k_max + 1))
    inercias = []

    print("Evaluando K-Means para distintos valores de K:")
    for k in valores_k:
        modelo = KMeans(n_clusters=k, random_state=42, n_init=10)
        modelo.fit(X)
        inercias.append(modelo.inertia_)
        print(f"  K={k}: inercia={modelo.inertia_:.2f}")

    knee_locator = KneeLocator(
        valores_k, inercias,
        curve="convex", direction="decreasing"
    )
    k_optimo = knee_locator.knee

    if k_optimo is None:
        print("No se pudo detectar el codo automáticamente. Usando K=4 por defecto.")
        k_optimo = 4
    else:
        print(f"\nK óptimo detectado por regla del codo: {k_optimo}")

    return int(k_optimo)

# -----------------------------
# ENTRENAMIENTO K-MEANS
# -----------------------------

def entrenar_kmeans(datos, k):
    """
    Entrena K-Means sobre las 3 variables normalizadas.
    Guarda el scaler dentro del modelo para poder normalizar nuevas muestras
    durante el monitoreo.
    """
    X = datos[["potencia_kw", "corriente_a", "factor_potencia"]].values

    # Normalizar
    scaler = StandardScaler()
    X_norm = scaler.fit_transform(X)

    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    clusters = kmeans.fit_predict(X_norm)

    # Guardamos el scaler como atributo del modelo para reutilizarlo
    kmeans.scaler_ = scaler

    datos_con_cluster = datos.copy()
    datos_con_cluster["cluster"] = clusters

    return kmeans, datos_con_cluster

# -----------------------------
# CÁLCULO DE BANDAS DE TOLERANCIA
# -----------------------------

def calcular_bandas(datos, k):
    """Calcula media y desvío de potencia, corriente y factor de potencia para cada cluster."""
    bandas = []

    for cluster_id in range(k):
        subset = datos[datos["cluster"] == cluster_id]

        banda = {
            "cluster_id": int(cluster_id),
            "potencia_media": float(subset["potencia_kw"].mean()),
            "potencia_desvio": float(subset["potencia_kw"].std()),
            "corriente_media": float(subset["corriente_a"].mean()),
            "corriente_desvio": float(subset["corriente_a"].std()),
            "fp_media": float(subset["factor_potencia"].mean()),
            "fp_desvio": float(subset["factor_potencia"].std()),
            "cantidad_muestras": int(len(subset)),
        }
        bandas.append(banda)

        print(f"Cluster {cluster_id}: "
              f"P={banda['potencia_media']:.3f}±{banda['potencia_desvio']:.3f} kW | "
              f"I={banda['corriente_media']:.3f}±{banda['corriente_desvio']:.3f} A | "
              f"FP={banda['fp_media']:.3f}±{banda['fp_desvio']:.3f} | "
              f"n={banda['cantidad_muestras']}")

    return bandas

# -----------------------------
# ENTRENAMIENTO ISOLATION FOREST
# -----------------------------

def entrenar_isolation_forest(datos):
    """Entrena Isolation Forest con potencia, corriente y factor de potencia."""
    X = datos[["potencia_kw", "corriente_a", "factor_potencia"]].values

    modelo = IsolationForest(
        n_estimators=100,
        contamination=0.01,
        random_state=42
    )
    modelo.fit(X)

    # Calcular umbral empírico: percentil 1 más bajo de los datos normales
    scores_normales = modelo.score_samples(X)
    umbral_empirico = float(np.percentile(scores_normales, 5))

    print(f"Umbral empírico Isolation Forest: {umbral_empirico:.4f}")

    return modelo, umbral_empirico

# -----------------------------
# ESCRITURA DE PUNTOS CLUSTERIZADOS EN INFLUXDB
# -----------------------------

def guardar_clustering_en_influx(datos_con_cluster, id_modelo, equipo_id):
    """
    Escribe en InfluxDB cada punto original junto con el cluster que le
    asignó K-Means, etiquetado con el id_modelo y equipo_id correspondientes.
    Esto permite a Grafana graficar la nube de puntos coloreada por cluster
    para un modelo específico, sin mezclarse con entrenamientos futuros ni
    con datos de otros equipos.
    """
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    puntos = []
    for _, fila in datos_con_cluster.iterrows():
        punto = (
            Point("clustering")
            .tag("id_modelo", str(id_modelo))
            .tag("equipo_id", equipo_id)
            .tag("cluster", str(int(fila["cluster"])))
            .field("potencia_kw", float(fila["potencia_kw"]))
            .field("corriente_a", float(fila["corriente_a"]))
            .time(fila["tiempo"], WritePrecision.NS)
        )
        puntos.append(punto)

    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=puntos)
    write_api.close()
    client.close()

    print(f"Clustering guardado en InfluxDB: {len(puntos)} puntos para modelo id={id_modelo}")

# -----------------------------
# DETECCIÓN DE CICLOS
# -----------------------------

def detectar_ciclos(datos, umbral_parada=0.3, muestras_minimas_ciclo=100):
    """
    Detecta los límites entre ciclos basándose en cruces por el umbral de parada.
    Devuelve una lista de (indice_inicio, indice_fin) para cada ciclo completo.
    """
    potencia = datos["potencia_kw"].values
    en_parada = potencia < umbral_parada

    transiciones = []
    for i in range(1, len(en_parada)):
        if en_parada[i - 1] and not en_parada[i]:
            transiciones.append(i)

    ciclos = []
    for i in range(len(transiciones) - 1):
        inicio = transiciones[i]
        fin = transiciones[i + 1]
        if fin - inicio >= muestras_minimas_ciclo:
            ciclos.append((inicio, fin))

    print(f"Ciclos detectados: {len(ciclos)}")
    for idx, (inicio, fin) in enumerate(ciclos):
        duracion = fin - inicio
        print(f"  Ciclo {idx}: muestras {inicio}-{fin} (duración: {duracion}s)")

    return ciclos

# -----------------------------
# CONSTRUCCIÓN DE LA CURVA PATRÓN
# -----------------------------

def construir_curva_patron(datos, ciclos, puntos_normalizados=200):
    """
    Alinea todos los ciclos detectados a una escala de tiempo relativa común
    (0 a 1) y promedia potencia, corriente y factor de potencia.
    """
    tiempo_relativo_comun = np.linspace(0, 1, puntos_normalizados)

    matriz_potencia = []
    matriz_corriente = []
    matriz_fp = []

    for inicio, fin in ciclos:
        segmento = datos.iloc[inicio:fin].reset_index(drop=True)
        n = len(segmento)

        tiempo_relativo_ciclo = np.linspace(0, 1, n)

        potencia_interpolada = np.interp(
            tiempo_relativo_comun, tiempo_relativo_ciclo, segmento["potencia_kw"].values
        )
        corriente_interpolada = np.interp(
            tiempo_relativo_comun, tiempo_relativo_ciclo, segmento["corriente_a"].values
        )
        fp_interpolada = np.interp(
            tiempo_relativo_comun, tiempo_relativo_ciclo, segmento["factor_potencia"].values
        )

        matriz_potencia.append(potencia_interpolada)
        matriz_corriente.append(corriente_interpolada)
        matriz_fp.append(fp_interpolada)

    matriz_potencia = np.array(matriz_potencia)
    matriz_corriente = np.array(matriz_corriente)
    matriz_fp = np.array(matriz_fp)

    patron = pd.DataFrame({
        "tiempo_relativo": tiempo_relativo_comun,
        "potencia_media": matriz_potencia.mean(axis=0),
        "potencia_desvio": matriz_potencia.std(axis=0),
        "corriente_media": matriz_corriente.mean(axis=0),
        "corriente_desvio": matriz_corriente.std(axis=0),
        "fp_media": matriz_fp.mean(axis=0),
        "fp_desvio": matriz_fp.std(axis=0),
    })

    return patron

# -----------------------------
# PERSISTENCIA: ARCHIVOS .pkl
# -----------------------------

def guardar_modelos_pkl(kmeans, isolation_forest, timestamp):
    """Guarda los modelos entrenados como archivos pickle."""
    os.makedirs(RUTA_MODELOS, exist_ok=True)

    ruta_kmeans = f"{RUTA_MODELOS}/kmeans_{timestamp}.pkl"
    ruta_iforest = f"{RUTA_MODELOS}/isolation_forest_{timestamp}.pkl"

    with open(ruta_kmeans, "wb") as f:
        pickle.dump(kmeans, f)

    with open(ruta_iforest, "wb") as f:
        pickle.dump(isolation_forest, f)

    return ruta_kmeans, ruta_iforest

# -----------------------------
# PERSISTENCIA: POSTGRESQL — MODELO Y BANDAS
# -----------------------------

def guardar_en_postgres(equipo_id, k, datos, ruta_kmeans, ruta_iforest, bandas, umbral_iforest, descripcion, es_ciclico):
    """
    Guarda la metadata del modelo y las bandas de tolerancia en PostgreSQL.

    El nuevo modelo se guarda como 'inactivo' por defecto: la activación
    es manual desde la interfaz (Aprendizaje → Modelos entrenados → Activar).
    Esto permite:
      1. Entrenar un modelo mientras el sistema sigue monitoreando con el actual.
      2. Revisar el modelo entrenado antes de reemplazar al vigente.
      3. Mantener consistencia (incluso el primer modelo del equipo requiere
         activación manual).

    El parámetro `es_ciclico` se determina automáticamente en el flujo de
    entrenamiento a partir de la cantidad de ciclos detectados en los datos.
    Es una propiedad del modelo (no del equipo): el mismo equipo puede
    tener modelos cíclicos y no cíclicos si se re-entrena en diferentes
    períodos operativos.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO modelos (
            equipo_id, nombre_contexto, k_optimo, fecha_inicio_datos, fecha_fin_datos,
            cantidad_ciclos, estado, ruta_kmeans, ruta_isolation_forest,
            umbral_iforest, descripcion, es_ciclico
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        equipo_id,
        f"{equipo_id}_contexto",
        k,
        datos["tiempo"].min(),
        datos["tiempo"].max(),
        None,
        "inactivo",              # ← siempre inactivo al crearse
        ruta_kmeans,
        ruta_iforest,
        umbral_iforest,
        descripcion,
        es_ciclico,
    ))

    id_modelo = cur.fetchone()[0]

    for banda in bandas:
        cur.execute("""
            INSERT INTO bandas_tolerancia (
                id_modelo, cluster_id, potencia_media, potencia_desvio,
                corriente_media, corriente_desvio, fp_media, fp_desvio,
                cantidad_muestras
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            id_modelo,
            banda["cluster_id"],
            banda["potencia_media"],
            banda["potencia_desvio"],
            banda["corriente_media"],
            banda["corriente_desvio"],
            banda["fp_media"],
            banda["fp_desvio"],
            banda["cantidad_muestras"]
        ))

    conn.commit()
    cur.close()
    conn.close()

    print(f"\nModelo guardado en PostgreSQL con id={id_modelo}")
    if descripcion:
        print(f"Descripción: {descripcion}")
    print(f"Umbral Isolation Forest guardado: {umbral_iforest:.4f}")
    return id_modelo

# -----------------------------
# PERSISTENCIA: POSTGRESQL — CURVA PATRÓN
# -----------------------------

def guardar_curva_patron(id_modelo, patron):
    """Guarda la curva patrón en PostgreSQL, vinculada al modelo."""
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()

    for _, fila in patron.iterrows():
        cur.execute("""
            INSERT INTO curva_patron (
                id_modelo, tiempo_relativo, potencia_media, potencia_desvio,
                corriente_media, corriente_desvio, fp_media, fp_desvio
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            id_modelo,
            float(fila["tiempo_relativo"]),
            float(fila["potencia_media"]),
            float(fila["potencia_desvio"]),
            float(fila["corriente_media"]),
            float(fila["corriente_desvio"]),
            float(fila["fp_media"]),
            float(fila["fp_desvio"]),
        ))

    conn.commit()
    cur.close()
    conn.close()

    print(f"Curva patrón guardada ({len(patron)} puntos) para modelo id={id_modelo}")

# -----------------------------
# MAIN
# -----------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Entrena modelos para un equipo")
    parser.add_argument("--equipo", type=str, default="motor_cinta_1",
                        help="ID del equipo a entrenar (default: motor_cinta_1)")
    args = parser.parse_args()

    equipo_id = args.equipo

    print(f"Iniciando entrenamiento para equipo: {equipo_id}\n")

    fecha_inicio, fecha_fin, descripcion = obtener_ultima_sesion_aprendizaje(equipo_id)

    print("\nExtrayendo datos de InfluxDB...")
    datos = extraer_datos(equipo_id, fecha_inicio, fecha_fin)
    print(f"Datos extraídos: {len(datos)} muestras\n")

    print("Calculando K óptimo mediante la regla del codo...")
    k_optimo = calcular_k_optimo(datos)

    print(f"\nEntrenando K-Means con K={k_optimo}...")
    kmeans, datos_con_cluster = entrenar_kmeans(datos, k_optimo)

    print("\nCalculando bandas de tolerancia por cluster...")
    bandas = calcular_bandas(datos_con_cluster, k_optimo)

    print("\nEntrenando Isolation Forest...")
    isolation_forest, umbral_iforest = entrenar_isolation_forest(datos)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\nGuardando modelos en {RUTA_MODELOS}...")
    ruta_kmeans, ruta_iforest = guardar_modelos_pkl(kmeans, isolation_forest, timestamp)

    print("\nDetectando ciclos en los datos...")
    ciclos = detectar_ciclos(datos)
    es_ciclico = len(ciclos) >= MINIMO_CICLOS_PARA_CICLICO

    if es_ciclico:
        print(f"Detectados {len(ciclos)} ciclos (≥ {MINIMO_CICLOS_PARA_CICLICO}): el modelo se clasifica como CÍCLICO.")
    else:
        print(f"Detectados {len(ciclos)} ciclos (< {MINIMO_CICLOS_PARA_CICLICO}): el modelo se clasifica como NO CÍCLICO.")

    print("\nGuardando metadata en PostgreSQL...")
    id_modelo = guardar_en_postgres(
        equipo_id, k_optimo, datos, ruta_kmeans, ruta_iforest,
        bandas, umbral_iforest, descripcion, es_ciclico
    )

    print("\nGuardando asignación de clusters en InfluxDB...")
    guardar_clustering_en_influx(datos_con_cluster, id_modelo, equipo_id)

    if es_ciclico:
        print("\nConstruyendo curva patrón a partir de los ciclos detectados...")
        patron = construir_curva_patron(datos, ciclos)
        guardar_curva_patron(id_modelo, patron)
        print("Curva patrón guardada.")
    else:
        print("\nSe omite la construcción de la curva patrón (modelo no cíclico).")

    print(f"\n{'='*60}")
    print(f"Entrenamiento completo.")
    print(f"Modelo id={id_modelo} guardado como INACTIVO ({'cíclico' if es_ciclico else 'no cíclico'}).")
    print(f"Para usarlo, actívelo manualmente desde:")
    print(f"  Aprendizaje → Modelos entrenados → Activar")
    print(f"{'='*60}")