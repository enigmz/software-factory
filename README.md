# Software Factory

Orquestador persistente para construir una software factory con OpenAI API,
Git, Brain versionado y cuatro revisiones independientes.

**Estado: primera versión del núcleo, no una plataforma de producción completa.**
El motor, los worktrees, las herramientas del Builder, el adaptador de OpenAI y
las reglas de aprobación están implementados. Las llamadas reales a OpenAI aún
no se han validado con credenciales. El navegador de UI, los webhooks y la
publicación automática de PR/despliegues requieren adaptadores adicionales.
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

Los cambios visuales requieren un harness de preview independiente. Actualmente
se inyecta mediante `Worker(..., browser_factory=...)` con estos métodos:

- `verify_task(commit, task) -> list[str]`: artefactos reales con prefijo `visual:`.
- `review(agent, context, path) -> Review`: sesión de navegador sobre la preview
  del commit exacto, capturas y acciones observadas antes de devolver el informe.

**Ese harness de computer use todavía no está implementado.** La CLI bloqueará
la validación visual o la revisión UI mientras falte. No basta seleccionar un
modelo con visión para poder operar un navegador.

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

22 pruebas cubren deduplicación concurrente, persistencia, transiciones inválidas,
revisiones obsoletas, cuatro revisores distintos, límites de corrección, riesgo,
aprobación humana ligada al commit, recibos de despliegue y reversión, restricciones
de herramientas y errores de OpenAI. La integración usa Git y comandos Python
reales con un ciclo red/green; agentes y navegador se sustituyen por dobles de
prueba. No requiere API key ni hace llamadas externas.

La configuración de CI está en `ci/github-actions.yml` como plantilla inactiva:
la sesión de publicación no tiene el permiso OAuth `workflow`. Para activarla,
autoriza ese permiso y mueve el archivo a `.github/workflows/tests.yml`.
La matriz prevista cubre Windows y Linux con Python 3.11 y 3.13; todavía no se
ha ejecutado en GitHub Actions. La suite local se ha probado con Python 3.13.

## Próximas integraciones

- Harness aislado de computer use para UI y recorridos UX.
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
