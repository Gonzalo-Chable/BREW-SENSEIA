"""
app.py — Backend Flask REST API + Autenticación + MariaDB (puerto 3307)
BrewSense AI · Café Aroma
"""

import os
import time
import logging
from functools import wraps
from pathlib import Path
from datetime import datetime
from decimal import Decimal

import joblib
import pandas as pd
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    request,
    send_from_directory,
    make_response,
    session,
    redirect,
)
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

from db import (
    get_connection,
    init_database,
    importar_csv_a_mariadb,
    DB_PORT,
    DB_NAME,
    DATA_DIR,
)

try:
    from mlxtend.preprocessing import TransactionEncoder
    from mlxtend.frequent_patterns import fpgrowth, association_rules
    MLXTEND_OK = True
except ImportError:
    MLXTEND_OK = False

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = os.getenv("SECRET_KEY", "brewsense-cafe-aroma-secret-2026")
CORS(app, supports_credentials=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BrewSenseApp")


# ──────────────────────────────────────────────────────────────
# Utilidades de serialización y seguridad por roles
# ──────────────────────────────────────────────────────────────
def clean_value(val):
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return val


def clean_rows(rows: list) -> list:
    return [{k: clean_value(v) for k, v in r.items()} for r in rows]


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sesión requerida. Inicia sesión nuevamente."}), 401
            return redirect("/login")
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sesión requerida."}), 401
            return redirect("/login")
        if user.get("rol") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Acceso denegado: requiere privilegios de Administrador."}), 403
            return redirect("/pos")
        return fn(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────
# Consultas analíticas reales sobre MariaDB
# ──────────────────────────────────────────────────────────────
def build_ticket_where(sucursal="all", turno="all", periodo="365d"):
    """Construye filtro SQL seguro; siempre incluye las ventas nuevas de origen POS."""
    clauses = ["1=1"]
    params = []

    if sucursal and sucursal.lower() not in ["all", "todas", "nacional"]:
        clauses.append("t.sucursal = %s")
        params.append(sucursal)

    if turno and turno in ["morning", "afternoon", "night"]:
        clauses.append("t.turno = %s")
        params.append(turno)

    dias = {"30d": 30, "90d": 90, "trimestre": 90, "365d": 365, "anual": 365}.get(periodo, 365)
    if dias < 365:
        clauses.append(
            "(t.origen = 'pos' OR t.fecha_hora >= DATE_SUB("
            "(SELECT COALESCE(MAX(fecha_hora), NOW()) FROM tickets WHERE origen = 'csv'), INTERVAL %s DAY))"
        )
        params.append(dias)

    return " AND ".join(clauses), params


def obtener_kpis_y_graficas(sucursal="all", turno="all", periodo="365d") -> dict:
    where_sql, params = build_ticket_where(sucursal, turno, periodo)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # KPIs principales de tickets
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS tickets_total,
                    COALESCE(AVG(t.total), 85.00) AS ticket_promedio,
                    SUM(CASE WHEN t.origen = 'pos' THEN 1 ELSE 0 END) AS tickets_pos
                FROM tickets t
                WHERE {where_sql}
                """,
                params,
            )
            agg = cur.fetchone()
            tickets_total = int(agg["tickets_total"] or 0)
            ticket_promedio = round(float(agg["ticket_promedio"] or 85.0), 2)
            tickets_pos = int(agg["tickets_pos"] or 0)

            # Producto Ancla Top (más frecuente en canastas filtradas)
            cur.execute(
                f"""
                SELECT p.nombre, COUNT(DISTINCT td.ticket_id) AS canastas
                FROM ticket_detalle td
                JOIN tickets t ON t.id = td.ticket_id
                JOIN productos p ON p.id = td.producto_id
                WHERE {where_sql}
                GROUP BY p.id, p.nombre
                ORDER BY canastas DESC
                LIMIT 1
                """,
                params,
            )
            top_row = cur.fetchone()
            top_ancla = top_row["nombre"] if top_row else "Sin datos"
            top_ancla_pct = round((top_row["canastas"] / max(tickets_total, 1)) * 100, 1) if top_row else 0.0

            # Distribución real de ingresos por categoría (para Gráfica de Dona)
            cur.execute(
                f"""
                SELECT p.categoria, ROUND(SUM(td.cantidad * td.precio_unitario), 2) AS ingresos
                FROM ticket_detalle td
                JOIN tickets t ON t.id = td.ticket_id
                JOIN productos p ON p.id = td.producto_id
                WHERE {where_sql}
                GROUP BY p.categoria
                ORDER BY ingresos DESC
                """,
                params,
            )
            distribucion_ventas = clean_rows(cur.fetchall())

            # Ventas por turno horario
            cur.execute(
                f"""
                SELECT t.turno, COUNT(*) AS tickets, ROUND(AVG(t.total), 2) AS ticket_prom
                FROM tickets t
                WHERE {where_sql}
                GROUP BY t.turno
                """,
                params,
            )
            ventas_turno = clean_rows(cur.fetchall())

            # Combos activos y métricas reales de adopción IA en POS
            cur.execute("SELECT COUNT(*) AS activos FROM combos WHERE activo = 1")
            combos_activos = int(cur.fetchone()["activos"] or 0)

            cur.execute(
                """
                SELECT
                    COUNT(*) AS mostrados,
                    COALESCE(SUM(aceptado), 0) AS aceptados,
                    COALESCE(SUM(CASE WHEN aceptado = 1 THEN ingreso_extra ELSE 0 END), 0) AS ingreso_ia
                FROM logs_recomendaciones
                """
            )
            log_ia = cur.fetchone()
            mostrados = int(log_ia["mostrados"] or 0)
            aceptados = int(log_ia["aceptados"] or 0)
            tasa_adopcion = round((aceptados / mostrados) * 100, 1) if mostrados > 0 else 0.0
            ingreso_ia = round(float(log_ia["ingreso_ia"] or 0.0), 2)

            # Vista previa real de 6 canastas multi-producto (Ingesta)
            cur.execute(
                """
                SELECT
                    t.codigo_ticket AS id_ticket,
                    DATE_FORMAT(t.fecha_hora, '%Y-%m-%d %H:%i') AS fecha,
                    t.sucursal,
                    t.total,
                    GROUP_CONCAT(DISTINCT p.nombre SEPARATOR '||') AS items_str
                FROM tickets t
                JOIN ticket_detalle td ON td.ticket_id = t.id
                JOIN productos p ON p.id = td.producto_id
                GROUP BY t.id
                HAVING COUNT(DISTINCT td.producto_id) >= 2
                ORDER BY t.id DESC
                LIMIT 6
                """
            )
            preview = []
            for r in cur.fetchall():
                preview.append({
                    "id_ticket": r["id_ticket"],
                    "fecha": r["fecha"],
                    "sucursal": r["sucursal"],
                    "articulos": r["items_str"].split("||") if r["items_str"] else [],
                    "total": float(r["total"]),
                })

            cur.execute("SELECT COUNT(*) AS total_det FROM ticket_detalle")
            total_det = int(cur.fetchone()["total_det"] or 0)

        return {
            "kpis": {
                "tickets_total": tickets_total,
                "tickets_pos": tickets_pos,
                "ticket_promedio": ticket_promedio,
                "ticket_meta": round(ticket_promedio * 1.25, 2),
                "top_ancla": top_ancla,
                "top_ancla_pct": top_ancla_pct,
                "combos_activos": combos_activos,
                "tasa_adopcion_ia": tasa_adopcion,
                "ingreso_ia": ingreso_ia,
            },
            "distribucion_ventas": distribucion_ventas,
            "ventas_turno": ventas_turno,
            "summary": {
                "archivo": f"MariaDB · {DB_NAME} (puerto {DB_PORT})",
                "registros_brutos": total_det,
                "nulos_depurados": 0,
                "canastas_unicas": tickets_total,
                "preview": preview,
            },
        }
    finally:
        conn.close()


def obtener_reglas_db(min_confidence=0.0, min_lift=1.0, incluir_bloqueadas=False) -> list:
    if min_confidence <= 1.0:
        min_confidence = min_confidence * 100.0

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            filtro_estado = "" if incluir_bloqueadas else "AND r.estado != 'bloqueada'"
            cur.execute(
                f"""
                SELECT
                    r.id,
                    r.producto_ancla_id,
                    r.producto_comp_id,
                    pa.nombre AS ancla,
                    pc.nombre AS complemento,
                    pa.precio_venta AS precio_ancla,
                    pc.precio_venta AS precio_comp,
                    pa.costo_unitario AS costo_ancla,
                    pc.costo_unitario AS costo_comp,
                    r.soporte,
                    r.confianza,
                    r.lift,
                    r.estado,
                    (SELECT c.id FROM combos c
                     WHERE c.producto_ancla_id = r.producto_ancla_id
                       AND c.producto_comp_id = r.producto_comp_id
                     LIMIT 1) AS combo_id
                FROM reglas_ia r
                JOIN productos pa ON pa.id = r.producto_ancla_id
                JOIN productos pc ON pc.id = r.producto_comp_id
                WHERE r.lift >= %s AND r.confianza >= %s {filtro_estado}
                ORDER BY r.lift DESC, r.confianza DESC
                LIMIT 35
                """,
                (min_lift, min_confidence),
            )
            rows = clean_rows(cur.fetchall())

            # Si el filtro del usuario es más estricto que las reglas existentes, devolver las top por Lift
            if not rows and min_confidence > 0:
                cur.execute(
                    f"""
                    SELECT
                        r.id, r.producto_ancla_id, r.producto_comp_id,
                        pa.nombre AS ancla, pc.nombre AS complemento,
                        pa.precio_venta AS precio_ancla, pc.precio_venta AS precio_comp,
                        pa.costo_unitario AS costo_ancla, pc.costo_unitario AS costo_comp,
                        r.soporte, r.confianza, r.lift, r.estado,
                        (SELECT c.id FROM combos c
                         WHERE c.producto_ancla_id = r.producto_ancla_id
                           AND c.producto_comp_id = r.producto_comp_id
                         LIMIT 1) AS combo_id
                    FROM reglas_ia r
                    JOIN productos pa ON pa.id = r.producto_ancla_id
                    JOIN productos pc ON pc.id = r.producto_comp_id
                    WHERE 1=1 {filtro_estado}
                    ORDER BY r.lift DESC, r.confianza DESC
                    LIMIT 15
                    """
                )
                rows = clean_rows(cur.fetchall())

        reglas = []
        for r in rows:
            p_a = float(r["precio_ancla"])
            p_c = float(r["precio_comp"])
            p_sep = round(p_a + p_c, 2)
            desc = 12.0 if float(r["lift"]) >= 2.0 else 10.0
            p_combo = round(p_sep * (1.0 - desc / 100.0), 2)
            costo_tot = round(float(r["costo_ancla"]) + float(r["costo_comp"]), 2)
            margen = round(((p_combo - costo_tot) / max(p_combo, 1.0)) * 100.0, 1)
            inc = round(((p_combo - p_a) / max(p_a, 1.0)) * 100.0, 1)

            reglas.append({
                "id": r["id"],
                "producto_ancla_id": r["producto_ancla_id"],
                "producto_comp_id": r["producto_comp_id"],
                "ancla": r["ancla"],
                "complemento": r["complemento"],
                "soporte": float(r["soporte"]),
                "confianza": float(r["confianza"]),
                "lift": float(r["lift"]),
                "estado": r["estado"],
                "combo_id": r["combo_id"],
                "precio_ancla": p_a,
                "precio_separado": p_sep,
                "precio_combo": p_combo,
                "costo_total": costo_tot,
                "margen_pct": margen,
                "incremento_ticket": max(inc, 5.0),
            })
        return reglas
    finally:
        conn.close()


def obtener_combos_db() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.*,
                    pa.nombre AS ancla,
                    pc.nombre AS complemento,
                    COALESCE(r.confianza, 65.0) AS confianza,
                    COALESCE(r.lift, 2.0) AS lift,
                    (SELECT COUNT(*) FROM logs_recomendaciones l WHERE l.combo_id = c.id) AS mostrados,
                    (SELECT COALESCE(SUM(l.aceptado), 0) FROM logs_recomendaciones l WHERE l.combo_id = c.id) AS aceptados
                FROM combos c
                JOIN productos pa ON pa.id = c.producto_ancla_id
                JOIN productos pc ON pc.id = c.producto_comp_id
                LEFT JOIN reglas_ia r ON r.id = c.regla_id
                ORDER BY c.activo DESC, c.id DESC
                """
            )
            rows = clean_rows(cur.fetchall())
        for r in rows:
            most = int(r["mostrados"] or 0)
            acep = int(r["aceptados"] or 0)
            r["conversion_real"] = round((acep / most) * 100, 1) if most > 0 else 0.0
            r["incremento_ticket"] = round(
                ((float(r["precio_combo"]) - float(r["precio_ancla"])) / max(float(r["precio_ancla"]), 1.0)) * 100, 1
            )
        return rows
    finally:
        conn.close()


def obtener_entrenamientos_db() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM entrenamientos_ia ORDER BY id DESC LIMIT 6")
            return clean_rows(cur.fetchall())
    finally:
        conn.close()


def entrenar_fpgrowth_mariadb(
    min_support=0.01,
    min_confidence=0.55,
    min_lift=1.2,
    sucursal="all",
    turno="all",
    periodo="365d",
) -> dict:
    """Ejecuta FP-Growth real sobre las canastas en MariaDB y persiste resultados."""
    t0 = time.perf_counter()

    if min_support > 1.0:
        min_support /= 100.0
    if min_confidence > 1.0:
        min_confidence /= 100.0

    where_sql, params = build_ticket_where(sucursal, turno, periodo)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Extraer canastas multi-producto reales desde MariaDB
            cur.execute(
                f"""
                SELECT td.ticket_id, td.producto_id
                FROM ticket_detalle td
                JOIN tickets t ON t.id = td.ticket_id
                WHERE {where_sql}
                """,
                params,
            )
            rows = cur.fetchall()

        df_tx = pd.DataFrame(rows)
        reglas_descubiertas = []
        reglas_descartadas = 0

        if not df_tx.empty and MLXTEND_OK:
            agrupado = df_tx.groupby("ticket_id")["producto_id"].apply(lambda s: list(set(s)))
            multi = [c for c in agrupado if len(c) >= 2]
            transacciones = multi if len(multi) >= 15 else list(agrupado)

            if len(transacciones) >= 10:
                te = TransactionEncoder()
                te_ary = te.fit(transacciones).transform(transacciones)
                df_oh = pd.DataFrame(te_ary, columns=te.columns_)

                # Soporte adaptativo para catálogos de 80+ productos
                soporte_ef = min(min_support, 0.005)
                freq = fpgrowth(df_oh, min_support=soporte_ef, use_colnames=True, max_len=2)
                if not freq.empty:
                    df_all = association_rules(freq, metric="lift", min_threshold=0.01)
                    df_valid = df_all[df_all["lift"] > 1.05].sort_values(
                        by=["lift", "confidence"], ascending=False
                    )
                    reglas_descartadas = int(len(df_all) - len(df_valid))

                    for _, r in df_valid.head(50).iterrows():
                        a_id = int(list(r["antecedents"])[0])
                        c_id = int(list(r["consequents"])[0])
                        reglas_descubiertas.append((
                            a_id,
                            c_id,
                            round(float(r["support"]) * 100, 2),
                            round(float(r["confidence"]) * 100, 2),
                            round(float(r["lift"]), 2),
                        ))

        tiempo_ms = round((time.perf_counter() - t0) * 1000, 2)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entrenamientos_ia
                    (min_support, min_confidence, min_lift, sucursal_filtro, turno_filtro, tiempo_ms, total_reglas, reglas_descartadas)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (min_support, min_confidence, min_lift, sucursal, turno, tiempo_ms, len(reglas_descubiertas), reglas_descartadas),
            )
            entrenamiento_id = cur.lastrowid

            # Upsert preservando estado ('aprobada' o 'bloqueada') definido por el dueño
            for a_id, c_id, sop, conf, lift in reglas_descubiertas:
                cur.execute(
                    """
                    INSERT INTO reglas_ia
                        (entrenamiento_id, producto_ancla_id, producto_comp_id, soporte, confianza, lift, estado)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pendiente')
                    ON DUPLICATE KEY UPDATE
                        entrenamiento_id = VALUES(entrenamiento_id),
                        soporte = VALUES(soporte),
                        confianza = VALUES(confianza),
                        lift = VALUES(lift)
                    """,
                    (entrenamiento_id, a_id, c_id, sop, conf, lift),
                )

            # Si aún no existe ningún combo en la BD, sembrar los 5 más fuertes para uso inmediato en POS
            cur.execute("SELECT COUNT(*) AS cnt FROM combos")
            if int(cur.fetchone()["cnt"]) == 0:
                cur.execute(
                    """
                    SELECT r.id, r.producto_ancla_id, r.producto_comp_id,
                           pa.nombre AS ancla, pc.nombre AS comp,
                           pa.precio_venta AS pa_p, pc.precio_venta AS pc_p,
                           pa.costo_unitario AS pa_c, pc.costo_unitario AS pc_c
                    FROM reglas_ia r
                    JOIN productos pa ON pa.id = r.producto_ancla_id
                    JOIN productos pc ON pc.id = r.producto_comp_id
                    ORDER BY r.lift DESC
                    LIMIT 5
                    """
                )
                for row in cur.fetchall():
                    p_a = float(row["pa_p"])
                    p_sep = round(p_a + float(row["pc_p"]), 2)
                    desc = 10.0
                    p_combo = round(p_sep * (1 - desc / 100.0), 2)
                    costo = round(float(row["pa_c"]) + float(row["pc_c"]), 2)
                    margen = round(((p_combo - costo) / max(p_combo, 1.0)) * 100, 1)
                    nombre_combo = f"Combo {row['ancla'][:22]} + {row['comp'][:22]}"
                    cur.execute(
                        """
                        INSERT INTO combos
                            (nombre_combo, regla_id, producto_ancla_id, producto_comp_id,
                             precio_ancla, precio_separado, descuento_pct, precio_combo,
                             costo_total, margen_pct, sucursal, turno, activo)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'all', 'all', 1)
                        """,
                        (nombre_combo, row["id"], row["producto_ancla_id"], row["producto_comp_id"],
                         p_a, p_sep, desc, p_combo, costo, margen),
                    )
                    cur.execute("UPDATE reglas_ia SET estado = 'aprobada' WHERE id = %s", (row["id"],))

        conn.commit()
    finally:
        conn.close()

    analytics = obtener_kpis_y_graficas(sucursal, turno, periodo)
    reglas = obtener_reglas_db(min_confidence=min_confidence, min_lift=min_lift, incluir_bloqueadas=False)
    combos = obtener_combos_db()
    entrenamientos = obtener_entrenamientos_db()

    payload = {
        "status": "ok",
        "tiempo_ms": tiempo_ms,
        "rules_count": len(reglas),
        "rules": reglas,
        "combos": combos,
        "entrenamientos": entrenamientos,
        "kpis": analytics["kpis"],
        "distribucion_ventas": analytics["distribucion_ventas"],
        "ventas_turno": analytics["ventas_turno"],
        "summary": analytics["summary"],
        "trained_at": datetime.now().strftime("%H:%M:%S"),
    }
    joblib.dump({"trained_at": payload["trained_at"], "rules": reglas}, MODELS_DIR / "reglas_fpgrowth_v1.joblib")
    return payload


