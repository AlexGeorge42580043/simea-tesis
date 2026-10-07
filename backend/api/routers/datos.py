from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime, timedelta, timezone

from auth import usuario_actual
from database import get_pg_cursor, get_influx_client
from config import INFLUX_BUCKET, INFLUX_ORG

router = APIRouter(
    prefix="/api/equipos/{equipo_id}",
    tags=["Datos"]
)

def validar_equipo(equipo_id: str):
    """Valida que el equipo exista y esté activo. Devuelve sus datos."""
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, nombre, tipo_operacion, descripcion, activo
            FROM equipos
            WHERE id = %s
        """, (equipo_id,))
        equipo = cur.fetchone()
        if not equipo:
            raise HTTPException(
                status_code=404,
                detail=f"Equipo '{equipo_id}' no encontrado"
            )
        if not equipo["activo"]:
            raise HTTPException(
                status_code=400,
                detail=f"Equipo '{equipo_id}' está dado de baja"
            )
        return dict(equipo)

# -----------------------------
# DATOS EN TIEMPO REAL
# -----------------------------

@router.get("/datos/actuales")
async def obtener_datos_actuales(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Devuelve la última medición disponible del equipo:
    potencia activa, corriente y factor de potencia con su timestamp.
    Filtra por equipo_id para que cada equipo vea únicamente sus propios datos.
    """
    validar_equipo(equipo_id)

    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
        |> range(start: -1m)
        |> filter(fn: (r) => r["_measurement"] == "mediciones")
        |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
        |> filter(fn: (r) => r["_field"] == "potencia_kw" or r["_field"] == "corriente_a" or r["_field"] == "factor_potencia")
        |> last()
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    client = get_influx_client()
    resultado = client.query_api().query_data_frame(query)

    if isinstance(resultado, list):
        if len(resultado) == 0:
            raise HTTPException(status_code=404, detail="No hay datos recientes")
        import pandas as pd
        resultado = pd.concat(resultado)

    if resultado.empty:
        raise HTTPException(status_code=404, detail="No hay datos recientes")

    fila = resultado.iloc[-1]
    return {
        "timestamp": fila["_time"].isoformat(),
        "potencia_kw": round(float(fila["potencia_kw"]), 4),
        "corriente_a": round(float(fila["corriente_a"]), 4),
        "factor_potencia": round(float(fila["factor_potencia"]), 4),
    }

