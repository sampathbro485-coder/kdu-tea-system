import os
import sqlite3
import smtplib
import csv
import secrets
import base64
import json
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import pandas as pd
import io

app = Flask(__name__)
app.secret_key = 'kdu_tea_factory_super_secret_key_change_me'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['ALLOWED_EXTENSIONS'] = {'csv', 'xlsx', 'xls'}
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'කරුණාකර ප්‍රවේශ වීමට පිවිසෙන්න.'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('scans', exist_ok=True)
os.makedirs('reports', exist_ok=True)

# ------------------- CONTEXT PROCESSOR -------------------
@app.context_processor
def utility_processor():
    def user_has_email_permission():
        if not current_user.is_authenticated:
            return False
        if current_user.role == 'superadmin':
            return True
        conn = sqlite3.connect('database.db')
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM user_permissions WHERE user_id = ? AND can_send_email = 1', (current_user.id,))
        count = cur.fetchone()[0]
        conn.close()
        return count > 0
    return dict(show_email_settings=user_has_email_permission())

# ------------------- LOGO HANDLER -------------------
def get_logo_cid():
    logo_path = os.path.join('static', 'logo.png')
    if os.path.exists(logo_path):
        return 'kdu_logo_cid'
    return None

def get_logo_image_part():
    logo_path = os.path.join('static', 'logo.png')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            img = MIMEImage(f.read(), _subtype='png')
            img.add_header('Content-ID', '<kdu_logo_cid>')
            img.add_header('Content-Disposition', 'inline')
            return img
    return None

# ------------------- VARIANCE LEVEL HELPER -------------------
def get_variance_level(glx):
    if glx is None or glx == '':
        glx = 0
    try:
        abs_val = abs(float(glx))
    except (ValueError, TypeError):
        abs_val = 0
    if abs_val <= 3:
        return ("Low", "success")
    elif abs_val <= 7:
        return ("Medium", "warning")
    else:
        return ("High", "danger")

app.jinja_env.globals.update(get_variance_level=get_variance_level)

# ------------------- FACTORY CONFIGURATION -------------------
FACTORIES = {
    'GTF': {'name': 'Galpaditanna Tea Factory', 'expected_lipe': 78.5, 'tolerance': 1.2, 'variance_threshold': 5.0},
    'KLCC': {'name': 'Kalawana leaves Collection Center', 'expected_lipe': 80.2, 'tolerance': 1.2, 'variance_threshold': 5.0},
    'PVTF': {'name': 'Peak View Tea Factory', 'expected_lipe': 76.8, 'tolerance': 1.2, 'variance_threshold': 5.0},
    'MTF': {'name': 'Matuwagala Tea Factory', 'expected_lipe': 79.1, 'tolerance': 1.2, 'variance_threshold': 5.0},
    'KTF': {'name': 'Kuttapitiya Tea Factory', 'expected_lipe': 77.4, 'tolerance': 1.2, 'variance_threshold': 5.0},
    'MPTF': {'name': 'Madampe Tea Factory', 'expected_lipe': 81.0, 'tolerance': 1.2, 'variance_threshold': 5.0}
}

