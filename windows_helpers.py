# windows_helpers.py
# ------------------
# Lógica específica para ejecutar y controlar Chrome en Windows (Win32 + PIL),
# arrancando maximizado y capturando toda la ventana (incluye decoración),
# pero ahora devolviendo JPEG en un io.BytesIO en lugar de guardar PNG en disco.

import os
import time
import signal
import uuid
import subprocess
from threading import Lock
from PIL import ImageGrab, Image
import io

import win32gui
import win32process
import win32api
import win32con

lock = Lock()


# ------------------------------------------------
#  1. Lanzar Chrome maximizado y localizar HWND
# ------------------------------------------------
def start_chrome_windows():
    """
    1) Lanza Chrome en modo maximizado (--start-maximized).
    2) Espera, localiza con EnumWindows el HWND cuyo PID coincida.
    3) Obtiene región completa de la ventana (incluye bordes/decoration).
    Devuelve dict con:
      {
        "session_id": <uuid>,
        "pid_chrome": <pid>,
        "hwnd": <handle>,
        "window_rect": (left, top, right, bottom)
      }
    """
    with lock:
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if not os.path.exists(chrome_path):
            chrome_path = "chrome"

        cmd = [
            chrome_path,
            "--new-window",
            "--start-maximized",
            f"--user-data-dir=C:\\Temp\\remote-profile-{uuid.uuid4()}",
            "about:blank"
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            raise RuntimeError(f"No se pudo lanzar Chrome en Windows: {e}")

        pid_chrome = proc.pid
        # Esperamos a que la ventana aparezca y se maximice
        time.sleep(2.5)

        hwnd = _find_chrome_window_windows(pid_chrome)
        if hwnd is None:
            try:
                os.kill(pid_chrome, signal.SIGTERM)
            except Exception:
                pass
            raise RuntimeError("No se encontró la ventana de Chrome en Windows.")

        rect = _get_window_rect_windows(hwnd)
        session_id = str(uuid.uuid4())
        return {
            "session_id": session_id,
            "pid_chrome": pid_chrome,
            "hwnd": hwnd,
            "window_rect": rect  # (left, top, right, bottom)
        }


def _find_chrome_window_windows(pid_chrome):
    """
    Recorre con EnumWindows todas las ventanas visibles y devuelve el primer HWND
    cuyo PID coincide con pid_chrome y cuyo título contenga "Chrome".
    """
    result = []

    def enum_callback(hwnd, extra):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != pid_chrome:
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if "Chrome" in title:
            result.append(hwnd)
        return True

    win32gui.EnumWindows(enum_callback, None)
    return result[0] if result else None


def _get_window_rect_windows(hwnd):
    """
    Dado un HWND, obtiene la región completa de la ventana (incluye barra de título y bordes)
    en coordenadas de pantalla: (left, top, right, bottom).
    """
    # GetWindowRect devuelve (left, top, right, bottom) ABSOLUTOS
    return win32gui.GetWindowRect(hwnd)


# ------------------------------------------------
#  2. Capturar la ventana completa (ahora JPEG en memoria)
# ------------------------------------------------
def capture_window_windows(session_info, out_path):
    """
    Usa PIL.ImageGrab.grab(bbox) para capturar la región completa de la ventana
    (obtiene coords con GetWindowRect). EN LUGAR DE GUARDAR PNG, convierte a JPEG en un
    io.BytesIO y retorna ese buffer. 'out_path' se IGNORA.
    Devuelve el io.BytesIO con JPEG (posición al inicio).
    """
    with lock:
        hwnd = session_info["hwnd"]
        pid = session_info["pid_chrome"]

        # Verificar que el proceso siga vivo
        try:
            os.kill(pid, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Windows.")

        # Reubicar HWND por si cambió
        new_hwnd = _find_chrome_window_windows(pid)
        if new_hwnd is None:
            raise RuntimeError("No se encontró el HWND de Chrome al recapturar en Windows.")
        session_info["hwnd"] = new_hwnd

        # Obtener el rect completo (left, top, right, bottom)
        left, top, right, bottom = _get_window_rect_windows(new_hwnd)
        rect = (left, top, right, bottom)
        session_info["window_rect"] = rect

        # Capturamos la región con PIL.ImageGrab
        img = ImageGrab.grab(bbox=rect)

        # Convertir la imagen a JPEG en memoria
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)

        return buf


# ------------------------------------------------
#  3. Simular clic en la ventana completa
# ------------------------------------------------
def click_window_windows(session_info, x_rel, y_rel):
    """
    Simula un clic izquierdo en (x_rel, y_rel) relativo a la esquina superior
    izquierda de la ventana completa (incluye decoración).
    Usa SetCursorPos y mouse_event. Devuelve (x_abs, y_abs).
    """
    with lock:
        hwnd = session_info["hwnd"]
        pid = session_info["pid_chrome"]

        # Verificar que el proceso siga vivo
        try:
            os.kill(pid, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Windows.")

        # Reubicar HWND por si cambió
        new_hwnd = _find_chrome_window_windows(pid)
        if new_hwnd is None:
            raise RuntimeError("No se encontró la ventana de Chrome al hacer clic en Windows.")
        session_info["hwnd"] = new_hwnd

        # Obtener el rect completo
        left, top, right, bottom = _get_window_rect_windows(new_hwnd)
        width = right - left
        height = bottom - top

        # Verificar que x_rel, y_rel estén dentro del tamaño de la ventana
        if not (0 <= x_rel < width and 0 <= y_rel < height):
            raise ValueError(f"Coordenadas fuera de rango: ({x_rel}, {y_rel})")

        # Coordenadas absolutas
        x_abs = left + x_rel
        y_abs = top + y_rel

        # Mover cursor y clic
        win32api.SetCursorPos((x_abs, y_abs))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x_abs, y_abs, 0, 0)
        time.sleep(0.02)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x_abs, y_abs, 0, 0)

        return (x_abs, y_abs)


# ------------------------------------------------
#  4. Envía texto a la ventana con xdotool
# ------------------------------------------------
def type_text_windows(session_info, text):
    """
    Envía texto a la ventana de Chrome (usa SendMessage+WM_CHAR, o bien puedes usar
    SetForegroundWindow + teclado simulado). Aquí se asume que basta con SetForegroundWindow
    y SendMessage de caracteres, pero para simplicidad usaremos win32api.keybd_event.
    """
    with lock:
        hwnd = session_info["hwnd"]
        pid = session_info["pid_chrome"]

        # Verificar proceso vivo
        try:
            os.kill(pid, 0)
        except OSError:
            raise RuntimeError("El proceso de Chrome ya no existe en Windows.")

        # Reubicar HWND
        new_hwnd = _find_chrome_window_windows(pid)
        if new_hwnd is None:
            raise RuntimeError("No se encontró la ventana de Chrome para enviar texto en Windows.")
        session_info["hwnd"] = new_hwnd

        # Traer la ventana al frente
        win32gui.SetForegroundWindow(new_hwnd)
        time.sleep(0.1)

        # Enviar cada carácter como evento de teclado
        for c in text:
            vk = win32api.VkKeyScan(c)
            win32api.keybd_event(vk, 0, 0, 0)           # tecla presionada
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)  # tecla liberada
            time.sleep(0.02)

        return True


# ------------------------------------------------
#  5. Detener sesión (matar Chrome)
# ------------------------------------------------
def stop_session_windows(session_info):
    """
    Mata el proceso de Chrome (no hay Xvfb en Windows).
    """
    with lock:
        pid = session_info.get("pid_chrome")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
