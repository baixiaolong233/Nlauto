const socket = io({transports: ['polling']});
socket.on('connect', () => {
    if (typeof USER_ID !== 'undefined') socket.emit('join', {user_id: USER_ID});
});

let currentLogTask = null;
const logOutput = document.getElementById('log-output');
const logTaskId = document.getElementById('log-task-id');
const tasksTbody = document.querySelector('#tasks-table tbody');

fetch('/api/last_config').then(r => r.json()).then(d => {
    if (!d || !d.zhs_username) return;
    zhs_username.value = d.zhs_username;
    zhs_password.value = d.zhs_password || '';
    course_urls.value = (d.course_urls || []).join('\n');
    speed.value = d.speed || 1.5;
    enable_auto_captcha.checked = !!d.enable_auto_captcha;
    enable_vlm.checked = !!d.enable_vlm;
    document.querySelector('.vlm-row').classList.toggle('hidden', !d.enable_vlm);
    vlm_api_key.value = d.vlm_api_key || '';
    vlm_model.value = d.vlm_model || '';
    vlm_base_url.value = d.vlm_base_url || '';
}).catch(() => {});

socket.on('log', (data) => {
    if (data.task_id !== currentLogTask) return;
    const div = document.createElement('div');
    div.className = 'log-line';
    div.textContent = data.line;
    logOutput.appendChild(div);
    logOutput.scrollTop = logOutput.scrollHeight;
});

socket.on('task_update', (data) => {
    const row = document.querySelector(`tr[data-task-id="${data.task_id}"]`);
    if (!row) return;
    const cell = row.querySelector('.status');
    cell.textContent = data.status;
    cell.className = `status ${data.status}`;
    if (data.status === 'done' || data.status === 'failed') {
        const cancelBtn = row.querySelector('.btn-danger');
        if (cancelBtn) cancelBtn.remove();
    }
});

document.getElementById('task-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
        zhs_username: zhs_username.value,
        zhs_password: zhs_password.value,
        course_urls: course_urls.value,
        speed: parseFloat(speed.value),
        enable_auto_captcha: enable_auto_captcha.checked,
        enable_vlm: enable_vlm.checked,
        vlm_api_key: vlm_api_key.value,
        vlm_model: vlm_model.value,
        vlm_base_url: vlm_base_url.value,
    };
    const res = await fetch('/api/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }

    const tr = document.createElement('tr');
    tr.className = 'task-row';
    tr.dataset.taskId = data.task_id;
    const now = new Date().toISOString().slice(0, 19);
    tr.innerHTML = `
        <td>${data.task_id}</td>
        <td>${body.zhs_username}</td>
        <td>${body.speed}</td>
        <td class="status queued">queued</td>
        <td>${now}</td>
        <td>
            <button class="btn-small" onclick="showLog('${data.task_id}')">日志</button>
            <button class="btn-small btn-danger" onclick="cancelTask('${data.task_id}')">取消</button>
        </td>
    `;
    tasksTbody.prepend(tr);
    showLog(data.task_id);
});

function showLog(taskId) {
    currentLogTask = taskId;
    logTaskId.textContent = '#' + taskId;
    logOutput.innerHTML = '';
    fetch(`/api/tasks`).then(r => r.json()).then(d => {
        const t = d.tasks.find(x => x.id === taskId);
        if (!t) return;
        const div = document.createElement('div');
        div.className = 'log-line';
        div.textContent = `[${t.status}] ${t.zhs_username} | 倍速 ${t.speed}`;
        logOutput.appendChild(div);
    });
}

function cancelTask(taskId) {
    if (!confirm('确认取消任务 ' + taskId + '？')) return;
    fetch('/api/cancel/' + taskId, { method: 'POST' }).then(() => {
        const row = document.querySelector(`tr[data-task-id="${taskId}"]`);
        if (row) {
            const cell = row.querySelector('.status');
            cell.textContent = 'failed';
            cell.className = 'status failed';
            const cancelBtn = row.querySelector('.btn-danger');
            if (cancelBtn) cancelBtn.remove();
        }
    });
}