# ------------------- DATABASE -------------------
def init_db():
    conn = sqlite3.connect('database.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        full_name TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT
    )''')
    try: conn.execute('ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1')
    except: pass
    try: conn.execute('ALTER TABLE users ADD COLUMN full_name TEXT')
    except: pass

    conn.execute('''CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        factory_code TEXT,
        report_date TEXT,
        excel_file TEXT,
        scanned_pdf TEXT,
        sent_to TEXT,
        status TEXT,
        timestamp TEXT
    )''')
    try: conn.execute('ALTER TABLE reports ADD COLUMN user_id INTEGER')
    except: pass

    conn.execute('''CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        api_key TEXT UNIQUE NOT NULL,
        name TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        last_used TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        factory_code TEXT NOT NULL,
        can_upload_tpms INTEGER DEFAULT 0,
        can_upload_pdf INTEGER DEFAULT 0,
        can_send_email INTEGER DEFAULT 0,
        UNIQUE(user_id, factory_code)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS mismatch_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        factory_code TEXT NOT NULL,
        report_date TEXT NOT NULL,
        route_name TEXT NOT NULL,
        glx_value REAL,
        pdf_uploaded INTEGER DEFAULT 0,
        pdf_path TEXT,
        uploaded_by INTEGER,
        uploaded_at TEXT,
        reason TEXT,
        UNIQUE(factory_code, report_date, route_name)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_email_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        display_name TEXT,
        email_address TEXT,
        email_password TEXT,
        incoming_server TEXT,
        incoming_port INTEGER,
        incoming_protocol TEXT,
        outgoing_server TEXT,
        outgoing_port INTEGER,
        updated_by INTEGER,
        updated_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS factory_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        factory_code TEXT NOT NULL,
        route_name TEXT NOT NULL,
        created_by INTEGER,
        created_at TEXT,
        UNIQUE(factory_code, route_name)
    )''')
    for tbl_col in [
        ('mismatch_routes', 'pdf_path TEXT'),
        ('mismatch_routes', 'uploaded_by INTEGER'),
        ('mismatch_routes', 'uploaded_at TEXT'),
        ('mismatch_routes', 'reason TEXT'),
        ('user_email_settings', 'display_name TEXT'),
        ('user_email_settings', 'incoming_protocol TEXT'),
        ('user_email_settings', 'email_address TEXT'),
        ('user_email_settings', 'email_password TEXT'),
        ('user_email_settings', 'incoming_server TEXT'),
        ('user_email_settings', 'incoming_port INTEGER'),
        ('user_email_settings', 'outgoing_server TEXT'),
        ('user_email_settings', 'outgoing_port INTEGER'),
        ('user_email_settings', 'updated_by INTEGER'),
        ('user_email_settings', 'updated_at TEXT'),
        ('factory_routes', 'created_by INTEGER'),
        ('factory_routes', 'created_at TEXT')
    ]:
        try:
            conn.execute(f"ALTER TABLE {tbl_col[0]} ADD COLUMN {tbl_col[1]}")
        except:
            pass
    conn.close()

init_db()

def log_activity(user_id, action, details=''):
    conn = sqlite3.connect('database.db')
    conn.execute('INSERT INTO user_activities (user_id, action, details, timestamp) VALUES (?,?,?,?)',
                 (user_id, action, details, datetime.now().isoformat()))
    conn.commit()
    conn.close()

class User(UserMixin):
    def __init__(self, id, username, role, is_active):
        self.id = id; self.username = username; self.role = role; self._is_active = is_active
    @property
    def is_active(self): return self._is_active
    @is_active.setter
    def is_active(self, value): self._is_active = value

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT id, username, role, is_active FROM users WHERE id = ?', (user_id,))
    user = cur.fetchone()
    conn.close()
    if user and user[3]==1:
        return User(user[0], user[1], user[2], user[3])
    return None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_user_permissions(user_id, factory_code):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT can_upload_tpms, can_upload_pdf, can_send_email FROM user_permissions WHERE user_id = ? AND factory_code = ?', (user_id, factory_code))
    row = cur.fetchone()
    conn.close()
    if row:
        return {'tpms': bool(row[0]), 'pdf': bool(row[1]), 'email': bool(row[2])}
    return {'tpms': False, 'pdf': False, 'email': False}

def user_can_upload_tpms(user, factory_code):
    if user.role == 'superadmin': return True
    return get_user_permissions(user.id, factory_code)['tpms']

def user_can_upload_pdf(user, factory_code):
    if user.role == 'superadmin': return True
    return get_user_permissions(user.id, factory_code)['pdf']

def user_can_send_email(user, factory_code):
    if user.role == 'superadmin': return True
    return get_user_permissions(user.id, factory_code)['email']

def get_user_allowed_factories(user_id):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT factory_code FROM user_permissions WHERE user_id = ? AND (can_upload_tpms=1 OR can_upload_pdf=1 OR can_send_email=1)', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows] if rows else []

def set_user_permission(user_id, factory_code, tpms, pdf, email):
    conn = sqlite3.connect('database.db')
    conn.execute('''INSERT OR REPLACE INTO user_permissions (user_id, factory_code, can_upload_tpms, can_upload_pdf, can_send_email)
                    VALUES (?, ?, ?, ?, ?)''', (user_id, factory_code, 1 if tpms else 0, 1 if pdf else 0, 1 if email else 0))
    conn.commit()
    conn.close()

# ------------------- EMAIL CONFIG -------------------
EMAIL_CONFIG = {
    'smtp_server': 'mail.kdugroup.com',
    'smtp_port': 587,
    'sender_email': 'pasindus@kdugroup.com',
    'sender_password': 'Pasindu@2345',
    'recipient_email': 'sampathbro485@gmail.com'
}

def get_user_email_settings(user_id):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('''SELECT display_name, email_address, email_password, incoming_server, incoming_port, 
                          incoming_protocol, outgoing_server, outgoing_port 
                   FROM user_email_settings WHERE user_id = ?''', (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            'display_name': row[0],
            'email_address': row[1],
            'password': row[2],
            'incoming_server': row[3],
            'incoming_port': row[4],
            'incoming_protocol': row[5],
            'outgoing_server': row[6],
            'outgoing_port': row[7]
        }
    return None

def set_user_email_settings(user_id, display_name, email_address, password, incoming_server, incoming_port, 
                            incoming_protocol, outgoing_server, outgoing_port, admin_id):
    conn = sqlite3.connect('database.db')
    conn.execute('''INSERT OR REPLACE INTO user_email_settings 
        (user_id, display_name, email_address, email_password, incoming_server, incoming_port, 
         incoming_protocol, outgoing_server, outgoing_port, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, display_name, email_address, password, incoming_server, incoming_port, 
         incoming_protocol, outgoing_server, outgoing_port, admin_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_date_format():
    return datetime.now().strftime('%Y.%m.%d')

def generate_filename(factory_code, ext, route_name=None):
    date_str = get_date_format()
    if route_name:
        safe_route = secure_filename(route_name).replace(' ', '_')
        return f"{factory_code}_{date_str}_{safe_route}.{ext}"
    else:
        return f"{factory_code}_{date_str}.{ext}"

def generate_tpms_filename(factory_code, report_date):
    factory_name = FACTORIES.get(factory_code, {}).get('name', factory_code)
    safe_name = secure_filename(factory_name).replace(' ', '_')
    date_obj = datetime.strptime(report_date, '%Y-%m-%d')
    date_str = date_obj.strftime('%Y.%m.%d')
    return f"{safe_name}_{date_str}"

def generate_pdf_filename(factory_code, report_date, route_name):
    safe_route = secure_filename(route_name).replace(' ', '_')
    date_obj = datetime.strptime(report_date, '%Y-%m-%d')
    date_str = date_obj.strftime('%Y.%m.%d')
    return f"{factory_code}_{date_str}_{safe_route}.pdf"

# ------------------- CREATE DEFAULT USERS -------------------
def create_default_users():
    default_users = [
        ('superadmin', 'admin123', 'superadmin', 'System Administrator'),
        ('Pasindu', '12345', 'user', 'Pasindu'),
        ('Ruchira', '12345', 'user', 'Ruchira'),
        ('Dulanga', '12345', 'user', 'Dulanga'),
        ('Niluka', '12345', 'user', 'Niluka'),
        ('Malinda', '12345', 'user', 'Malinda'),
        ('Bimsara', '12345', 'user', 'Bimsara'),
        ('Madampe', '12345', 'user', 'Madampe')
    ]
    conn = sqlite3.connect('database.db')
    for un, pw, role, full in default_users:
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE username = ?', (un,))
        if not cur.fetchone():
            hashed = generate_password_hash(pw)
            conn.execute('INSERT INTO users (username, password, role, full_name, is_active, created_at) VALUES (?,?,?,?,?,?)',
                         (un, hashed, role, full, 1, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    for fac in FACTORIES:
        set_user_permission(1, fac, True, True, True)

create_default_users()

# ------------------- EMAIL SENDING FUNCTION -------------------
def send_email_with_multiple_attachments(recipient, factory_code, tpms_path, pdf_paths, report_date):
    user_settings = get_user_email_settings(current_user.id)
    if user_settings and user_settings.get('outgoing_server'):
        smtp_server = user_settings['outgoing_server']
        smtp_port = user_settings['outgoing_port']
        sender_email = user_settings['email_address']
        sender_password = user_settings['password']
        display_name = user_settings.get('display_name')
        if display_name:
            sender = f"{display_name} <{sender_email}>"
            sender_full_name = display_name
        else:
            sender = sender_email
            sender_full_name = current_user.full_name or current_user.username
    else:
        smtp_server = EMAIL_CONFIG['smtp_server']
        smtp_port = EMAIL_CONFIG['smtp_port']
        sender_email = EMAIL_CONFIG['sender_email']
        sender_password = EMAIL_CONFIG['sender_password']
        display_name = current_user.full_name or current_user.username
        sender = f"{display_name} <{sender_email}>"
        sender_full_name = display_name
    
    if not recipient:
        recipient = EMAIL_CONFIG['recipient_email']
    
    factory_name = FACTORIES.get(factory_code, {}).get('name', factory_code)
    logo_cid = get_logo_cid()
    logo_part = get_logo_image_part()
    sender_email_addr = sender_email
    
    subject = f"KDU Tag Mismatch & Variance Report - {factory_code} - {report_date}"
    
    html_body = f"""
    <html>
    <head><style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.5; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 15px; }}
        .header {{ text-align: center; border-bottom: 2px solid #2E7D32; padding-bottom: 8px; }}
        .logo {{ max-width: 100px; margin-bottom: 8px; }}
        .content {{ padding: 15px 0; }}
        .signature {{ margin-top: 20px; padding-top: 12px; border-top: 1px solid #ddd; font-size: 13px; color: #555; }}
        .signature strong {{ color: #2E7D32; }}
        .footer {{ font-size: 11px; color: #999; text-align: center; margin-top: 15px; }}
        .sender-name {{ font-size: 14px; font-weight: 600; color: #1B5E20; }}
    </style></head>
    <body>
        <div class="container">
            <div class="header">
                {f'<img src="cid:{logo_cid}" class="logo" alt="KDU Logo">' if logo_cid else '<h3>KDU Group - IT Division</h3>'}
            </div>
            <div class="content">
                <p>Dear Sir,</p>
                <p>Attached please find the <strong>Tag Mismatch & Variance Report</strong> and scanned leaf collector remarks for:</p>
                <p><strong>{factory_name} ({factory_code})</strong><br>Report Date: <strong>{report_date}</strong></p>
                <p>Attachments:</p>
                <ul>
                    <li>TPMS Report: {os.path.basename(tpms_path)}</li>
                    {''.join([f'<li>Scanned PDF: {os.path.basename(pdf_path)} (Route: {route_name})</li>' for pdf_path, route_name in pdf_paths if pdf_path])}
                </ul>
                <p>Should you require any further information, please contact us.</p>
            </div>
            <div class="signature">
                <div class="sender-name">{sender_full_name}</div>
                <strong>KDU Group – IT Division</strong><br>
                Email: {sender_email_addr} | Web: www.kdugroup.com<br>
                <em>Precision Lipe Analytics™</em>
            </div>
            <div class="footer">
                This is an automatically generated email. Please do not reply directly.
            </div>
        </div>
    </body>
    </html>
    """
    
    text_body = f"""Dear Sir,

Attached please find the Tag Mismatch & Variance Report and scanned leaf collector remarks for:
{factory_name} ({factory_code})
Report Date: {report_date}

Attachments:
- TPMS Report: {os.path.basename(tpms_path)}
{''.join([f'- Scanned PDF: {os.path.basename(pdf_path)} (Route: {route_name})\n' for pdf_path, route_name in pdf_paths if pdf_path])}

Regards,
{sender_full_name}
KDU Group – IT Division
Email: {sender_email_addr} | Web: www.kdugroup.com
"""
    
    msg = MIMEMultipart('related')
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    
    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(text_body, 'plain'))
    alt.attach(MIMEText(html_body, 'html'))
    msg.attach(alt)
    
    if logo_part:
        msg.attach(logo_part)
    
    with open(tpms_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(tpms_path)}')
        msg.attach(part)
    
    for pdf_path, route_name in pdf_paths:
        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                part = MIMEBase('application', 'pdf')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(pdf_path)}')
                msg.attach(part)
    
    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
    except Exception as ssl_err:
        try:
            server = smtplib.SMTP(smtp_server, 587)
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
            server.quit()
        except Exception as fallback_err:
            raise Exception(f"SSL failed: {ssl_err}, STARTTLS failed: {fallback_err}")

# ------------------- AUTH ROUTES (AJAX Enabled) -------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        remember = True if request.form.get('remember') else False
        conn = sqlite3.connect('database.db')
        cur = conn.cursor()
        cur.execute('SELECT id, username, password, role, is_active FROM users WHERE username = ?', (username,))
        user = cur.fetchone()
        conn.close()
        if user and user[4]==1 and check_password_hash(user[2], password):
            login_user(User(user[0], user[1], user[3], user[4]), remember=remember)
            log_activity(user[0], 'LOGIN', 'logged in')
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'redirect': url_for('index')})
            else:
                flash('පිවිසුම සාර්ථකයි!', 'success')
                return redirect(url_for('index'))
        else:
            error_msg = 'පරිශීලක නම හෝ මුරපදය වැරදියි, හෝ ගිණුම අක්‍රියයි.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': error_msg}), 401
            else:
                flash(error_msg, 'danger')
                return render_template('login.html')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    log_activity(current_user.id, 'LOGOUT', 'logged out')
    logout_user()
    session.clear()
    flash('ඔබ ඉවත් විය.', 'info')
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET','POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old = request.form['old_password']
        new = request.form['new_password']
        confirm = request.form['confirm_password']
        conn = sqlite3.connect('database.db')
        cur = conn.cursor()
        cur.execute('SELECT password FROM users WHERE id = ?', (current_user.id,))
        db_pwd = cur.fetchone()[0]
        if not check_password_hash(db_pwd, old):
            flash('පැරණි මුරපදය වැරදියි.', 'danger')
        elif new != confirm:
            flash('නව මුරපද සමාන නැත.', 'danger')
        elif len(new) < 4:
            flash('මුරපදය අවම වශයෙන් අක්ෂර 4ක් විය යුතුයි.', 'danger')
        else:
            conn.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(new), current_user.id))
            conn.commit()
            log_activity(current_user.id, 'PASSWORD_CHANGE', 'changed')
            flash('මුරපදය සාර්ථකව වෙනස් කරන ලදී.', 'success')
            return redirect(url_for('index'))
        conn.close()
    return render_template('change_password.html')

