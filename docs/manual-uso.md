# Manual de uso

Esta guía describe las operaciones básicas que un operador o un
supervisor realiza sobre el sistema SIMEA desde la interfaz web.

Si todavía no instalaste el sistema, consultá primero
[`instalacion.md`](instalacion.md); este manual asume que los
servicios están corriendo y que `frontend/simea.html` se abre
correctamente en el navegador.

## 1. Primer acceso y creación del usuario supervisor inicial

El usuario supervisor inicial se crea automáticamente la primera vez
que arranca el backend, usando las credenciales definidas en el
archivo `.env` (`ADMIN_USERNAME` y `ADMIN_PASSWORD`). Si la tabla de
usuarios ya tiene al menos un usuario activo, este paso se omite.

Para completar el primer acceso:

1. Abrir `frontend/simea.html` en el navegador.
2. Iniciar sesión con `ADMIN_USERNAME` y `ADMIN_PASSWORD`.
3. Ir a la gestión de usuarios y cambiar la contraseña del
   supervisor inicial por una contraseña propia antes de cualquier
   otra operación.

El sistema reconoce dos roles:

- `supervisor`: puede dar de alta equipos, gestionar usuarios,
  activar modelos y controlar los modos de operación.
- `operador`: puede visualizar mediciones y alertas, pero no
  modificar la configuración del sistema.

## 2. Alta de un equipo nuevo

Un equipo representa una pieza de maquinaria caracterizable por sus
tres variables eléctricas: potencia activa, corriente y factor de
potencia. Cada equipo tiene su propio flujo de datos, su propio
modelo entrenado y su propio conjunto de alertas.

El alta la realiza un supervisor desde la vista de configuración de
equipos, completando los siguientes campos:

- **id**: identificador único del equipo. Se usa como etiqueta MQTT
  en toda la cadena (generador → Mosquitto → Node-RED → InfluxDB →
  monitor), por lo que debe coincidir con el valor con el que el
  equipo publica sus mediciones.
- **nombre**: nombre descriptivo visible en la interfaz.
- **descripción**: texto libre con información adicional del equipo.
- **tiempo mínimo de aprendizaje**: duración mínima aceptada para
  una sesión de aprendizaje válida.
- **tiempo recomendado de aprendizaje**: duración sugerida que
  permite capturar todos los estados operativos esperables.

El sistema no requiere declarar el tipo de equipo (cíclico o no
cíclico). La ciclicidad se detecta automáticamente a partir de las
mediciones durante la fase de aprendizaje.

## 3. Alta de un usuario operador

Desde la gestión de usuarios, el supervisor puede crear nuevos
usuarios indicando:

- usuario (nombre de inicio de sesión, único).
- nombre descriptivo.
- contraseña provisional.
- rol: `supervisor` u `operador`.

Un usuario con rol `operador` puede iniciar y detener modos,
reconocer alertas y consultar el historial, pero no puede dar de
alta equipos ni gestionar otros usuarios. Las contraseñas se
almacenan hasheadas con `bcrypt`; el valor en claro nunca queda
registrado.

## 4. Ciclo de aprendizaje

El ciclo de aprendizaje enseña al sistema qué es el comportamiento
normal del equipo. Es un proceso manual con cuatro pasos.

### 4.1. Iniciar la sesión de aprendizaje

Desde la pantalla de modos del equipo, el supervisor inicia el modo
`aprendizaje`. En ese momento:

- Se abre una sesión en la tabla `sesiones_modo` con estado activo.
- Los datos que lleguen por MQTT para ese equipo quedan registrados
  en InfluxDB y, al finalizar la sesión, se usarán para entrenar
  el modelo.

### 4.2. Arrancar el generador correspondiente

Los generadores están definidos con el perfil `aprendizaje` en
Docker Compose, por lo que no arrancan automáticamente. Para
iniciarlos manualmente desde la raíz del repositorio:

```bash
docker-compose --profile aprendizaje up generador_cinta
# o
docker-compose --profile aprendizaje up generador_ventilador
```

Cada orquestador ejecuta un ciclo de duración predefinida (90
minutos para `cinta`, 45 minutos para `ventilador` por defecto) y
termina solo.

### 4.3. Seguir el recorrido de estados

