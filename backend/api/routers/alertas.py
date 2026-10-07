from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional

from auth import usuario_actual
from database import get_pg_cursor, get_influx_client
from config import INFLUX_BUCKET, INFLUX_ORG

router = APIRouter(
    prefix="/api/equipos/{equipo_id}/alertas",
    tags=["Alertas"]
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
# MODELOS DE DATOS
# -----------------------------

class ReconocimientoAlerta(BaseModel):
    """Datos enviados cuando el operador reconoce una alerta."""
    descripcion: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Hipótesis o descripción del operador sobre la causa de la alerta"
    )

# -----------------------------
# CONSULTAR ALERTAS
# -----------------------------

@router.get("")
async def listar_alertas(
    equipo_id: str,
    estado: Optional[str] = Query(
        default=None,
        description="Filtro: 'pendiente' o 'reconocida'. Si no se indica, devuelve todas."
    ),
    minutos: Optional[int] = Query(
        default=None,
        ge=1,
        le=43200,
        description="Filtro opcional: alertas de los últimos N minutos. Si se omite, se devuelve todo el histórico."
    ),
    user: dict = Depends(usuario_actual)
):
    """
    Lista las alertas del equipo. Combina datos de InfluxDB (alertas detectadas)
    con datos de PostgreSQL (reconocimientos del operador).
    Por defecto, sin filtro de tiempo, devuelve todo el histórico existente en la base.
    """
    validar_equipo(equipo_id)

    # 1. Construir el rango de la query Flux
    # Si no se especifica 'minutos', se consulta todo el histórico (start: 0).
    if minutos is None:
        rango_flux = "start: 0"
    else:
        rango_flux = f"start: -{minutos}m"

    # 2. Traer las alertas detectadas desde InfluxDB
    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
        |> range({rango_flux})
        |> filter(fn: (r) => r["_measurement"] == "alertas")
        |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> sort(columns: ["_time"], desc: true)
    '''

    client = get_influx_client()
    resultado = client.query_api().query_data_frame(query)

    if isinstance(resultado, list):
        if len(resultado) == 0:
            return {"alertas": []}
        import pandas as pd
        resultado = pd.concat(resultado)

    if resultado.empty:
        return {"alertas": []}

    # 3. Traer los reconocimientos desde PostgreSQL
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT timestamp_alerta, descripcion, usuario,
                   fecha_reconocimiento
            FROM reconocimientos_alertas
        """)
        reconocimientos = {
            r["timestamp_alerta"].isoformat(): r
            for r in cur.fetchall()
        }

    # 4. Combinar ambos datasets
    alertas = []
    for _, fila in resultado.iterrows():
        ts = fila["_time"].isoformat()
        reconocimiento = reconocimientos.get(ts)

        alerta = {
            "timestamp": ts,
            "severidad": fila.get("severidad", "DESCONOCIDA"),
            "firma": fila.get("firma", ""),
            "potencia_kw": round(float(fila.get("potencia_kw", 0)), 4),
            "corriente_a": round(float(fila.get("corriente_a", 0)), 4),
            "factor_potencia": round(float(fila.get("factor_potencia", 0)), 4),
            "cluster_id": int(fila.get("cluster_id", -1)),
            "score_iforest": round(float(fila.get("score_iforest", 0)), 4),
            "desvio_maximo": round(float(fila.get("desvio_maximo", 0)), 2),
            "desvio_potencia": round(float(fila.get("desvio_potencia", 0)), 2),
            "desvio_corriente": round(float(fila.get("desvio_corriente", 0)), 2),
            "desvio_fp": round(float(fila.get("desvio_fp", 0)), 2),
            "reconocida": reconocimiento is not None,
            "reconocimiento": None
        }

        if reconocimiento:
            alerta["reconocimiento"] = {
                "descripcion": reconocimiento["descripcion"],
                "usuario": reconocimiento["usuario"],
                "fecha": reconocimiento["fecha_reconocimiento"].isoformat()
            }

        # Aplicar filtro de estado si se solicita
        if estado == "pendiente" and alerta["reconocida"]:
            continue
        if estado == "reconocida" and not alerta["reconocida"]:
            continue

        alertas.append(alerta)

    return {"alertas": alertas}

# -----------------------------
# RECONOCER UNA ALERTA
# -----------------------------

@router.post("/reconocer")
async def reconocer_alerta(
    equipo_id: str,
    timestamp: str = Query(
        ...,
        description="Timestamp ISO 8601 de la alerta a reconocer"
    ),
    datos: ReconocimientoAlerta = ...,
    user: dict = Depends(usuario_actual)
):
    """
    Permite al operador reconocer una alerta y dejar registrada una descripción
    o hipótesis sobre su causa. Queda asociada al usuario y a la fecha actual.
    """
    validar_equipo(equipo_id)

    # Validar el formato del timestamp
    try:
        ts_alerta = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Formato de timestamp inválido. Usar ISO 8601."
        )

    # Verificar que no esté reconocida previamente
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id FROM reconocimientos_alertas
            WHERE timestamp_alerta = %s
        """, (ts_alerta,))

        if cur.fetchone():
            raise HTTPException(
                status_code=409,
                detail="Esta alerta ya fue reconocida"
            )

        # Insertar el reconocimiento
        cur.execute("""
            INSERT INTO reconocimientos_alertas (
                timestamp_alerta, descripcion, usuario, fecha_reconocimiento
            ) VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (
            ts_alerta,
            datos.descripcion,
            user["usuario"],
            datetime.now(timezone.utc)
        ))

        id_reconocimiento = cur.fetchone()["id"]

    return {
        "id_reconocimiento": id_reconocimiento,
        "mensaje": "Alerta reconocida exitosamente",
        "usuario": user["nombre"],
        "fecha": datetime.now(timezone.utc).isoformat()
    }

# -----------------------------
# ARCHIVAR ALERTAS RECONOCIDAS
# -----------------------------

@router.post("/archivar")
async def archivar_alertas_reconocidas(
    equipo_id: str,
    user: dict = Depends(usuario_actual)
):
    """
    Archiva todas las alertas reconocidas del equipo. Devuelve la lista
    completa de alertas archivadas para que la interfaz genere el archivo CSV
    a descargar, y elimina los datos de InfluxDB y PostgreSQL.
    """
    validar_equipo(equipo_id)

    # 1. Traer todas las alertas reconocidas (todo el histórico)
    query = f'''
    from(bucket: "{INFLUX_BUCKET}")
        |> range(start: 0)
        |> filter(fn: (r) => r["_measurement"] == "alertas")
        |> filter(fn: (r) => r["equipo_id"] == "{equipo_id}")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    client = get_influx_client()
    resultado = client.query_api().query_data_frame(query)

    if isinstance(resultado, list):
        if len(resultado) == 0:
            return {"archivadas": [], "cantidad": 0}
        import pandas as pd
        resultado = pd.concat(resultado)

    if resultado.empty:
        return {"archivadas": [], "cantidad": 0}

    # 2. Traer reconocimientos
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT timestamp_alerta, descripcion, usuario, fecha_reconocimiento
            FROM reconocimientos_alertas
        """)
        reconocimientos = {
            r["timestamp_alerta"].isoformat(): r
            for r in cur.fetchall()
        }

    # 3. Filtrar solo las reconocidas y armar la lista para el CSV
    archivadas = []
    timestamps_a_borrar = []

    for _, fila in resultado.iterrows():
        ts = fila["_time"].isoformat()
        rec = reconocimientos.get(ts)
        if not rec:
            continue

        archivadas.append({
            "timestamp": ts,
            "severidad": fila.get("severidad", "DESCONOCIDA"),
            "firma": fila.get("firma", ""),
            "potencia_kw": round(float(fila.get("potencia_kw", 0)), 4),
            "corriente_a": round(float(fila.get("corriente_a", 0)), 4),
            "factor_potencia": round(float(fila.get("factor_potencia", 0)), 4),
            "cluster_id": int(fila.get("cluster_id", -1)),
            "score_iforest": round(float(fila.get("score_iforest", 0)), 4),
            "desvio_maximo": round(float(fila.get("desvio_maximo", 0)), 2),
            "desvio_potencia": round(float(fila.get("desvio_potencia", 0)), 2),
            "desvio_corriente": round(float(fila.get("desvio_corriente", 0)), 2),
            "desvio_fp": round(float(fila.get("desvio_fp", 0)), 2),
            "descripcion_reconocimiento": rec["descripcion"],
            "usuario_reconocimiento": rec["usuario"],
            "fecha_reconocimiento": rec["fecha_reconocimiento"].isoformat(),
        })
        timestamps_a_borrar.append(fila["_time"])

    # 4. Borrar de InfluxDB las alertas reconocidas
    if timestamps_a_borrar:
        delete_api = client.delete_api()
        for ts in timestamps_a_borrar:
            inicio = ts.isoformat()
            # Sumamos 1 segundo para el rango de borrado
            from datetime import timedelta
            fin = (ts + timedelta(seconds=1)).isoformat()
            delete_api.delete(
                start=inicio,
                stop=fin,
                predicate=f'_measurement="alertas" AND equipo_id="{equipo_id}"',
                bucket=INFLUX_BUCKET,
                org=INFLUX_ORG
            )

    # 5. Borrar reconocimientos de PostgreSQL
    if archivadas:
        with get_pg_cursor() as cur:
            timestamps_iso = [a["timestamp"] for a in archivadas]
            cur.execute("""
                DELETE FROM reconocimientos_alertas
                WHERE timestamp_alerta::text = ANY(%s)
            """, (timestamps_iso,))

    return {
        "archivadas": archivadas,
        "cantidad": len(archivadas),
        "usuario": user["nombre"],
        "fecha_archivado": datetime.now(timezone.utc).isoformat()
    }

# -----------------------------
# ELIMINAR UNA ALERTA INDIVIDUAL
# -----------------------------

@router.delete("/{timestamp}")
async def eliminar_alerta(
    equipo_id: str,
    timestamp: str,
    user: dict = Depends(usuario_actual)
):
    """
    Elimina una alerta individual. La alerta debe estar reconocida previamente.
    """
    validar_equipo(equipo_id)

    try:
        ts_alerta = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Formato de timestamp inválido"
        )

    # Verificar que la alerta esté reconocida
    with get_pg_cursor() as cur:
        cur.execute("""
            SELECT id FROM reconocimientos_alertas
            WHERE timestamp_alerta = %s
        """, (ts_alerta,))

        if not cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail="Solo se pueden eliminar alertas reconocidas"
            )

        # Borrar reconocimiento
        cur.execute("""
            DELETE FROM reconocimientos_alertas
            WHERE timestamp_alerta = %s
        """, (ts_alerta,))

    # Borrar la alerta de InfluxDB
    client = get_influx_client()
    delete_api = client.delete_api()
    from datetime import timedelta
    inicio = ts_alerta.isoformat()
    fin = (ts_alerta + timedelta(seconds=1)).isoformat()
    delete_api.delete(
        start=inicio,
        stop=fin,
        predicate=f'_measurement="alertas" AND equipo_id="{equipo_id}"',
        bucket=INFLUX_BUCKET,
        org=INFLUX_ORG
    )

    return {
        "mensaje": "Alerta eliminada",
        "timestamp": timestamp,
        "usuario": user["nombre"]
    }