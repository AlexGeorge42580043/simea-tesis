import time
import pickle
import numpy as np
import pandas as pd
import psycopg2
import os
from collections import deque
from dotenv import load_dotenv
load_dotenv()
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone, timedelta

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

INTERVALO_SEGUNDOS = 5

# Umbral de detección por bandas de tolerancia (desvíos estándar)
DESVIOS_UMBRAL_BANDAS = 3.0

# Ventana de confirmación: cuántas muestras consecutivas se evalúan
# y cuántas de ellas deben ser anómalas para confirmar una alerta
VENTANA_CONFIRMACION = 10
UMBRAL_CONFIRMACION = 8

# Tiempo de supresión de evaluación tras cambio de cluster (segundos)
SUPRESION_TRANSICION = 8

# Umbrales de clasificación por desvío máximo (en sigmas)
UMBRAL_ALERTA = 3.5   # entre 3.5σ y 6σ: ALERTA
UMBRAL_CRITICA = 6.0  # mayor a 6σ: CRÍTICA

# -----------------------------
# ESTADO POR EQUIPO
# -----------------------------

class EstadoMonitor:
    """
    Estado del sistema de monitoreo (una instancia por equipo).

    Implementa dos etapas de decisión:
    1. Detección: cada muestra se evalúa como anómala o normal.
    2. Confirmación: solo si la mayoría de muestras en una ventana son
       anómalas, se confirma la alerta y se genera evento.

    La recuperación requiere muchas muestras normales consecutivas y solo
    ocurre por comportamiento del motor o por reconocimiento del operador.
    """

    def __init__(self):
        # Ventana deslizante para confirmación de anomalías
        self.ventana_confirmacion = deque(maxlen=VENTANA_CONFIRMACION)

        # Estado del sistema de alertas: solo se resuelve por reconocimiento del operador
        self.alerta_activa = False
        self.timestamp_alerta_activa = None

        # Control de transiciones entre clusters
        self.cluster_anterior = None
        self.timestamp_cambio_cluster = None

    def registrar_evaluacion(self, evaluacion, timestamp):
        """Registra el resultado de una evaluación en la ventana de confirmación."""
        # Durante transiciones, la muestra no cuenta para nada
        if self.en_transicion(timestamp):
            self._actualizar_cluster(evaluacion, timestamp)
            return

        es_anomala = evaluacion["anomalia_banda"] or evaluacion["anomalia_iforest"]

        # Alimentar ventana de confirmación
        self.ventana_confirmacion.append(1 if es_anomala else 0)

        self._actualizar_cluster(evaluacion, timestamp)

    def _actualizar_cluster(self, evaluacion, timestamp):
        """Detecta cambios de cluster para gestionar transiciones."""
        cluster_actual = evaluacion["cluster_id"]
        if self.cluster_anterior is not None and cluster_actual != self.cluster_anterior:
            self.timestamp_cambio_cluster = timestamp
        self.cluster_anterior = cluster_actual

    def en_transicion(self, timestamp):
        """Devuelve True si el sistema está en período de supresión por transición."""
        if self.timestamp_cambio_cluster is None:
            return False
        segundos_desde_cambio = (
            timestamp - self.timestamp_cambio_cluster
        ).total_seconds()
        return segundos_desde_cambio < SUPRESION_TRANSICION

    def debe_confirmar_alerta(self):
        """
        Devuelve True si la ventana está llena y la mayoría de muestras
        son anómalas. Solo se puede confirmar si no hay alerta activa.
        """
        if self.alerta_activa:
            return False

        if len(self.ventana_confirmacion) < VENTANA_CONFIRMACION:
            return False

        positivos = sum(self.ventana_confirmacion)
        return positivos >= UMBRAL_CONFIRMACION

    def registrar_alerta(self, timestamp):
        """Registra que se generó una alerta y activa el estado 'en alerta'."""
        self.alerta_activa = True
        self.timestamp_alerta_activa = timestamp
        # Vaciamos la ventana para no re-confirmar con datos del mismo evento
        self.ventana_confirmacion.clear()

    def reconocer_alerta(self, equipo_id):
        """Desactiva el estado 'en alerta' porque el operador la reconoció."""
        if self.alerta_activa:
            print(f"[{equipo_id}] 🟢 Alerta reconocida por operador. Sistema listo para nuevas alertas.")
        self.alerta_activa = False
        self.timestamp_alerta_activa = None
        self.ventana_confirmacion.clear()