Durante la sesión de aprendizaje, el panel principal muestra la
evolución de las tres variables en tiempo real. En equipos
cíclicos el recorrido de estados puede repetirse varias veces; en
equipos no cíclicos se mantienen bandas estables alrededor del
punto de operación. Esperar a que el generador complete al menos
un ciclo completo asegura que el modelo capture todos los estados.

### 4.4. Finalizar la sesión

Al terminar el ciclo, el supervisor finaliza la sesión desde la
misma pantalla. En ese momento, el backend:

1. Cierra la sesión en PostgreSQL.
2. Lanza en segundo plano el script
   `backend/analizador/entrenar.py` como subproceso, con el
   identificador del equipo como parámetro.
3. El script lee las mediciones de la ventana en InfluxDB, aplica
   K-Means para segmentar los estados operativos, calcula las
   bandas de tolerancia y entrena un modelo de Isolation Forest
   sobre los datos nominales.

Los archivos del modelo entrenado quedan persistidos en el volumen
`modelos_data`, y sus metadatos se registran en PostgreSQL con
estado inicial `inactivo`. Los logs del proceso de entrenamiento
quedan en el volumen `logs_entrenamiento_data`, útiles para
diagnóstico si el entrenamiento no termina correctamente.

## 5. Activación del modelo entrenado

Al terminar un entrenamiento, el modelo queda registrado con
estado `inactivo`. Para usarlo en monitoreo hay que activarlo
desde la pantalla de modelos entrenados.

El supervisor:

1. Accede a la pantalla de modelos entrenados.
2. Selecciona el modelo recién creado para el equipo correspondiente.
3. Lo activa. En ese momento su estado pasa a `activo`.

Un equipo solo puede tener un modelo activo por vez. Si ya había
otro modelo activo para el mismo equipo, se desactiva al activar
el nuevo. Los modelos antiguos quedan en el historial con estado
`inactivo` y se pueden volver a activar si hiciera falta.

## 6. Arranque del monitoreo y lectura del panel principal

Con un modelo activo para el equipo, el supervisor inicia el modo
`monitoreo` desde la pantalla de modos. En ese momento:

- Se abre una sesión de monitoreo en PostgreSQL.
- El servicio `monitoreo`, que corre en su propio contenedor en un
  bucle de cinco segundos, detecta la nueva sesión activa en su
  próxima iteración y carga el modelo correspondiente de forma
  diferida. No es necesario reiniciar nada manualmente.

El panel principal muestra, para el equipo seleccionado:

- El modo del sistema y el modelo activo del equipo.
- Las tres variables eléctricas en tiempo real (valores actuales y
  serie temporal, con rango seleccionable).
- Los estados operativos identificados por el modelo, ordenados por
  consumo creciente, con sus valores medios ± desvío.
- Las alertas pendientes de reconocimiento, con los desvíos de cada
  variable expresados en sigmas.

El detalle de la evaluación de cada muestra (estado operativo
asignado, desvíos respecto a la banda, resultado de las dos etapas
de detección y estado de la ventana de confirmación) no se muestra
en la interfaz: lo imprime el servicio `monitoreo` en su salida, y
se puede seguir con:

```bash
docker-compose logs -f monitoreo
```

Durante el monitoreo, el sistema procesa cualquier medición que
llegue por MQTT para ese equipo y haya sido persistida por
Node-RED. En un entorno real los datos vienen del equipo físico;
para pruebas o demostraciones se pueden relanzar los generadores
con el perfil `aprendizaje`.

## 7. Interpretación de alertas y reconocimiento

El sistema genera una alerta solo cuando se confirman al menos 8
muestras anómalas dentro de una ventana deslizante de 10 muestras
consecutivas, procesadas fuera de los períodos de supresión por
transición entre estados. Este criterio evita falsas alarmas por
muestras aisladas y por los transitorios de cambio de estado.

Cada alerta queda registrada en InfluxDB con:

- **Severidad**, calculada a partir del desvío máximo observado:
  - `AVISO`: desvío máximo por debajo de 3.5σ.
  - `ALERTA`: desvío máximo entre 3.5σ y 6σ.
  - `CRITICA`: desvío máximo mayor a 6σ.