@router.get("/datos/serie")
async def obtener_serie_temporal(
    equipo_id: str,
    minutos: int = Query(default=15, ge=1, le=10080),
    agregacion: str = Query(
        default=None,
        description="Ventana de agregación (ej: '10s', '1m', '10m'). Si se omite, no agrega."
    ),
    user: dict = Depends(usuario_actual)
):
    """
    Devuelve la serie temporal de potencia, corriente y factor de potencia
    para los últimos N minutos. Si se especifica 'agregacion', aplica un
    promedio por ventanas de esa duración (downsampling).
    Filtra por equipo_id para que cada equipo vea únicamente sus propios datos.
    """
    validar_equipo(equipo_id)

    # Si se pide agregación, se aplica aggregateWindow con función mean
    agregacion_flux = ""
    if agregacion:
        agregacion_flux = f'|> aggregateWindow(every: {agregacion}, fn: mean, createEmpty: false)'

    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
        |> range(start: -{minutos}m)
        |> filter(fn: (r) => r["_measurement"] == "mediciones")
        |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
        |> filter(fn: (r) => r["_field"] == "potencia_kw" or r["_field"] == "corriente_a" or r["_field"] == "factor_potencia")
        {agregacion_flux}
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> sort(columns: ["_time"])
    '''

    client = get_influx_client()
    resultado = client.query_api().query_data_frame(query)

    if isinstance(resultado, list):
        if len(resultado) == 0:
            return {"datos": []}
        import pandas as pd
        resultado = pd.concat(resultado)

    if resultado.empty:
        return {"datos": []}

    datos = []
    for _, fila in resultado.iterrows():
        datos.append({
            "timestamp": fila["_time"].isoformat(),
            "potencia_kw": round(float(fila["potencia_kw"]), 4),
            "corriente_a": round(float(fila["corriente_a"]), 4),
            "factor_potencia": round(float(fila["factor_potencia"]), 4),
        })

    return {"datos": datos}

# -----------------------------
# MODELO Y PATRÓN APRENDIDO
# -----------------------------

@router.get("/modelo")
async def obtener_modelo_activo(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Devuelve la metadata del modelo activo para el equipo:
    K óptimo, fechas, bandas de tolerancia por cluster.
    """
    validar_equipo(equipo_id)

    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id, k_optimo, fecha_entrenamiento,
                   fecha_inicio_datos, fecha_fin_datos,
                   umbral_iforest, es_ciclico
            FROM modelos
            WHERE estado = 'activo' AND equipo_id = %s
            ORDER BY fecha_entrenamiento DESC
            LIMIT 1
        """, (equipo_id,))

        modelo = cur.fetchone()
        if not modelo:
            raise HTTPException(
                status_code=404,
                detail="No hay modelo activo para este equipo"
            )

        cur.execute("""
            SELECT cluster_id, potencia_media, potencia_desvio,
                   corriente_media, corriente_desvio,
                   fp_media, fp_desvio, cantidad_muestras
            FROM bandas_tolerancia
            WHERE id_modelo = %s
            ORDER BY cluster_id
        """, (modelo["id"],))
        bandas = cur.fetchall()

    return {
        "id_modelo": modelo["id"],
        "k_optimo": modelo["k_optimo"],
        "fecha_entrenamiento": modelo["fecha_entrenamiento"].isoformat(),
        "fecha_inicio_datos": modelo["fecha_inicio_datos"].isoformat(),
        "fecha_fin_datos": modelo["fecha_fin_datos"].isoformat(),
        "umbral_iforest": float(modelo["umbral_iforest"]) if modelo["umbral_iforest"] else None,
        "es_ciclico": modelo["es_ciclico"],
        "bandas_tolerancia": [
            {
                "cluster_id": b["cluster_id"],
                "potencia_media": round(float(b["potencia_media"]), 4),
                "potencia_desvio": round(float(b["potencia_desvio"]), 4),
                "corriente_media": round(float(b["corriente_media"]), 4),
                "corriente_desvio": round(float(b["corriente_desvio"]), 4),
                "fp_media": round(float(b["fp_media"]), 4) if b["fp_media"] is not None else None,
                "fp_desvio": round(float(b["fp_desvio"]), 4) if b["fp_desvio"] is not None else None,
                "cantidad_muestras": b["cantidad_muestras"],
            }
            for b in bandas
        ]
    }

@router.get("/patron")
async def obtener_curva_patron(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Devuelve la curva patrón del ciclo de trabajo del equipo:
    200 puntos de potencia y corriente media con sus bandas de variabilidad.
    """
    validar_equipo(equipo_id)

    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT cp.tiempo_relativo,
                   cp.potencia_media, cp.potencia_desvio,
                   cp.corriente_media, cp.corriente_desvio,
                   cp.fp_media, cp.fp_desvio
            FROM curva_patron cp
            JOIN modelos m ON cp.id_modelo = m.id
            WHERE m.estado = 'activo' AND m.equipo_id = %s
            ORDER BY cp.tiempo_relativo
        """, (equipo_id,))
        puntos = cur.fetchall()

    if not puntos:
        raise HTTPException(
            status_code=404,
            detail="No hay curva patrón disponible para este equipo"
        )

    return {
        "puntos": [
            {
                "tiempo_relativo": round(float(p["tiempo_relativo"]), 4),
                "potencia_media": round(float(p["potencia_media"]), 4),
                "potencia_desvio": round(float(p["potencia_desvio"]), 4),
                "corriente_media": round(float(p["corriente_media"]), 4),
                "corriente_desvio": round(float(p["corriente_desvio"]), 4),
                "fp_media": round(float(p["fp_media"]), 4) if p["fp_media"] is not None else None,
                "fp_desvio": round(float(p["fp_desvio"]), 4) if p["fp_desvio"] is not None else None,
            }
            for p in puntos
        ]
    }