# ══════════════════════════════════════════════════════════════
# RUTAS DE VISTAS HTML (PROTEGIDAS POR SESIÓN Y ROL)
# ══════════════════════════════════════════════════════════════
@app.route("/login")
def page_login():
    user = session.get("user")
    if user:
        return redirect("/" if user["rol"] == "admin" else "/pos")
    return send_from_directory(str(BASE_DIR), "login.html")


@app.route("/")
@admin_required
def page_dashboard():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/index.html")
def redirect_index():
    return redirect("/")


@app.route("/pos")
@login_required
def page_pos():
    return send_from_directory(str(BASE_DIR), "pos.html")


@app.route("/pos.html")
def redirect_pos():
    return redirect("/pos")


# ══════════════════════════════════════════════════════════════
# API DE AUTENTICACIÓN Y GESTIÓN DE USUARIOS
# ══════════════════════════════════════════════════════════════
@app.route("/api/auth/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

    if not email or not password:
        return jsonify({"error": "Ingresa correo y contraseña."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE LOWER(email) = %s", (email,))
            user = cur.fetchone()
    finally:
        conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Correo o contraseña incorrectos."}), 401

    if not user["activo"]:
        return jsonify({"error": "Tu cuenta está desactivada. Contacta a Gerencia."}), 403

    session["user"] = {
        "id": user["id"],
        "nombre": user["nombre"],
        "email": user["email"],
        "rol": user["rol"],
        "sucursal": user["sucursal"],
    }
    destino = "/" if user["rol"] == "admin" else "/pos"
    return jsonify({"status": "ok", "user": session["user"], "redirect": destino})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"status": "ok", "redirect": "/login"})


@app.route("/api/auth/me", methods=["GET"])
def api_me():
    user = session.get("user")
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"user": user})


