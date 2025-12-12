from flask import session, flash
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_file
import hashlib
import sqlite3
import csv
from io import StringIO
from datetime import datetime, timedelta
import random
import string
from functools import wraps
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from datetime import datetime
import io
import re
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Table, TableStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
import arabic_reshaper
from bidi.algorithm import get_display

app = Flask(__name__)
app.secret_key = "replace_this_with_a_random_secret"  # change to a strong random string


def safe_float(value, default=0.0):
    """Convert a string to float safely. Returns default if empty or invalid."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

# --- Hashing helpers ---
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def check_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


# --- ✅ Add your login_required decorator HERE ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash("Access denied. Admins only.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# --- Database setup & safe migrations ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Create table if it doesn't exist with the full schema
    c.execute('''
    CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT,
            customer_email TEXT,
            customer_phone TEXT,
            origin TEXT,
            destination TEXT,
            weight REAL,
            shipment_date TEXT,
            tracking_number TEXT,
            status TEXT,
            est_delivery_date TEXT,
            shipping_cost REAL,
            carrier TEXT,
            driver TEXT,
            created_at TEXT,
            customer_frt_cost REAL,
            transport_cost REAL,
            custom_clearance_cost REAL,
            other_cost REAL,
            other_cost_1 REAL,
            other_cost_2 REAL,
            other_cost_3 REAL,
            other_cost_4 REAL,
            container_number TEXT,
            bl_number TEXT,
            customer_address TEXT,
            vat_number TEXT,
            consignee_name TEXT,
            consignee_address TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            other_cost_desc TEXT,
            other_cost_1_desc TEXT,
            other_cost_2_desc TEXT,
            other_cost_3_desc TEXT,
            other_cost_4_desc TEXT,
            customs_agent TEXT,
            comments TEXT,
            shipment_vat TEXT,
            other_cost_vat TEXT,
            qty INTEGER,
            shipping_cost_desc TEXT
    )
    ''')
    conn.commit()

    # Ensure any missing columns (in case table existed before)
    expected_cols = {
        'tracking_number': "TEXT",
        'status': "TEXT",
        'est_delivery_date': "TEXT",
        'shipping_cost': "REAL",
        'carrier': "TEXT",
        'driver': "TEXT",
        'created_at': "TEXT"
    }

    # get existing columns
    c.execute("PRAGMA table_info(shipments)")
    cols = [row[1] for row in c.fetchall()]

    for col, coltype in expected_cols.items():
        if col not in cols:
            # Add column with NULL default
            sql = f"ALTER TABLE shipments ADD COLUMN {col} {coltype}"
            c.execute(sql)
    conn.commit()
    conn.close()

# Hashing helpers (we'll use SHA-256 for password hashing)
def hash_password(password: str) -> str:
    """Return the SHA-256 hex digest of the password."""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def check_password(password: str, password_hash: str) -> bool:
    """Compare plaintext password to stored hash."""
    return hash_password(password) == password_hash

# Create users table if not exists
def init_users_table():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Helper: create initial admin user if it doesn't exist
def create_initial_admin(username='admin', password='admin123'):
    """
    Creates an admin user only if a user with that username doesn't exist.
    Change the default password immediately after first login.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username = ?", (username,))
    if c.fetchone():
        conn.close()
        return False  # already exists

    password_hash = hash_password(password)
    now = datetime.utcnow().isoformat(timespec='seconds')
    c.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
              (username, password_hash, 'admin', now))
    conn.commit()
    conn.close()
    return True

def arabic_text(text):
    reshaped_text = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped_text)
    return bidi_text


# --- Invoice helper (place near other helper functions) ---
def generate_invoice_number():
    """
    Creates invoice number in format INYYYYNNNN (e.g. IN20250001).
    Uses a short SQLite lock to avoid race conditions.
    """
    year = datetime.utcnow().year
    prefix = f"IN{year}"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        # Reserve write lock to avoid duplicate sequences in concurrent requests
        c.execute("BEGIN IMMEDIATE")

        # Find last invoice for this year
        c.execute(
            "SELECT invoice_number FROM shipments WHERE invoice_number LIKE ? ORDER BY invoice_number DESC LIMIT 1",
            (f"{prefix}%",)
        )
        row = c.fetchone()
        if row and row[0]:
            last_invoice = row[0]
            try:
                last_seq = int(last_invoice[-4:])
            except Exception:
                last_seq = 0
        else:
            last_seq = 0

        next_seq = last_seq + 1
        invoice_number = f"{prefix}{next_seq:04d}"

        conn.commit()
        return invoice_number
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

# --- Utility functions ---
def generate_tracking_number():
    """
    Format: RUH/EXP/{seq}/{YYYY}
    seq is zero-padded to 2 digits, starting at 01 each year.
    """
    year = datetime.utcnow().strftime("%Y")
    prefix = "RUH/EXP"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Find last sequence for this year (pattern RUH/EXP/NN/YYYY)
    like_pattern = f"{prefix}/%/{year}"
    # We fetch tracking_number and parse seq
    c.execute("SELECT tracking_number FROM shipments WHERE tracking_number LIKE ? ORDER BY id DESC", (like_pattern,))
    rows = c.fetchall()
    conn.close()

    max_seq = 0
    for r in rows:
        tn = r[0] or ""
        # tn format RUH/EXP/01/2025
        parts = tn.split('/')
        if len(parts) >= 4 and parts[-1] == year:
            try:
                seq = int(parts[-2])
                if seq > max_seq:
                    max_seq = seq
            except Exception:
                continue

    next_seq = max_seq + 1
    seq_str = str(next_seq).zfill(2)  # '01', '02', ...
    return f"{prefix}/{seq_str}/{year}"

