"""
db.py — Capa de Base de Datos MariaDB (puerto 3307) para BrewSense AI
Crea el esquema normalizado, usuarios con roles (admin/empleado) e importa el CSV inicial.
"""

import os
from pathlib import Path
from datetime import datetime
import pandas as pd
import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
load_dotenv(BASE_DIR / ".env")

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3307"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "brewsense_db")

MAPEO_ESTADOS = {
    "Lower Manhattan": "Quintana Roo",
    "Hell's Kitchen": "Jalisco",
    "Astoria": "Nuevo León",
}


def get_connection(use_db: bool = True):
    """Abre una conexión PyMySQL hacia MariaDB en el puerto configurado (3307)."""
    params = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
    }
    if use_db:
        params["database"] = DB_NAME
    return pymysql.connect(**params)


def normalizar_estado(valor: str) -> str:
    txt = str(valor).strip()
    if txt in MAPEO_ESTADOS:
        return MAPEO_ESTADOS[txt]
    low = txt.lower()
    if "quintana" in low or "manhattan" in low:
        return "Quintana Roo"
    if "jalisco" in low or "kitchen" in low:
        return "Jalisco"
    if "nuevo le" in low or "astoria" in low:
        return "Nuevo León"
    return txt if txt and txt != "nan" else "Quintana Roo"


def clasificar_turno(hora: int) -> str:
    if 6 <= hora < 12:
        return "morning"
    if 12 <= hora < 18:
        return "afternoon"
    return "night"


def inferir_categoria(nombre: str) -> str:
    n = nombre.lower()
    if any(k in n for k in ["scone", "croissant", "biscotti", "muffin", "brownie", "pan", "galleta", "bagel", "alfajor", "pastry"]):
        return "Panadería"
    if any(k in n for k in ["tea", "chai", "earl grey", "matcha", "peppermint"]):
        return "Té e Infusiones"
    if any(k in n for k in ["chocolate", "cocoa", "mocha"]):
        return "Chocolate"
    return "Café y Bebidas"


