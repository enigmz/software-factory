# Aislamiento de ejecución

`isolation.mode` es `docker` por defecto. El controlador conserva OpenAI, Git,
SQLite y la política de aprobación. Los checks ejecutan código del proyecto en
un contenedor Linux nuevo por llamada. La revisión visual ejecuta Chromium y una
preview estática del commit en otro contenedor. No se publican puertos.

## Preparación

Desde la raíz de este repositorio, con Docker Linux operativo:

```powershell
docker build -f docker/Dockerfile -t software-factory-worker:local .
```

El contexto de build usa una allowlist en `.dockerignore`. La imagen contiene
el runner y sus dependencias; no copia `.env`, metadatos Git ni el adaptador OpenAI.
La construcción necesita red. La ejecución no instala dependencias: deben
añadirse a la imagen revisada para cada proyecto. Usa `python` en los checks,
no una ruta al Python de Windows. El preflight resuelve la imagen local a su ID
inmutable y no descarga imágenes. Si falla Docker, se bloquea antes de llamar
al modelo; no hay fallback automático al host.

## Restricciones

- Usuario 10001, sin capabilities, `no-new-privileges` y raíz de solo lectura.
- Red `none`, sin socket Docker, sin puertos publicados y sin variables del host.
- Una copia del código montada de solo lectura; ejecución en `/workspace` tmpfs.
- Exclusión de `.env*`, metadatos Git, claves comunes, estado y entornos virtuales.
  Se rechazan enlaces, hardlinks y snapshots que excedan el límite de tamaño.
- CPU, memoria, procesos, salida y duración limitados. Limpieza del contenedor
  propio al terminar o fallar; nunca se utiliza `docker prune`.
- Evidencias visuales exportadas con nombres permitidos y tamaño limitado.

La exclusión de archivos no sustituye a un escáner de secretos: los secretos
incrustados en código ordinario podrían entrar en el snapshot. Git/configuración
del repositorio, dependencias de la imagen y el daemon Docker son de confianza.
Un contenedor comparte el kernel Linux y no demuestra resistencia a cualquier
escape. El sandbox interno de Chromium permanece habilitado; si el entorno no
lo admite, la revisión se bloquea. No se añade modo privilegiado para resolverlo.
El navegador usa un perfil basado en [Playwright v1.58.0](https://github.com/microsoft/playwright/blob/v1.58.0/utils/docker/seccomp_profile.json),
conservado en `docker/seccomp-browser.json`; permite namespaces de usuario para
su sandbox. Se añade `chroot` para que Chromium lo utilice dentro de su namespace;
el contenedor mantiene `cap-drop ALL`, sin conceder `SYS_CHROOT` ni `SYS_ADMIN`.
Los checks conservan el perfil por defecto de Docker.

El modo explícito `{"isolation": {"mode": "local"}}` está reservado para
fixtures de confianza. Ejecuta código en el host y no ofrece estas garantías.

## Validación sin OpenAI

```powershell
$env:FACTORY_TEST_DOCKER='1'
python -m pytest tests/test_docker_integration.py -q
python -m factory browser-check --config factory.local.json --commit SHA_COMPLETO
```

Las pruebas de integración son opt-in: sin esa variable se omiten, no se
consideran evidencia de una ejecución real. Cubren secretos, permisos, red,
timeout, limpieza y capturas desktop/móvil de un commit de prueba.

Validación del 30 de octubre de 2026: 55 pruebas pasan con Docker Linux en
Docker Desktop sobre Windows, incluyendo las tres integraciones de contenedores.
`browser-check` también pasa sobre el commit inicial del laboratorio. No se
realizaron llamadas a OpenAI y no quedaron contenedores del worker tras la suite.

Si el controlador se cierra abruptamente, inspecciona los contenedores con
`docker ps -a --filter label=software-factory.worker=true` y elimina únicamente
el contenedor de la ejecución interrumpida. La recuperación automática después
de una caída del controlador sigue pendiente.

Referencias: [opciones de ejecución Docker](https://docs.docker.com/engine/containers/run/)
y [Playwright en Docker](https://playwright.dev/docs/docker).
