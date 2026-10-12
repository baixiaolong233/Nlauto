import os
import sqlite3
import subprocess
from pathlib import Path
from collections import deque
from datetime import datetime

import eventlet


class TaskQueue:
    def __init__(self, socketio, max_concurrent, autovisor_py, venv_python,
                 base_dir, data_dir, log_dir, display, db_path):
        self.socketio = socketio
        self.max_concurrent = max_concurrent
        self.autovisor_py = autovisor_py
        self.venv_python = venv_python
        self.base_dir = base_dir
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.display = display
        self.db_path = db_path

        self.queue = deque()
        self.running = {}
        self.cancelled = set()

    def _update_db_status(self, task_id, status):
        db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            db.execute(
                'UPDATE tasks SET status=?, finished_at=? WHERE id=?',
                (status, datetime.now().isoformat(), task_id),
            )
            db.commit()
        finally:
            db.close()

    def submit(self, task_id, user_id, config_data):
        self.queue.append((task_id, user_id, config_data))
        self.socketio.emit(
            'task_update', {'task_id': task_id, 'status': 'queued'},
            room=f'user_{user_id}',
        )
        eventlet.spawn(self._schedule)

    def _schedule(self):
        while self.queue and len(self.running) < self.max_concurrent:
            task_id, user_id, config_data = self.queue.popleft()
            if task_id in self.cancelled:
                self.cancelled.discard(task_id)
                continue
            eventlet.spawn(self._run_task, task_id, user_id, config_data)

    def _run_task(self, task_id, user_id, config_data):
        config_path = self.data_dir / f'config_{task_id}.ini'
        cookie_path = self.data_dir / f'cookies_{task_id}.json'
        log_path = self.log_dir / f'web_{task_id}.txt'

        self._write_config(config_data, config_path)

        self._update_db_status(task_id, 'running')
        self.socketio.emit(
            'task_update', {'task_id': task_id, 'status': 'running'},
            room=f'user_{user_id}',
        )

        env = os.environ.copy()
        env['DISPLAY'] = self.display
        env['AUTOVISOR_COOKIE_PATH'] = str(cookie_path)
        env['PYTHONUNBUFFERED'] = '1'

        cmd = [
            str(self.venv_python), '-u', str(self.autovisor_py),
            '--config', str(config_path),
        ]

        status = 'done'

        try:
            log_fp = open(log_path, 'w', encoding='utf-8')
            proc = subprocess.Popen(
                cmd,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(self.base_dir),
            )
            log_fp.close()
            self.running[task_id] = proc

            pos = 0
            while proc.poll() is None:
                eventlet.sleep(1)
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    if line:
                        self.socketio.emit(
                            'log', {'task_id': task_id, 'line': line},
                            room=f'user_{user_id}',
                        )

            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(pos)
                for line in f:
                    line = line.rstrip()
                    if line:
                        self.socketio.emit(
                            'log', {'task_id': task_id, 'line': line},
                            room=f'user_{user_id}',
                        )

            if task_id in self.cancelled:
                status = 'failed'
            elif proc.returncode != 0:
                status = 'failed'

        except Exception as e:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[TaskQueue Error] {e}\n')
            status = 'failed'
        finally:
            self.running.pop(task_id, None)
            self.cancelled.discard(task_id)
            try:
                config_path.unlink()
            except OSError:
                pass
            if status == 'done':
                try:
                    log_path.unlink()
                except OSError:
                    pass

        self._update_db_status(task_id, status)
        self.socketio.emit(
            'task_update', {'task_id': task_id, 'status': status},
            room=f'user_{user_id}',
        )
        eventlet.spawn(self._schedule)

    def cancel(self, task_id, user_id):
        proc = self.running.get(task_id)
        self.cancelled.add(task_id)
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass

    def _write_config(self, config_data, config_path):
        lines = [
            '[user-account]',
            f'username = {config_data["zhs_username"]}',
            f'password = {config_data["zhs_password"]}',
            '',
            '[browser-option]',
            'driver = chromium',
            'EXE_PATH =',
            '',
            '[script-option]',
            f'enableAutoCaptcha = {bool(config_data.get("enable_auto_captcha", True))}',
            'enableHideWindow = False',
            'showDonateCode = False',
            f'enableVlmCaptcha = {bool(config_data.get("enable_vlm", False))}',
            f'vlmApiKey = {config_data.get("vlm_api_key", "")}',
            f'vlmModel = {config_data.get("vlm_model") or "qwen-vl-max"}',
            f'vlmBaseUrl = {config_data.get("vlm_base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"}',
            'vlmTimeout = 120',
            'vlmEnableThinking = False',
            '',
            '[course-option]',
            f'limitMaxTime = {int(config_data.get("limit_max_time", 0))}',
            f'limitSpeed = {config_data.get("speed", 1.5)}',
            'soundOff = True',
            '',
            '[course-url]',
        ]
        for i, url in enumerate(config_data['course_urls'], 1):
            lines.append(f'URL{i} = {url}')
        config_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
