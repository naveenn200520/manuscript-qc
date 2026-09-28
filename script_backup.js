
(function() {
    'use strict';

    const manuscriptText = {{ manuscript.working_text | tojson }};
    const originalText = {{ manuscript.original_text | default('') | tojson }};
    const lines = manuscriptText.split('\n');
    const origLines = originalText.split('\n');
    const projectId = {{ project.id }};
    let selectedEid = null;
    let isCompareMode = false;

    // ─── RENDER EDITOR ───
    const editor = document.getElementById('editorScroll');

    function escapeHtml(t) {
        const d = document.createElement('div');
        d.textContent = t;
        return d.innerHTML;
    }

    function renderEditor() {
        let html = '';
        for (let i = 0; i < lines.length; i++) {
            const n = i + 1;
            const c = escapeHtml(lines[i]) || '&nbsp;';
            html += `<div class="fv-line" id="ln-${n}" data-ln="${n}">` +
                    `<span class="fv-line-num">${n}</span>` +
                    `<span class="fv-line-indicator"></span>` +
                    `<span class="fv-line-content">${c}</span></div>`;
        }
        editor.innerHTML = html;
    }

    renderEditor();

    // ─── APPLY HIGHLIGHTS ───
    function applyHighlights() {
        renderEditor();
        const rows = document.querySelectorAll('.fv-err-row');
        const byLine = {};

        rows.forEach(r => {
            const ln = parseInt(r.dataset.line) || 1;
            const s = parseInt(r.dataset.start);
            const e = parseInt(r.dataset.end);
            const sev = r.dataset.sev;
            const st = r.dataset.status;
            const eid = r.dataset.eid;
            if (!byLine[ln]) byLine[ln] = [];
            byLine[ln].push({ s, e, sev, st, eid });
        });

        for (const [ln, errs] of Object.entries(byLine)) {
            const lineEl = document.getElementById(`ln-${ln}`);
            if (!lineEl) continue;

            // Determine worst severity on this line (for line-level highlight)
            const hasOpen = errs.some(e => e.st === 'OPEN');
            if (hasOpen) {
                const hasCrit = errs.some(e => e.sev === 'CRITICAL' && e.st === 'OPEN');
                const hasReview = errs.some(e => e.sev === 'REVIEW' && e.st === 'OPEN');
                if (hasCrit) lineEl.classList.add('fv-line-error');
                else if (hasReview) lineEl.classList.add('fv-line-warning');
            }

            // Selected line
            const isSelected = errs.some(e => e.eid == selectedEid);
            if (isSelected) {
                lineEl.classList.remove('fv-line-error', 'fv-line-warning');
                lineEl.classList.add('fv-line-selected');
            }

            // Inline highlights
            const contentEl = lineEl.querySelector('.fv-line-content');
            const lineText = lines[parseInt(ln) - 1] || '';
            let lineStart = 0;
            for (let i = 0; i < parseInt(ln) - 1; i++) lineStart += lines[i].length + 1;

            errs.sort((a, b) => b.s - a.s);
            let htmlContent = escapeHtml(lineText);

            for (const err of errs) {
                const rs = err.s - lineStart;
                const re = err.e - lineStart;
                if (rs < 0 || rs > lineText.length) continue;
                const ce = Math.min(re, lineText.length);
                if (ce <= rs) continue;

                const before = escapeHtml(lineText.substring(0, rs));
                const part = escapeHtml(lineText.substring(rs, ce));
                const after = escapeHtml(lineText.substring(ce));
                const resolvedCls = err.st !== 'OPEN' ? ' resolved' : '';
                const selCls = err.eid == selectedEid ? ' selected-err' : '';

                htmlContent = before +
                    `<span class="fv-err-span sev-${err.sev}${resolvedCls}${selCls}" data-eid="${err.eid}" onclick="selectById('${err.eid}')">${part}</span>` +
                    after;
            }
            contentEl.innerHTML = htmlContent;
        }
    }

    applyHighlights();

    // ─── SELECT ROW ───
    window.selectRow = function(row) {
        document.querySelectorAll('.fv-err-row.fv-row-selected').forEach(r => r.classList.remove('fv-row-selected'));
        document.querySelectorAll('.fv-line.fv-line-selected').forEach(l => l.classList.remove('fv-line-selected'));

        row.classList.add('fv-row-selected');
        selectedEid = row.dataset.eid;

        const ln = parseInt(row.dataset.line) || 1;
        const col = parseInt(row.dataset.col) || 1;
        const etype = row.dataset.etype;
        const sev = row.dataset.sev;
        const status = row.dataset.status;
        const original = row.dataset.original;
        const edited = row.dataset.edited;
        const desc = row.dataset.desc;

        // Scroll editor to line
        const lineEl = document.getElementById(`ln-${ln}`);
        if (lineEl) lineEl.scrollIntoView({ behavior: 'smooth', block: 'center' });

        // Populate drawer
        const drawer = document.getElementById('detailDrawer');
        drawer.classList.add('active');

        document.getElementById('drawerDesc').textContent = desc || 'Content difference detected.';
        document.getElementById('drawerOriginal').textContent = original || '—';
        document.getElementById('drawerEdited').textContent = edited || '—';

        const actEl = document.getElementById('drawerActions');
        if (status === 'OPEN') {
            const restoreLabel = etype.includes('JOB_SHEET') ? 'Apply Fix' : 'Restore Original';
            actEl.innerHTML = `
                <button class="fv-action-btn fv-action-restore" onclick="resolveErr(${selectedEid},'RESTORE_ORIGINAL')">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9 15L3 9m0 0l6-6M3 9h12a6 6 0 010 12h-3"/></svg>
                    ${restoreLabel}
                </button>
                <button class="fv-action-btn fv-action-keep" onclick="resolveErr(${selectedEid},'KEEP_EDIT')">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg>
                    Keep Edit
                </button>
                <button class="fv-action-btn" onclick="resolveErr(${selectedEid},'IGNORE')">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
                    Dismiss
                </button>
                <button class="fv-action-btn" onclick="resolveErr(${selectedEid},'MARK_REVIEWED')" style="color:var(--fv-warning);border-color:var(--fv-warning)">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                    Reviewed
                </button>`;
        } else {
            actEl.innerHTML = `<span class="fv-status-tag ${status}" style="text-align:center;padding:10px;font-size:13px;width:100%">${status}</span>`;
        }

        applyHighlights();
    };

    window.selectById = function(eid) {
        const row = document.querySelector(`.fv-err-row[data-eid="${eid}"]`);
        if (row) selectRow(row);
    };

    // ─── RESOLVE ERROR ───
    window.resolveErr = function(eid, action) {
        fetch(`/errors/${eid}/action`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ action }),
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                const row = document.querySelector(`.fv-err-row[data-eid="${eid}"]`);
                if (row) {
                    row.dataset.status = data.new_status;
                    row.classList.add('fv-row-resolved');
                    const tag = row.querySelector('[data-status-tag]');
                    if (tag) { tag.className = `fv-status-tag ${data.new_status}`; tag.textContent = data.new_status; }
                }
                const actEl = document.getElementById('drawerActions');
                actEl.innerHTML = `<span class="fv-status-tag ${data.new_status}" style="text-align:center;padding:10px;font-size:13px;width:100%">${data.new_status}</span>`;
                applyHighlights();
                updateStats();
            }
        })
        .catch(e => console.error('Resolve error:', e));
    };

    function updateStats() {
        const rows = document.querySelectorAll('.fv-err-row');
        let open = 0, crit = 0, rev = 0, warn = 0, resolved = 0;
        rows.forEach(r => {
            if (r.dataset.status === 'OPEN') {
                open++;
                if (r.dataset.sev === 'CRITICAL') crit++;
                if (r.dataset.sev === 'REVIEW') rev++;
                if (r.dataset.sev === 'WARNING') warn++;
            } else resolved++;
        });

        document.getElementById('openCount').textContent = open;
        document.getElementById('statCritical').textContent = crit;
        document.getElementById('statReview').textContent = rev;
        document.getElementById('statWarning').textContent = warn;
        document.getElementById('statResolved').textContent = resolved;
        document.getElementById('panelCounts').textContent = `${crit} Errors, ${rev} Review, ${warn} Info`;

        const total = rows.length;
        const pct = total > 0 ? Math.round((resolved / total) * 100) : 100;
        document.getElementById('progressFill').style.width = pct + '%';
        document.getElementById('progressText').textContent = pct + '%';
    }

    // ─── FILTER ───
    window.applyFilter = function(f, btn) {
        document.querySelectorAll('.fv-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.fv-err-row').forEach(row => {
            let show = true;
            if (f === 'CRITICAL' || f === 'REVIEW' || f === 'WARNING') show = row.dataset.sev === f;
            else if (f === 'OPEN') show = row.dataset.status === 'OPEN';
            row.style.display = show ? '' : 'none';
        });
    };

    // ─── RESIZE HANDLE ───
    const handle = document.getElementById('resizeHandle');
    const panel = document.getElementById('problemPanel');
    const editorWrap = document.getElementById('editorWrap');
    let isResizing = false;

    handle.addEventListener('mousedown', (e) => {
        isResizing = true;
        document.body.style.cursor = 'row-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!isResizing) return;
        const appRect = document.querySelector('.fv-app').getBoundingClientRect();
        const toolbarH = 52;
        const handleH = 6;
        const minPanel = 120;
        const minEditor = 200;
        const y = e.clientY - appRect.top - toolbarH;
        const available = appRect.height - toolbarH - handleH;
        const panelH = available - y;
        if (panelH >= minPanel && y >= minEditor) {
            panel.style.height = panelH + 'px';
        }
    });

    document.addEventListener('mouseup', () => {
        if (isResizing) {
            isResizing = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        }
    });

    // ─── AUTO-SELECT FIRST OPEN ───
    const firstOpen = document.querySelector('.fv-err-row:not(.fv-row-resolved)');
    if (firstOpen) setTimeout(() => selectRow(firstOpen), 250);

})();

// ─── EXPORT DROPDOWN ───
window.toggleExport = function(e) {
    e.stopPropagation();
    document.getElementById('exportDropdown').classList.toggle('open');
};
document.addEventListener('click', () => {
    const dd = document.getElementById('exportDropdown');
    if (dd) dd.classList.remove('open');
});
