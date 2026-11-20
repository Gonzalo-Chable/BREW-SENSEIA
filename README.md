# BrewSense AI — Inteligencia de Ventas y POS para Cafeterías (Café Aroma)

Sistema web Full-Stack desarrollado con Python (Flask), MariaDB y MLxtend (FP-Growth) para el análisis de canasta de compra, gestión rentable de combos con efecto de anclaje y recomendaciones en vivo en caja (POS).

## Requisitos previos
- Python 3.13
- MariaDB Server 11+ / 13.0.2 (puerto configurable en `.env`, por defecto `3307`)

## Instalación y ejecución local
1. Clonar el repositorio:
   `git clone https://github.com/TU_USUARIO/brewsense-ai.git`
   `cd brewsense-ai`
2. Crear y activar el entorno virtual:
   `python -m venv venv`
   `.\venv\Scripts\activate`
3. Instalar dependencias:
   `pip install -r requirements.txt`
4. Configurar variables de entorno:
   Copiar `.env.example` a `.env` y colocar la contraseña y puerto local de MariaDB.
5. Inicializar base de datos, tablas y migración del dataset:
   `python db.py`
6. Arrancar el servidor web:
   `python app.py`
7. Abrir en el navegador: `http://127.0.0.1:5000/login`

## Credenciales de prueba
- **Administrador:** `admin@cafearoma.mx` / `Admin123!`
- **Empleado (POS):** `empleado@cafearoma.mx` / `Barista123!`