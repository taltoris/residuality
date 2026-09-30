/**
 * residuality.js — shared JS for all Residuality pages
 */

const SIZE_LIMIT = 300 * 1024; // 300KB — files larger than this are auto-deselected

// ── Utilities ─────────────────────────────────────────────────────────────

function escapeHtml(t) {
    return String(t)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatSize(bytes) {
    if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + 'MB';
    if (bytes > 1024)        return (bytes / 1024).toFixed(0) + 'KB';
    return bytes + 'B';
}

function setStatus(el, msg, color) {
    el.style.display = 'block';
    el.style.color   = color || '#8b949e';
    el.textContent   = msg;
}

// ── .gitignore ────────────────────────────────────────────────────────────

function loadGitignore(projectId) {
    fetch(`/projects/${projectId}/gitignore`)
        .then(r => r.json())
        .then(data => {
            const c = document.getElementById('ignore-tags');
            if (!c) return;
            c.innerHTML = (data.entries || []).map(e =>
                `<span class="ignore-tag">${escapeHtml(e)}
                 <button onclick="removeIgnore(${JSON.stringify(projectId)}, ${JSON.stringify(e)})">×</button>
                 </span>`
            ).join('');
        })
        .catch(err => console.error('gitignore load:', err));
}

function addIgnore(projectId) {
    const input   = document.getElementById('new-ignore');
    const pattern = input.value.trim();
    if (!pattern) return;
    const form = new FormData();
    form.append('pattern', pattern);
    fetch(`/projects/${projectId}/gitignore/add`, { method: 'POST', body: form })
        .then(() => { input.value = ''; loadGitignore(projectId); })
        .catch(err => console.error('addIgnore:', err));
}

function removeIgnore(projectId, pattern) {
    const form = new FormData();
    form.append('pattern', pattern);
    fetch(`/projects/${projectId}/gitignore/remove`, { method: 'POST', body: form })
        .then(() => loadGitignore(projectId))
        .catch(err => console.error('removeIgnore:', err));
}

// ── File list rendering ───────────────────────────────────────────────────

function renderFileList(files) {
    const container = document.getElementById('file-list');
    if (!container) return;
    container.innerHTML = files.map(f => {
        const large      = f.size > SIZE_LIMIT;
        const checked    = !large;
        const sizeStr    = formatSize(f.size);
        const statusCls  = f.status === '??' ? 'Q' : (f.status.trim()[0] || 'M');
        const statusLbl  = f.status === '??' ? 'new' : f.status.trim();
        const safeId     = 'file_' + f.path.replace(/[^a-zA-Z0-9]/g, '_');
        return `
        <div class="file-row">
            <span class="file-status ${statusCls}">${statusLbl}</span>
            <label for="${safeId}" class="${large ? 'large-file' : ''}"
                   style="flex:1; cursor:pointer; font-family:monospace;">
                ${escapeHtml(f.path)}
            </label>
            <span class="file-size">${sizeStr}${large ? ' — skipped' : ''}</span>
            <input type="checkbox" id="${safeId}" value="${escapeHtml(f.path)}"
                   ${checked ? 'checked' : ''} ${large ? 'disabled' : ''}>
        </div>`;
    }).join('');
}

function toggleAll(state) {
    document.querySelectorAll('#file-list input[type=checkbox]:not(:disabled)')
        .forEach(cb => cb.checked = state);
}

function getSelectedFiles() {
    return Array.from(
        document.querySelectorAll('#file-list input[type=checkbox]:checked')
    ).map(cb => cb.value);
}

// ── Build Graph (all committed files) ────────────────────────────────────

function buildGraphAll(projectId) {
    const btn    = document.getElementById('build-all-btn');
    const status = document.getElementById('regen-status');
    btn.disabled    = true;
    btn.textContent = 'Building...';
    setStatus(status, 'Running tree-sitter over all files...', '#8b949e');

    fetch(`/projects/${projectId}/graph/build_all`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok' || data.status === 'partial') {
                const hasErrors = data.errors && data.errors.length > 0;
                setStatus(status,
                    `✓ Built graph from ${data.processed} files` +
                    (hasErrors ? ` (${data.errors.length} errors: ${data.errors.slice(0,3).join('; ')})` : '') +
                    ' — reloading...',
                    hasErrors ? '#d29922' : '#3fb950'
                );
                setTimeout(() => location.reload(), 1200);
            } else {
                setStatus(status, 'Error: ' + JSON.stringify(data), '#f85149');
                btn.disabled    = false;
                btn.textContent = '⚙ Build Graph';
            }
        })
        .catch(e => {
            setStatus(status, 'Fetch error: ' + e, '#f85149');
            btn.disabled    = false;
            btn.textContent = '⚙ Build Graph';
        });
}