@app.route('/my_email_settings', methods=['GET','POST'])
@login_required
def my_email_settings():
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    if request.method == 'POST':
        display_name = request.form.get('display_name')
        email_address = request.form.get('email_address')
        password = request.form.get('password')
        incoming_server = request.form.get('incoming_server')
        incoming_port = request.form.get('incoming_port')
        incoming_protocol = request.form.get('incoming_protocol')
        outgoing_server = request.form.get('outgoing_server')
        outgoing_port = request.form.get('outgoing_port')
        if email_address and password:
            set_user_email_settings(current_user.id, display_name, email_address, password,
                                   incoming_server, int(incoming_port), incoming_protocol,
                                   outgoing_server, int(outgoing_port), current_user.id)
            flash('Your email settings have been updated.', 'success')
        else:
            flash('Email address and password are required.', 'danger')
        return redirect(url_for('my_email_settings'))
    cur.execute('''SELECT display_name, email_address, incoming_server, incoming_port, incoming_protocol,
                          outgoing_server, outgoing_port 
                   FROM user_email_settings WHERE user_id = ?''', (current_user.id,))
    row = cur.fetchone()
    settings = {}
    if row:
        settings = {
            'display_name': row[0] or '',
            'email_address': row[1] or '',
            'incoming_server': row[2] or 'mail.kdugroup.com',
            'incoming_port': row[3] or 993,
            'incoming_protocol': row[4] or 'IMAP',
            'outgoing_server': row[5] or 'mail.kdugroup.com',
            'outgoing_port': row[6] or 587
        }
    else:
        settings = {
            'display_name': '',
            'email_address': '',
            'incoming_server': 'mail.kdugroup.com',
            'incoming_port': 993,
            'incoming_protocol': 'IMAP',
            'outgoing_server': 'mail.kdugroup.com',
            'outgoing_port': 587
        }
    conn.close()
    return render_template('my_email_settings.html', settings=settings)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'superadmin':
            flash('මෙම පිටුවට ප්‍රවේශ වීමට සුපිරි පරිපාලක අවසරය අවශ්‍යයි.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

@app.route('/view_file/<file_type>/<path:filename>')
@login_required
def view_file(file_type, filename):
    if '..' in filename or filename.startswith('/'):
        flash('Invalid file name', 'danger')
        return redirect(url_for('index'))
    if file_type == 'tpms':
        folder = app.config['UPLOAD_FOLDER']
    elif file_type == 'pdf':
        folder = 'scans'
    else:
        flash('Invalid file type', 'danger')
        return redirect(url_for('index'))
    base_filename = os.path.basename(filename)
    filepath = os.path.join(folder, base_filename)
    if not os.path.exists(filepath):
        if os.path.exists(filename):
            filepath = filename
        else:
            flash(f'File not found: {base_filename}', 'danger')
            return redirect(url_for('index'))
    if file_type == 'pdf':
        return send_file(filepath, mimetype='application/pdf')
    else:
        return send_file(filepath, as_attachment=True, download_name=base_filename)

# ------------------- API ROUTES -------------------
@app.route('/api/notifications')
@login_required
def api_notifications():
    allowed = get_user_allowed_factories(current_user.id)
    result = []
    conn = sqlite3.connect('database.db')
    for code in allowed:
        cur = conn.cursor()
        cur.execute('''SELECT report_date, route_name, glx_value, pdf_uploaded
                       FROM mismatch_routes 
                       WHERE factory_code = ? AND pdf_uploaded = 0''', (code,))
        mismatches = cur.fetchall()
        routes_status = []
        for rd, rn, gx, pu in mismatches:
            level, color = get_variance_level(gx)
            routes_status.append({
                'route': rn,
                'glx': gx,
                'report_date': rd,
                'pdf_uploaded': pu,
                'status': 'PDF Uploaded' if pu else 'Pending PDF',
                'variance_level': level,
                'variance_color': color
            })
        if routes_status:
            result.append({
                'factory_code': code,
                'factory_name': FACTORIES[code]['name'],
                'routes': routes_status,
                'has_pending': True
            })
    conn.close()
    return jsonify(result)

@app.route('/api/get_routes/<factory_code>')
@login_required
def api_get_routes(factory_code):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT route_name FROM factory_routes WHERE factory_code = ? ORDER BY route_name', (factory_code,))
    routes = [row[0] for row in cur.fetchall()]
    conn.close()
    return jsonify({'routes': routes})

@app.route('/api/pending_mismatches')
@login_required
def api_pending_mismatches():
    factory_code = request.args.get('factory_code')
    report_date = request.args.get('date')
    if not factory_code or not report_date:
        return jsonify({'error': 'Missing factory_code or date'}), 400
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('''
        SELECT route_name, glx_value, reason
        FROM mismatch_routes 
        WHERE factory_code = ? AND report_date = ? AND pdf_uploaded = 0
        ORDER BY route_name
    ''', (factory_code, report_date))
    rows = cur.fetchall()
    conn.close()
    mismatches = []
    for row in rows:
        mismatches.append({
            'route_name': row[0],
            'glx_value': row[1] if row[1] is not None else 0,
            'reason': row[2] or ''
        })
    return jsonify({'mismatches': mismatches})

# ------------------- MULTI‑FILE PDF UPLOAD -------------------
@app.route('/upload_multiple_pdf/<factory_code>', methods=['POST'])
@login_required
def upload_multiple_pdf(factory_code):
    if not user_can_upload_pdf(current_user, factory_code):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    report_date = request.form.get('report_date')
    if not report_date:
        return jsonify({'success': False, 'error': 'Missing report date'}), 400
    files = request.files.getlist('pdf_files')
    if not files:
        return jsonify({'success': False, 'error': 'No files uploaded'}), 400
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('''SELECT route_name FROM mismatch_routes 
                   WHERE factory_code = ? AND report_date = ? AND pdf_uploaded = 0
                   ORDER BY route_name''', (factory_code, report_date))
    pending = [row[0] for row in cur.fetchall()]
    conn.close()
    if not pending:
        return jsonify({'success': False, 'error': 'No pending mismatches for this date'}), 400
    if len(files) > len(pending):
        return jsonify({'success': False, 'error': f'You selected {len(files)} files but only {len(pending)} mismatches pending'}), 400
    uploaded = 0
    errors = []
    conn = sqlite3.connect('database.db')
    for i, file in enumerate(files):
        if i >= len(pending):
            break
        if not file or not file.filename.lower().endswith('.pdf'):
            errors.append(f'{file.filename} is not a valid PDF')
            continue
        route_name = pending[i]
        filename = generate_pdf_filename(factory_code, report_date, route_name)
        filepath = os.path.join('scans', filename)
        file.save(filepath)
        conn.execute('''UPDATE mismatch_routes 
            SET pdf_uploaded = 1, pdf_path = ?, uploaded_by = ?, uploaded_at = ?
            WHERE factory_code = ? AND report_date = ? AND route_name = ?''',
            (filepath, current_user.id, datetime.now().isoformat(), factory_code, report_date, route_name))
        uploaded += 1
    conn.commit()
    conn.close()
    if uploaded > 0:
        flash(f'{uploaded} PDF(s) uploaded successfully!', 'success')
    if errors:
        flash(f'Errors: {", ".join(errors)}', 'danger')
    return redirect(url_for('upload_pdf', factory_code=factory_code))

# ------------------- MAIN ROUTES -------------------
@app.route('/')
@login_required
def index():
    allowed = get_user_allowed_factories(current_user.id)
    filtered = {}
    for code in allowed:
        if code in FACTORIES:
            filtered[code] = {
                'info': FACTORIES[code],
                'can_tpms': user_can_upload_tpms(current_user, code),
                'can_pdf': user_can_upload_pdf(current_user, code)
            }
    today_str = date.today().isoformat()
    return render_template('index.html', factories=filtered, user=current_user, today=today_str)

@app.route('/upload/<factory_code>')
@login_required
def upload_page(factory_code):
    if not user_can_upload_tpms(current_user, factory_code):
        flash('ඔබට TPMS ගොනු උඩුගත කිරීමට අවසර නැත.', 'warning')
        return redirect(url_for('index'))
    if factory_code not in FACTORIES:
        flash('වලංගු කර්මාන්ත ශාලාවක් නොවේ', 'danger')
        return redirect(url_for('index'))
    return render_template('upload.html', factory_code=factory_code, factory_name=FACTORIES[factory_code]['name'], factories=FACTORIES, today=date.today().isoformat())

@app.route('/upload_tpms_no_analysis/<factory_code>', methods=['POST'])
@login_required
def upload_tpms_no_analysis(factory_code):
    if not user_can_upload_tpms(current_user, factory_code):
        return jsonify({'success': False, 'error': 'Permission denied'}), 403
    file = request.files.get('report_file')
    report_date = request.form.get('report_date')
    if not file or not report_date:
        return jsonify({'success': False, 'error': 'Missing file or date'}), 400
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Only CSV, XLSX, XLS allowed'}), 400
    ext = file.filename.rsplit('.',1)[1].lower()
    base_name = generate_tpms_filename(factory_code, report_date)
    filename = secure_filename(f"{base_name}.{ext}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    session['tpms_file'] = filepath
    session['tpms_factory'] = factory_code
    session['tpms_date'] = report_date
    return jsonify({'success': True, 'redirect': url_for('prepare_email', factory_code=factory_code, report_date=report_date)})

@app.route('/analyze/<factory_code>', methods=['POST'])
@login_required
def analyze(factory_code):
    return redirect(url_for('upload_tpms_no_analysis', factory_code=factory_code))

@app.route('/prepare_email/<factory_code>/<report_date>')
@login_required
def prepare_email(factory_code, report_date):
    if not user_can_send_email(current_user, factory_code):
        flash('You are not allowed to send emails for this factory.', 'danger')
        return redirect(url_for('index'))
    tpms_file = session.get('tpms_file')
    if not tpms_file:
        flash('No TPMS file found. Please upload first.', 'danger')
        return redirect(url_for('upload_page', factory_code=factory_code))
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT pdf_path, route_name FROM mismatch_routes WHERE factory_code = ? AND report_date = ? AND pdf_uploaded = 1', (factory_code, report_date))
    pdfs = cur.fetchall()
    conn.close()
    return render_template('prepare_email.html', factory_code=factory_code, factory_name=FACTORIES[factory_code]['name'],
                           report_date=report_date, tpms_file=tpms_file, pdfs=pdfs)

@app.route('/send_email_with_all/<factory_code>/<report_date>', methods=['POST'])
@login_required
def send_email_with_all(factory_code, report_date):
    if not user_can_send_email(current_user, factory_code):
        return jsonify({'success': False, 'error': 'You are not allowed to send emails for this factory'}), 403
    recipient = request.form.get('recipient')
    tpms_file = session.get('tpms_file')
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT pdf_path, route_name FROM mismatch_routes WHERE factory_code = ? AND report_date = ? AND pdf_uploaded = 1', (factory_code, report_date))
    pdfs = cur.fetchall()
    conn.close()
    if not tpms_file:
        return jsonify({'success': False, 'error': 'TPMS file missing'}), 400
    try:
        send_email_with_multiple_attachments(recipient, factory_code, tpms_file, pdfs, report_date)
        conn = sqlite3.connect('database.db')
        conn.execute('INSERT INTO reports (user_id, factory_code, report_date, excel_file, scanned_pdf, sent_to, status, timestamp) VALUES (?,?,?,?,?,?,?,?)',
                     (current_user.id, factory_code, report_date, tpms_file, None, recipient or '', 'Sent', datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log_activity(current_user.id, 'EMAIL_SEND', f'{factory_code} {report_date} to {recipient}')
        session.pop('tpms_file', None)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/upload_pdf/<factory_code>', methods=['GET','POST'])
@login_required
def upload_pdf(factory_code):
    if not user_can_upload_pdf(current_user, factory_code):
        flash('මෙම කර්මාන්ත ශාලාව සඳහා PDF උඩුගත කිරීමට අවසර නැත.', 'danger')
        return redirect(url_for('index'))
    if factory_code not in FACTORIES:
        flash('වලංගු කර්මාන්ත ශාලාවක් නොවේ', 'danger')
        return redirect(url_for('index'))
    return render_template('upload_pdf.html', factory_code=factory_code, factory_name=FACTORIES[factory_code]['name'], pending=None, today=date.today().isoformat())

# ------------------- OTHER ROUTES -------------------
@app.route('/print_mismatch/<factory_code>')
@login_required
def print_mismatch(factory_code):
    analysis = session.get('last_analysis')
    if not analysis:
        flash('විශ්ලේෂණ දත්ත නැත.', 'danger')
        return redirect(url_for('index'))
    return render_template('mismatch_print.html', factory_name=analysis['factory_name'],
                         factory_code=analysis['factory_code'], mismatches=analysis['mismatches'],
                         date=session.get('report_date', datetime.now().strftime('%Y-%m-%d')))

@app.route('/upload_scan/<factory_code>', methods=['GET','POST'])
@login_required
def upload_scan(factory_code):
    if request.method == 'POST':
        file = request.files.get('scan_file')
        if file and file.filename.lower().endswith('.pdf'):
            filename = secure_filename(f"{factory_code}_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            filepath = os.path.join('scans', filename)
            file.save(filepath)
            session['scanned_pdf'] = filepath
            log_activity(current_user.id, 'SCAN_UPLOAD', f'PDF for {factory_code}')
            flash('Scanned PDF uploaded', 'success')
            return redirect(url_for('send_final_report', factory_code=factory_code))
        else:
            flash('PDF ගොනුවක් උඩුගත කරන්න', 'danger')
    return render_template('scan_upload.html', factory_code=factory_code)

@app.route('/send_final_report/<factory_code>')
@login_required
def send_final_report(factory_code):
    analysis = session.get('last_analysis')
    scanned = session.get('scanned_pdf')
    report_date = session.get('report_date', date.today().isoformat())
    if not analysis or not scanned:
        flash('විශ්ලේෂණය හෝ PDF නැත', 'danger')
        return redirect(url_for('index'))
    return render_template('final_email.html', factory_code=factory_code, factory_name=analysis['factory_name'],
                         scanned_pdf=scanned, report_date=report_date)

# ------------------- SUPERADMIN ROUTES -------------------
@app.route('/admin/user_permissions', methods=['GET','POST'])
@login_required
@admin_required
def admin_user_permissions():
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT id, username, full_name, role FROM users WHERE role != "superadmin" ORDER BY username')
    users = cur.fetchall()
    if request.method == 'POST':
        user_id = request.form.get('user_id')
        factory_code = request.form.get('factory_code')
        can_tpms = 'can_tpms' in request.form
        can_pdf = 'can_pdf' in request.form
        can_email = 'can_email' in request.form
        if user_id and factory_code:
            set_user_permission(user_id, factory_code, can_tpms, can_pdf, can_email)
            flash(f'Permissions updated for user ID {user_id}', 'success')
        return redirect(url_for('admin_user_permissions'))
    permissions = {}
    for user in users:
        user_id = user[0]
        permissions[user_id] = {}
        for fac in FACTORIES:
            perms = get_user_permissions(user_id, fac)
            permissions[user_id][fac] = perms
    conn.close()
    return render_template('admin_user_permissions.html', users=users, factories=FACTORIES, permissions=permissions)

@app.route('/admin/email_settings', methods=['GET','POST'])
@login_required
@admin_required
def admin_email_settings():
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    if request.method == 'POST':
        user_id = request.form.get('user_id')
        display_name = request.form.get('display_name')
        email_address = request.form.get('email_address')
        password = request.form.get('password')
        incoming_server = request.form.get('incoming_server')
        incoming_port = request.form.get('incoming_port')
        incoming_protocol = request.form.get('incoming_protocol')
        outgoing_server = request.form.get('outgoing_server')
        outgoing_port = request.form.get('outgoing_port')
        if user_id and email_address and password:
            set_user_email_settings(int(user_id), display_name, email_address, password, 
                                   incoming_server, int(incoming_port), incoming_protocol,
                                   outgoing_server, int(outgoing_port), current_user.id)
            flash('Email settings updated successfully.', 'success')
        else:
            flash('All fields are required.', 'danger')
        return redirect(url_for('admin_email_settings'))
    cur.execute('SELECT id, username, full_name FROM users WHERE role != "superadmin" ORDER BY username')
    users = cur.fetchall()
    settings = {}
    for u in users:
        cur.execute('''SELECT display_name, email_address, incoming_server, incoming_port, incoming_protocol,
                              outgoing_server, outgoing_port 
                       FROM user_email_settings WHERE user_id = ?''', (u[0],))
        row = cur.fetchone()
        settings[u[0]] = {
            'display_name': row[0] if row else '',
            'email_address': row[1] if row else '',
            'incoming_server': row[2] if row else '',
            'incoming_port': row[3] if row else '',
            'incoming_protocol': row[4] if row else 'IMAP',
            'outgoing_server': row[5] if row else '',
            'outgoing_port': row[6] if row else ''
        } if row else {}
    conn.close()
    return render_template('admin_email_settings.html', users=users, settings=settings)

@app.route('/admin/mismatch_entry', methods=['GET','POST'])
@login_required
@admin_required
def admin_mismatch_entry():
    conn = sqlite3.connect('database.db')
    if request.method == 'POST':
        if 'add_mismatch' in request.form:
            factory_code = request.form.get('factory_code')
            report_date = request.form.get('report_date')
            route_name = request.form.get('route_name')
            glx_value = request.form.get('glx_value')
            if not all([factory_code, report_date, route_name, glx_value]):
                flash('All mismatch fields are required.', 'danger')
                return redirect(url_for('admin_mismatch_entry'))
            try:
                glx = float(glx_value)
            except:
                flash('Variance must be a number.', 'danger')
                return redirect(url_for('admin_mismatch_entry'))
            conn.execute('''INSERT OR REPLACE INTO mismatch_routes 
                (factory_code, report_date, route_name, glx_value, pdf_uploaded)
                VALUES (?, ?, ?, ?, 0)''',
                (factory_code, report_date, route_name, glx))
            conn.commit()
            flash(f'Mismatch added for {factory_code} on {report_date}.', 'success')
            return redirect(url_for('admin_mismatch_entry'))
    today = date.today().isoformat()
    cur = conn.cursor()
    cur.execute('''SELECT factory_code, report_date, route_name, glx_value, pdf_uploaded
                   FROM mismatch_routes 
                   WHERE pdf_uploaded = 0
                   ORDER BY report_date DESC, factory_code''')
    pending_mismatches = cur.fetchall()
    conn.close()
    return render_template('admin_mismatch_entry.html', factories=FACTORIES, today=today, 
                           pending_mismatches=pending_mismatches)

@app.route('/admin/bulk_mismatch_entry', methods=['POST'])
@login_required
@admin_required
def admin_bulk_mismatch_entry():
    factory_code = request.form.get('factory_code')
    report_date = request.form.get('report_date')
    rows_json = request.form.get('rows_json')
    if not factory_code or not report_date or not rows_json:
        return jsonify({'success': False, 'error': 'Missing factory, date, or data'}), 400
    try:
        rows = json.loads(rows_json)
    except:
        return jsonify({'success': False, 'error': 'Invalid row data'}), 400
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    for row in rows:
        route_name = row.get('route_name')
        glx_value = row.get('glx_value')
        reason = row.get('reason')
        if not route_name:
            continue
        try:
            glx = float(glx_value) if glx_value and glx_value != '' else 0.0
        except (ValueError, TypeError):
            glx = 0.0
        cur.execute('''INSERT OR REPLACE INTO mismatch_routes 
            (factory_code, report_date, route_name, glx_value, pdf_uploaded, reason)
            VALUES (?, ?, ?, ?, 0, ?)''',
            (factory_code, report_date, route_name, glx, reason))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Data saved'})

@app.route('/admin/bulk_add_routes', methods=['POST'])
@login_required
@admin_required
def admin_bulk_add_routes():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400
    factory_code = data.get('factory_code')
    routes = data.get('routes', [])
    if not factory_code or not routes:
        return jsonify({'success': False, 'error': 'Missing factory code or routes list'}), 400
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    added = 0
    for route in routes:
        try:
            cur.execute('INSERT INTO factory_routes (factory_code, route_name, created_by, created_at) VALUES (?, ?, ?, ?)',
                        (factory_code, route, current_user.id, datetime.now().isoformat()))
            added += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'added_count': added})

@app.route('/admin/manage_routes')
@login_required
@admin_required
def admin_manage_routes():
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    route_data = {}
    for code in FACTORIES:
        cur.execute('SELECT id, route_name FROM factory_routes WHERE factory_code = ? ORDER BY route_name', (code,))
        route_data[code] = cur.fetchall()
    conn.close()
    return render_template('admin_route_manager.html', factories=FACTORIES, route_data=route_data)

@app.route('/admin/add_route', methods=['POST'])
@login_required
@admin_required
def admin_add_route():
    factory_code = request.form.get('factory_code')
    route_name = request.form.get('route_name').strip()
    if not factory_code or not route_name:
        flash('Factory and route name required', 'danger')
        return redirect(url_for('admin_manage_routes'))
    conn = sqlite3.connect('database.db')
    try:
        conn.execute('INSERT INTO factory_routes (factory_code, route_name, created_by, created_at) VALUES (?, ?, ?, ?)',
                     (factory_code, route_name, current_user.id, datetime.now().isoformat()))
        conn.commit()
        flash(f'Route "{route_name}" added for {factory_code}', 'success')
    except sqlite3.IntegrityError:
        flash('Route already exists for this factory', 'warning')
    conn.close()
    return redirect(url_for('admin_manage_routes'))

@app.route('/admin/delete_route/<int:route_id>')
@login_required
@admin_required
def admin_delete_route(route_id):
    conn = sqlite3.connect('database.db')
    conn.execute('DELETE FROM factory_routes WHERE id = ?', (route_id,))
    conn.commit()
    conn.close()
    flash('Route deleted', 'success')
    return redirect(url_for('admin_manage_routes'))

@app.route('/admin/daily_overview')
@login_required
@admin_required
def admin_daily_overview():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if not start_date:
        start_date = date.today().isoformat()
    if not end_date:
        end_date = date.today().isoformat()
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        SELECT 
            mr.factory_code,
            mr.report_date,
            mr.route_name,
            mr.glx_value,
            mr.pdf_uploaded,
            mr.uploaded_at,
            mr.reason,
            CASE WHEN r.id IS NOT NULL THEN 'Sent' ELSE 'Pending' END AS email_status
        FROM mismatch_routes mr
        LEFT JOIN reports r ON r.factory_code = mr.factory_code 
                           AND r.report_date = mr.report_date
                           AND r.status = 'Sent'
        WHERE mr.report_date BETWEEN ? AND ?
        ORDER BY mr.report_date DESC, mr.factory_code, mr.route_name
    ''', (start_date, end_date))
    rows = cur.fetchall()
    conn.close()
    grouped = {}
    for row in rows:
        fac = row['factory_code']
        date_str = row['report_date']
        key = (fac, date_str)
        if key not in grouped:
            grouped[key] = {
                'factory_code': fac,
                'report_date': date_str,
                'mismatches': []
            }
        grouped[key]['mismatches'].append({
            'route': row['route_name'],
            'glx': row['glx_value'],
            'pdf_uploaded': row['pdf_uploaded'],
            'uploaded_at': row['uploaded_at'],
            'reason': row['reason'],
            'email_status': row['email_status']
        })
    return render_template('admin_daily_overview.html', 
                           grouped=grouped, 
                           factories=FACTORIES,
                           start_date=start_date,
                           end_date=end_date)

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT id, username, role, full_name, is_active, created_at FROM users ORDER BY id')
    users = cur.fetchall()
    cur.execute('''
        SELECT ua.id, u.username, u.full_name, ua.action, ua.details, ua.timestamp
        FROM user_activities ua
        LEFT JOIN users u ON ua.user_id = u.id
        ORDER BY ua.timestamp DESC LIMIT 50
    ''')
    activities_raw = cur.fetchall()
    activities = []
    for act in activities_raw:
        activities.append({
            'id': act[0],
            'username': act[1],
            'full_name': act[2],
            'action': act[3],
            'details': act[4],
            'timestamp': act[5]
        })
    conn.close()
    today_date = datetime.now().strftime('%B %d, %Y')
    return render_template('admin_dashboard.html',
                           users=users,
                           factories=FACTORIES,
                           activities=activities,
                           today_date=today_date)

@app.route('/admin/user_activity/<int:user_id>')
@login_required
@admin_required
def user_activity(user_id):
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT username FROM users WHERE id = ?', (user_id,))
    user = cur.fetchone()
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('admin_dashboard'))
    cur.execute('SELECT action, details, timestamp FROM user_activities WHERE user_id = ? ORDER BY timestamp DESC', (user_id,))
    activities = cur.fetchall()
    conn.close()
    return render_template('user_activity.html', user=user, activities=activities)

@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def reset_password(user_id):
    new_password = request.form.get('new_password', '12345')
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT username FROM users WHERE id = ?', (user_id,))
    user = cur.fetchone()
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('admin_dashboard'))
    hashed = generate_password_hash(new_password)
    conn.execute('UPDATE users SET password = ? WHERE id = ?', (hashed, user_id))
    conn.commit()
    conn.close()
    log_activity(current_user.id, 'RESET_PASSWORD', f'Reset password for user {user[0]} (ID: {user_id})')
    flash(f'Password for {user[0]} has been reset to "{new_password}"', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def toggle_user(user_id):
    if user_id == current_user.id:
        flash('You cannot disable your own account.', 'danger')
        return redirect(url_for('admin_dashboard'))
    conn = sqlite3.connect('database.db')
    cur = conn.cursor()
    cur.execute('SELECT is_active FROM users WHERE id = ?', (user_id,))
    row = cur.fetchone()
    if row:
        new_status = 0 if row[0] else 1
        conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        flash('User status updated.', 'success')
    else:
        flash('User not found.', 'danger')
    conn.close()
    log_activity(current_user.id, 'TOGGLE_USER', f'Toggled user ID {user_id}')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_user/<int:user_id>')
@login_required
@admin_required
def delete_user(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_dashboard'))
    conn = sqlite3.connect('database.db')
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.execute('DELETE FROM user_activities WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM user_permissions WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM user_email_settings WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM mismatch_routes WHERE uploaded_by = ?', (user_id,))
    conn.commit()
    conn.close()
    log_activity(current_user.id, 'DELETE_USER', f'Deleted user ID {user_id}')
    flash('User deleted permanently.', 'success')
    return redirect(url_for('admin_dashboard'))

# ------------------- SAMPLE DOWNLOADS -------------------
@app.route('/sample_csv')
def download_sample_csv():
    sample = io.StringIO()
    w = csv.writer(sample)
    w.writerow(['batch_id','lipe_value','moisture','grade'])
    w.writerow(['B001',78.2,3.5,'BOP'])
    w.writerow(['B002',79.8,3.2,'BOP'])
    w.writerow(['B003',77.1,3.8,'BOP'])
    sample.seek(0)
    return send_file(io.BytesIO(sample.getvalue().encode('utf-8')), mimetype='text/csv', as_attachment=True, download_name='sample_tea_report.csv')

@app.route('/sample_excel')
def download_sample_excel():
    df = pd.DataFrame({'batch_id':['B001','B002','B003'], 'lipe_value':[78.2,79.8,77.1]})
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name='sample.xlsx')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)