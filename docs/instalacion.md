# Guía de instalación

## Requisitos previos

- Docker Desktop (Windows / macOS) o Docker Engine + Docker Compose (Linux).
- git.
- Puertos disponibles: 1883 (MQTT), 8086 (InfluxDB), 5432 (PostgreSQL), 1880 (Node-RED), 8000 (API).
- Python 3.10 o superior (requerido por el uso de union types
  `dict | None` en el backend).

## Pasos

1. Clonar el repositorio:

   ```bash
   git clone https://github.com/<usuario>/simea-tesis.git
   cd simea-tesis
   ```

2. Copiar el archivo de variables de entorno:

   ```bash
   cp .env.example .env
   ```

3. Editar `.env` con las contraseñas y tokens definitivos. Asegurate de generar valores seguros para `POSTGRES_PASSWORD`, `INFLUXDB_PASSWORD`, `INFLUXDB_TOKEN` y `JWT_SECRET`.

4. Levantar todos los servicios:

   ```bash
   docker-compose up -d
   ```

5. Verificar que los servicios estén corriendo:

   ```bash
   docker-compose ps
   ```

6. Verificar que la API responde abriendo `http://localhost:8000`
   en el navegador (devuelve un JSON con el estado de la API).
   La interfaz web se abre a continuación desde el archivo local
   `frontend/simea.html`.

7. Importar el flujo de Node-RED. En una instalación nueva
   Node-RED arranca con flujos vacíos y es necesario importar
   `nodered/flows.json` una sola vez:

   a. Abrir `http://localhost:1880` en el navegador.
   b. Menú superior derecho → `Import` → `select a file to import`.
   c. Elegir `nodered/flows.json` del repositorio.
   d. Confirmar con `Deploy`.

   Node-RED persiste el flujo en el volumen `nodered_data`, así
   que este paso no se repite en arranques posteriores.

## Primeros pasos

Al levantar los servicios por primera vez, el backend crea
automáticamente la tabla `usuarios` en PostgreSQL e inserta el
supervisor inicial con las credenciales definidas en `.env`
(`ADMIN_USERNAME` y `ADMIN_PASSWORD`). Si la tabla ya existe con
al menos un usuario activo, este paso se omite.

### 1. Abrir la interfaz y autenticarse

1. Abrir `frontend/simea.html` en el navegador local. La página
   apunta por defecto a `http://localhost:8000` para consultar la
   API; la API tiene CORS abierto, por lo que acepta solicitudes
   desde cualquier origen.
2. Iniciar sesión con las credenciales `ADMIN_USERNAME` y
   `ADMIN_PASSWORD` configuradas en `.env`.
3. Cambiar la contraseña del supervisor inicial desde la pantalla
   de gestión de usuarios antes de cualquier otra operación.

### 2. Dar de alta el primer equipo

Como supervisor, acceder a la gestión de equipos y crear uno
indicando:

- id: identificador único (se usa como etiqueta MQTT y recorre
  toda la cadena).
- nombre descriptivo.
- descripción.
- tiempo mínimo de aprendizaje.
- tiempo recomendado de aprendizaje.

El sistema no requiere especificar un tipo de equipo. La
ciclicidad del comportamiento se detecta automáticamente a partir
de las mediciones.

### 3. Ejecutar una sesión de aprendizaje

El flujo es manual y tiene cinco pasos:

1. En la pantalla de modos del equipo, iniciar el modo
   `aprendizaje`. Esto abre una sesión en PostgreSQL.

2. Arrancar el generador correspondiente al equipo. Los generadores
   están definidos con el perfil `aprendizaje` en Docker Compose,
   por lo que no arrancan con un `up` normal: hay que invocarlos
   explícitamente:

   ```bash
   docker-compose --profile aprendizaje up generador_cinta
   # o
   docker-compose --profile aprendizaje up generador_ventilador
   ```

3. Esperar a que el orquestador complete su ciclo (90 minutos para
   `cinta`, 45 minutos para `ventilador` por defecto, según la
   configuración del orquestador).

4. Finalizar la sesión de aprendizaje desde la interfaz. En ese
   momento, el backend dispara `analizador/entrenar.py` en segundo
   plano como subproceso. El modelo entrenado queda registrado en
   PostgreSQL con estado `inactivo`.

5. Desde la pantalla de modelos entrenados, activar el modelo
   recién creado. Puede haber solo un modelo activo por equipo.

### 4. Iniciar el modo monitoreo

Con un modelo activo para el equipo, iniciar el modo monitoreo
desde la pantalla de modos. El servicio `monitoreo` detecta la
sesión activa en su próxima iteración, carga el modelo on-demand
y comienza a procesar las mediciones de ese equipo. Las anomalías
confirmadas se publican como alertas en InfluxDB y aparecen en el
panel de alertas de la interfaz.

Durante el monitoreo, el sistema procesa cualquier medición que
llegue por MQTT para ese equipo y haya sido persistida por
Node-RED. En un entorno real los datos vienen del equipo físico;
para pruebas o demostraciones se pueden volver a lanzar los
generadores con el mismo perfil `aprendizaje`.

### Roles disponibles

El sistema reconoce dos roles de usuario:

- `supervisor`: puede dar de alta equipos, gestionar usuarios,
  activar modelos y controlar los modos de operación.
- `operador`: puede visualizar mediciones y alertas, pero no
  modificar la configuración del sistema.