// ── Regenerate & Commit ───────────────────────────────────────────────────

function startRegenerate(projectId) {
    const btn    = document.getElementById('regen-btn');
    const status = document.getElementById('regen-status');
    btn.disabled    = true;
    btn.textContent = 'Loading...';
    setStatus(status, 'Fetching git status...', '#8b949e');

    fetch(`/projects/${projectId}/git_status`)
        .then(r => r.json())
        .then(data => {
            status.style.display = 'none';
            btn.disabled    = false;
            btn.textContent = '↺ Regenerate & Commit';

            if (!data.files || !data.files.length) {
                setStatus(status, 'No changed files — working tree clean.', '#8b949e');
                return;
            }

            renderFileList(data.files);
            document.getElementById('file-panel').style.display = 'block';
            generateCommitMessage(projectId);
        })
        .catch(e => {
            setStatus(status, 'Error fetching status: ' + e, '#f85149');
            btn.disabled    = false;
            btn.textContent = '↺ Regenerate & Commit';
        });
}

function generateCommitMessage(projectId) {
    const input = document.getElementById('commit-message');
    const btn   = document.getElementById('refresh-msg-btn');
    if (!input) return;
    input.value       = '';
    input.placeholder = 'Generating AI commit message...';
    if (btn) btn.disabled = true;

    fetch(`/projects/${projectId}/suggest_commit_message`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            input.value       = data.message || 'Update files';
            input.placeholder = 'Commit message...';
            if (btn) btn.disabled = false;
        })
        .catch(() => {
            input.value       = 'Update files';
            input.placeholder = 'Commit message...';
            if (btn) btn.disabled = false;
        });
}

function cancelRegenerate() {
    const panel        = document.getElementById('file-panel');
    const status       = document.getElementById('regen-status');
    const commitStatus = document.getElementById('commit-status');
    if (panel)        panel.style.display  = 'none';
    if (status)       status.style.display = 'none';
    if (commitStatus) commitStatus.textContent = '';
}

function confirmRegenerate(projectId) {
    const files  = getSelectedFiles();
    const msg    = (document.getElementById('commit-message')?.value || '').trim() || 'Update files';
    const status = document.getElementById('commit-status');
    const btn    = document.getElementById('confirm-btn');

    if (!files.length) {
        setStatus(status, 'No files selected.', '#f85149');
        return;
    }

    btn.disabled = true;
    setStatus(status, 'Building graph and committing...', '#8b949e');

    const form = new FormData();
    form.append('message', msg);
    files.forEach(f => form.append('files', f));

    fetch(`/projects/${projectId}/graph/regenerate_and_commit`, { method: 'POST', body: form })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok') {
                setStatus(status, `✓ Committed ${data.hash} — reloading...`, '#3fb950');
                setTimeout(() => location.reload(), 1000);
            } else {
                setStatus(status,
                    'Error: ' + (data.error || JSON.stringify(data.errors)),
                    '#f85149'
                );
                btn.disabled = false;
            }
        })
        .catch(e => {
            setStatus(status, 'Fetch error: ' + e, '#f85149');
            btn.disabled = false;
        });
}

// ── Graph node search ─────────────────────────────────────────────────────