@app.route("/api/users", methods=["GET"])
@admin_required
def api_list_users():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre, email, rol, sucursal, activo, creado_en FROM usuarios ORDER BY id ASC")
            return jsonify({"users": clean_rows(cur.fetchall())})
    finally:
        conn.close()


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    body = request.get_json(force=True, silent=True) or {}
    nombre = str(body.get("nombre", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    rol = body.get("rol", "empleado")
    sucursal = body.get("sucursal", "Quintana Roo")

    if not nombre or not email or len(password) < 6:
        return jsonify({"error": "Completa nombre, correo y una contraseña de mínimo 6 caracteres."}), 400
    if rol not in ["admin", "empleado"]:
        return jsonify({"error": "Rol inválido."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO usuarios (nombre, email, password_hash, rol, sucursal, activo)
                VALUES (%s, %s, %s, %s, %s, 1)
                """,
                (nombre, email, generate_password_hash(password), rol, sucursal),
            )
            conn.commit()
            return jsonify({"status": "ok", "id": cur.lastrowid}), 201
    except Exception as e:
        return jsonify({"error": f"No se pudo crear usuario (¿correo duplicado?): {e}"}), 400
    finally:
        conn.close()


@app.route("/api/users/<int:user_id>/toggle", methods=["PATCH"])
@admin_required
def api_toggle_user(user_id: int):
    if session["user"]["id"] == user_id:
        return jsonify({"error": "No puedes desactivar tu propia cuenta de administrador."}), 400
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET activo = IF(activo = 1, 0, 1) WHERE id = %s", (user_id,))
            conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# API ANALÍTICA, IA FP-GROWTH E INGESTA CSV (ADMIN)
# ══════════════════════════════════════════════════════════════
@app.route("/api/status", methods=["GET"])
@login_required
def api_status():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS ver")
            ver = cur.fetchone()["ver"]
            cur.execute("SELECT COUNT(*) AS c FROM tickets")
            tkt = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM productos")
            prod = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM combos WHERE activo = 1")
            cmb = cur.fetchone()["c"]
        return jsonify({
            "api": "BrewSense AI v3.0 (Flask + MariaDB)",
            "db_status": f"MariaDB {ver} (puerto {DB_PORT})",
            "tickets_count": int(tkt),
            "products_count": int(prod),
            "combos_activos": int(cmb),
            "model_ready": (MODELS_DIR / "reglas_fpgrowth_v1.joblib").exists(),
        })
    except Exception as e:
        return jsonify({"db_status": f"Error: {e}"}), 500
    finally:
        conn.close()


@app.route("/api/rules", methods=["GET"])
@admin_required
def api_rules():
    sucursal = request.args.get("sucursal", "all")
    periodo = request.args.get("periodo", "365d")
    turno = request.args.get("turno", "all")
    incluir_bloqueadas = request.args.get("all", "0") == "1"

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM reglas_ia")
                hay_reglas = int(cur.fetchone()["cnt"]) > 0
        finally:
            conn.close()

        if not hay_reglas:
            return jsonify(entrenar_fpgrowth_mariadb(0.005, 0.10, 1.1, sucursal, turno, periodo))

        analytics = obtener_kpis_y_graficas(sucursal, turno, periodo)
        reglas = obtener_reglas_db(min_confidence=0.0, min_lift=1.0, incluir_bloqueadas=incluir_bloqueadas)
        combos = obtener_combos_db()
        entrenamientos = obtener_entrenamientos_db()

        return jsonify({
            "status": "ok",
            "rules_count": len(reglas),
            "rules": reglas,
            "combos": combos,
            "entrenamientos": entrenamientos,
            "kpis": analytics["kpis"],
            "distribucion_ventas": analytics["distribucion_ventas"],
            "ventas_turno": analytics["ventas_turno"],
            "summary": analytics["summary"],
            "trained_at": entrenamientos[0]["fecha"] if entrenamientos else "ahora",
        })
    except Exception as e:
        logger.exception("Error en /api/rules")
        return jsonify({"error": str(e)}), 500


@app.route("/api/predict", methods=["POST"])
@admin_required
def api_predict():
    body = request.get_json(force=True, silent=True) or {}
    try:
        data = entrenar_fpgrowth_mariadb(
            min_support=float(body.get("min_support", 0.01)),
            min_confidence=float(body.get("min_confidence", 0.55)),
            min_lift=float(body.get("min_lift", 1.2)),
            sucursal=body.get("sucursal", "all"),
            turno=body.get("turno", "all"),
            periodo=body.get("periodo", "365d"),
        )
        return jsonify(data)
    except Exception as e:
        logger.exception("Error en /api/predict")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rules/<int:rule_id>/status", methods=["PATCH"])
@admin_required
def api_rule_status(rule_id: int):
    body = request.get_json(force=True, silent=True) or {}
    estado = body.get("estado")
    if estado not in ["pendiente", "aprobada", "bloqueada"]:
        return jsonify({"error": "Estado inválido."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE reglas_ia SET estado = %s WHERE id = %s", (estado, rule_id))
            if estado == "bloqueada":
                cur.execute("UPDATE combos SET activo = 0 WHERE regla_id = %s", (rule_id,))
            conn.commit()
        return jsonify({"status": "ok", "estado": estado})
    finally:
        conn.close()


@app.route("/api/upload", methods=["POST"])
@admin_required
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo."}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Solo se aceptan archivos .csv"}), 400

    destino = DATA_DIR / file.filename
    file.save(destino)
    try:
        res_etl = importar_csv_a_mariadb(destino)
        trained = entrenar_fpgrowth_mariadb(0.005, 0.10, 1.1)
        trained["summary"].update(res_etl)
        return jsonify(trained)
    except Exception as e:
        logger.exception("Error en /api/upload")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# API CRUD DE COMBOS CON VALIDACIÓN DE MARGEN (ADMIN)
# ══════════════════════════════════════════════════════════════
@app.route("/api/combos", methods=["GET"])
@admin_required
def api_get_combos():
    return jsonify({"combos": obtener_combos_db()})


@app.route("/api/combos", methods=["POST"])
@admin_required
def api_create_combo():
    body = request.get_json(force=True, silent=True) or {}
    ancla_id = int(body.get("producto_ancla_id", 0))
    comp_id = int(body.get("producto_comp_id", 0))
    regla_id = body.get("regla_id")
    nombre = str(body.get("nombre_combo", "")).strip()
    descuento = float(body.get("descuento_pct", 10.0))
    sucursal = body.get("sucursal", "all")
    turno = body.get("turno", "all")

    if ancla_id <= 0 or comp_id <= 0 or ancla_id == comp_id:
        return jsonify({"error": "Selecciona dos productos distintos para el combo."}), 400
    if not (1.0 <= descuento <= 45.0):
        return jsonify({"error": "El descuento debe estar entre 1% y 45%."}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre, precio_venta, costo_unitario FROM productos WHERE id IN (%s, %s)", (ancla_id, comp_id))
            prods = {r["id"]: r for r in cur.fetchall()}
            if ancla_id not in prods or comp_id not in prods:
                return jsonify({"error": "Productos no encontrados en catálogo."}), 404

            pa, pc = prods[ancla_id], prods[comp_id]
            p_ancla = float(pa["precio_venta"])
            p_sep = round(p_ancla + float(pc["precio_venta"]), 2)
            p_combo = round(p_sep * (1.0 - descuento / 100.0), 2)
            costo_tot = round(float(pa["costo_unitario"]) + float(pc["costo_unitario"]), 2)

            if p_combo <= costo_tot:
                return jsonify({
                    "error": f"Combo no rentable: el precio (${p_combo}) es menor o igual al costo (${costo_tot}). Reduce el descuento."
                }), 400

            margen_pct = round(((p_combo - costo_tot) / p_combo) * 100.0, 1)
            if not nombre:
                nombre = f"Combo {pa['nombre'][:22]} + {pc['nombre'][:22]}"

            cur.execute(
                """
                INSERT INTO combos
                    (nombre_combo, regla_id, producto_ancla_id, producto_comp_id,
                     precio_ancla, precio_separado, descuento_pct, precio_combo,
                     costo_total, margen_pct, sucursal, turno, activo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                """,
                (nombre, regla_id, ancla_id, comp_id, p_ancla, p_sep, descuento, p_combo, costo_tot, margen_pct, sucursal, turno),
            )
            combo_id = cur.lastrowid
            if regla_id:
                cur.execute("UPDATE reglas_ia SET estado = 'aprobada' WHERE id = %s", (regla_id,))
            conn.commit()
        return jsonify({"status": "ok", "id": combo_id, "combos": obtener_combos_db()}), 201
    finally:
        conn.close()


@app.route("/api/combos/<int:combo_id>", methods=["PATCH"])
@admin_required
def api_update_combo(combo_id: int):
    body = request.get_json(force=True, silent=True) or {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM combos WHERE id = %s", (combo_id,))
            combo = cur.fetchone()
            if not combo:
                return jsonify({"error": "Combo no encontrado."}), 404

            activo = int(body["activo"]) if "activo" in body else int(combo["activo"])
            nombre = str(body.get("nombre_combo", combo["nombre_combo"])).strip()
            sucursal = body.get("sucursal", combo["sucursal"])
            turno = body.get("turno", combo["turno"])
            descuento = float(body.get("descuento_pct", combo["descuento_pct"]))

            if not (1.0 <= descuento <= 45.0):
                return jsonify({"error": "El descuento debe estar entre 1% y 45%."}), 400

            p_sep = float(combo["precio_separado"])
            costo_tot = float(combo["costo_total"])
            p_combo = round(p_sep * (1.0 - descuento / 100.0), 2)
            if p_combo <= costo_tot:
                return jsonify({"error": f"Descuento excesivo: precio (${p_combo}) debajo del costo (${costo_tot})."}), 400
            margen = round(((p_combo - costo_tot) / p_combo) * 100.0, 1)

            cur.execute(
                """
                UPDATE combos
                SET nombre_combo = %s, descuento_pct = %s, precio_combo = %s,
                    margen_pct = %s, sucursal = %s, turno = %s, activo = %s
                WHERE id = %s
                """,
                (nombre, descuento, p_combo, margen, sucursal, turno, activo, combo_id),
            )
            conn.commit()
        return jsonify({"status": "ok", "combos": obtener_combos_db()})
    finally:
        conn.close()


@app.route("/api/combos/<int:combo_id>", methods=["DELETE"])
@admin_required
def api_delete_combo(combo_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM combos WHERE id = %s", (combo_id,))
            conn.commit()
        return jsonify({"status": "ok", "combos": obtener_combos_db()})
    finally:
        conn.close()


@app.route("/api/simulate", methods=["POST"])
@admin_required
def api_simulate():
    body = request.get_json(force=True, silent=True) or {}
    active_combos = body.get("active_combos", [])
    base_ticket = float(body.get("base_ticket", 85.0))
    conversion_rate = float(body.get("conversion_rate", 0.35))

    projected = base_ticket
    for combo in active_combos:
        price = float(combo.get("combo_price", 0.0))
        conf = float(combo.get("confidence", 0.5))
        if conf > 1.0:
            conf /= 100.0
        projected += price * conf * conversion_rate

    diff = projected - base_ticket
    pct = (diff / base_ticket * 100.0) if base_ticket > 0 else 0.0
    return jsonify({
        "base_ticket": round(base_ticket, 2),
        "projected_ticket": round(projected, 2),
        "total_gain": round(diff, 2),
        "pct_increase": round(pct, 1),
    })


# ══════════════════════════════════════════════════════════════
# API POS (EMPLEADO / CAJA CON RECOMENDACIONES IA EN VIVO)
# ══════════════════════════════════════════════════════════════
@app.route("/api/pos/products", methods=["GET"])
@login_required
def api_pos_products():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nombre, categoria, precio_venta, costo_unitario "
                "FROM productos WHERE activo = 1 ORDER BY categoria, nombre"
            )
            return jsonify({"products": clean_rows(cur.fetchall())})
    finally:
        conn.close()


@app.route("/api/pos/recommend", methods=["POST"])
@login_required
def api_pos_recommend():
    """Devuelve combos activos de MariaDB que coinciden con el carrito actual del cliente."""
    body = request.get_json(force=True, silent=True) or {}
    cart_ids = [int(x) for x in body.get("product_ids", [])]
    sucursal = body.get("sucursal", session["user"].get("sucursal", "Quintana Roo"))
    turno = body.get("turno", "morning")

    if not cart_ids:
        return jsonify({"recommendations": []})

    placeholders = ",".join(["%s"] * len(cart_ids))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    c.id AS combo_id,
                    c.nombre_combo,
                    c.producto_ancla_id,
                    c.producto_comp_id,
                    pa.nombre AS ancla,
                    pc.nombre AS complemento,
                    pc.precio_venta AS precio_comp_regular,
                    c.precio_ancla,
                    c.precio_separado,
                    c.precio_combo,
                    c.descuento_pct,
                    COALESCE(r.lift, 2.0) AS lift,
                    COALESCE(r.confianza, 65.0) AS confianza
                FROM combos c
                JOIN productos pa ON pa.id = c.producto_ancla_id
                JOIN productos pc ON pc.id = c.producto_comp_id
                LEFT JOIN reglas_ia r ON r.id = c.regla_id
                WHERE c.activo = 1
                  AND c.producto_ancla_id IN ({placeholders})
                  AND c.producto_comp_id NOT IN ({placeholders})
                  AND (c.sucursal = 'all' OR c.sucursal = %s)
                  AND (c.turno = 'all' OR c.turno = %s)
                ORDER BY lift DESC
                LIMIT 4
                """,
                cart_ids + cart_ids + [sucursal, turno],
            )
            recs = clean_rows(cur.fetchall())

            for r in recs:
                ahorro = round(float(r["precio_separado"]) - float(r["precio_combo"]), 2)
                extra_comp = max(round(float(r["precio_combo"]) - float(r["precio_ancla"]), 2), 1.0)
                r["ahorro_mxn"] = ahorro
                r["precio_complemento_combo"] = extra_comp

        return jsonify({"recommendations": recs})
    finally:
        conn.close()


@app.route("/api/pos/checkout", methods=["POST"])
@login_required
def api_pos_checkout():
    """Valida integridad de combos y precios en MariaDB antes de registrar la venta."""
    body = request.get_json(force=True, silent=True) or {}
    raw_items = body.get("items", [])
    sucursal = body.get("sucursal") or session["user"].get("sucursal", "Quintana Roo")
    if sucursal == "all":
        sucursal = "Quintana Roo"
    turno = body.get("turno", "morning")
    if turno not in ["morning", "afternoon", "night"]:
        turno = "morning"

    shown_ids = [int(x) for x in body.get("shown_combo_ids", [])]

    if not raw_items:
        return jsonify({"error": "El carrito está vacío."}), 400

    prod_ids = list({int(i["producto_id"]) for i in raw_items})
    combo_ids = list({int(i["combo_id"]) for i in raw_items if i.get("combo_id")})

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Consultar precios reales de productos en MariaDB
            ph_p = ",".join(["%s"] * len(prod_ids))
            cur.execute(f"SELECT id, precio_venta FROM productos WHERE id IN ({ph_p})", prod_ids)
            prod_map = {int(r["id"]): float(r["precio_venta"]) for r in cur.fetchall()}

            # 2. Consultar combos activos reales en MariaDB
            combo_map = {}
            if combo_ids:
                ph_c = ",".join(["%s"] * len(combo_ids))
                cur.execute(
                    f"""
                    SELECT id, producto_ancla_id, producto_comp_id, precio_ancla, precio_combo, activo
                    FROM combos
                    WHERE id IN ({ph_c}) AND activo = 1
                    """,
                    combo_ids,
                )
                combo_map = {int(r["id"]): r for r in cur.fetchall()}

            # 3. Verificar que cada complemento en combo tenga su producto ancla presente
            qty_by_prod = {}
            for it in raw_items:
                pid = int(it["producto_id"])
                qty = max(int(it.get("cantidad", 1)), 1)
                qty_by_prod[pid] = qty_by_prod.get(pid, 0) + qty

            used_anchors = {}
            verified_items = []
            verified_accepted = {}

            for it in raw_items:
                pid = int(it["producto_id"])
                qty = max(int(it.get("cantidad", 1)), 1)
                if pid not in prod_map:
                    return jsonify({"error": f"Producto inválido (ID {pid})."}), 400

                regular_price = prod_map[pid]
                cid = int(it["combo_id"]) if it.get("combo_id") else None

                if cid and cid in combo_map and int(combo_map[cid]["producto_comp_id"]) == pid:
                    combo = combo_map[cid]
                    anchor_id = int(combo["producto_ancla_id"])
                    available = max(qty_by_prod.get(anchor_id, 0) - used_anchors.get(anchor_id, 0), 0)
                    valid_qty = min(qty, available)
                    normal_qty = qty - valid_qty

                    if valid_qty > 0:
                        used_anchors[anchor_id] = used_anchors.get(anchor_id, 0) + valid_qty
                        combo_comp_price = max(
                            round(float(combo["precio_combo"]) - float(combo["precio_ancla"]), 2),
                            1.0,
                        )
                        verified_items.append((pid, valid_qty, combo_comp_price, cid))
                        verified_accepted[cid] = round(
                            verified_accepted.get(cid, 0.0) + combo_comp_price * valid_qty, 2
                        )

                    if normal_qty > 0:
                        verified_items.append((pid, normal_qty, regular_price, None))
                else:
                    verified_items.append((pid, qty, regular_price, None))

            total = round(sum(price * qty for _, qty, price, _ in verified_items), 2)
            codigo = f"POS-{datetime.now().strftime('%Y%m%d%H%M%S')}-{session['user']['id']}"

            # 4. Guardar ticket y detalle con los precios validados
            cur.execute(
                """
                INSERT INTO tickets (codigo_ticket, fecha_hora, sucursal, turno, usuario_id, total, origen)
                VALUES (%s, NOW(), %s, %s, %s, %s, 'pos')
                """,
                (codigo, sucursal, turno, session["user"]["id"], total),
            )
            ticket_id = cur.lastrowid

            for pid, qty, unit_price, cid in verified_items:
                cur.execute(
                    """
                    INSERT INTO ticket_detalle (ticket_id, producto_id, cantidad, precio_unitario, combo_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (ticket_id, pid, qty, unit_price, cid),
                )

            # 5. Registrar adopción real (solo combos que permanecieron válidos al cobrar)
            todos_combos = set(shown_ids) | set(verified_accepted.keys())
            for cid in todos_combos:
                acep = 1 if cid in verified_accepted else 0
                extra = verified_accepted.get(cid, 0.0)
                cur.execute(
                    """
                    INSERT INTO logs_recomendaciones (ticket_id, combo_id, usuario_id, sucursal, aceptado, ingreso_extra)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (ticket_id, cid, session["user"]["id"], sucursal, acep, extra),
                )

        conn.commit()
        return jsonify({"status": "ok", "ticket_id": ticket_id, "codigo_ticket": codigo, "total": total}), 201
    except Exception as e:
        conn.rollback()
        logger.exception("Error en checkout POS")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/pos/recent", methods=["GET"])
@login_required
def api_pos_recent():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id, t.codigo_ticket, DATE_FORMAT(t.fecha_hora, '%H:%i:%s') AS hora,
                    t.sucursal, t.turno, t.total,
                    GROUP_CONCAT(p.nombre SEPARATOR ', ') AS productos,
                    COALESCE(SUM(CASE WHEN td.combo_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS uso_combo_ia
                FROM tickets t
                JOIN ticket_detalle td ON td.ticket_id = t.id
                JOIN productos p ON p.id = td.producto_id
                WHERE t.origen = 'pos'
                GROUP BY t.id
                ORDER BY t.id DESC
                LIMIT 10
                """
            )
            return jsonify({"tickets": clean_rows(cur.fetchall())})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# RESPALDO SQL REAL DESDE MARIADB (ADMIN)
# ══════════════════════════════════════════════════════════════
@app.route("/api/backup-sql", methods=["GET"])
@admin_required
def api_backup_sql():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"-- BrewSense AI — Respaldo Real MariaDB ({DB_NAME} · puerto {DB_PORT})",
        f"-- Generado: {ts}",
        f"USE `{DB_NAME}`;",
        "",
    ]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre_combo, precio_separado, descuento_pct, precio_combo, margen_pct, activo FROM combos")
            for c in cur.fetchall():
                nom = str(c["nombre_combo"]).replace("'", "''")
                lines.append(
                    f"-- Combo #{c['id']}: '{nom}' | Regular: ${c['precio_separado']} | Combo: ${c['precio_combo']} ({c['descuento_pct']}% desc, Margen {c['margen_pct']}%) | Activo={c['activo']}"
                )
            lines.append("")
            cur.execute(
                """
                SELECT pa.nombre AS ancla, pc.nombre AS comp, r.soporte, r.confianza, r.lift, r.estado
                FROM reglas_ia r
                JOIN productos pa ON pa.id = r.producto_ancla_id
                JOIN productos pc ON pc.id = r.producto_comp_id
                ORDER BY r.lift DESC
                """
            )
            for r in cur.fetchall():
                a = str(r["ancla"]).replace("'", "''")
                c = str(r["comp"]).replace("'", "''")
                lines.append(
                    f"INSERT INTO reglas_export (ancla, complemento, soporte, confianza, lift, estado) "
                    f"VALUES ('{a}', '{c}', {r['soporte']}, {r['confianza']}, {r['lift']}, '{r['estado']}');"
                )
    finally:
        conn.close()

    resp = make_response("\n".join(lines))
    resp.headers["Content-Type"] = "application/sql"
    resp.headers["Content-Disposition"] = f"attachment; filename=backup_brewsense_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
    return resp


if __name__ == "__main__":
    init_database()
    print("\n" + "=" * 65)
    print(f"  BrewSense AI (Flask + MariaDB puerto {DB_PORT}) listo en:")
    print("  -> http://127.0.0.1:5000/login")
    print("  Admin:    admin@cafearoma.mx    / Admin123!")
    print("  Empleado: empleado@cafearoma.mx / Barista123!")
    print("=" * 65 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=True)