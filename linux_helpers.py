# linux_helpers.py
# ----------------
# Lógica para Linux (Xvfb + xdotool) que:
#  1. Arranca Xvfb
#  2. Lanza Chrome maximizado
#  3. Detecta la ventana con xdotool search
#  4. Captura toda la ventana con ImageGrab
#  5. Simula clics usando xdotool
#  6. Detiene procesos

import os
import time
import signal
import uuid
import subprocess
from threading import Lock
from PIL import ImageGrab

lock = Lock()

# ------------------------------------------------
#  1. Encontrar un DISPLAY libre para Xvfb
# ------------------------------------------------
def find_free_display():
    from pathlib import Path

    for n in range(1, 100):
        display = f":{n}"
        socket_path = Path(f"/tmp/.X11-unix/X{n}")
        if socket_path.exists():
            continue

        # Verificar si ya hay Xvfb corriendo en ese DISPLAY
        cmd = ["pgrep", "-f", f"Xvfb {display}"]
        if subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            continue

        return display

    raise RuntimeError("No se encontró ningún DISPLAY libre para Xvfb.")


# ------------------------------------------------
#  2. Lanzar Chrome maximizado sobre Xvfb y localizar su ventana con xdotool
# ------------------------------------------------
def start_chrome_linux():
    """
    1) Arranca Xvfb en un DISPLAY libre
    2) Lanza Chrome en modo maximizado (--start-maximized)
    3) Espera, luego busca con xdotool la ventana de Chrome
    4) Devuelve un dict con:
       {
         "session_id": <uuid>,
         "pid_xvfb": <pid>,
         "pid_chrome": <pid>,
         "display": ":N",
         "window_id": <id_hexa>,
         "geometry": (x, y, width, height)
       }
    """
    with lock:
        # 1) Arrancar Xvfb
        display = find_free_display()
        cmd_xvfb = ["Xvfb", display, "-screen", "0", "1920x1080x24"]
        xvfb_proc = subprocess.Popen(cmd_xvfb, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            f"--user-data-dir=/tmp/remote-profile-{uuid.uuid4()}",
            "about:blank"
        ]
        chrome_proc = subprocess.Popen(chrome_cmd, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 3) Esperar a que Chrome abra su ventana (6 s para asegurar)
        time.sleep(6)

        # 4) Buscar ventana de Chrome con xdotool
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            # Si no se detecta ventana, matamos procesos y error
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
            raise RuntimeError(f"No se detectó la ventana de Chrome en DISPLAY {display}.")

        # 5) Obtener geometría (x,y,width,height) de la ventana completa
        x, y, width, height = _get_window_geometry_xdotool(display, window_id)

        session_id = str(uuid.uuid4())
        return {
            "session_id": session_id,
            "pid_xvfb": xvfb_proc.pid,
            "pid_chrome": chrome_proc.pid,
            "display": display,
            "window_id": window_id,
            "geometry": (x, y, width, height)
        }


def _find_chrome_window_xdotool(display):
    """
    Usa `xdotool search --onlyvisible --class "chrome"` para encontrar el primer
    window_id cuyo nombre/clase contenga "Chrome". Retorna el ID (string hexa) o None.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        # Buscamos ventanas visibles con clase/class "chrome"
        salida = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--class", "chrome"],
            env=env, encoding="utf-8"
        ).strip().splitlines()
    except subprocess.CalledProcessError:
        return None

    # stdout puede listar varios, tomamos el primero
    if salida:
        return salida[0].strip()
    return None


def _get_window_geometry_xdotool(display, window_id):
    """
    Llama a `xdotool getwindowgeometry --shell <window_id>` para obtener:
      X=...
      Y=...
      WIDTH=...
      HEIGHT=...
    Retorna (x, y, width, height) como enteros.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        salida = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            env=env, encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("No se pudo obtener geometría de la ventana con xdotool.")

    # Parsear líneas como "X=0", "Y=0", "WIDTH=1920", "HEIGHT=1080", ...
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


# ------------------------------------------------
#  3. Capturar la ventana completa (incluye deco)
# ------------------------------------------------
def capture_window_linux(session_info, out_path):
    """
    - Recalcula (x, y, w, h) con xdotool (en caso de que la ventana se moviera).
    - Hace ImageGrab.grab(bbox=(x, y, x+w, y+h)); guarda PNG en out_path.
    - Devuelve el bbox usado.
    """
    with lock:
        display = session_info["display"]

        # 1) Verificar que proceso siga vivo
        pid_chrome = session_info["pid_chrome"]
        try:
            os.kill(pid_chrome, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Linux.")

        # 2) Rebuscar window_id (por si cambió)
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al recapturar en DISPLAY {display}.")
        session_info["window_id"] = window_id

        # 3) Obtener geometría completa
        x, y, width, height = _get_window_geometry_xdotool(display, window_id)
        session_info["geometry"] = (x, y, width, height)

        # 4) Definir bbox y capturar
        bbox = (x, y, x + width, y + height)
        # Necesitamos exportar DISPLAY para que ImageGrab use Xvfb
        os.environ["DISPLAY"] = display
        img = ImageGrab.grab(bbox=bbox)
        img.save(out_path, "PNG")
        return bbox


# ------------------------------------------------
#  4. Simular clic en la ventana completa (xdotool)
# ------------------------------------------------
def click_window_linux(session_info, x_rel, y_rel):
    """
    - Recalcula (x, y, w, h) con xdotool.
    - Interpreta (x_rel, y_rel) como coords relativas a ventana completa.
    - Usa `xdotool mousemove --window <window_id> <x_rel> <y_rel> click 1`.
    - Devuelve (x_abs, y_abs).
    """
    with lock:
        display = session_info["display"]

        # 1) Verificar proceso vivo
        pid_chrome = session_info["pid_chrome"]
        try:
            os.kill(pid_chrome, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Linux.")

        # 2) Rebuscar window_id
        window_id = _find_chrome_window_xdotool(display)
        if window_id is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al hacer clic en DISPLAY {display}.")
        session_info["window_id"] = window_id

        # 3) Obtener geometría completa
        x, y, width, height = _get_window_geometry_xdotool(display, window_id)
        session_info["geometry"] = (x, y, width, height)

        # 4) Validar coords relativas
        if not (0 <= x_rel < width and 0 <= y_rel < height):
            raise ValueError(f"Coordenadas fuera de rango: ({x_rel}, {y_rel})")

        # 5) Calcular coords absolutas
        x_abs = x + x_rel
        y_abs = y + y_rel

        # 6) Simular clic con xdotool
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


# ------------------------------------------------
#  5. Detener sesión (matar procesos)
# ------------------------------------------------
def stop_session_linux(session_info):
    """
    Mata el proceso de Chrome y el de Xvfb, si existen.
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
