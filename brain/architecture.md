# Arquitectura

El orquestador controla estados y permisos; los agentes producen propuestas.
Separar contratos, motor determinista, adaptadores y efectos externos.
No autorizar una transición con texto libre del modelo. Vincular evidencias al
commit exacto. Las salidas incompletas y la incertidumbre bloquean el avance.

Python 3.11+, Pydantic para contratos y SQLite para el control plane local.
No acoplar el motor a un proveedor LLM, Git remoto o plataforma de despliegue.
Persistir transición y evento en la misma transacción. No eliminar trazas para
simular que un intento nunca falló. Evitar dependencias si la biblioteca estándar
resuelve el problema con claridad.

Los workers de código no confiable necesitan aislamiento de sistema operativo.
Una carpeta, rama, prompt o worktree no constituye una frontera de seguridad.
