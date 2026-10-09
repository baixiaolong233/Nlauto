import eventlet
eventlet.monkey_patch()

import os
import json
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify,
)
from flask_socketio import SocketIO, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash

from task_queue import TaskQueue

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
DB_PATH = WEB_DIR / 'users.db'
AUTOVISOR_PY = BASE_DIR / 'Autovisor.py'
VENV_PYTHON = BASE_DIR / 'venv' / 'bin' / 'python'
DATA_DIR = BASE_DIR / 'data'
LOG_DIR = BASE_DIR / 'logs'

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'autovisor-secret-change-me')

socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', '1'))
DISPLAY = os.environ.get('DISPLAY', ':99')

task_queue = TaskQueue(
    socketio=socketio,
    max_concurrent=MAX_CONCURRENT,
    autovisor_py=AUTOVISOR_PY,
    venv_python=VENV_PYTHON,
    base_dir=BASE_DIR,
    data_dir=DATA_DIR,
    log_dir=LOG_DIR,
    display=DISPLAY,
    db_path=DB_PATH,
)


def get_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            zhs_username TEXT NOT NULL,
            zhs_password TEXT NOT NULL,
            course_urls TEXT NOT NULL,
            speed REAL DEFAULT 1.5,
            enable_auto_captcha INTEGER DEFAULT 1,
            enable_vlm INTEGER DEFAULT 0,
            vlm_api_key TEXT DEFAULT '',
            vlm_model TEXT DEFAULT '',
            vlm_base_url TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            created_at TEXT NOT NULL,
            finished_at TEXT,
            log_path TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    try:
        db.execute("ALTER TABLE users ADD COLUMN last_config TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.commit()
    db.close()


init_db()


def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    db.close()
    return row


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if not username or not password:
        return render_template('index.html', error='用户名和密码不能为空')
    if len(password) < 6:
        return render_template('index.html', error='密码至少 6 位')
    db = get_db()
    try:
        db.execute(
            'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
            (username, generate_password_hash(password), datetime.now().isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        return render_template('index.html', error='用户名已存在')
    db.close()
    return redirect(url_for('index'))


@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    db.close()
    if row and check_password_hash(row['password_hash'], password):
        session['user_id'] = row['id']
        return redirect(url_for('dashboard'))
    return render_template('index.html', error='用户名或密码错误')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    db = get_db()
    tasks = db.execute(
        'SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC',
        (user['id'],),
    ).fetchall()
    db.close()
    return render_template('dashboard.html', user=user, tasks=tasks)


@app.route('/api/submit', methods=['POST'])
@login_required
def api_submit():
    user = current_user()
    data = request.json or request.form

    zhs_username = data.get('zhs_username', '').strip()
    zhs_password = data.get('zhs_password', '').strip()
    course_urls_raw = data.get('course_urls', '').strip()

    if not zhs_username or not zhs_password:
        return jsonify({'error': '账号密码不能为空'}), 400
    if not course_urls_raw:
        return jsonify({'error': '请填写至少一个课程链接'}), 400

    course_urls = [u.strip() for u in course_urls_raw.replace(',', '\n').split('\n') if u.strip()]
    task_id = str(uuid.uuid4())[:8]

    db = get_db()
    db.execute(
        """INSERT INTO tasks
           (id, user_id, zhs_username, zhs_password, course_urls, speed,
            enable_auto_captcha, enable_vlm, vlm_api_key, vlm_model, vlm_base_url,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
        (
            task_id, user['id'], zhs_username, zhs_password, json.dumps(course_urls),
            float(data.get('speed', 1.5)),
            int(data.get('enable_auto_captcha', True)),
            int(data.get('enable_vlm', False)),
            data.get('vlm_api_key', ''),
            data.get('vlm_model', ''),
            data.get('vlm_base_url', ''),
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    db.close()

    config_data = {
        'zhs_username': zhs_username,
        'zhs_password': zhs_password,
        'course_urls': course_urls,
        'speed': float(data.get('speed', 1.5)),
        'limit_max_time': int(data.get('limit_max_time', 0)),
        'enable_auto_captcha': int(data.get('enable_auto_captcha', True)),
        'enable_vlm': int(data.get('enable_vlm', False)),
        'vlm_api_key': data.get('vlm_api_key', ''),
        'vlm_model': data.get('vlm_model', ''),
        'vlm_base_url': data.get('vlm_base_url', ''),
    }
    last_config = json.dumps(config_data)
    db = get_db()
    db.execute('UPDATE users SET last_config=? WHERE id=?', (last_config, user['id']))
    db.commit()
    db.close()

    task_queue.submit(task_id, user['id'], config_data)
    return jsonify({'task_id': task_id, 'status': 'queued'})


@app.route('/api/tasks')
@login_required
def api_tasks():
    user = current_user()
    db = get_db()
    tasks = db.execute(
        'SELECT id, status, zhs_username, course_urls, speed, created_at, finished_at FROM tasks WHERE user_id=? ORDER BY created_at DESC',
        (user['id'],),
    ).fetchall()
    db.close()
    return jsonify({'tasks': [dict(t) for t in tasks]})


@app.route('/api/cancel/<task_id>', methods=['POST'])
@login_required
def api_cancel(task_id):
    user = current_user()
    task_queue.cancel(task_id, user['id'])
    db = get_db()
    db.execute(
        "UPDATE tasks SET status='failed', finished_at=? WHERE id=? AND user_id=?",
        (datetime.now().isoformat(), task_id, user['id']),
    )
    db.commit()
    db.close()
    return jsonify({'status': 'failed'})


@app.route('/api/last_config')
@login_required
def api_last_config():
    user = current_user()
    lc = user['last_config'] if 'last_config' in user.keys() else None
    if lc:
        return jsonify(json.loads(lc))
    return jsonify({})


@socketio.on('connect')
def on_connect():
    pass


@socketio.on('join')
def on_join(data):
    uid = data.get('user_id')
    if uid:
        join_room(f'user_{uid}')


@socketio.on('disconnect')
def on_disconnect():
    pass


if __name__ == '__main__':
    print(f'Starting on 0.0.0.0:5000 (max_concurrent={MAX_CONCURRENT}, display={DISPLAY})')
    socketio.run(app, host='0.0.0.0', port=5000)
