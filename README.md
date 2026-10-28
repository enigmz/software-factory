# Software Factory

Orquestador persistente para construir una software factory con OpenAI API,
Git, Brain versionado y cuatro revisiones independientes.

**Estado: primera versión del núcleo, no una plataforma de producción completa.**
El motor, los worktrees, las herramientas del Builder, el adaptador de OpenAI y
las reglas de aprobación están implementados. Las llamadas reales a OpenAI aún
no se han validado con credenciales. El harness de UI está implementado con
Chromium y probado contra una preview local real. Los webhooks y la publicación
automática de PR/despliegues requieren adaptadores adicionales.
El sistema bloquea las etapas que no tienen esas capacidades; no simula un OK.

## Instalación

Python 3.11+ y Git. Desde la raíz de este repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m factory --help
```

En Linux usa `.venv/bin/python` en lugar de `.\.venv\Scripts\python.exe`.

## Uso

1. El repositorio objetivo necesita al menos un commit y documentos `brain/*.md`
   versionados. Copia y adapta los documentos de `brain/` a ese proyecto.
2. Copia `factory.example.json` a una configuración fuera del worktree del Builder.
   Ajusta rutas absolutas, modelos disponibles en tu cuenta y comandos de checks.
3. Define `OPENAI_API_KEY` en el entorno del worker. No la guardes en la
   configuración, en los prompts ni en Git. El código del proyecto y Brain
   enviados al modelo salen del equipo hacia OpenAI.
4. Registra una tarea y ejecuta el orquestador:

```powershell
python -m factory submit --source prompt --event change-001 --repo C:/repos/app --prompt "Cambiar el fondo de la landing"
python -m factory run TASK_ID --config factory.local.json
python -m factory status TASK_ID
python -m factory events TASK_ID
```

`run` ejecuta hasta 20 etapas y se detiene ante un bloqueo o decisión humana.
`step` ejecuta solo una etapa. Repetir el comando reanuda el estado persistido.
Si cambias la base de datos en la configuración, pasa la misma ruta con
`--db RUTA` **antes del subcomando** para submit/status/events/approve.

La fuente puede ser `prompt`, `git` o `sentry`. Esto registra una entrada local;
no implica que haya un webhook conectado. El productor debe proporcionar una
clave estable por evento. Un payload diferente con la misma clave se rechaza.

Para la aprobación humana local:

```powershell
python -m factory approve TASK_ID --commit SHA_COMPLETO --actor NOMBRE
```

Este comando confía en el operador local del sistema operativo; no autentica una
identidad remota. No se debe exponer como endpoint público. La aprobación llega
a `RELEASE_READY`, **no hace push, merge ni despliega**.

## Flujo y garantías implementadas

1. `SPEC_PENDING`: Spec Writer consulta Brain desde una revisión Git concreta.
2. `BUILD_PENDING`: worktree y rama `codex/factory-ID`; Builder con sesión nueva.
3. `VERIFY_PENDING`: runner ejecuta tests, lint y aceptación de forma independiente.
4. `REVIEW_PENDING`: seguridad, UX, UI y consistencia; cada llamada empieza sin
   historial del Builder ni informes de los otros revisores.
5. Approver determinista: requiere cuatro `PASS` del commit vigente. `FAIL`,
   `BLOCKED` y `NOT_APPLICABLE` no se convierten en aprobación.
6. `RISK_PENDING`: agente de riesgo seguido de política determinista.
7. `HUMAN_REQUIRED` o `RELEASE_READY`: automático solo con política habilitada,
   rutas permitidas, riesgo bajo, reversibilidad y ausencia de incertidumbre.

El rechazo de una verificación o revisión invalida los informes del intento y
vuelve al Builder con feedback. Tras tres rechazos, `ESCALATED` requiere intervención.
Los errores de infraestructura dejan el estado pendiente para poder reanudarlo;
no consumen una aprobación ni provocan reintentos infinitos.

`release.py` define además transiciones por recibos verificados: `MERGED`,
`OBSERVING`, `COMPLETED`, `ROLLBACK_REQUIRED`, `ROLLED_BACK`. Son contratos para un
servicio de publicación separado, no una integración ya conectada a producción.

SQLite serializa transiciones y eventos en una transacción. Una clave de entrada
única evita duplicados concurrentes. Un lock de worker evita dos ejecuciones
simultáneas; tras una caída abrupta, el operador debe confirmar que el proceso
murió antes de retirar el archivo `*.worker.lock`. No hay recuperación automática
de procesos huérfanos ni promesa de efectos externos exactamente una vez.

## Brain

Brain se lee de Git y queda fijado al commit base de la spec. El Builder no puede
editarlo mediante sus herramientas; un diff que lo modifica se bloquea.
Arquitectura, convenciones de revisión y criterios de producto deben adaptarse
al proyecto. Los cambios de Brain se gestionan en una tarea separada y revisada.

## TDD y evidencias

El Builder recibe herramientas `list_files`, `read_file`, `write_file` y
`run_check`. Solo ejecuta comandos configurados por el operador, sin shell.
Los resultados y sus hashes se guardan fuera del worktree. Cada tarea de
comportamiento necesita un fallo de aserción y un éxito posterior del mismo
check; un error de importación o de colección no cuenta como fase roja.

La detección roja actual está orientada a Python: salida con `AssertionError`,
código 1 y sin error de colección. Hay que adaptar el runner para otros stacks.
Estas evidencias **no prueban por sí solas que el test sea semánticamente bueno**:
el revisor de consistencia y la suite independiente deben detectar tests débiles
o eliminados. Las pruebas de aceptación deben estar controladas fuera del alcance
del Builder cuando se utilice con código no confiable.

Los cambios visuales usan `BrowserHarness`: `verify_task` devuelve un
`VisualResult` con veredicto, evidencia y resumen; `review` devuelve un `Review`.
Un fallo visual vuelve al Builder mediante las mismas reglas de reintento.

## Harness de navegador

Instala la dependencia opcional y el navegador:

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

En `factory.local.json`, configura `browser.base_url` como un origen de loopback
con puerto explícito, por ejemplo `http://127.0.0.1:8765`. El resto de opciones
está en `factory.example.json`. La CLI conecta automáticamente el harness al
worker cuando existe esa configuración. Sin ella, la revisión UI sigue bloqueada.

La preview debe responder en `/.factory/revision` con **exactamente**
`{"commit":"SHA_COMPLETO"}`. El servicio que publica ese identificador es una
frontera de confianza: debe servir realmente esa revisión, con datos sintéticos,
y nunca falsificar el SHA para saltarse el control. Se comprueba antes y después
de las acciones y antes de aceptar la revisión.

Para sitios estáticos con archivos versionados, hay un servidor incluido:

```powershell
python -m factory.preview --repo C:/repos/app --commit SHA_COMPLETO --directory public --port 8765
```

Este servidor lee los archivos directamente del objeto Git, no del checkout.
Los cambios locales posteriores no alteran lo que se muestra. No sirve archivos
ocultos, enlaces simbólicos ni rutas fuera del directorio público. Para una app
dinámica necesitas un servicio de preview aislado con el mismo contrato de revisión.
El orquestador no inicia ni reconstruye automáticamente esa app.

Puedes verificar el navegador sin llamar a OpenAI:

```powershell
python -m factory browser-check --config factory.local.json --commit SHA_COMPLETO
```

Este smoke test captura escritorio y móvil y devuelve la carpeta de evidencias.
**No aprueba el diseño ni sus criterios de aceptación.**

Para una revisión de agente, `models.ui` debe admitir imágenes, function calling
y salidas estructuradas en Responses API. El harness proporciona herramientas
para screenshot, clic por coordenadas, texto, teclado, scroll, viewport y navegación
en el mismo origen. Es computer use mediante herramientas propias; no usa la
herramienta nativa `computer` de OpenAI ni permite ejecutar código arbitrario.

Cada revisión crea un contexto de Chromium nuevo, sin perfil ni cookies del usuario.
Bloquea solicitudes a otros orígenes, WebSockets, service workers, ventanas nuevas
y descargas. Los límites de acciones y tiempo son configurables. Un intento
bloqueado invalida la sesión; el modelo no puede convertirlo después en PASS.

Un PASS requiere capturas observadas en escritorio y móvil, revisión vigente y
sesión sin bloqueos. Esto verifica la existencia de observaciones, **no garantiza
la corrección del juicio visual del modelo**. No se deben dar por comprobadas
accesibilidad o contraste si el agente no tiene evidencia suficiente.

Capturas PNG, hashes, `manifest.json`, `review.json` y una traza Playwright se
guardan en `artifacts/browser/COMMIT/SESSION` (bajo la ruta de artifacts configurada).
El harness sustituye referencias de evidencia inventadas por el modelo por las
referencias de sus archivos reales. La traza se puede abrir con:

```powershell
python -m playwright show-trace RUTA/trace.zip
```

Estas trazas y capturas pueden contener datos visibles de la preview. Usa datos
sintéticos y no las publiques ni las envíes a modelos con sesiones personales.
Los contextos de navegador y las restricciones de herramientas no sustituyen
un contenedor/VM ni un firewall de salida para código hostil o producción.

## Límites de seguridad

Esta versión es un control plane local para un único operador. Sus métodos Python
son una API interna de confianza: quien tiene acceso de escritura a la base de
datos o puede invocar directamente esas funciones puede falsificar evidencias.
El historial es transaccional, pero no es un registro criptográfico inmutable.

**Un worktree no es un sandbox.** Ejecutar tests ejecuta código del repositorio.
Los límites de rutas y el filtrado de variables del proceso no sustituyen el
aislamiento del sistema operativo. Para trabajo autónomo sobre código no confiable,
usa workers efímeros aislados sin acceso al control plane, secretos de producción
o credenciales de merge. La separación física de permisos no se provisiona aquí.

El worker no recibe credenciales de GitHub ni de despliegue y la publicación
está desactivada. Las rutas sensibles y cambios fuera de `.md`/`.css` no obtienen
aprobación automática en la política inicial. Una allowlist de rutas no demuestra
que un CSS carezca de efectos globales: se requiere revisión y evidencia visual.

## Pruebas

Validación local del harness: **33 pruebas pasan** con Python 3.13 en Windows,
incluidas pruebas con Chromium real. La integración con OpenAI usa respuestas
simuladas; todavía queda pendiente validarla con una clave API real.

Las pruebas cubren deduplicación concurrente, persistencia, transiciones inválidas,
revisiones obsoletas, cuatro revisores distintos, límites de corrección, riesgo,
aprobación humana ligada al commit, recibos de despliegue y reversión, restricciones
de herramientas y errores de OpenAI. La integración usa Git y comandos Python
reales con un ciclo red/green. Las pruebas del harness operan Chromium real contra
un servidor HTTP local; solo se simula la decisión del agente y las respuestas de
OpenAI. Ninguna prueba requiere API key ni llama a OpenAI.

`python -m pytest -q` ejecuta la suite sin navegador. Para ejecutar las pruebas
de Chromium usa `python -m pytest -q -m browser`; para todo, usa
`python -m pytest -q -o addopts=`. Las pruebas de loopback necesitan permiso
para abrir y conectar sockets locales. En Linux, instala además las bibliotecas
del sistema con `python -m playwright install --with-deps chromium`.

La configuración de CI está en `ci/github-actions.yml` como plantilla inactiva:
la sesión de publicación no tiene el permiso OAuth `workflow`. Para activarla,
autoriza ese permiso y mueve el archivo a `.github/workflows/tests.yml`.
La matriz prevista cubre Windows y Linux con Python 3.11 y 3.13; todavía no se
ha ejecutado en GitHub Actions. La suite local se ha probado con Python 3.13.

## Próximas integraciones

- Ejecución de previews dinámicas y recorridos UX con navegador.
- Identidad y autorización remotas; aislamiento real de workers y del almacén de evidencias.
- GitHub/GitLab: PR, protección de ramas, checks verificados y merge con SHA esperado.
- Sentry: autenticación de webhooks, agrupación por incidencia y observación posdespliegue.
- Proveedor de despliegue, ventanas de observación y reversión idempotente.
- Recuperación de trabajos interrumpidos, presupuesto persistente y métricas de coste.

## Referencias de OpenAI

El adaptador usa Responses API, `store: false`, salida estructurada estricta y
function calling. Cada rol inicia una conversación independiente; el historial
solo se conserva dentro de su propio ciclo de herramientas.

- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use)