def calculate_shipping_cost(weight):
    # Simple cost formula: base fee + per-kg
    try:
        w = float(weight)
    except Exception:
        w = 0.0
    base = 5.00
    per_kg = 1.50
    return round(base + (per_kg * w), 2)

def default_estimated_date(shipment_date_str):
    # If shipment_date provided, add 3 days; otherwise use today +3
    try:
        d = datetime.strptime(shipment_date_str, "%Y-%m-%d")
    except Exception:
        d = datetime.utcnow()
    est = d + timedelta(days=3)
    return est.strftime("%Y-%m-%d")

DB_PATH = '/var/data/database.db'

try:
    init_db()
    init_users_table()
    create_initial_admin(username='admin', password='9884570669')
except Exception as e:
    print("Database initialization error:", e)

# --- Routes ---
@app.route('/')
@login_required
def index():
    return render_template('form.html')

@app.route('/submit', methods=['POST'])
@login_required
def submit():
    # --- Basic shipment info ---
    customer_name = request.form.get('customer_name', '').strip()
    customer_email = request.form.get('customer_email', '').strip()
    customer_phone = request.form.get('customer_phone', '').strip()
    origin = request.form.get('origin', '').strip()
    destination = request.form.get('destination', '').strip()
    weight = safe_float(request.form.get('weight'))
    qty = request.form.get('qty', '').strip()
    shipment_date = request.form.get('shipment_date', '').strip()
    est_delivery_date = request.form.get('est_delivery_date', '').strip()
    shipping_cost = safe_float(request.form.get('shipping_cost'))
    shipping_cost_desc = request.form.get('shipping_cost_desc', '').strip() #-- New Col
    carrier = request.form.get('carrier', '').strip()
    driver = request.form.get('driver', '').strip()
    status = request.form.get('status', 'Pending').strip()
    comments = request.form.get('comments', '').strip()

    # --- Cost fields ---
    customer_frt_cost = safe_float(request.form.get('customer_frt_cost'))
    transport_cost = safe_float(request.form.get('transport_cost'))
    custom_clearance_cost = safe_float(request.form.get('custom_clearance_cost'))
    other_cost = safe_float(request.form.get('other_cost'))
    other_cost_desc = request.form.get('other_cost_desc', '').strip() #-- New Col
    other_cost_1 = safe_float(request.form.get('other_cost_1'))
    other_cost_1_desc = request.form.get('other_cost_1_desc', '').strip()
    other_cost_2 = safe_float(request.form.get('other_cost_2'))
    other_cost_2_desc = request.form.get('other_cost_2_desc', '').strip()
    other_cost_3 = safe_float(request.form.get('other_cost_3'))
    other_cost_3_desc = request.form.get('other_cost_3_desc', '').strip()
    other_cost_4 = safe_float(request.form.get('other_cost_4'))
    other_cost_4_desc = request.form.get('other_cost_4_desc', '').strip()
    shipment_vat = request.form.get("shipment_vat", "0").strip()
    other_cost_vat = request.form.get("other_cost_vat", "0").strip()

    # --- Additional info ---
    container_number = request.form.get('container_number', '').strip()
    bl_number = request.form.get('bl_number', '').strip()
    customer_address = request.form.get('customer_address', '').strip()
    vat_number = request.form.get('vat_number', '').strip()
    consignee_name = request.form.get('consignee_name', '').strip()
    consignee_address = request.form.get('consignee_address', '').strip()
    customs_agent = request.form.get('customs_agent', '').strip()

    # --- Generate tracking number RUH/EXP/XX/YYYY ---
    year = datetime.now().year
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM shipments WHERE strftime('%Y', created_at)=?", (str(year),))
    count = c.fetchone()[0] + 1
    tracking_number = f"RUH/EXP/{str(count).zfill(2)}/{year}"

    created_at = datetime.utcnow().isoformat(timespec='seconds')

    # --- Insert into DB --- #New Col
    c.execute("""
        INSERT INTO shipments 
        (customer_name, customer_email, customer_phone, origin, destination, weight, qty, shipment_date,
         tracking_number, status, est_delivery_date, shipping_cost, shipping_cost_desc,  carrier, driver,
         customer_frt_cost, transport_cost, custom_clearance_cost, other_cost,other_cost_desc,
         other_cost_1,other_cost_1_desc, other_cost_2, other_cost_2_desc, other_cost_3,other_cost_3_desc, 
         other_cost_4, other_cost_4_desc, shipment_vat, other_cost_vat,
         container_number, bl_number, customer_address, vat_number, consignee_name, consignee_address, customs_agent, comments,
         created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        customer_name, customer_email, customer_phone, origin, destination, weight, qty, shipment_date,
        tracking_number, status, est_delivery_date, shipping_cost, shipping_cost_desc, carrier, driver,
        customer_frt_cost, transport_cost, custom_clearance_cost, other_cost, other_cost_desc, 
        other_cost_1, other_cost_1_desc, other_cost_2, other_cost_2_desc, other_cost_3,other_cost_3_desc, 
        other_cost_4, other_cost_4_desc, shipment_vat, other_cost_vat,
        container_number, bl_number, customer_address, vat_number, consignee_name, consignee_address, customs_agent, comments,
        created_at
    ))

    conn.commit()
    conn.close()

    return redirect(url_for('shipments'))

# ==========================
# 🧩 USER MANAGEMENT ROUTES
# ==========================

@app.route('/users')
@admin_required
def users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
    user_list = c.fetchall()
    conn.close()
    return render_template('users.html', users=user_list)

@app.route('/add_user', methods=['GET', 'POST'])
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'user')

        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for('add_user'))

        password_hash = hash_password(password)
        now = datetime.utcnow().isoformat(timespec='seconds')

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                      (username, password_hash, role, now))
            conn.commit()
            conn.close()
            flash("User added successfully!", "success")
        except sqlite3.IntegrityError:
            flash("Username already exists!", "danger")
        return redirect(url_for('users'))

    return render_template('add_user.html')

@app.route('/delete_user/<int:user_id>')
@admin_required
def delete_user(user_id):
    # Optional: Prevent deleting yourself
    if user_id == session.get('user_id'):
        flash("You cannot delete your own account while logged in.", "warning")
        return redirect(url_for('users'))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted successfully.", "info")
    return redirect(url_for('users'))

@app.route('/admin/reset_password', methods=['GET', 'POST'])
@admin_required
def admin_reset_password():
    if request.method == 'POST':
        old_password = request.form.get('old_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for('admin_reset_password'))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE id = ?", (session['user_id'],))
        row = c.fetchone()

        if not row or not check_password(old_password, row[0]):
            flash("Old password is incorrect.", "danger")
            conn.close()
            return redirect(url_for('admin_reset_password'))

        new_hash = hash_password(new_password)
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, session['user_id']))
        conn.commit()
        conn.close()

        flash("Password updated successfully!", "success")
        return redirect(url_for('index'))

    return render_template("reset_password.html")


# ==========================
# 🧩 SHIPMENTS ROUTES
# ==========================

@app.route("/get_customer/<customer_name>")
@login_required
def get_customer(customer_name):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fetch the latest shipment for this customer
    c.execute("""
        SELECT customer_email, customer_phone, origin, destination,
               container_number, bl_number, customer_address, vat_number
        FROM shipments
        WHERE customer_name = ?
        ORDER BY created_at DESC
        LIMIT 1
    """, (customer_name,))
    row = c.fetchone()
    conn.close()

    if row:
        return {
            "customer_email": row["customer_email"],
            "customer_phone": row["customer_phone"],
            "origin": row["origin"],
            "destination": row["destination"],
            "container_number": row["container_number"],
            "bl_number": row["bl_number"],
            "customer_address": row["customer_address"],
            "vat_number": row["vat_number"]
        }
    else:
        return {}
    
@app.route("/search_customers")
@login_required
def search_customers():
    query = request.args.get("q", "").strip()

    if not query:
        return {"customers": []}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fetch unique customer names starting with the typed letters
    c.execute("""
        SELECT DISTINCT customer_name 
        FROM shipments 
        WHERE customer_name LIKE ?
        ORDER BY customer_name ASC
        LIMIT 20
    """, (query + "%",))

    result = [row["customer_name"] for row in c.fetchall()]
    conn.close()

    return {"customers": result}


@app.route("/shipments")
@login_required
def shipments():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    sort = request.args.get("sort", "id")
    order = request.args.get("order", "desc")
    page = int(request.args.get("page", 1))
    per_page = 10
    offset = (page - 1) * per_page

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Access by column name
    c = conn.cursor()

    # --- Query columns in same order as CSV ---#New Col
    base_query = """
        SELECT id, tracking_number, customer_name, customer_email, customer_phone,
               origin, destination, weight, qty, shipment_date, est_delivery_date,
               shipping_cost, shipping_cost_desc, carrier, driver, status,
               customer_frt_cost, transport_cost, custom_clearance_cost,
               other_cost, other_cost_desc, other_cost_1, other_cost_1_desc, other_cost_2, other_cost_2_desc,
               other_cost_3, other_cost_3_desc, other_cost_4, other_cost_4_desc, shipment_vat, other_cost_vat,
               container_number, bl_number, customer_address, vat_number,
               consignee_name, consignee_address, customs_agent, comments, created_at
        FROM shipments
    """

    count_query = "SELECT COUNT(*) FROM shipments"

    params = []
    where_clauses = []

    if search:
        where_clauses.append(
        "(customer_name LIKE ? OR origin LIKE ? OR destination LIKE ? OR tracking_number LIKE ? "
        "OR container_number LIKE ? OR bl_number LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like])

    if status_filter:
        where_clauses.append("status = ?")
        params.append(status_filter)
        
    # Restrict "Delivered" visibility to admins only
    if not session.get('role') == 'admin':
        where_clauses.append("status != 'Delivered'")

    if where_clauses:
        where_sql = " WHERE " + " AND ".join(where_clauses)
        base_query += where_sql
        count_query += where_sql

    # --- Same sorting as CSV ---
    valid_sorts = {
        "id": "id",
        "customer": "customer_name",
        "date": "shipment_date",
        "status": "status"
    }
    sort_column = valid_sorts.get(sort, "id")
    base_query += f" ORDER BY {sort_column} {order.upper()}"
    base_query += f" LIMIT {per_page} OFFSET {offset}"

    c.execute(base_query, params)
    rows = c.fetchall()  # Row objects for dict-like access

    c.execute(count_query, params)
    total_records = c.fetchone()[0]
    conn.close()

    total_pages = (total_records + per_page - 1) // per_page
    statuses = ["Pending", "In Transit", "Delivered", "Cancelled"]

    # --- Calculate totals like in CSV ---
    shipments_list = []
    for s in rows:
        frt = float(s['customer_frt_cost'] or 0)
        transport = float(s['transport_cost'] or 0)
        clearance = float(s['custom_clearance_cost'] or 0)
        other = float(s['other_cost'] or 0)
        other1 = float(s['other_cost_1'] or 0)
        other2 = float(s['other_cost_2'] or 0)
        other3 = float(s['other_cost_3'] or 0)
        other4 = float(s['other_cost_4'] or 0)
        shipping_cost = float(s['shipping_cost'] or 0)

        total_expenses = frt + transport + clearance + other + other1 + other2 + other3 + other4
        profit_loss = shipping_cost - total_expenses

        shipment = dict(s)  # Convert Row to dict
        shipment['shipment_vat'] = f"{s['shipment_vat']}%" if s['shipment_vat'] else "0%"
        shipment['other_cost_vat'] = f"{s['other_cost_vat']}%" if s['other_cost_vat'] else "0%"

        shipment['total_expenses'] = total_expenses
        shipment['profit_loss'] = profit_loss
        shipment['created_at'] = s['created_at']
        shipments_list.append(shipment)

    return render_template(
        "shipments.html",
        shipments=shipments_list,
        search=search,
        status_filter=status_filter,
        statuses=statuses,
        page=page,
        total_pages=total_pages,
        sort=sort,
        order=order
    )

from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle

@app.route("/invoice/<int:shipment_id>")
@admin_required
def generate_invoice(shipment_id):
    import io, os, re, qrcode
    from datetime import datetime
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from bidi.algorithm import get_display
    import arabic_reshaper
    from num2words import num2words
    import sqlite3
    from flask import send_file

    # --- Fetch shipment ---
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,))
    shipment = c.fetchone()
    conn.close()

    if not shipment:
        return "Shipment not found", 404

    # --- Font setup ---
    font_path = os.path.join(os.path.dirname(__file__), "static", "fonts", "Amiri-Regular.ttf")
    if not os.path.exists(font_path):
        raise FileNotFoundError(f"Font file not found at {font_path}")
    pdfmetrics.registerFont(TTFont("Amiri", font_path))

    # --- Styles ---
    styles = getSampleStyleSheet()
    english = styles["Normal"]
    arabic = ParagraphStyle(name="Arabic", parent=english, fontName="Amiri", alignment=TA_RIGHT)

    from reportlab.lib.enums import TA_CENTER
    centered = ParagraphStyle(name="Centered", parent=styles["Normal"], alignment=TA_CENTER)

    from reportlab.lib.enums import TA_LEFT
    arabic_left = ParagraphStyle(name="ArabicLeft", parent=styles["Normal"], fontName="Amiri", fontSize=11, alignment=TA_LEFT)

    from reportlab.lib.enums import TA_RIGHT
    value_right = ParagraphStyle(name="ValueRight", parent=styles["Normal"], alignment=TA_RIGHT)

    from reportlab.lib.enums import TA_RIGHT
    footer_right = ParagraphStyle(name="FooterRight", parent=styles["Normal"], alignment=TA_RIGHT)

    from reportlab.lib.styles import ParagraphStyle

    from reportlab.platypus import HRFlowable
    from reportlab.lib import colors

    disclaimer_style = ParagraphStyle(
        name="DisclaimerStyle",
        parent=styles["Normal"],
        fontName="Amiri",       # Arabic-friendly font
        fontSize=6.5,             # Smaller font size
        leading=8,             # Line spacing
        alignment=TA_LEFT       # Needed for Arabic shaping
    )

    # --- Helper to fix Arabic text ---
    def arabic_text(text):
        reshaped_text = arabic_reshaper.reshape(str(text))
        bidi_text = get_display(reshaped_text)
        return bidi_text

    # --- Helper to convert amount to words ---
    def amount_in_words(amount):
        amount = round(float(amount), 2)
        riyal = int(amount)
        halalah = int(round((amount - riyal) * 100))
        words = num2words(riyal, lang='en').replace('-', ' ').replace(',', ',')
        result = f"{words.capitalize()} Riyal"
        if halalah > 0:
            halalah_words = num2words(halalah, lang='en').replace('-', ' ').replace(',', ',')
            result += f" and {halalah_words} Halalah"
        result += " only"
        return result

    # --- PDF setup ---
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm
    )
    elements = []

    # --- Header: logo + company info ---
    logo_path = "static/logo.jpg"
    logo = Image(logo_path, width=90, height=55)
    header_data = [
        [
            [
                logo,
                Paragraph("<b>R.D. LOGISTICS</b>", english),
                Paragraph("Malaz - P.O Box: 235738, Riyadh - 11332", english),
                Paragraph("Phone: +966112496476 | Email: shamsu@rdlogisticsksa.com", english),
                Paragraph("CR: 1010854762 | VAT: 310523650900003", english)
            ],
            [
                Spacer(1, 40),
                Paragraph(arabic_text("<b>ار دي لوجيستكس</b>"), arabic),
                Paragraph(arabic_text("ملز - ص.ب: 235738، الرياض - 11332"), arabic),
                Paragraph(arabic_text("هاتف: +966112496476 | بريد إلكتروني: shamsu@rdlogisticsksa.com"), arabic),
                Paragraph(arabic_text("س.ت: 1010854762 | ضريبة القيمة المضافة: 310523650900003"), arabic)
            ]
        ]
    ]
    elements.append(Table(header_data, colWidths=[270, 270], style=[("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(Spacer(1, 10))

    # --- Heading (Arabic + English side by side) ---
    
    heading_style = ParagraphStyle(
        name="HeadingStyle",
        parent=styles["Normal"],
        fontName="Amiri",
        fontSize=14,
        alignment=TA_CENTER
    )

    heading_text = Paragraph(
        "<b>TAX INVOICE / " + arabic_text("فاتورة ضريبية") + "</b>",
        heading_style
    )

    elements.append(heading_text)
    elements.append(Spacer(1, 10))


    # --- Customer + Invoice Info ---
    def get_field(field):
        return shipment[field] if field in shipment.keys() and shipment[field] else "-"

    customer_name = get_field("customer_name")
    customer_address = get_field("customer_address")
    customer_vat = get_field("vat_number")
    customer_phone = get_field("customer_phone")
    customer_email = get_field("customer_email")

    # Left column: Customer info
        # Left column: Customer info — "To / إلى :" on one line, name below
        # Create bilingual "To / إلى :" label without extra gap
   
    
    # Create a single bilingual label with controlled spacing
    to_label = Paragraph("To /&nbsp;&nbsp;" + arabic_text("إلى") + " :", disclaimer_style)

    # Build the customer box with proper commas
    customer_box = [
        [to_label],
        [Paragraph(f"<b>{customer_name}</b>", english)],
        [Paragraph(customer_address, english)],
        [Paragraph(f"VAT No: {customer_vat}", english)],
        [Paragraph(f"Phone: {customer_phone}", english)],
        [Paragraph(f"Email: {customer_email}", english)]
    ]

    customer_table = Table(customer_box, colWidths=[250])
    customer_table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 0.5, colors.grey),           # match invoice box border
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 4),                  # match invoice box padding
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("ALIGN", (0,0), (-1,-1), "LEFT")
    ]))



    # Right column: Invoice info
    info_box = [
        [Paragraph("Invoice No / " + arabic_text("رقم الفاتورة:"), arabic_left),
         Paragraph(f"IN{datetime.now().year}{shipment['id']:04}", value_right)],
        [Paragraph("Invoice Date / " + arabic_text("تاريخ الفاتورة:"), arabic_left),
         Paragraph(datetime.now().strftime('%d-%m-%Y'), value_right)],
        [Paragraph("Origin / " + arabic_text("المنشأ:"), arabic_left),
         Paragraph(shipment['origin'], value_right)],
        [Paragraph("Destination / " + arabic_text("الوجهة:"), arabic_left),
         Paragraph(shipment['destination'], value_right)],
        [Paragraph("BL No / " + arabic_text("بوليصة الشحن:"), arabic_left),
         Paragraph(shipment['bl_number'] or "-", value_right)],
        [Paragraph("Container No / " + arabic_text("رقم الحاوية:"), arabic_left),
         Paragraph(shipment['container_number'] or "-", value_right)]
    ]
    info_table = Table(info_box, colWidths=[130, 130])
    info_table.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN", (0,0), (0,-1), "LEFT"),
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4)
    ]))

    # Combine both (Customer left, Invoice right)
    combined_table = Table(
        [[customer_table, info_table]],
        colWidths=[260, 270]
    )
    combined_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    elements.append(combined_table)
    elements.append(Spacer(1, 12))


    # --- Invoice details table ---
    def extract(value):
        return float(re.search(r"[\d.]+", str(value or "0")).group()) if re.search(r"[\d.]+", str(value or "0")) else 0

    ship = extract(shipment['shipping_cost'])
    other1 = extract(shipment['other_cost_1'])
    other2 = extract(shipment['other_cost_2'])
    other3 = extract(shipment['other_cost_3'])
    ship_vat_rate = float(shipment['shipment_vat']) / 100
    other_vat_rate = float(shipment['other_cost_vat']) / 100
    vat_ship = ship * ship_vat_rate
    vat_other1 = other1 * other_vat_rate
    vat_other2 = other2 * other_vat_rate
    vat_other3 = other3 * other_vat_rate

    total_vat = vat_ship + vat_other1 + vat_other2 + vat_other3
    subtotal = ship + other1 + other2 + other3
    grand_total = subtotal + total_vat

    rows = [
        [
            Paragraph(arabic_text("Description / الوصف"), arabic),
            Paragraph(arabic_text("Comments / الملاحظات"), arabic),
            Paragraph(arabic_text("Qty / الكمية"), arabic),
            Paragraph(arabic_text("Amount (SAR) / المبلغ"), arabic),
            Paragraph(arabic_text("VAT% / ٪ ضريبة"), arabic),
            Paragraph(arabic_text("Total (SAR) / الإجمالي"), arabic)
        ],
        ["Shipping Cost", shipment['shipping_cost_desc'] or "", shipment['qty'] or "", f"{ship:.2f}", f"{shipment['shipment_vat']}%", f"{ship + (ship * float(shipment['shipment_vat'] or 0)/100):.2f}"],
        ["Other Cost 1", shipment['other_cost_1_desc'] or "", "", f"{other1:.2f}", f"{shipment['other_cost_vat']}%", f"{other1 + (other1 * float(shipment['other_cost_vat'] or 0)/100):.2f}"],
        ["Other Cost 2", shipment['other_cost_2_desc'] or "", "", f"{other2:.2f}", f"{shipment['other_cost_vat']}%", f"{other2 + (other2 * float(shipment['other_cost_vat'] or 0)/100):.2f}"],
        ["Other Cost 3", shipment['other_cost_3_desc'] or "", "", f"{other3:.2f}", f"{shipment['other_cost_vat']}%", f"{other3 + (other3 * float(shipment['other_cost_vat'] or 0)/100):.2f}"]
    ]


    table = Table(rows, colWidths=[100, 140, 50, 80, 60, 80])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightblue),
        ("ALIGN", (2, 1), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Amiri")
    ]))
    elements.append(table)
    elements.append(Spacer(1, 12))

    # --- Summary ---


    summary = [
        [Paragraph("Total excl VAT / " + arabic_text("الإجمالي بدون ضريبة:"), arabic_left),
        Paragraph(f"SAR {subtotal:.2f}", value_right)],
        [Paragraph("Value Added Tax / " + arabic_text("ضريبة القيمة المضافة:"), arabic_left),
        Paragraph(f"SAR {total_vat:.2f}", value_right)],
        [Paragraph("Total with VAT / " + arabic_text("الإجمالي مع ضريبة:"), arabic_left),
        Paragraph(f"SAR {grand_total:.2f}", value_right)]
    ]

    summary_table = Table(summary, colWidths=[370, 140])
    summary_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke)
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"SAR - {amount_in_words(grand_total)}", centered))
    elements.append(Spacer(1, 15))

    # --- QR + BANK INFO BOX (inline Arabic after English, left-aligned) ---
    qr_img = qrcode.make(f"Invoice:{shipment['id']}|Total:{grand_total:.2f}|Date:{datetime.now().strftime('%d-%m-%Y')}")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf)
    qr_buf.seek(0)
    qr = Image(qr_buf, width=70, height=70)

    # Build a single-row header with three columns: EN label | small spacer | AR label (left aligned)
    bank_header = Table(
        [[Paragraph("<b>Bank Details / " + arabic_text("تفاصيل البنك") + "</b>", arabic_left)]],
        colWidths=[400]
    )
    
    bank_header.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (0,0), (0,0), "LEFT"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))

    # The actual bank lines (under the header)
    bank_lines = [
        [Paragraph("Name: THE SAUDI NATIONAL BANK", english)],
        [Paragraph("Account No: 01400017034808", english)],
        [Paragraph("SWIFT Code: SA2910000001400017034808", english)],
        [Paragraph("Account Holder: RAMAL DANIA LOGISTICS", english)]
    ]
    bank_info_table = Table(bank_lines, colWidths=[400])
    bank_info_table.setStyle(TableStyle([
        ("ALIGN", (0,0), (-1,-1), "LEFT"),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
    ]))

    # Stack header above the info lines
    bank_combined = Table([[bank_header], [bank_info_table]], colWidths=[400])
    bank_combined.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
    ]))

    # Combine with QR on the left (as before)
    elements.append(
        Table([[qr, bank_combined]], colWidths=[80, 440], style=[
            ("BOX", (0,0), (-1,-1), 0.75, colors.black),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("ALIGN", (1,0), (1,-1), "LEFT"),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6)
        ])
    )
    # Add vertical space to push the footer block dow
    elements.append(Spacer(1, 40)) 

    footer_lines_left = [
        "This is computer generated invoice doesn't require signature or stamp. / " +
        arabic_text("هذا الفاتورة مولدة آلياً ولا تحتاج لتوقيع أو ختم."),
        "Any discrepancy must be notified within 7 days from the date of invoice. / " +
        arabic_text("يجب الإبلاغ عن أي اختلاف خلال 7 أيام من تاريخ الفاتورة.")
    ]  

    # Define a left-aligned style for Arabic rendering
    footer_mixed = ParagraphStyle(
        name="FooterMixed",
        parent=styles["Normal"],
        fontName="Amiri",
        fontSize=7,
        alignment=TA_LEFT  # Needed for Arabic shaping
    )

    # Paragraph style for footer right
    footer_right_style = ParagraphStyle(
        name="FooterRight",
        parent=styles["Normal"],
        fontName="Amiri",
        fontSize=6.5,
        leading=8,
        alignment=TA_RIGHT  # align text to right
    )

    from reportlab.lib.units import mm

    # Create the bilingual contact lines
    footer_right_lines = [
        Paragraph("Print Date: " + datetime.now().strftime('%d-%m-%Y %I:%M %p'), footer_right_style),
        Paragraph("Email / " + arabic_text("البريد الإلكتروني") + ": shamsu@rdlogisticsksa.com", footer_right_style),
        Paragraph("Phone / " + arabic_text("الهاتف") + ": +966112496476", footer_right_style)
    ]

    # Build a two-column table: left column empty, right column contains content
    footer_table = Table(
        [[Spacer(1,0), line] for line in footer_right_lines],
        colWidths=[1*mm, 180*mm],  # ensure two numbers for 2 columns
        hAlign='RIGHT'             # right-align table as a whole
    )

    footer_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0)
    ]))


    # Add left-aligned bilingual lines
    for line in footer_lines_left:
        elements.append(Paragraph(line, disclaimer_style))  

     # Add to document
    elements.append(Spacer(1, 10))
    elements.append(footer_table)


    # --- Build PDF ---
    pdf.build(elements)
    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"invoice_IN{datetime.now().year}{shipment['id']:04}.pdf"
    )

@app.route('/export_csv') ##New Col
@admin_required
def export_csv():
    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()
    sort = request.args.get('sort', 'id')
    order = request.args.get('order', 'desc')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Access columns by name
    c = conn.cursor()

    # --- Base query ---
    base_query = """
        SELECT id, tracking_number, customer_name, customer_email, customer_phone,
               origin, destination, weight, qty, shipment_date, est_delivery_date,
               shipping_cost,shipping_cost_desc, carrier, driver, status,
               customer_frt_cost, transport_cost, custom_clearance_cost,
               other_cost, other_cost_desc, other_cost_1, other_cost_1_desc, other_cost_2, other_cost_2_desc,
               other_cost_3, other_cost_3_desc, other_cost_4, other_cost_4_desc, shipment_vat, other_cost_vat,
               container_number, bl_number, customer_address, vat_number,
               consignee_name, consignee_address, customs_agent, comments, created_at
        FROM shipments
    """
    params = []
    where_clauses = []

    if search:
        where_clauses.append(
            "(customer_name LIKE ? OR origin LIKE ? OR destination LIKE ? OR tracking_number LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like])

    if status_filter:
        where_clauses.append("status = ?")
        params.append(status_filter)

    if where_clauses:
        base_query += " WHERE " + " AND ".join(where_clauses)

    # Apply same sorting as shipment list
    valid_sorts = {
        "id": "id",
        "customer": "customer_name",
        "date": "shipment_date",
        "status": "status"
    }
    sort_column = valid_sorts.get(sort, "id")
    base_query += f" ORDER BY {sort_column} {order.upper()}"

    c.execute(base_query, params)
    rows = c.fetchall()
    conn.close()

    from io import StringIO, BytesIO
    import csv

    si = StringIO()
    writer = csv.writer(si)

    # --- Headers exactly as in shipment.html ---
    headers = [
        "ID", "Tracking Number", "Created At", "Customer Name", "Email", "Phone",
        "Origin", "Destination", "Weight (kg)", "Qty", "Shipment Date", "Estimated Delivery",
        "Shipment Cost (SAR)", "Shipping Cost Desc",  "Carrier", "Freight Cost (SAR)", "Driver", "Transport Cost (SAR)", 
         "Customs_agent", "Custom Clearance Cost (SAR)",
        "Other Cost (SAR)","Other Cost Desc", "Other Cost 1 (SAR)", "Other Cost 1 Desc", "Other Cost 2 (SAR)", "Other Cost 2 Desc", 
        "Other Cost 3 (SAR)", "Other Cost 3 Desc", "Other Cost 4 (SAR)","Other Cost 4 Desc", "Shipment Vat", "Other Cost Vat",
        "Container Number", "BL Number", "Customer Address", "VAT Number",
        "Consignee Name", "Consignee Address", "Status", "Comments",
        "Total Expenses (SAR)", "Profit/Loss (SAR)"
    ]
    writer.writerow(headers)

    # --- Write rows in same order ---
    for s in rows:
        frt = float(s['customer_frt_cost'] or 0)
        transport = float(s['transport_cost'] or 0)
        clearance = float(s['custom_clearance_cost'] or 0)
        other = float(s['other_cost'] or 0)
        other1 = float(s['other_cost_1'] or 0)
        other2 = float(s['other_cost_2'] or 0)
        other3 = float(s['other_cost_3'] or 0)
        other4 = float(s['other_cost_4'] or 0)
        shipping_cost = float(s['shipping_cost'] or 0)

        total_expenses = frt + transport + clearance + other + other1 + other2 + other3 + other4
        profit_loss = shipping_cost - total_expenses

        safe_row = [
            s['id'],
            s['tracking_number'],
            s['created_at'],
            s['customer_name'],
            s['customer_email'],
            s['customer_phone'],
            s['origin'],
            s['destination'],
            s['weight'],
            s['qty'],
            s['shipment_date'],
            s['est_delivery_date'],
            f"{shipping_cost:.2f}",
            s['shipping_cost_desc'],
            s['carrier'],
            f"{frt:.2f}",
            s['driver'],
            f"{transport:.2f}",
            s['customs_agent'],
            f"{clearance:.2f}",
            f"{other:.2f}",
            s['other_cost_desc'],
            f"{other1:.2f}",
            s['other_cost_1_desc'],
            f"{other2:.2f}",
            s['other_cost_2_desc'],
            f"{other3:.2f}",
            s['other_cost_3_desc'],
            f"{other4:.2f}",
            s['other_cost_4_desc'],
            f"{s['shipment_vat']}%" if s['shipment_vat'] else "0%",
            f"{s['other_cost_vat']}%" if s['other_cost_vat'] else "0%",
            s['container_number'],
            s['bl_number'],
            s['customer_address'],
            s['vat_number'],
            s['consignee_name'],
            s['consignee_address'],
            s['status'],
            s['comments'],
            f"{total_expenses:.2f}",
            f"{profit_loss:.2f}" 
        ]

        writer.writerow(safe_row)

    si.seek(0)
    output = BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)

    filename = f"shipments_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name=filename)


@app.route('/delete/<int:id>')
@admin_required
def delete(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM shipments WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('shipments'))

@app.route('/edit/<int:shipment_id>')
@login_required
def edit(shipment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,))
    shipment = c.fetchone()
    conn.close()

    if not shipment:
        flash("Shipment not found.", "danger")
        return redirect(url_for('shipments'))

    return render_template("edit.html", shipment=shipment)


@app.route('/update/<int:id>', methods=['POST'])
@login_required
def update(id):
    # --- Basic shipment info ---
    customer_name = request.form.get('customer_name', '').strip()
    customer_email = request.form.get('customer_email', '').strip()
    customer_phone = request.form.get('customer_phone', '').strip()
    origin = request.form.get('origin', '').strip()
    destination = request.form.get('destination', '').strip()
    weight = safe_float(request.form.get('weight'))
    qty = request.form.get('qty', '').strip()
    shipping_cost = safe_float(request.form.get('shipping_cost'))  # manual input
    shipping_cost_desc = request.form.get('shipping_cost_desc', '').strip()
    est_delivery_date = request.form.get('est_delivery_date', '').strip()  # manual input
    carrier = request.form.get('carrier', '').strip()
    driver = request.form.get('driver', '').strip()
    status = request.form.get('status', 'Pending').strip()
    tracking_number = request.form.get('tracking_number', '').strip()

    # --- Cost fields ---
    customer_frt_cost = safe_float(request.form.get('customer_frt_cost'))
    transport_cost = safe_float(request.form.get('transport_cost'))
    custom_clearance_cost = safe_float(request.form.get('custom_clearance_cost'))
    other_cost = safe_float(request.form.get('other_cost'))
    other_cost_desc = request.form.get('other_cost_desc', '').strip() #New col
    other_cost_1 = safe_float(request.form.get('other_cost_1'))
    other_cost_1_desc = request.form.get('other_cost_1_desc', '').strip()
    other_cost_2 = safe_float(request.form.get('other_cost_2'))
    other_cost_2_desc = request.form.get('other_cost_2_desc', '').strip()
    other_cost_3 = safe_float(request.form.get('other_cost_3'))
    other_cost_3_desc = request.form.get('other_cost_3_desc', '').strip()
    other_cost_4 = safe_float(request.form.get('other_cost_4'))
    other_cost_4_desc = request.form.get('other_cost_4_desc', '').strip()
    shipment_vat = request.form.get("shipment_vat", "0").strip()
    other_cost_vat = request.form.get("other_cost_vat", "0").strip()

    # --- Additional info ---
    container_number = request.form.get('container_number', '').strip()
    bl_number = request.form.get('bl_number', '').strip()
    customer_address = request.form.get('customer_address', '').strip()
    vat_number = request.form.get('vat_number', '').strip()
    consignee_name = request.form.get('consignee_name', '').strip()
    consignee_address = request.form.get('consignee_address', '').strip()
    customs_agent = request.form.get('customs_agent', '').strip()
    comments = request.form.get('comments', '').strip()

    # --- Update DB --- #New Col
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE shipments
        SET customer_name=?, customer_email=?, customer_phone=?,
            origin=?, destination=?, weight=?, qty=?,
            shipping_cost=?, shipping_cost_desc=?, est_delivery_date=?, customer_frt_cost=?, transport_cost=?,
            custom_clearance_cost=?, other_cost=?, other_cost_desc=?, other_cost_1=?, other_cost_1_desc=?, 
            other_cost_2=?, other_cost_2_desc=?, other_cost_3=?, other_cost_3_desc=?, other_cost_4=?, other_cost_4_desc=?, 
            shipment_vat=?, other_cost_vat=?, 
            container_number=?, bl_number=?, customer_address=?, vat_number=?,
            consignee_name=?, consignee_address=?, customs_agent=?,
            carrier=?, driver=?, status=?,  comments=?, tracking_number=?
        WHERE id=?
    """, (
        customer_name, customer_email, customer_phone,
        origin, destination, weight, qty,
        shipping_cost, shipping_cost_desc, est_delivery_date, customer_frt_cost, transport_cost,
        custom_clearance_cost, other_cost, other_cost_desc, other_cost_1, other_cost_1_desc,
        other_cost_2,  other_cost_2_desc, other_cost_3,  other_cost_3_desc, other_cost_4,  other_cost_4_desc, 
        shipment_vat, other_cost_vat, container_number, bl_number, customer_address, vat_number,
        consignee_name, consignee_address, customs_agent,
        carrier, driver, status, comments, tracking_number,
        id
    ))
    conn.commit()
    conn.close()

    return redirect(url_for('shipments'))

# --- LOGIN / LOGOUT ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, password_hash, role FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        conn.close()

        if user and check_password(password, user[1]):
            session['user_id'] = user[0]
            session['username'] = username
            session['role'] = user[2]
            flash("Login successful!", "success")
            return redirect(url_for('index'))
        else:
            flash("Invalid username or password", "danger")
            return render_template('login.html')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

if __name__ == '__main__':
    # Ensure DB and tables exist
    init_db()             # create/upgrade shipments table
    init_users_table()    # create users table if missing

    # Create initial admin only if not present (change the password immediately)
    created = create_initial_admin(username='admin', password='ChangeMeNow!')
    if created:
        print("Initial admin account created: username='admin' (please change password)")
    else:
        print("Admin user already exists.")

    app.run(debug=True)