function searchGraph(projectId) {
    const q = document.getElementById('search-input')?.value.trim();
    if (!q) return;
    const form = new FormData();
    form.append('query', q);
    fetch(`/projects/${projectId}/graph/search`, { method: 'POST', body: form })
        .then(r => r.json())
        .then(results => {
            const div = document.getElementById('search-results');
            if (!div) return;
            if (!results.length) {
                div.innerHTML = '<p style="color:#8b949e;">No results.</p>';
                return;
            }
            div.innerHTML = '<div class="card"><table>' +
                '<tr><th>Score</th><th>Node</th><th>Location</th><th></th></tr>' +
                results.map(r =>
                    `<tr>
                        <td style="color:#8b949e;font-size:0.8rem;">${r.score}</td>
                        <td style="font-family:monospace;font-size:0.82rem;color:#58a6ff;">${escapeHtml(r.node_id)}</td>
                        <td style="font-size:0.82rem;color:#8b949e;">${escapeHtml(r.file)}:${r.line_start}-${r.line_end}</td>
                        <td><a href="/projects/${projectId}/graph/node/${encodeURIComponent(r.node_id)}" class="btn">View</a></td>
                    </tr>`
                ).join('') +
                '</table></div>';
        })
        .catch(err => console.error('searchGraph:', err));
}

// ── Plan ──────────────────────────────────────────────────────────────────
//
// The Plan tab is two things and both are here: the conversation, and the
// assessment. The conversation posts one turn at a time and paints the reply
// into the page. The assessment asks the planner model to read what the last
// commits settled, what the graph looks like and where the log stopped, and
// answer with the next task.

function sendPlanMessage(projectId, convId) {
    const input    = document.getElementById('plan-input');
    const messages = document.getElementById('plan-messages');
    const sendBtn  = document.getElementById('plan-send');
    const msg      = input?.value.trim();
    if (!msg) return;

    messages.innerHTML +=
        `<div class="msg user"><div class="role">You</div>
        <div class="content">${escapeHtml(msg)}</div></div>
        <div class="msg assistant" id="pending">
        <div class="role">Residuality</div>
        <div class="content" style="color:#8b949e;">Thinking... (local models may take a minute)</div></div>`;
    messages.scrollTop = messages.scrollHeight;
    input.value = '';
    if (sendBtn) sendBtn.disabled = true;

    const form = new FormData();
    form.append('message', msg);
    form.append('chat', convId);

    fetch(`/projects/${projectId}/plan/chat`, { method: 'POST', body: form })
        .then(r => {
            if (!r.ok) return r.text().then(t => { throw new Error('Server error ' + r.status + ': ' + t.slice(0, 200)); });
            return r.json();
        })
        .then(data => {
            const pending = document.getElementById('pending');
            if (pending) pending.querySelector('.content').textContent =
                data.response || data.error || '(no response)';
            messages.scrollTop = messages.scrollHeight;
            if (sendBtn) sendBtn.disabled = false;
        })
        .catch(e => {
            const pending = document.getElementById('pending');
            if (pending) pending.querySelector('.content').textContent = 'Error: ' + e.message;
            if (sendBtn) sendBtn.disabled = false;
        });
}

// Both of these are form posts that the server answers with a redirect, so
// there is nothing to parse back: the response's own URL is where the browser
// should end up. The fallback is the Plan page itself, for the case the
// redirect is stripped by something in front of the app.
function newConversation(projectId) {
    const form = new FormData();
    form.append('chat', '');
    fetch(`/projects/${projectId}/plan/new`, { method: 'POST', body: form })
        .then(r => { window.location.href = r.url || `/projects/${projectId}/plan`; })
        .catch(e => alert('Could not start a new conversation: ' + e.message));
}

function deleteConversation(projectId, convId) {
    if (!confirm('Delete this conversation?')) return;
    const form = new FormData();
    form.append('chat', convId);
    fetch(`/projects/${projectId}/plan/delete`, { method: 'POST', body: form })
        .then(r => { window.location.href = r.url || `/projects/${projectId}/plan`; })
        .catch(e => alert('Could not delete the conversation: ' + e.message));
}

// ── Plan: assessment ──────────────────────────────────────────────────────

function assessListHtml(title, items) {
    if (!items || !items.length) return '';
    return '<p class="lab">' + title + '</p><ul>' +
        items.map(i => '<li>' + escapeHtml(i) + '</li>').join('') +
        '</ul>';
}

