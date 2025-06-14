# linux_helpers.py
import os
import time
import signal
import uuid
import subprocess
from threading import Lock
from pathlib import Path
from PIL import ImageGrab, Image
import io

lock = Lock()


def find_free_display():
    """
    Busca un DISPLAY libre para Xvfb, probando ":1", ":2", ...
    Retorna el primer display disponible.
    """
    for n in range(1, 100):
        d = f":{n}"
        socket_path = Path(f"/tmp/.X11-unix/X{n}")
        if socket_path.exists():
            continue
        # Verificamos que no haya un proceso Xvfb corriendo en ese display
        if subprocess.call(
            ["pgrep", "-f", f"Xvfb {d}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        ) == 0:
            continue
        return d
    raise RuntimeError("No se encontró ningún DISPLAY libre para Xvfb.")


def start_chrome_linux():
    """
    Inicia una sesión remota de Chrome en Linux:
      1) Si ya existe $DISPLAY, se usa ese.
      2) Si no, se arranca Xvfb en un display libre.
      3) Se lanza Chrome con --start-maximized en el DISPLAY elegido.
      4) Se espera a que abra la ventana y se busca con xdotool.
      5) Si falla, mata procesos y lanza RuntimeError.
      6) Devuelve un dict con:
         {
           "session_id": <uuid>,
           "pid_xvfb": <pid> o None,
           "pid_chrome": <pid>,
           "display": display,
           "window_id": <window_id>,
           "geometry": (x, y, width, height)
         }
    """
    with lock:
        # 1) Determinar qué DISPLAY usar
        current_display = os.environ.get("DISPLAY")
        usar_xvfb = False
        xvfb_proc = None

        if current_display:
            display = current_display
        else:
            display = find_free_display()
            usar_xvfb = True
            xvfb_proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1)

        # 2) Lanzar Chrome maximizado en ese DISPLAY
        env = os.environ.copy()
        env["DISPLAY"] = display

        chrome_exe = "/usr/bin/google-chrome"
        chrome_cmd = [
            chrome_exe,
            "--no-sandbox",
            "--disable-gpu",
            "--start-maximized",
            "about:blank"
        ]
        chrome_proc = subprocess.Popen(
            chrome_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # 3) Esperar a que Chrome abra la ventana (6 s)
        time.sleep(6)

        # 4) Buscar la ventana de Chrome con xdotool
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            if usar_xvfb and xvfb_proc:
                try:
                    xvfb_proc.terminate()
                except Exception:
                    pass
            raise RuntimeError(f"No se detectó la ventana de Chrome en DISPLAY {display}.")

        # 5) Obtener la geometría completa (x, y, width, height)
        x, y, width, height = _get_window_geometry_xdotool(display, window_id)

        session_id = str(uuid.uuid4())
        return {
            "session_id": session_id,
            "pid_xvfb": xvfb_proc.pid if usar_xvfb else None,
            "pid_chrome": chrome_proc.pid,
            "display": display,
            "window_id": window_id,
            "geometry": (x, y, width, height)
        }


def _find_chrome_window_xdotool(display):
    """
    Usa `xdotool search --onlyvisible --class chrome` en el DISPLAY dado.
    Retorna el primer window_id encontrado o None.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        salida = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--class", "chrome"],
            env=env,
            encoding="utf-8"
        ).strip().splitlines()
    except subprocess.CalledProcessError:
        return None

    return salida[0].strip() if salida else None


def _get_window_geometry_xdotool(display, window_id):
    """
    Llama a `xdotool getwindowgeometry --shell <window_id>` en el DISPLAY.
    Parsear la salida para devolver (x, y, width, height).
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        salida = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            env=env,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("No se pudo obtener geometría de la ventana con xdotool.")

    x = y = width = height = None
    for linea in salida.splitlines():
        if linea.startswith("X="):
            x = int(linea.split("=", 1)[1])
        elif linea.startswith("Y="):
            y = int(linea.split("=", 1)[1])
        elif linea.startswith("WIDTH="):
            width = int(linea.split("=", 1)[1])
        elif linea.startswith("HEIGHT="):
            height = int(linea.split("=", 1)[1])
    if None in (x, y, width, height):
        raise RuntimeError("Salida inesperada de getwindowgeometry: " + salida)
    return (x, y, width, height)


def capture_window_linux(session_info, out_path=None):
    """
    Captura la ventana completa de Chrome y devuelve un BytesIO con JPEG en memoria.
      1) Verifica que el proceso de Chrome siga vivo.
      2) Busca la ventana nuevamente con xdotool (puede haberse movido).
      3) Obtiene la geometría completa.
      4) Hace ImageGrab.grab(bbox=(x,y,x+width,y+height)) usando el DISPLAY.
      5) Genera un JPEG en un io.BytesIO (calidad 75) y lo retorna.
      6) IGNORA el parámetro opcional 'out_path' (ya no se escribe PNG en disco).
    """
    with lock:
        display = session_info["display"]
        pid_chrome = session_info["pid_chrome"]

        # 1) Verificar que Chrome siga vivo
        try:
            os.kill(pid_chrome, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Linux.")

        # 2) Volver a encontrar el window_id (por si se movió)
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al recapturar en DISPLAY {display}.")
        session_info["window_id"] = window_id

        # 3) Obtener geometría
        x, y, width, height = _get_window_geometry_xdotool(display, window_id)
        session_info["geometry"] = (x, y, width, height)

        # 4) Capturar la pantalla con PIL.ImageGrab
        bbox = (x, y, x + width, y + height)
        os.environ["DISPLAY"] = display
        img = ImageGrab.grab(bbox=bbox)

        # 5) Convertir a JPEG en memoria
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)

        # 6) Devolver el buffer con JPEG
        return buf


def click_window_linux(session_info, x_rel, y_rel):
    """
    Simula un clic izquierdo en coordenadas relativas (x_rel, y_rel)
    dentro de la ventana completa de Chrome:
      1) Verifica proceso vivo y busca window_id.
      2) Obtiene geometría completa.
      3) Calcula (x_abs, y_abs) = (x + x_rel, y + y_rel).
      4) Usa `xdotool mousemove --window <window_id> <x_rel> <y_rel> click 1`.
      5) Retorna (x_abs, y_abs).
    """
    with lock:
        display = session_info["display"]
        pid_chrome = session_info["pid_chrome"]
        try:
            os.kill(pid_chrome, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Linux.")

        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al hacer clic en DISPLAY {display}.")
        session_info["window_id"] = window_id

        x, y, width, height = _get_window_geometry_xdotool(display, window_id)
        session_info["geometry"] = (x, y, width, height)

        if not (0 <= x_rel < width and 0 <= y_rel < height):
            raise ValueError(f"Coordenadas fuera de rango: ({x_rel}, {y_rel})")

        x_abs = x + x_rel
        y_abs = y + y_rel

        env = os.environ.copy()
        env["DISPLAY"] = display
        try:
            subprocess.check_call(
                [
                    "xdotool", "mousemove", "--window", window_id, str(x_rel), str(y_rel), "click", "1"
                ],
                env=env
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Error ejecutando xdotool: {e}")

        return (x_abs, y_abs)


def type_text_linux(session_info, text):
    """
    Envía texto a la ventana de Chrome:
      1) Verifica que el proceso de Chrome siga vivo.
      2) Busca la ventana (window_id).
      3) Llama a `xdotool type --window <window_id> --delay 100 "<text>"`.
      4) Retorna True si tuvo éxito, lanza excepción si falla.
    """
    with lock:
        display = session_info["display"]
        pid_chrome = session_info["pid_chrome"]
        try:
            os.kill(pid_chrome, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Linux.")

        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome para enviar texto en DISPLAY {display}.")

        env = os.environ.copy()
        env["DISPLAY"] = display
        try:
            subprocess.check_call(
                ["xdotool", "type", "--window", window_id, "--delay", "100", text],
                env=env
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Error enviando texto con xdotool: {e}")

        return True


def stop_session_linux(session_info):
    """
    Mata el proceso de Chrome y, si se arrancó Xvfb, también lo mata.
    """
    with lock:
        pid_chrome = session_info.get("pid_chrome")
        pid_xvfb = session_info.get("pid_xvfb")

        if pid_chrome:
            try:
                os.kill(pid_chrome, signal.SIGTERM)
            except Exception:
                pass

        if pid_xvfb:
            try:
                os.kill(pid_xvfb, signal.SIGTERM)
            except Exception:
                pass