class MonitorEquipo:
    """
    Contenedor por equipo: agrupa el modelo cargado, sus bandas, mapeos
    y todo el estado dinámico (EstadoMonitor + último timestamp procesado).
    Reemplaza las variables globales del monitor mono-equipo original.
    """

    def __init__(self, id_modelo, equipo_id, nombre_equipo, tipo_operacion,
                 kmeans, isolation_forest, bandas, umbral_iforest, mapeo_estados):
        self.id_modelo = id_modelo
        self.equipo_id = equipo_id
        self.nombre_equipo = nombre_equipo
        self.tipo_operacion = tipo_operacion
        self.kmeans = kmeans
        self.isolation_forest = isolation_forest
        self.bandas = bandas
        self.umbral_iforest = umbral_iforest
        self.mapeo_estados = mapeo_estados

        # Estado dinámico por equipo
        self.estado = EstadoMonitor()
        self.ultimo_timestamp = None
        self.sesion_anterior = False
        self.alertas_totales = 0


# -----------------------------
# CARGA DE MODELOS ACTIVOS
# -----------------------------

def cargar_modelo_de_equipo(equipo_id):
    """
    Carga el modelo activo del equipo indicado desde PostgreSQL y disco.
    Devuelve un MonitorEquipo listo para procesar, o None si no hay modelo activo.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()

    cur.execute("""
        SELECT id, k_optimo, ruta_kmeans, ruta_isolation_forest, umbral_iforest
        FROM modelos
        WHERE estado = 'activo' AND equipo_id = %s
        LIMIT 1
    """, (equipo_id,))
    modelo_row = cur.fetchone()

    if not modelo_row:
        cur.close()
        conn.close()
        return None

    id_modelo, k_optimo, ruta_kmeans, ruta_iforest, umbral_iforest = modelo_row

    cur.execute("""
        SELECT cluster_id, potencia_media, potencia_desvio,
               corriente_media, corriente_desvio,
               fp_media, fp_desvio
        FROM bandas_tolerancia
        WHERE id_modelo = %s
        ORDER BY cluster_id
    """, (id_modelo,))
    bandas_rows = cur.fetchall()

    cur.execute("SELECT tipo_operacion, nombre FROM equipos WHERE id = %s", (equipo_id,))
    fila_equipo = cur.fetchone()
    tipo_operacion, nombre_equipo = fila_equipo if fila_equipo else (None, None)

    cur.close()
    conn.close()

    bandas = {}
    for row in bandas_rows:
        cluster_id, p_media, p_desvio, i_media, i_desvio, fp_media, fp_desvio = row
        bandas[cluster_id] = {
            "potencia_media": p_media,
            "potencia_desvio": p_desvio,
            "corriente_media": i_media,
            "corriente_desvio": i_desvio,
            "fp_media": fp_media,
            "fp_desvio": fp_desvio,
        }

    # Calcular mapeo cluster_interno → estado_operativo (ordenado por potencia)
    # Esto asegura que la numeración mostrada al usuario sea consistente con
    # la que aparece en la interfaz web (ordenada de menor a mayor consumo).
    clusters_ordenados = sorted(bandas.keys(), key=lambda c: bandas[c]["potencia_media"])
    mapeo_estados = {cluster_id: idx + 1 for idx, cluster_id in enumerate(clusters_ordenados)}

    with open(ruta_kmeans, "rb") as f:
        kmeans = pickle.load(f)

    with open(ruta_iforest, "rb") as f:
        isolation_forest = pickle.load(f)

    print(f"[{equipo_id}] Modelo activo cargado: id={id_modelo}, K={k_optimo}, nombre={nombre_equipo}")
    print(f"[{equipo_id}] Bandas de tolerancia: {len(bandas)} clusters, umbral IF={float(umbral_iforest):.4f}")
    for cluster_id, estado_op in mapeo_estados.items():
        print(f"[{equipo_id}]   Cluster {cluster_id} → Estado #{estado_op} (P={bandas[cluster_id]['potencia_media']:.3f} kW)")

    return MonitorEquipo(
        id_modelo=id_modelo,
        equipo_id=equipo_id,
        nombre_equipo=nombre_equipo,
        tipo_operacion=tipo_operacion,
        kmeans=kmeans,
        isolation_forest=isolation_forest,
        bandas=bandas,
        umbral_iforest=float(umbral_iforest),
        mapeo_estados=mapeo_estados,
    )


def cargar_todos_los_modelos_activos():
    """
    Descubre todos los equipos con modelo activo y carga la configuración
    de cada uno. Devuelve un dict {equipo_id: MonitorEquipo}.
    """
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    cur = conn.cursor()
    cur.execute("SELECT equipo_id FROM modelos WHERE estado = 'activo'")
    equipos = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    monitores = {}
    for equipo_id in equipos:
        try:
            monitor = cargar_modelo_de_equipo(equipo_id)
            if monitor:
                monitores[equipo_id] = monitor
        except Exception as e:
            print(f"[{equipo_id}] ❌ No se pudo cargar el modelo: {e}")

    return monitores

# -----------------------------
# VERIFICACIÓN DE SESIONES DE MONITOREO
# -----------------------------

def equipos_con_sesion_activa():
    """Devuelve la lista de equipo_id con sesión de monitoreo activa."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT equipo_id FROM sesiones_modo
            WHERE modo = 'monitoreo' AND activa = TRUE
        """)
        equipos = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return equipos
    except Exception as e:
        print(f"Error consultando sesiones: {e}")
        return []

# -----------------------------
# LECTURA DE NUEVOS DATOS (POR EQUIPO)
# -----------------------------

def leer_nuevos_datos(client, equipo_id, ultimo_timestamp):
    """
    Lee los datos de InfluxDB del equipo indicado, más recientes que el
    último timestamp procesado. El filtro por equipo_id es lo único que
    permite el multi-equipo: cada equipo solo ve sus propios datos.
    Devuelve (DataFrame, nuevo_ultimo_timestamp).
    """
    query_api = client.query_api()

    if ultimo_timestamp is None:
        rango = "-10s"
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
            |> range(start: {rango})
            |> filter(fn: (r) => r["_measurement"] == "mediciones")
            |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
            |> filter(fn: (r) => r["_field"] == "potencia_kw" or r["_field"] == "corriente_a" or r["_field"] == "factor_potencia")
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["_time"])
        '''
    else:
        ts = ultimo_timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
            |> range(start: time(v: "{ts}"))
            |> filter(fn: (r) => r["_measurement"] == "mediciones")
            |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
            |> filter(fn: (r) => r["_field"] == "potencia_kw" or r["_field"] == "corriente_a" or r["_field"] == "factor_potencia")
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["_time"])
        '''

    try:
        resultado = query_api.query_data_frame(query)
        if isinstance(resultado, list):
            if len(resultado) == 0:
                return pd.DataFrame(), ultimo_timestamp
            resultado = pd.concat(resultado)
        if resultado.empty:
            return pd.DataFrame(), ultimo_timestamp

        resultado = resultado[["_time", "potencia_kw", "corriente_a", "factor_potencia"]].dropna()
        resultado.columns = ["tiempo", "potencia_kw", "corriente_a", "factor_potencia"]
        resultado = resultado.reset_index(drop=True)

        nuevo_ultimo = ultimo_timestamp
        if not resultado.empty:
            nuevo_ultimo = resultado["tiempo"].max()

        return resultado, nuevo_ultimo

    except Exception as e:
        print(f"[{equipo_id}] Error leyendo InfluxDB: {e}")
        return pd.DataFrame(), ultimo_timestamp

# -----------------------------
# EVALUACIÓN DE ANOMALÍAS (IDÉNTICA AL ORIGINAL)
# -----------------------------

def evaluar_punto(punto, kmeans, isolation_forest, bandas, umbral_iforest):
    """
    Evalúa una muestra individual.

    Devuelve un dict con:
    - cluster_id: estado operativo identificado por K-Means
    - anomalia_banda: True si alguna variable supera DESVIOS_UMBRAL_BANDAS
    - anomalia_iforest: True si Isolation Forest la marca como anómala
    - desvios: dict con desvíos (en sigmas) de cada variable respecto al cluster
    - score_iforest: score de Isolation Forest
    """
    p = punto["potencia_kw"]
    i = punto["corriente_a"]
    fp = punto["factor_potencia"]

    # K-Means usa las 3 variables normalizadas (P, I, FP)
    X_kmeans = np.array([[p, i, fp]])
    # Aplicar el mismo scaler que se usó en entrenamiento
    X_kmeans_norm = kmeans.scaler_.transform(X_kmeans)
    cluster_id = int(kmeans.predict(X_kmeans_norm)[0])
    banda = bandas[cluster_id]

    # Calcular desvíos de cada variable en sigmas
    desvios = {
        "potencia": abs(p - banda["potencia_media"]) / banda["potencia_desvio"] if banda["potencia_desvio"] > 0 else 0.0,
        "corriente": abs(i - banda["corriente_media"]) / banda["corriente_desvio"] if banda["corriente_desvio"] > 0 else 0.0,
        "factor_potencia": abs(fp - banda["fp_media"]) / banda["fp_desvio"] if banda["fp_desvio"] > 0 else 0.0,
    }

    # También guardamos las direcciones del desvío (signo)
    direcciones = {
        "potencia": 1 if p > banda["potencia_media"] else -1,
        "corriente": 1 if i > banda["corriente_media"] else -1,
        "factor_potencia": 1 if fp > banda["fp_media"] else -1,
    }

    # Bandas: alguna variable supera DESVIOS_UMBRAL_BANDAS
    anomalia_banda = any(d > DESVIOS_UMBRAL_BANDAS for d in desvios.values())

    # Isolation Forest evalúa con las tres variables
    X_iforest = np.array([[p, i, fp]])
    score = isolation_forest.score_samples(X_iforest)[0]
    anomalia_iforest = score < umbral_iforest

    return {
        "cluster_id": cluster_id,
        "anomalia_banda": anomalia_banda,
        "anomalia_iforest": anomalia_iforest,
        "desvios": desvios,
        "direcciones": direcciones,
        "score_iforest": round(float(score), 4),
    }

# -----------------------------
# CLASIFICACIÓN DE SEVERIDAD (IDÉNTICA AL ORIGINAL)
# -----------------------------

def clasificar_severidad(desvios):
    """
    Clasifica la severidad de la anomalía en base al desvío máximo
    entre las tres variables.

    - AVISO: desvío máximo por debajo de UMBRAL_ALERTA (3.5σ). Cubre tanto
      los desvíos que recién superan el umbral de detección por bandas (3σ)
      como los confirmados únicamente por Isolation Forest, que puede
      disparar con desvíos individuales menores.
    - ALERTA: desvío máximo entre UMBRAL_ALERTA (3.5σ) y UMBRAL_CRITICA (6σ)
    - CRÍTICA: desvío máximo mayor a UMBRAL_CRITICA (6σ)
    """
    desvio_maximo = max(desvios.values())

    if desvio_maximo >= UMBRAL_CRITICA:
        return "CRITICA", desvio_maximo
    elif desvio_maximo >= UMBRAL_ALERTA:
        return "ALERTA", desvio_maximo
    else:
        return "AVISO", desvio_maximo


# -----------------------------
# CLASIFICACIÓN DE FIRMA FÍSICA (IDÉNTICA AL ORIGINAL)
# -----------------------------

def identificar_firma(desvios, direcciones):
    """
    Genera una descripción sintomática objetiva del patrón de anomalía detectado.

    A diferencia de un enfoque basado en firmas específicas (que asume conocimiento
    del tipo de equipo monitoreado), este enfoque reporta únicamente los hechos
    observables: qué variables se desviaron significativamente, en qué dirección
    y con qué magnitud.

    La interpretación causal se delega al operador o a la capa de análisis
    complementaria, respetando la filosofía del sistema de aprender del
    comportamiento sin conocimiento previo del equipo.

    Umbrales de clasificación:
    - Leve: desvío entre 2σ y 3σ
    - Significativo: desvío entre 3σ y 5σ
    - Marcado: desvío mayor a 5σ
    """
    UMBRAL_MINIMO = 2.0
    UMBRAL_SIGNIFICATIVO = 3.0
    UMBRAL_MARCADO = 5.0

    def clasificar_magnitud(desvio):
        if desvio < UMBRAL_MINIMO:
            return None  # Dentro del rango normal
        elif desvio < UMBRAL_SIGNIFICATIVO:
            return "leve"
        elif desvio < UMBRAL_MARCADO:
            return "significativo"
        else:
            return "marcado"

    def descripcion_variable(nombre, desvio, direccion):
        """Devuelve una descripción de una variable específica o None si es normal."""
        magnitud = clasificar_magnitud(desvio)
        if magnitud is None:
            return None
        sentido = "aumento" if direccion > 0 else "caída"
        return f"{nombre}: {sentido} {magnitud} ({desvio:.1f}σ)"

    # Analizar cada variable
    partes = []

    desc_p = descripcion_variable("Potencia", desvios["potencia"], direcciones["potencia"])
    if desc_p:
        partes.append(desc_p)

    desc_i = descripcion_variable("Corriente", desvios["corriente"], direcciones["corriente"])
    if desc_i:
        partes.append(desc_i)

    desc_fp = descripcion_variable("Factor de potencia", desvios["factor_potencia"], direcciones["factor_potencia"])
    if desc_fp:
        partes.append(desc_fp)

    # Si ninguna variable está fuera de rango pero el sistema detectó anomalía
    # (por Isolation Forest), significa que hay un patrón multivariable anómalo
    if not partes:
        return "Patrón multivariable anómalo detectado por análisis global"

    return " | ".join(partes)

# -----------------------------
# GUARDADO DE ALERTAS (IDÉNTICA AL ORIGINAL)
# -----------------------------

def guardar_alerta(write_api, timestamp, punto, evaluacion, severidad, desvio_maximo, firma, id_modelo, equipo_id):
    """Guarda una alerta confirmada en InfluxDB con la información completa."""
    point = (
        Point("alertas")
        .tag("severidad", severidad)
        .tag("id_modelo", str(id_modelo))
        .tag("equipo_id", equipo_id)
        .field("firma", firma)
        .field("potencia_kw", float(punto["potencia_kw"]))
        .field("corriente_a", float(punto["corriente_a"]))
        .field("factor_potencia", float(punto["factor_potencia"]))
        .field("cluster_id", int(evaluacion["estado_operativo"]))
        .field("score_iforest", float(evaluacion["score_iforest"]))
        .field("desvio_maximo", float(desvio_maximo))
        .field("desvio_potencia", float(evaluacion["desvios"]["potencia"]))
        .field("desvio_corriente", float(evaluacion["desvios"]["corriente"]))
        .field("desvio_fp", float(evaluacion["desvios"]["factor_potencia"]))
        .time(timestamp, WritePrecision.NS)
    )
    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)

# -----------------------------
# CONSULTAR RECONOCIMIENTOS (IDÉNTICA AL ORIGINAL)
# -----------------------------

def alerta_fue_reconocida(timestamp_alerta):
    """
    Consulta PostgreSQL para saber si una alerta específica ya fue reconocida.
    Se usa para que el monitor sepa cuándo dejar de estar "en alerta"
    porque el operador reconoció desde la interfaz.
    """
    if timestamp_alerta is None:
        return False
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM reconocimientos_alertas
            WHERE timestamp_alerta = %s
            LIMIT 1
        """, (timestamp_alerta,))
        resultado = cur.fetchone()
        cur.close()
        conn.close()
        return resultado is not None
    except Exception as e:
        print(f"Error consultando reconocimiento: {e}")
        return False