function renderAssessment(p) {
    let html = '';
    if (p.assessment) html += '<p><span class="lab">Assessment:</span> ' + escapeHtml(p.assessment) + '</p>';
    if (p.next_task)  html += '<p><span class="lab">Next task:</span> '  + escapeHtml(p.next_task)  + '</p>';
    if (p.approach)   html += '<p><span class="lab">Approach:</span> '   + escapeHtml(p.approach)   + '</p>';
    html += assessListHtml('Files to touch', p.files_to_touch);
    if (p.risks)      html += '<p><span class="lab">Risks:</span> '      + escapeHtml(p.risks)      + '</p>';
    html += assessListHtml('Open questions', p.open_questions);

    // What the answer was actually based on. A fresh project has no snapshot
    // and no graph yet, and an assessment that does not say so reads as though
    // the model had looked at everything.
    const c    = p.context || {};
    const commits = c.commits || 0;
    const bits = [
        c.snapshot      ? 'latest snapshot' : 'no snapshot indexed yet',
        c.graph_summary ? 'graph outline'   : 'no graph built yet',
        commits + ' recent commit' + (commits === 1 ? '' : 's'),
    ];
    html += '<p style="font-size:0.75rem;color:#8b949e;margin-top:0.5rem;">' +
            'Read from: ' + bits.join(' · ') + '</p>';
    return html;
}

function assessProject(projectId) {
    const btn    = document.getElementById('assess-btn');
    const status = document.getElementById('assess-status');
    const out    = document.getElementById('assess-output');
    if (!btn || !out) return;

    const label = btn.textContent;
    btn.disabled       = true;
    btn.textContent    = 'Thinking…';
    status.textContent = 'Reading the snapshot, the graph and the log…';
    out.innerHTML      = '';

    fetch(`/projects/${projectId}/plan/assess`, { method: 'POST' })
        .then(r => r.json().then(d => ({ ok: r.ok, d })))
        .then(({ ok, d }) => {
            btn.disabled    = false;
            btn.textContent = label;
            status.textContent = '';
            if (!ok || d.error) {
                out.innerHTML = '<p style="color:#f85149;font-size:0.88rem;">Assess failed: ' +
                    escapeHtml(d.error || 'unknown error') + '</p>';
                return;
            }
            out.innerHTML = renderAssessment(d);
        })
        .catch(e => {
            btn.disabled    = false;
            btn.textContent = label;
            status.textContent = '';
            out.innerHTML = '<p style="color:#f85149;font-size:0.88rem;">Assess failed: ' +
                escapeHtml(String(e)) + '</p>';
        });
}

// ── Graph drill-down and external imports toggle ──────────────────────────

var _showExternal = false;

function wireGraphClicks() {
    document.querySelectorAll('.svg-container a').forEach(function(a) {
        a.addEventListener('click', function(e) {
            e.preventDefault();
            var title = a.querySelector('title');
            if (title) {
                drillDown(title.textContent.trim());
            }
        });
    });
}

function toggleExternalImports(projectId) {
    _showExternal = !_showExternal;
    var btn = document.getElementById('toggle-external-btn');
    if (btn) btn.textContent = _showExternal ? '− Hide pip imports' : '+ Show pip imports';
    loadFileGraph(projectId);
}

function loadFileGraph(projectId) {
    var container = document.querySelector('.svg-container');
    var status    = document.getElementById('regen-status');
    setStatus(status, 'Rendering...', '#8b949e');

    fetch('/projects/' + projectId + '/graph/svg?external=' + _showExternal)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            status.style.display = 'none';
            container.innerHTML  = data.svg;
            wireGraphClicks();
        })
        .catch(function(e) { setStatus(status, 'Error: ' + e, '#f85149'); });
}

function drillDown(projectId, filepath) {
    var status    = document.getElementById('regen-status');
    var container = document.querySelector('.svg-container');
    var backBtn   = document.getElementById('back-btn');
    var toggleBtn = document.getElementById('toggle-external-btn');
    setStatus(status, 'Loading ' + filepath + '...', '#8b949e');

    fetch('/projects/' + projectId + '/graph/file/' + encodeURIComponent(filepath))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            status.style.display = 'none';
            container.innerHTML  = data.svg;
            if (backBtn)   backBtn.style.display   = 'inline-block';
            if (toggleBtn) toggleBtn.style.display = 'none';
        })
        .catch(function(e) { setStatus(status, 'Error: ' + e, '#f85149'); });
}

// Wire clicks on initial page load — unless the page owns SVG clicks itself
// (graph.html sets window.__graphClicksOwned, since its handler is aware of
// graphviz's <g class="node"><title> structure).
document.addEventListener('DOMContentLoaded', function() {
    if (window.__graphClicksOwned) return;
    wireGraphClicks();
});