- **Firma**, descripción sintomática de qué variables se desviaron,
  en qué sentido (aumento o caída) y con qué magnitud (leve,
  significativo, marcado). Por ejemplo:
  `Potencia: aumento marcado (6.4σ) | Corriente: aumento
  significativo (4.1σ)`.
- **Valores instantáneos** de las tres variables, el score de
  Isolation Forest y los desvíos individuales.

Una vez generada una alerta, el estado del monitor queda "en
alerta" para ese equipo y no se emiten alertas nuevas hasta que
el operador la reconoce desde el panel. El reconocimiento se
registra en la tabla `reconocimientos_alertas` de PostgreSQL y, en
la siguiente iteración, el monitor detecta el reconocimiento y
vuelve al estado de evaluación normal.

Mientras el equipo está en estado "en alerta", el panel principal
muestra las muestras siguientes sin generar nuevos eventos, de modo
que el operador puede observar si la anomalía persiste o se
resuelve antes de reconocerla.

## 8. Resolución de problemas comunes

### La API no responde en http://localhost:8000

Verificar que el contenedor del backend esté arriba:

```bash
docker-compose ps
```

Si el estado del contenedor es `Exit`, revisar los logs:

```bash
docker-compose logs backend
```

Causas frecuentes: variable `JWT_SECRET` o `POSTGRES_PASSWORD`
sin definir en `.env`, PostgreSQL todavía inicializándose
(esperar unos segundos), o puerto 8000 ocupado por otro proceso en
el host.

### Las mediciones no llegan a InfluxDB

Primero verificar que el flujo de Node-RED esté importado y
desplegado. Node-RED arranca con flujos vacíos en una instalación
nueva: hay que importar `nodered/flows.json` desde su interfaz en
`http://localhost:1880` (menú → Import → select a file to import →
Deploy).

Si el flujo está desplegado, verificar en orden:

- Que el generador correspondiente esté corriendo
  (`docker-compose ps` debería mostrar el service `generador_cinta`
  o `generador_ventilador` como `Up` cuando una sesión de
  aprendizaje está activa).
- Que Mosquitto esté recibiendo publicaciones. En la pestaña de
  depuración de Node-RED se ven los mensajes entrantes.
- Que InfluxDB esté aceptando escrituras. El token y la
  organización configurados en Node-RED deben coincidir con los
  del `.env`.

### El entrenamiento falla silenciosamente

El entrenamiento se dispara en segundo plano al finalizar una
sesión de aprendizaje. Si al cabo de unos minutos el modelo nuevo
no aparece en la pantalla de modelos entrenados, revisar los logs
dentro del volumen `logs_entrenamiento_data`:

```bash
docker-compose exec backend ls analizador/logs_entrenamiento/
docker-compose exec backend cat analizador/logs_entrenamiento/<archivo>.log
```

Los nombres de archivo tienen el formato
`<equipo_id>_<YYYYMMDD>_<HHMMSS>.log`.

### El modo monitoreo no arranca

El inicio de monitoreo requiere que exista un modelo activo para
ese equipo. Si la interfaz devuelve un error "No hay modelo activo
para el equipo...", hay que entrenar un modelo (sección 4) y
activarlo (sección 5) antes de iniciar el monitoreo.

### El monitor no detecta anomalías esperadas

Verificar:

- Que la sesión de monitoreo esté activa (tabla `sesiones_modo`
  con `modo='monitoreo'` y `activa=TRUE` para el equipo).
- Que el modelo activo corresponda al equipo correcto.
- Que las mediciones estén llegando a InfluxDB (sección anterior).
- Que la anomalía supere los umbrales (3σ en bandas de tolerancia,
  o el umbral de Isolation Forest aprendido en entrenamiento).
- Que las anomalías no estén cayendo dentro de un período de
  supresión por transición entre estados (los primeros 8 segundos
  después de un cambio de cluster).

### Las credenciales del supervisor inicial no funcionan

El supervisor inicial se crea una sola vez, con los valores de
`ADMIN_USERNAME` y `ADMIN_PASSWORD` que había en `.env` al primer
arranque. Cambiar esas variables después no afecta al usuario ya
creado. Para recuperar el acceso, usar la base de datos
directamente para resetear la contraseña o crear un nuevo
supervisor desde PostgreSQL.
