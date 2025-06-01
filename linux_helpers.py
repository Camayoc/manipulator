# linux_helpers.py
# ----------------
# Lógica específica para ejecutar y controlar Chrome en Linux (X11 + Xvfb),
# arrancando maximizado y capturando toda la ventana (incluye barras y bordes).

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
#  2. Lanzar Chrome maximizado sobre Xvfb y localizar su ventana
# ------------------------------------------------
def start_chrome_linux():
    """
    1) Encuentra DISPLAY libre y arranca Xvfb.
    2) Lanza Chrome en modo maximizado (--start-maximized).
    3) Espera, localiza la ventana de Chrome con wmctrl y obtiene su ID, posición y tamaño.
    4) Devuelve un dict con toda esa info.
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

        chrome_cmd = [
            "google-chrome",
            "--no-sandbox",
            "--disable-gpu",
            "--start-maximized",
            "--user-data-dir=/tmp/remote-profile-{}".format(uuid.uuid4()),
            "about:blank"
        ]
        chrome_proc = subprocess.Popen(chrome_cmd, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 3) Esperar a que aparezca la ventana
        time.sleep(2)

        window_info = _find_window_linux(display, "Chrome")
        if window_info is None:
            # Si no se detecta la ventana, matamos procesos y error
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
            raise RuntimeError(f"No se detectó la ventana de Chrome en DISPLAY {display}.")

        window_id, x, y, width, height = window_info

        session_id = str(uuid.uuid4())
        return {
            "session_id": session_id,
            "pid_xvfb": xvfb_proc.pid,
            "pid_chrome": chrome_proc.pid,
            "display": display,
            "window_id": window_id,
            # Ya no usamos 'decor' ni 'geometry' separados: guardamos la geometría completa
            "geometry": (x, y, width, height)
        }


def _find_window_linux(display, title_substring):
    """
    Usa `wmctrl -lG` sobre el DISPLAY dado para buscar la primera ventana
    cuyo título contenga title_substring. Devuelve (window_id, x, y, w, h) o None.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        salida = subprocess.check_output(["wmctrl", "-lG"], env=env, encoding="utf-8")
    except subprocess.CalledProcessError:
        return None

    for linea in salida.splitlines():
        partes = linea.split(None, 7)
        if len(partes) < 8:
            continue
        win_id, _, x_str, y_str, w_str, h_str, _, title = partes
        if title_substring.lower() in title.lower():
            try:
                x = int(x_str)
                y = int(y_str)
                w = int(w_str)
                h = int(h_str)
            except ValueError:
                continue
            return (win_id, x, y, w, h)
    return None


# ------------------------------------------------
#  3. Capturar la ventana completa (incluyendo decoración)
# ------------------------------------------------
def capture_window_linux(session_info, out_path):
    """
    - Recalcula (x, y, w, h) con wmctrl para la ventana de Chrome.
    - Usa ese rectángulo completo (incluye barras/bordes) como bbox.
    - Captura con Pillow y guarda en out_path (PNG).
    - Devuelve el bbox usado.
    """
    with lock:
        display = session_info["display"]

        # 1) Encontrar nuevamente la ventana (por si cambió de HWND)
        window_info = _find_window_linux(display, "Chrome")
        if window_info is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al recapturar en DISPLAY {display}.")

        window_id, x, y, width, height = window_info
        session_info["window_id"] = window_id
        session_info["geometry"] = (x, y, width, height)

        # 2) Definimos bbox = (x, y, x+width, y+height)
        bbox = (x, y, x + width, y + height)

        # 3) Ajustamos DISPLAY para Pillow
        env = os.environ.copy()
        env["DISPLAY"] = display
        os.environ["DISPLAY"] = display

        # 4) Captura
        img = ImageGrab.grab(bbox=bbox)
        img.save(out_path, "PNG")
        return bbox


# ------------------------------------------------
#  4. Simular clic en la ventana Linux (xdotool)
# ------------------------------------------------
def click_window_linux(session_info, x_rel, y_rel):
    """
    - Recalcula (x, y, w, h) con wmctrl.
    - Interpreta (x_rel, y_rel) como coordenadas relativas a la esquina superior izquierda
      de la ventana completa (incluye decoración).
    - Usa `xdotool mousemove --window <window_id> <x_rel> <y_rel> click 1`.
    - Devuelve (x_abs, y_abs) en coords de pantalla absolutas.
    """
    with lock:
        display = session_info["display"]

        # 1) Encontrar ventana de nuevo
        window_info = _find_window_linux(display, "Chrome")
        if window_info is None:
            raise RuntimeError(f"No se encontró la ventana de Chrome al hacer clic en DISPLAY {display}.")

        window_id, x, y, width, height = window_info
        session_info["window_id"] = window_id
        session_info["geometry"] = (x, y, width, height)

        # 2) Verificar que x_rel,y_rel estén dentro de (width, height)
        if not (0 <= x_rel < width and 0 <= y_rel < height):
            raise ValueError(f"Coordenadas fuera de rango: ({x_rel}, {y_rel})")

        # 3) Calcular coordenadas absolutas
        x_abs = x + x_rel
        y_abs = y + y_rel

        # 4) Ejecutar xdotool
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
