#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uuid
import platform
from datetime import datetime
from threading import Lock
from flask import Flask, jsonify, send_file, abort, request, render_template_string

# Detectar sistema operativo
SO = platform.system()  # "Linux", "Windows", "Darwin", etc.

# Importar los helpers según corresponda
if SO == "Linux":
    import linux_helpers as helpers
elif SO == "Windows":
    import windows_helpers as helpers
else:
    raise RuntimeError(f"SO no soportado: {SO}")

# Inicialización de Flask
app = Flask(__name__)

# ------------------------------------------------
#  Estructuras globales “en memoria”
# ------------------------------------------------
# sessions = {
#   session_id: session_info_dict
# }
sessions = {}
sessions_lock = Lock()

# actions_log: lista de dicts
# {
#   "action_id": <uuid>,
#   "timestamp": <ISO 8601 UTC>,
#   "type": "capture" o "click",
#   "session_id": ...,
#   "details": { ... }
# }
actions_log = []
actions_lock = Lock()

# Carpeta para guardar capturas
CAPTURES_DIR = os.path.join(os.getcwd(), "captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)


def log_action(action_type, session_id, details):
    """
    Registra en actions_log:
      {
        "action_id": <uuid>,
        "timestamp": <ISO 8601 UTC>,
        "type": ...,
        "session_id": ...,
        "details": { ... }
      }
    """
    action_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"
    entry = {
        "action_id": action_id,
        "timestamp": timestamp,
        "type": action_type,
        "session_id": session_id,
        "details": details
    }
    with actions_lock:
        actions_log.append(entry)
    return action_id


# ------------------------------------------------
#  ENDPOINT: /start_session  (POST)
# ------------------------------------------------
@app.route("/start_session", methods=["POST"])
def start_session():
    """
    Inicia una nueva sesión remota invocando:
      - helpers.start_chrome_linux()   si SO == "Linux"
      - helpers.start_chrome_windows() si SO == "Windows"
    Guarda session_info en sessions[session_id] y devuelve {"session_id": ...}
    """
    with sessions_lock:
        try:
            if SO == "Linux":
                session_info = helpers.start_chrome_linux()
            else:
                session_info = helpers.start_chrome_windows()
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        sessions[session_info["session_id"]] = session_info
        return jsonify({"session_id": session_info["session_id"]})


# ------------------------------------------------
#  ENDPOINT: /get_capture/<session_id>  (GET)
# ------------------------------------------------
@app.route("/get_capture/<session_id>", methods=["GET"])
def get_capture(session_id):
    """
    Recaptura la ventana (zona cliente) y devuelve un PNG:
      - helpers.capture_window_linux(...) si SO == "Linux"
      - helpers.capture_window_windows(...) si SO == "Windows"
    Registra la acción en el log y sirve el archivo.
    """
    with sessions_lock:
        session_info = sessions.get(session_id)
        if not session_info:
            return abort(404)

    # Preparamos ruta de salida y registramos la acción “pending”
    action_id = log_action("capture", session_id, {"note": "pendiente_bbox"})
    out_path = os.path.join(CAPTURES_DIR, f"{action_id}.png")

    try:
        if SO == "Linux":
            bbox = helpers.capture_window_linux(session_info, out_path)
        else:
            bbox = helpers.capture_window_windows(session_info, out_path)
    except Exception as e:
        # Si falló, borramos la acción del log y devolvemos error
        with actions_lock:
            actions_log[:] = [a for a in actions_log if a["action_id"] != action_id]
        return jsonify({"error": str(e)}), 500

    # Actualizamos el detalle del bbox en el log
    with actions_lock:
        for act in actions_log:
            if act["action_id"] == action_id:
                act["details"] = {"bbox": bbox}
                break

    return send_file(out_path, mimetype="image/png")


# ------------------------------------------------
#  ENDPOINT: /click/<session_id>  (POST)
# ------------------------------------------------
@app.route("/click/<session_id>", methods=["POST"])
def click_window(session_id):
    """
    Recibe JSON {"x": <int>, "y": <int>}.
    Invoca helpers.click_window_linux o ...windows.
    Registra acción y devuelve {"action_id": ..., "status": "ok"}.
    """
    data = request.get_json()
    if not data or "x" not in data or "y" not in data:
        return jsonify({"error": "JSON inválido. Se requiere 'x' e 'y'."}), 400

    x_rel = int(data["x"])
    y_rel = int(data["y"])

    with sessions_lock:
        session_info = sessions.get(session_id)
        if not session_info:
            return abort(404)

    try:
        if SO == "Linux":
            x_abs, y_abs = helpers.click_window_linux(session_info, x_rel, y_rel)
        else:
            x_abs, y_abs = helpers.click_window_windows(session_info, x_rel, y_rel)
        action_id = log_action("click", session_id, {
            "x_rel": x_rel, "y_rel": y_rel, "x_abs": x_abs, "y_abs": y_abs
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"action_id": action_id, "status": "ok"})


# ------------------------------------------------
#  ENDPOINT: /stop_session/<session_id>  (POST o GET)
# ------------------------------------------------
@app.route("/stop_session/<session_id>", methods=["POST", "GET"])
def stop_session(session_id):
    """
    Invoca helpers.stop_session_linux o ...windows según el SO,
    borra la sesión de sessions y devuelve {"stopped": True}.
    """
    with sessions_lock:
        session_info = sessions.get(session_id)
        if not session_info:
            return abort(404)

    try:
        if SO == "Linux":
            helpers.stop_session_linux(session_info)
        else:
            helpers.stop_session_windows(session_info)
    except Exception:
        pass

    with sessions_lock:
        sessions.pop(session_id, None)

    return jsonify({"stopped": True})


# ------------------------------------------------
#  ENDPOINT: /actions  (GET)
# ------------------------------------------------
@app.route("/actions", methods=["GET"])
def view_actions():
    """
    Renderiza un HTML sencillo con la tabla de actions_log.
    """
    with actions_lock:
        html = """
        <!DOCTYPE html>
        <html lang="es">
        <head>
          <meta charset="UTF-8">
          <title>Listado de Acciones</title>
          <style>
            body { font-family: sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #666; padding: 8px; text-align: left; }
            th { background: #eee; }
            pre { white-space: pre-wrap; word-break: break-all; }
          </style>
        </head>
        <body>
          <h1>Registro de Acciones</h1>
          <table>
            <thead>
              <tr>
                <th>Action ID</th>
                <th>Timestamp (UTC)</th>
                <th>Tipo</th>
                <th>Session ID</th>
                <th>Detalles</th>
              </tr>
            </thead>
            <tbody>
              {% for act in actions %}
              <tr>
                <td>{{ act["action_id"] }}</td>
                <td>{{ act["timestamp"] }}</td>
                <td>{{ act["type"] }}</td>
                <td>{{ act["session_id"] }}</td>
                <td><pre>{{ act["details"] }}</pre></td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </body>
        </html>
        """
        return render_template_string(html, actions=actions_log)


# ------------------------------------------------
#  ENDPOINT: servir index.html (cliente web)
# ------------------------------------------------
@app.route("/")
def home():
    return send_file("index.html")


# ------------------------------------------------
#  ENDPOINT: servir capturas estáticas
# ------------------------------------------------
@app.route("/captures/<filename>")
def serve_capture(filename):
    path = os.path.join(CAPTURES_DIR, filename)
    if not os.path.exists(path):
        return abort(404)
    return send_file(path, mimetype="image/png")


# ------------------------------------------------
#  MAIN: arranque de Flask
# ------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
