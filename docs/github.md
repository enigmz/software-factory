# GitHub: publicación y cierre de tareas

La integración inicial apunta a `enigmz/software-factory`, base `main`.
Usa la sesión de `gh auth login` del **controlador**, sin exportar el token ni
pasarlo a los modelos o contenedores. Requiere Git y GitHub CLI en PATH.
El controlador puede fusionar un SHA autorizado y registrar el resultado.
No usa bypass administrativo ni despliega.

Añade al JSON de configuración:

```json
{
  "github": {
    "repository": "enigmz/software-factory",
    "base": "main",
    "required_checks": []
  },
  "auto_merge": false
}
```

Una tarea debe pertenecer al repositorio local configurado y haber completado
verificación, cuatro revisiones PASS y análisis de riesgo. El publicador verifica
los remotos de lectura/escritura, el worktree limpio y el SHA revisado. Empuja ese
SHA a `codex/factory-TASK-SHA`, sin force push. Publica una PR en borrador y
guarda su identidad en SQLite sin adelantar la aprobación humana.

```powershell
python -m factory publish-pr TASK_ID --config factory.local.json
python -m factory pr-status TASK_ID --config factory.local.json
python -m factory release-step TASK_ID --config factory.local.json
```

La PR contiene identificadores y un resumen fijo del proceso; no publica prompts,
Brain, logs, capturas ni rutas del equipo. El diff sí es código público: la
revisión de seguridad debe estar completa antes de publicar.

Si se interrumpe después de crear la PR, la siguiente invocación busca por
repositorio/rama/base antes de crear otra. Reutiliza la abierta; una PR cerrada,
varias coincidencias o un SHA diferente requieren intervención. No sobrescribe
una rama remota que haya cambiado.

`pr-status` consulta todos los check-runs y estados del SHA exacto, con paginación,
y vuelve a comprobar la cabecera de la PR. `required_checks` debe contener objetos
`{"name": "nombre exacto del check", "app_id": 123}` con el ID real de la
aplicación que lo emite. No inventes nombres ni IDs: obténlos de una ejecución
real. Un check obligatorio ausente, pendiente, fallido o emitido por otra app
no pasa. Una lista vacía tampoco pasa. Otros checks pendientes/fallidos bloquean.
`checks_passed` es informativo y no concede permiso de merge.

## Merge y recuperación

`run` publica la PR al terminar la evaluación de riesgo y ejecuta un paso de
release. `release-step` permite repetir solo esta fase, sin agentes ni llamadas
a OpenAI. Publica si falta el vínculo, espera si faltan checks y se detiene en
`HUMAN_REQUIRED` si necesita aprobación. No hay polling ni servicio en segundo
plano: invoca de nuevo `release-step` cuando termine CI o tras aprobar la tarea.

Para automatizar cambios acotados, configura `auto_merge: true` y una lista
`auto_paths` explícita, por ejemplo `["README.md"]`. También deben existir checks
requeridos. El riesgo debe ser bajo, reversible y sin incertidumbre, las rutas
deben cumplir la política `.md`/`.css` y las cuatro revisiones y verificaciones
deben pertenecer al SHA actual. El valor predeterminado sigue siendo `false`.
Cambiar o revocar la política afecta también a tareas pendientes de merge.

Para cambios que requieren intervención, aprueba el SHA usando `approve` con
el `--db` de la configuración y luego ejecuta `release-step`, o fusiona la PR
en GitHub y ejecuta `release-step` para registrar ese resultado externo. La
aprobación local es una operación del controlador de confianza; `--actor` es
una etiqueta de auditoría, no un mecanismo de autenticación remota.

Antes de fusionar, el adaptador exige protección estricta, aplicable a admins,
con los checks configurados y sus aplicaciones emisoras. Compara la base y las
rutas del diff remoto con el build revisado, saca la PR de borrador si procede,
revalida CI y envía el SHA esperado al endpoint de merge. Si cambia la base,
se bloquea para revisar y verificar el build actualizado; no hace rebase solo.

La intención se guarda antes de enviar el merge. Si la respuesta se pierde,
la siguiente ejecución consulta primero GitHub: registra el merge existente
sin repetirlo. Estado `MERGED` y evento se guardan en una sola transacción, con
SHA revisado, SHA de merge y URL. Repetirlo no duplica eventos. Una PR cerrada
sin merge o un recibo incompleto bloquean el avance. Una fusión manual externa
no fabrica una aprobación local. Este paso termina en `MERGED`; despliegue y
observación siguen pendientes de integración.

## Estado de la primera conexión

El 30 de octubre de 2026 se verificó acceso ADMIN como `enigmz`. `main` no estaba
protegida y no había workflows activos. El workflow ahora se define en
`.github/workflows/tests.yml`, con matriz Windows/Linux, Chromium e integración
Docker. Su publicación requiere el permiso OAuth `workflow`. Tras publicarlo,
observa los nombres/IDs reales y configura los checks requeridos. Después aplica
protección de rama en GitHub con revisión y checks obligatorios.

El 2 de noviembre se activaron los seis checks, la protección estricta de `main`
incluidos administradores, y el bloqueo de force push y borrado. La PR #1 quedó
fusionada. La configuración local registra los nombres reales y app ID 15368.
El adaptador no modifica estas reglas y verifica su presencia antes de ordenar
un merge; un administrador conserva la capacidad de cambiar la configuración.

Las pruebas del adaptador simulan respuestas GitHub para fallos, timeouts tras
merge y condiciones de carrera. No prueban el ciclo completo con agentes reales
ni generan aprobaciones artificiales en la base de datos de producción.

Referencia: [API de pull requests](https://docs.github.com/en/rest/pulls/pulls).
