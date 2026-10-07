# SIMEA — Sistema Inteligente de Monitoreo de Equipos Automatizados

Sistema de monitoreo energético basado en IoT para la detección automatizada de anomalías en el consumo eléctrico de equipos industriales mediante técnicas de aprendizaje automático no supervisado.

> Proyecto Final de Tesis — Licenciatura en Automatización y Control de Procesos Industriales, Facultad de Ciencia y Tecnología, Universidad Autónoma de Entre Ríos (UADER FCyT), Concepción del Uruguay, Argentina.

## Descripción general

SIMEA monitorea de forma continua tres variables eléctricas —potencia activa, corriente y factor de potencia— sobre uno o más equipos industriales, aprende automáticamente su comportamiento normal mediante aprendizaje no supervisado (K-Means e Isolation Forest), y genera alertas clasificadas por severidad ante desviaciones significativas respecto al patrón aprendido.

El sistema fue diseñado bajo el principio de **aprendizaje sin conocimiento previo del equipo**: no requiere curvas técnicas del fabricante, valores nominales de placa ni suposiciones sobre el tipo de equipo. Toda la caracterización del comportamiento normal se construye a partir de la observación estadística durante una sesión de aprendizaje supervisada por el operador.

## Características principales

- Arquitectura multi-equipo con propagación de identificador a lo largo de toda la cadena de datos.
- Estrategia de detección multi-etapa que combina bandas de tolerancia estadísticas (K-Means) con detección multivariable (Isolation Forest).
- Clasificación automática de ciclicidad (equipos cíclicos vs continuos) sin configuración manual.
- Supresión de transitorios entre estados operativos para evitar falsos positivos.
- Confirmación de alertas por ventana temporal.
- Severidad en tres niveles (aviso, alerta, crítica) con firma descriptiva objetiva.
- Interfaz web con roles diferenciados (supervisor, operador), panel de monitoreo en tiempo real, gestión de modelos entrenados y trazabilidad de alarmas.

## Stack tecnológico

- **Backend:** Python 3.x, FastAPI, JWT para autenticación.
- **Frontend:** React (archivo HTML autocontenido).
- **Mensajería IoT:** MQTT (Eclipse Mosquitto).
- **Preprocesamiento:** Node-RED.
- **Base de datos temporal:** InfluxDB (mediciones y alertas).
- **Base de datos relacional:** PostgreSQL (metadatos y configuración).
- **Aprendizaje automático:** scikit-learn (K-Means e Isolation Forest).
- **Despliegue:** Docker + Docker Compose.

## Arquitectura

Ver [docs/arquitectura.md](docs/arquitectura.md) para un detalle de la arquitectura por capas del sistema.

## Instalación

Ver [docs/instalacion.md](docs/instalacion.md) para instrucciones paso a paso.

Resumen rápido:

```bash
git clone https://github.com/<usuario>/simea-tesis.git
cd simea-tesis
cp .env.example .env
# Editar .env con las credenciales deseadas
docker-compose up -d
```

## Uso

Ver [docs/manual-uso.md](docs/manual-uso.md) para una guía de uso básica.

## Resultados principales de la validación

El sistema fue validado empíricamente sobre dos equipos con comportamientos operativos radicalmente distintos (una cinta transportadora cíclica y un ventilador axial continuo), ambos accionados por motor asíncrono trifásico WEG W22 de 7,5 kW. Los principales resultados obtenidos:

| Métrica | Valor |
|---|---|
| Detección correcta (motor en marcha) | 96,3 % |
| Falsos negativos | 0 |
| Falsos positivos (línea base, 13,8 min) | 0 |
| Aislamiento multi-equipo verificado | Sí |
| Clasificación automática de ciclicidad | Correcta en ambos equipos |

Los datos crudos de todas las pruebas se encuentran en la carpeta [`datos/`](datos/).

## Estructura del repositorio

```
simea-tesis/
├── .env.example         Plantilla de variables de entorno
├── backend/             Servicio de aplicación (FastAPI + analizador)
├── datos/               CSVs de validación empírica
├── docker/              Configuración de servicios Docker (Mosquitto)
├── docs/                Documentación técnica
├── frontend/            Interfaz web (archivo HTML único)
├── generador/           Generador de datos simulados (motor WEG + anomalías)
├── nodered/             Flujo de preprocesamiento Node-RED (flows.json)
└── docker-compose.yml   Orquestación completa del stack
```

## Contexto académico

- **Autor:** George Alex
- **Director/Tutor:** Guillermo Sinise
- **Institución:** UADER FCyT, Concepción del Uruguay, Argentina
- **Carrera:** Licenciatura en Automatización y Control de Procesos Industriales
- **Año:** 2026

## Licencia

Este proyecto se distribuye bajo la licencia MIT. Ver el archivo [LICENSE](LICENSE) para los términos completos.