# -----------------------------
# PROCESAMIENTO DE UN EQUIPO EN UNA ITERACIÓN
# -----------------------------

def procesar_iteracion_equipo(monitor, influx_client, write_api, sesion_actual):
    """
    Procesa una iteración del loop para un equipo dado.
    Encapsula toda la lógica que en el original estaba dentro del `while True`,
    pero por equipo (usando su MonitorEquipo).
    """
    equipo_id = monitor.equipo_id

    # Detectar cambios de estado para informar al usuario
    if sesion_actual and not monitor.sesion_anterior:
        print(f"[{equipo_id}] ▶️  Sesión de monitoreo iniciada. Procesando datos...")
        monitor.estado = EstadoMonitor()  # reiniciar estado al iniciar sesión nueva
        monitor.ultimo_timestamp = None   # empezar desde datos actuales
    elif not sesion_actual and monitor.sesion_anterior:
        print(f"[{equipo_id}] ⏸️  Sesión de monitoreo finalizada. Esperando nueva sesión...")

    monitor.sesion_anterior = sesion_actual

    if not sesion_actual:
        return  # no procesamos nada este equipo esta iteración

    # Verificar si la alerta activa fue reconocida por el operador
    if monitor.estado.alerta_activa:
        if alerta_fue_reconocida(monitor.estado.timestamp_alerta_activa):
            monitor.estado.reconocer_alerta(equipo_id)

    datos, nuevo_ultimo = leer_nuevos_datos(influx_client, equipo_id, monitor.ultimo_timestamp)
    monitor.ultimo_timestamp = nuevo_ultimo

    if datos.empty:
        return

    for _, fila in datos.iterrows():
        timestamp = fila["tiempo"]

        if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        punto = {
            "potencia_kw": fila["potencia_kw"],
            "corriente_a": fila["corriente_a"],
            "factor_potencia": fila["factor_potencia"],
        }

        evaluacion = evaluar_punto(
            punto, monitor.kmeans, monitor.isolation_forest,
            monitor.bandas, monitor.umbral_iforest
        )
        monitor.estado.registrar_evaluacion(evaluacion, timestamp)

        # Verificar si corresponde generar una alerta confirmada
        alerta_generada = False
        if monitor.estado.debe_confirmar_alerta():
            severidad, desvio_maximo = clasificar_severidad(evaluacion["desvios"])
            firma = identificar_firma(evaluacion["desvios"], evaluacion["direcciones"])

            monitor.estado.registrar_alerta(timestamp)
            monitor.alertas_totales += 1

            evaluacion["estado_operativo"] = monitor.mapeo_estados[evaluacion['cluster_id']]

            guardar_alerta(
                write_api, timestamp, punto,
                evaluacion, severidad, desvio_maximo, firma,
                monitor.id_modelo, equipo_id
            )

            estado_operativo = monitor.mapeo_estados[evaluacion['cluster_id']]
            print(f"[{equipo_id}] ⚠️  {severidad}: {firma}")
            print(f"[{equipo_id}]    [Estado #{estado_operativo}] "
                  f"P={punto['potencia_kw']:.3f} kW (Δ={evaluacion['desvios']['potencia']:.1f}σ) | "
                  f"I={punto['corriente_a']:.3f} A (Δ={evaluacion['desvios']['corriente']:.1f}σ) | "
                  f"FP={punto['factor_potencia']:.3f} (Δ={evaluacion['desvios']['factor_potencia']:.1f}σ)")
            alerta_generada = True

        # Mensaje de estado normal
        if not alerta_generada:
            estado_operativo = monitor.mapeo_estados[evaluacion['cluster_id']]

            # Info detallada de detección
            b = "B✓" if evaluacion["anomalia_banda"] else "B·"
            f = "F✓" if evaluacion["anomalia_iforest"] else "F·"
            dp = evaluacion["desvios"]["potencia"]
            di = evaluacion["desvios"]["corriente"]
            dfp = evaluacion["desvios"]["factor_potencia"]
            max_desvio = max(dp, di, dfp)

            n_ventana = sum(monitor.estado.ventana_confirmacion)
            total_ventana = len(monitor.estado.ventana_confirmacion)
            vent_str = f"V[{n_ventana}/{total_ventana}]"

            det_str = (
                f"[E#{estado_operativo}] "
                f"P={punto['potencia_kw']:.3f}(Δ{dp:.1f}σ) | "
                f"I={punto['corriente_a']:.3f}(Δ{di:.1f}σ) | "
                f"FP={punto['factor_potencia']:.3f}(Δ{dfp:.1f}σ) | "
                f"IF={evaluacion['score_iforest']:.4f} | "
                f"{b}{f} | maxΔ={max_desvio:.1f}σ | {vent_str}"
            )

            if monitor.estado.en_transicion(timestamp):
                print(f"[{equipo_id}] 🔄 TRANS  | {det_str}")
            elif monitor.estado.alerta_activa:
                print(f"[{equipo_id}] 🟡 ALERTA_ACTIVA (esperando reconocimiento) | {det_str}")
            else:
                if evaluacion["anomalia_banda"] or evaluacion["anomalia_iforest"]:
                    print(f"[{equipo_id}] ⚠️  ANOMALA | {det_str}")
                else:
                    print(f"[{equipo_id}] ✅ Normal  | {det_str}")