def init_database():
    """Crea la base de datos brewsense_db, todas las tablas y los usuarios iniciales."""
    conn = get_connection(use_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()

    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            # 1. Usuarios y Roles (Admin / Empleado)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nombre VARCHAR(100) NOT NULL,
                    email VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    rol ENUM('admin', 'empleado') NOT NULL DEFAULT 'empleado',
                    sucursal VARCHAR(60) NOT NULL DEFAULT 'Quintana Roo',
                    activo TINYINT(1) NOT NULL DEFAULT 1,
                    creado_en DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
            """)

            # 2. Catálogo de Productos con precio y costo unitario
            cur.execute("""
                CREATE TABLE IF NOT EXISTS productos (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nombre VARCHAR(150) NOT NULL UNIQUE,
                    categoria VARCHAR(80) NOT NULL DEFAULT 'Café y Bebidas',
                    precio_venta DECIMAL(10,2) NOT NULL,
                    costo_unitario DECIMAL(10,2) NOT NULL,
                    activo TINYINT(1) NOT NULL DEFAULT 1,
                    creado_en DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
            """)

            # 3. Tickets / Canastas (histórico CSV + ventas en vivo del POS)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    codigo_ticket VARCHAR(150) NOT NULL UNIQUE,
                    fecha_hora DATETIME NOT NULL,
                    sucursal VARCHAR(60) NOT NULL,
                    turno ENUM('morning', 'afternoon', 'night') NOT NULL DEFAULT 'morning',
                    usuario_id INT NULL,
                    total DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                    origen ENUM('csv', 'pos') NOT NULL DEFAULT 'csv',
                    INDEX idx_tickets_sucursal (sucursal),
                    INDEX idx_tickets_turno (turno),
                    INDEX idx_tickets_fecha (fecha_hora),
                    FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """)

            # 4. Historial de Entrenamientos FP-Growth
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entrenamientos_ia (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
                    min_support DECIMAL(6,4) NOT NULL,
                    min_confidence DECIMAL(6,4) NOT NULL,
                    min_lift DECIMAL(6,2) NOT NULL,
                    sucursal_filtro VARCHAR(60) NOT NULL DEFAULT 'all',
                    turno_filtro VARCHAR(20) NOT NULL DEFAULT 'all',
                    tiempo_ms DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                    total_reglas INT NOT NULL DEFAULT 0,
                    reglas_descartadas INT NOT NULL DEFAULT 0
                ) ENGINE=InnoDB
            """)

            # 5. Reglas IA con estado (pendiente / aprobada / bloqueada)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reglas_ia (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    entrenamiento_id INT NOT NULL,
                    producto_ancla_id INT NOT NULL,
                    producto_comp_id INT NOT NULL,
                    soporte DECIMAL(6,2) NOT NULL,
                    confianza DECIMAL(6,2) NOT NULL,
                    lift DECIMAL(8,2) NOT NULL,
                    estado ENUM('pendiente', 'aprobada', 'bloqueada') NOT NULL DEFAULT 'pendiente',
                    UNIQUE KEY uq_regla_par (producto_ancla_id, producto_comp_id),
                    FOREIGN KEY (entrenamiento_id) REFERENCES entrenamientos_ia(id) ON DELETE CASCADE,
                    FOREIGN KEY (producto_ancla_id) REFERENCES productos(id) ON DELETE CASCADE,
                    FOREIGN KEY (producto_comp_id) REFERENCES productos(id) ON DELETE CASCADE
                ) ENGINE=InnoDB
            """)

            # 6. Combos Comerciales (Efecto de Anclaje + Margen + Vigencia)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS combos (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nombre_combo VARCHAR(160) NOT NULL,
                    regla_id INT NULL,
                    producto_ancla_id INT NOT NULL,
                    producto_comp_id INT NOT NULL,
                    precio_ancla DECIMAL(10,2) NOT NULL,
                    precio_separado DECIMAL(10,2) NOT NULL,
                    descuento_pct DECIMAL(5,2) NOT NULL DEFAULT 10.00,
                    precio_combo DECIMAL(10,2) NOT NULL,
                    costo_total DECIMAL(10,2) NOT NULL,
                    margen_pct DECIMAL(5,2) NOT NULL,
                    sucursal VARCHAR(60) NOT NULL DEFAULT 'all',
                    turno ENUM('all', 'morning', 'afternoon', 'night') NOT NULL DEFAULT 'all',
                    activo TINYINT(1) NOT NULL DEFAULT 1,
                    creado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (regla_id) REFERENCES reglas_ia(id) ON DELETE SET NULL,
                    FOREIGN KEY (producto_ancla_id) REFERENCES productos(id) ON DELETE CASCADE,
                    FOREIGN KEY (producto_comp_id) REFERENCES productos(id) ON DELETE CASCADE
                ) ENGINE=InnoDB
            """)

            # 7. Detalle de Tickets
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticket_detalle (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    ticket_id INT NOT NULL,
                    producto_id INT NOT NULL,
                    cantidad INT NOT NULL DEFAULT 1,
                    precio_unitario DECIMAL(10,2) NOT NULL,
                    combo_id INT NULL,
                    INDEX idx_td_ticket (ticket_id),
                    INDEX idx_td_producto (producto_id),
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
                    FOREIGN KEY (producto_id) REFERENCES productos(id) ON DELETE CASCADE,
                    FOREIGN KEY (combo_id) REFERENCES combos(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """)

            # 8. Tracking de Recomendaciones IA en Caja (Aceptadas vs Rechazadas)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS logs_recomendaciones (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    ticket_id INT NULL,
                    combo_id INT NOT NULL,
                    usuario_id INT NULL,
                    sucursal VARCHAR(60) NOT NULL,
                    aceptado TINYINT(1) NOT NULL DEFAULT 0,
                    ingreso_extra DECIMAL(10,2) NOT NULL DEFAULT 0.00,
                    creado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE SET NULL,
                    FOREIGN KEY (combo_id) REFERENCES combos(id) ON DELETE CASCADE,
                    FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
            """)

            # Usuarios iniciales (Admin y Empleado)
            usuarios_seed = [
                ("Gerencia Café Aroma", "admin@cafearoma.mx", generate_password_hash("Admin123!"), "admin", "all"),
                ("Barista Caja Q. Roo", "empleado@cafearoma.mx", generate_password_hash("Barista123!"), "empleado", "Quintana Roo"),
            ]
            cur.executemany(
                """
                INSERT IGNORE INTO usuarios (nombre, email, password_hash, rol, sucursal)
                VALUES (%s, %s, %s, %s, %s)
                """,
                usuarios_seed,
            )

        conn.commit()
        print(f"[OK] Base de datos '{DB_NAME}' y 8 tablas verificadas en MariaDB (puerto {DB_PORT}).")
    finally:
        conn.close()


def importar_csv_a_mariadb(ruta_csv: Path) -> dict:
    """Importa un archivo CSV (procesado o crudo del POS) directamente en MariaDB."""
    try:
        df = pd.read_csv(ruta_csv, encoding="utf-8")
    except UnicodeDecodeError:
        df = pd.read_csv(ruta_csv, encoding="latin-1")

    brutos = len(df)

    if "basket_id" not in df.columns:
        cols_req = [c for c in ["transaction_date", "transaction_time", "store_location", "product_detail"] if c in df.columns]
        df = df.dropna(subset=cols_req).copy()
        df["estado_sucursal"] = df["store_location"].apply(normalizar_estado) if "store_location" in df.columns else "Quintana Roo"
        store_col = "store_id" if "store_id" in df.columns else "estado_sucursal"
        df["basket_id"] = (
            df["transaction_date"].astype(str) + "_" +
            df["transaction_time"].astype(str) + "_" +
            df[store_col].astype(str)
        )
    else:
        df = df.dropna(subset=["basket_id", "product_detail"]).copy()
        df["estado_sucursal"] = df["estado_sucursal"].apply(normalizar_estado) if "estado_sucursal" in df.columns else "Quintana Roo"

    df["unit_price"] = (
        df["unit_price"].astype(str).str.replace("$", "", regex=False).str.replace(",", ".", regex=False)
    )
    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce").fillna(0.0)
    df = df[df["unit_price"] > 0].copy()
    df["product_detail"] = df["product_detail"].astype(str).str.strip()
    df["qty"] = pd.to_numeric(df["transaction_qty"], errors="coerce").fillna(1).astype(int) if "transaction_qty" in df.columns else 1

    # Escalar USD -> MXN si el archivo original viene en dólares
    factor_mxn = 18.0 if df["unit_price"].mean() < 15 else 1.0
    df["precio_mxn"] = (df["unit_price"] * factor_mxn).round(2)

    # Extraer fecha y hora real
    fechas = pd.to_datetime(df["transaction_date"], errors="coerce").fillna(pd.Timestamp("2023-01-01"))
    if "transaction_time" in df.columns:
        horas = pd.to_datetime(df["transaction_time"].astype(str), format="%H:%M:%S", errors="coerce").dt.hour.fillna(10).astype(int)
    else:
        horas = pd.to_numeric(df["basket_id"].astype(str).str.extract(r"_(\d{1,2}):\d{2}")[0], errors="coerce").fillna(10).astype(int)

    df["fecha_hora"] = fechas + pd.to_timedelta(horas, unit="h")
    df["turno"] = horas.apply(clasificar_turno)
    df["subtotal"] = (df["precio_mxn"] * df["qty"]).round(2)

    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            # 1. Insertar catálogo de productos (costo estimado 38% del precio de venta)
            prod_df = df.groupby("product_detail", as_index=False)["precio_mxn"].mean()
            prod_rows = [
                (
                    row["product_detail"],
                    inferir_categoria(row["product_detail"]),
                    round(float(row["precio_mxn"]), 2),
                    round(float(row["precio_mxn"]) * 0.38, 2),
                )
                for _, row in prod_df.iterrows()
            ]
            cur.executemany(
                """
                INSERT IGNORE INTO productos (nombre, categoria, precio_venta, costo_unitario)
                VALUES (%s, %s, %s, %s)
                """,
                prod_rows,
            )
            conn.commit()

            cur.execute("SELECT id, nombre FROM productos")
            prod_map = {r["nombre"]: r["id"] for r in cur.fetchall()}

            # 2. Insertar tickets agrupados en lotes de 5,000
            tkt_df = df.groupby("basket_id", as_index=False).agg(
                fecha_hora=("fecha_hora", "first"),
                sucursal=("estado_sucursal", "first"),
                turno=("turno", "first"),
                total=("subtotal", "sum"),
            )
            tkt_rows = [
                (
                    str(r["basket_id"])[:150],
                    r["fecha_hora"].strftime("%Y-%m-%d %H:%M:%S"),
                    r["sucursal"],
                    r["turno"],
                    round(float(r["total"]), 2),
                    "csv",
                )
                for _, r in tkt_df.iterrows()
            ]
            for i in range(0, len(tkt_rows), 5000):
                cur.executemany(
                    """
                    INSERT IGNORE INTO tickets (codigo_ticket, fecha_hora, sucursal, turno, total, origen)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    tkt_rows[i:i + 5000],
                )
            conn.commit()

            cur.execute("SELECT id, codigo_ticket FROM tickets WHERE origen = 'csv'")
            tkt_map = {r["codigo_ticket"]: r["id"] for r in cur.fetchall()}

            # 3. Insertar detalle de tickets (solo para códigos recién cargados sin detalle)
            cur.execute("SELECT DISTINCT ticket_id FROM ticket_detalle")
            existentes = {r["ticket_id"] for r in cur.fetchall()}

            det_rows = []
            for row in df.itertuples(index=False):
                tid = tkt_map.get(str(row.basket_id)[:150])
                pid = prod_map.get(row.product_detail)
                if tid and pid and tid not in existentes:
                    det_rows.append((tid, pid, int(row.qty), float(row.precio_mxn)))

            for i in range(0, len(det_rows), 10000):
                cur.executemany(
                    """
                    INSERT INTO ticket_detalle (ticket_id, producto_id, cantidad, precio_unitario)
                    VALUES (%s, %s, %s, %s)
                    """,
                    det_rows[i:i + 10000],
                )
            conn.commit()

        return {
            "archivo": ruta_csv.name,
            "registros_brutos": int(brutos),
            "registros_procesados": int(len(df)),
            "nulos_depurados": int(brutos - len(df)),
            "canastas_unicas": int(len(tkt_df)),
            "productos_catalogo": int(len(prod_map)),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    init_database()

    # Si aún no hay tickets en MariaDB, migrar automáticamente el CSV en data/
    conn = get_connection(use_db=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM tickets")
            total_actual = cur.fetchone()["total"]
    finally:
        conn.close()

    if total_actual == 0:
        candidatos = [
            DATA_DIR / "canastas_cafe_aroma.csv",
            DATA_DIR / "Coffee Shop Sales(in).csv",
        ] + (list(DATA_DIR.glob("*.csv")) if DATA_DIR.exists() else [])
        csv_encontrado = next((p for p in candidatos if p.exists()), None)
        if csv_encontrado:
            print(f"[INFO] Migrando '{csv_encontrado.name}' a MariaDB (esto tarda ~5-10 segundos)...")
            res = importar_csv_a_mariadb(csv_encontrado)
            print(f"[OK] Migración completada: {res['canastas_unicas']:,} tickets y {res['productos_catalogo']} productos insertados.")
        else:
            print("[AVISO] No se encontró CSV en data/. Podrás cargarlo desde el módulo Ingesta de Datos.")
    else:
        print(f"[OK] MariaDB ya contiene {total_actual:,} tickets listos para operar.")