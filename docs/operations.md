# Presupuesto y recuperación

El worker persiste reservas, consumos, etapas, recuperaciones y checkpoints en
SQLite. No contiene claves API ni conversaciones del modelo en estas tablas.
Los recibos anteriores a esta versión no incluyen consumo: no se reconstruye
ni se presenta como cero facturado. Los límites solo abarcan nuevas llamadas
de esta factory, no toda la cuenta OpenAI.

## Límites por tarea

Valores iniciales: 5 USD estimados, 500.000 tokens acumulados, 100 llamadas de
generación, 100.000 tokens de entrada por llamada, 8.000 de salida y 3 recuperaciones.
`budget.prices` debe asignar a cada modelo sus tarifas revisadas de entrada y
salida en USD por millón de tokens. Sin tarifa, se bloquea antes de contactar
la API. Usa tarifas conservadoras que cubran contexto, caché y modalidad de uso.
El adaptador solicita el servicio `default`; no activa Fast/Ultrafast.

Antes de generar se consulta el [contador oficial de tokens](https://developers.openai.com/api/docs/guides/token-counting),
incluyendo historial, herramientas, esquema e imágenes. Después se reserva la
entrada contada y toda la salida permitida, en una transacción SQLite. Si el
contador falla o no queda presupuesto, no se envía la generación.

`usage` devuelto por OpenAI sustituye la reserva cuando es válido, incluso si
la respuesta es incompleta. Los tokens de razonamiento forman parte de la salida.
El coste es una **estimación conservadora**, no una factura: usa la tarifa
configurada para todos los tokens de entrada, sin descontar aciertos de caché.
No incluye infraestructura Docker, impuestos ni otros servicios. Revisa las
[tarifas oficiales](https://developers.openai.com/api/docs/pricing) cuando cambies de modelo.
Si el consumo supera la reserva, se registra el consumo real y se bloquea.

Un timeout, una respuesta sin `usage`, un error HTTP o una caída antes de guardar
el consumo conservan toda la reserva. No hay reintentos automáticos. `recover`
marca esa reserva como `unknown` y la mantiene descontada del presupuesto; no
afirma que OpenAI no haya cobrado. Las reservas nuevas no borran las anteriores.

## Consultar y recuperar

```powershell
python -m factory --db RUTA/state.sqlite3 usage TASK_ID
python -m factory recover TASK_ID --config factory.local.json --reason "Worktree y consumos inspeccionados"
python -m factory run TASK_ID --config factory.local.json
```

`usage` muestra consumo por llamada/rol/modelo, estado de ejecución y motivos
de recuperación. La recuperación requiere el mismo bloqueo de proceso que
el worker: no permite intervenir mientras otro worker está ejecutando la tarea.
El bloqueo lo libera el sistema operativo al morir el proceso; no se debe borrar
el archivo `.worker.lock`. El despliegue soportado sigue siendo un solo host.
Antes de actualizar desde el bloqueo antiguo, detén los workers antiguos.

Las etapas completadas y las revisiones guardadas se conservan. Cada check del
Builder se guarda al terminar. Su resultado final y huella del contenido se
guardan antes de hacer commit, permitiendo recuperar una caída antes o después
del commit sin volver a llamar al Builder. Un cambio posterior de contenido
bloquea ese checkpoint. Nunca se hace reset del worktree para recuperarlo.

Una interrupción **dentro** de la conversación del agente reinicia ese rol tras
recuperación explícita, con el estado del repositorio y comprobaciones disponibles;
no se recupera su conversación ni se garantiza ejecución exactamente una vez de
cada herramienta. Las verificaciones independientes incompletas se repiten.
Una comprobación interrumpida sin recibo no cuenta como evidencia. Las previews
se vuelven a crear y observar; no se aprueban con capturas incompletas anteriores.
Si el controlador murió con un contenedor vivo, sigue la recuperación de
[aislamiento](isolation.md) antes de reanudar.

La configuración local del laboratorio usa 5 USD/millón de entrada y 20 de salida
como **tarifas de planificación conservadoras**, con el límite de 100.000 tokens
de entrada. No son la tarifa estándar publicada: el 30 de octubre de 2026,
GPT-6.1 Sol publica 2 USD de entrada y 10 USD de salida por millón en contexto
corto; las escrituras de caché tienen tarifas diferentes. La reserva deliberadamente
mayor deja margen y puede detener una tarea antes de consumir 5 USD facturados.

Las pruebas usan respuestas HTTP simuladas: no gastan saldo y no sustituyen una
validación con el endpoint real de conteo y generación en tu cuenta.
