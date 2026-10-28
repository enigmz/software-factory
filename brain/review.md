# Convenciones de código y revisión

Funciones pequeñas con nombres concretos. Comentarios para decisiones y límites,
no para repetir lo que hace una instrucción. No esconder errores ni usar except
genérico para convertir un fallo en éxito. No almacenar secretos en archivos.

Tests basados en comportamientos y fallos reales: commit obsoleto, duplicados,
fallos de infraestructura, rollback, timeout y salida malformada. Probar las
invariantes independientemente del agente que construyó la solución.

Toda revisión debe aportar ubicación, evidencia, impacto y corrección sugerida
en su informe. No heredar la conversación del Builder. Si no se ha podido
verificar el criterio asignado, devolver BLOCKED y explicar lo que falta.
