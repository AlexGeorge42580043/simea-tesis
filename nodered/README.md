# Flujo de preprocesamiento Node-RED

Esta carpeta contiene el flujo de preprocesamiento implementado en Node-RED, responsable de recibir las mediciones vía MQTT, aplicar un filtro de promedio móvil sobre una ventana de cinco muestras, preservar el identificador de equipo como etiqueta y persistir las mediciones en InfluxDB.

## Archivos esperados

- `flows.json`: exportación del flujo completo de Node-RED, listo para importarse en una instancia Node-RED limpia.

## Cómo exportar el flujo desde Node-RED

1. Acceder a la interfaz de Node-RED en el navegador (`http://localhost:1880` por defecto).
2. Abrir el menú superior derecho (ícono de hamburguesa).
3. Seleccionar `Export` → `All flows` → `Clipboard` → `JSON`.
4. Copiar el contenido JSON y guardarlo como `flows.json` en esta carpeta.

## Cómo importar el flujo en una instancia nueva

1. Acceder a la interfaz de Node-RED.
2. Menú superior derecho → `Import` → `select a file to import`.
3. Seleccionar el archivo `flows.json` de esta carpeta.
4. `Deploy` para activar el flujo.
