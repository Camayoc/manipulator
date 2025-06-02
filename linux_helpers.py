# linux_helpers.py
# ----------------
# Lógica para Linux que:
#  1. Detecta si hay un DISPLAY X real (por ejemplo ":0")
#  2. Si no hay DISPLAY, arranca Xvfb en uno libre
#  3. Lanza Chrome maximizado en ese DISPLAY
#  4. Busca la ventana de Chrome con xdotool y obtiene su geometría
#  5. Captura toda la ventana con PIL.ImageGrab
#  6. Simula clics usando xdotool
#  7. Detiene procesos al cerrar la sesión

import os
import time
import signal
import uuid
import subprocess
from threading import Lock
from PIL import ImageGrab

lock = Lock()


def display_activo(display):
    """
    Comprueba si el DISPLAY proporcionado está disponible para xdotool.
    Retorna True si `xdotool getdisplaygeometry <display>` no falla.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        subprocess.check_output(
            ["xdotool", "getdisplaygeometry", display],
            env=env,
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False


def find_free_display():
    """
    Busca un DISPLAY libre para Xvfb, probando ":1", ":2", ...
    Retorna el primer display disponible.
    """
    from pathlib import Path

    for n in range(1, 100):
        d = f":{n}"
        socket_path = Path(f"/tmp/.X11-unix/X{n}")
        if socket_path.exists():
            continue
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
      1) Si ya existe $DISPLAY activo y válido, se usa ese.
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
        # 1) Comprobar si $DISPLAY ya está definido y activo
        current_display = os.environ.get("DISPLAY")
        usar_xvfb = False
        xvfb_proc = None

        if current_display and display_activo(current_display):
            display = current_display
        else:
            # No hay X en marcha, arrancamos Xvfb
            display = find_free_display()
            usar_xvfb = True
            xvfb_proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1)  # dar tiempo a Xvfb para iniciar

        # 2) Lanzar Chrome en modo maximizado sobre el DISPLAY
        env = os.environ.copy()
        env["DISPLAY"] = display

        chrome_exe = "/usr/bin/google-chrome"
        chrome_cmd = [
            chrome_exe,
            "--no-sandbox",
            "--disable-gpu",
            "--start-maximized",
            f"--user-data-dir=/tmp/remote-profile-{uuid.uuid4()}",
            "about:blank"
        ]
        chrome_proc = subprocess.Popen(
            chrome_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # 3) Esperar a que Chrome abra la ventana
        time.sleep(6)

        # 4) Buscar la ventana de Chrome con xdotool
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            # Si no se detecta la ventana, matamos procesos y error
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            if usar_xvfb:
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


def capture_window_linux(session_info, out_path):
    """
    Captura la ventana completa de Chrome:
      1) Verifica que el proceso de Chrome siga vivo.
      2) Busca la ventana nuevamente con xdotool (puede haberse movido).
      3) Obtiene la geometría completa.
      4) Hace ImageGrab.grab(bbox=(x,y,x+width,y+height)) usando DISPLAY correcto.
      5) Guarda PNG en out_path y retorna el bbox.
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
            raise RuntimeError(f"No se encontró la ventana de Chrome al recapturar en DISPLAY {display}.")
        session_info["window_id"] = window_id

        x, y, width, height = _get_window_geometry_xdotool(display, window_id)
        session_info["geometry"] = (x, y, width, height)

        bbox = (x, y, x + width, y + height)
        os.environ["DISPLAY"] = display
        img = ImageGrab.grab(bbox=bbox)
        img.save(out_path, "PNG")
        return bbox


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
                ["xdotool", "mousemove", "--window", window_id, str(x_rel), str(y_rel), "click", "1"],
                env=env
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Error ejecutando xdotool: {e}")

        return (x_abs, y_abs)


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