# -----------------------------
# LOOP PRINCIPAL
# -----------------------------

def imprimir_arranque(monitores, sesiones_activas):
    print()
    print("=" * 60)
    print("MONITOR MULTI-EQUIPO - Iniciando")
    print("=" * 60)

    print("\nModelos activos cargados:")
    if monitores:
        for equipo_id, m in monitores.items():
            print(f"  - {equipo_id} (modelo #{m.id_modelo}, K={m.kmeans.n_clusters}, "
                  f"umbral IF={m.umbral_iforest:.3f})")
    else:
        print("  (ninguno)")

    print("\nEquipos con monitoreo activo al arrancar:")
    activos_con_modelo = [e for e in sesiones_activas if e in monitores]
    if activos_con_modelo:
        for equipo_id in activos_con_modelo:
            print(f"  - {equipo_id}")
    else:
        print("  (ninguno)")

    equipos_esperando = [e for e in monitores if e not in sesiones_activas]
    if equipos_esperando:
        print("\nEquipos con modelo pero sin monitoreo (esperando):")
        for equipo_id in equipos_esperando:
            print(f"  - {equipo_id}")

    sesiones_sin_modelo = [e for e in sesiones_activas if e not in monitores]
    if sesiones_sin_modelo:
        print("\n[ADVERTENCIA] Sesiones activas SIN modelo activo (se ignoran):")
        for equipo_id in sesiones_sin_modelo:
            print(f"  - {equipo_id}")

    print()
    print(f"  Umbral bandas: {DESVIOS_UMBRAL_BANDAS} desvíos estándar")
    print(f"  Ventana confirmación: {VENTANA_CONFIRMACION} muestras")
    print(f"  Umbral confirmación: {UMBRAL_CONFIRMACION}/{VENTANA_CONFIRMACION}")
    print(f"  Supresión transición: {SUPRESION_TRANSICION}s")
    print(f"\nIterando cada {INTERVALO_SEGUNDOS} segundos. Presioná Ctrl+C para detener.")
    print("=" * 60)
    print()


