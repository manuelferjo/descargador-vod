# Descargador VOD · YouTube y Twitch

Programa para Windows que descarga vídeos de **YouTube** y **VODs/clips de Twitch** en la calidad que quieras (con prioridad a **60 fps**) o solo el audio en **MP3**, enteros o **solo un fragmento** (por ejemplo, del minuto 45 a la 1:55:00).

Interfaz con el estilo de Vortexs (el mismo que Stream Delay).

## Descargar el programa (para usarlo)

1. Ve a **[Releases](../../releases/latest)** y descarga el archivo **`DescargadorVOD-…-PARA-COMPARTIR.zip`**.
   (No descargues "Source code": eso es el código, no el programa.)
2. Clic derecho en el zip → **Extraer todo…** → **Extraer**.
3. Abre **`DescargadorYouTube.exe`** desde la carpeta extraída. Deja `ffmpeg.exe` y la carpeta `_internal` a su lado.

Si Windows dice "Windows protegió su PC": **Más información** → **Ejecutar de todas formas**.

## Qué hace

- YouTube y Twitch (VODs y clips), vídeo MP4 o MP3, varias calidades.
- **60 fps primero**: si la calidad elegida no los tiene, sube a la más cercana que sí.
- **Fragmentos** con Desde / Hasta. Si el enlace lleva tiempo (`?t=1h45m00s`), se rellena solo.
- **Vista previa** (miniatura, título y duración) al pegar el enlace.
- **Porcentaje real**, velocidad y tiempo restante, también en fragmentos.
- **Cancelar** en cualquier momento (borra los archivos a medias).
- **Priorizar H.264**: el vídeo se abre en cualquier PC y editor; si llega en HEVC/AV1 se convierte.
- **Sesión del navegador** (Firefox, Chrome, Edge…) para vídeos que piden iniciar sesión.
- **Se actualiza solo** (desde la v1.4): al abrirse mira si hay una Release nueva en este repositorio y, si aceptas, la descarga, la instala y se vuelve a abrir.
- **Actualizar el motor** (yt-dlp) desde la propia app cuando YouTube cambia algo.

## Publicar una versión nueva

1. Sube `VERSION` en `descargador_youtube.py` (p. ej. `"1.5"`), y el nombre del zip en `crear_exe.bat` y `Guia.html`.
2. Ejecuta `crear_exe.bat`.
3. Crea una Release con la etiqueta `v1.5` y adjunta el `…-PARA-COMPARTIR.zip`.

Todos los que tengan la v1.4 o posterior verán el aviso de actualización al abrir el programa.

## Compilar desde el código

Requisitos: Python 3, y FFmpeg (`winget install ffmpeg`).

```bat
crear_exe.bat
```

Instala `yt-dlp`, `pillow` y `pyinstaller`, genera el `.exe` y crea el zip para compartir.

Para probarlo sin compilar: `pip install yt-dlp pillow` y `python descargador_youtube.py`.

## Uso responsable

Úsalo para tus propios vídeos o contenido que tengas permiso para guardar.

Hecho con [yt-dlp](https://github.com/yt-dlp/yt-dlp) y [FFmpeg](https://ffmpeg.org/). Fuentes Outfit, Plus Jakarta Sans y JetBrains Mono bajo licencia OFL (en `res/`).
