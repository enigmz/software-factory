# GitHub: publicación con revisión humana

La integración inicial apunta a `enigmz/software-factory`, base `main`.
Usa la sesión de `gh auth login` del **controlador**, sin exportar el token ni
pasarlo a los modelos o contenedores. Requiere Git y GitHub CLI en PATH.
No hay endpoint de merge, bypass administrativo ni despliegue automático.

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

## Estado de la primera conexión

El 30 de octubre de 2026 se verificó acceso ADMIN como `enigmz`. `main` no estaba
protegida y no había workflows activos. La sesión OAuth tiene `repo`, sin
`workflow`; la plantilla `ci/github-actions.yml` sigue inactiva. Para obtener CI,
activa ese permiso y publica la plantilla en `.github/workflows/tests.yml`,
observa los nombres/IDs reales y configura los checks requeridos. Después aplica
protección de rama en GitHub con revisión y checks obligatorios.

Esta integración no configura esas reglas ni afirma que GitHub ya las imponga.
Hasta entonces el límite efectivo es que la factory no ofrece un comando de merge;
un administrador del repositorio conserva sus permisos de GitHub.

Las pruebas del adaptador simulan respuestas GitHub para fallos y condiciones de
carrera. La conexión se ensaya publicando una PR de integración humana; no se
fabrican aprobaciones de agentes para simular una tarea completada por OpenAI.

Referencia: [API de pull requests](https://docs.github.com/en/rest/pulls/pulls).