def main():
    print("Iniciando módulo de monitoreo multi-equipo...")

    # Cargar todos los modelos activos al arrancar
    monitores = cargar_todos_los_modelos_activos()

    sesiones_iniciales = equipos_con_sesion_activa()
    imprimir_arranque(monitores, sesiones_iniciales)

    influx_client = InfluxDBClient(
        url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)

    try:
        while True:
            try:
                equipos_activos = set(equipos_con_sesion_activa())

                # Detectar equipos con sesión nueva cuyo modelo aún no está cargado
                # (lazy load: cuando el usuario entrena y activa un modelo, y luego
                #  inicia sesión de monitoreo desde la UI, sin reiniciar el monitor)
                equipos_nuevos = equipos_activos - set(monitores.keys())
                for equipo_id in equipos_nuevos:
                    try:
                        monitor = cargar_modelo_de_equipo(equipo_id)
                        if monitor:
                            monitores[equipo_id] = monitor
                            print(f"[{equipo_id}] 🔵 Modelo cargado on-demand (nueva sesión detectada)")
                        else:
                            print(f"[{equipo_id}] ⚠️  Sesión activa pero sin modelo activo. Se ignora.")
                    except Exception as e:
                        print(f"[{equipo_id}] ❌ Error cargando modelo on-demand: {e}")

                # Procesar cada equipo con modelo cargado (esté o no en sesión activa)
                # Los que no están en sesión activa solo actualizan `sesion_anterior`.
                for equipo_id, monitor in monitores.items():
                    sesion_actual = equipo_id in equipos_activos
                    try:
                        procesar_iteracion_equipo(monitor, influx_client, write_api, sesion_actual)
                    except Exception as e:
                        print(f"[{equipo_id}] ❌ Error procesando iteración: {e}")

            except Exception as e:
                print(f"[ERROR GENERAL] {e}")

            time.sleep(INTERVALO_SEGUNDOS)

    except KeyboardInterrupt:
        total = sum(m.alertas_totales for m in monitores.values())
        print(f"\n\nMonitor detenido. Alertas generadas totales: {total}")
        for equipo_id, m in monitores.items():
            if m.alertas_totales > 0:
                print(f"  {equipo_id}: {m.alertas_totales} alertas")

    finally:
        write_api.close()
        influx_client.close()


if __name__ == "__main__":
    main()