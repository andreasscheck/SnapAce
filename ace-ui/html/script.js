'use strict';

const QUERY_OBJECTS = { ace: null, print_stats: ['state'] };

let ws = null;
let wsReady = false;
let requestId = 1;
const pendingRequests = new Map();
let subscribed = false;
let initialized = false;

const GATE_EMPTY = 0;

let cachedAce = {
    connected: false,
    status: 'unknown',
    temp: 0,
    dryer_status: { status: 'stop', target_temp: 0, remain_time: 0 },
    gate_status: [0, 0, 0, 0],
    gate_extruder: [null, null, null, null],
    slots: [{}, {}, {}, {}],
    feed_assist_index: -1,
    desired_feed_assist_index: -1,
    pending_extruder_gate: [null, null, null, null],
    gate_map_source: 'config',
    gate_map_restart_required: false,
};

let printState = 'standby';
// Per-extruder gate selections the user has changed but not saved yet;
// null while the selects simply mirror the printer's mapping.
let gateMapDraft = null;

document.addEventListener('DOMContentLoaded', () => {
    if (initialized) return;
    initialized = true;
    initializeWebSocket();
    initializeDryerControls();
    initializeGateMapControls();
});

function initializeDryerControls() {
    document.getElementById('dryer-start-btn').addEventListener('click', async () => {
        const temp = parseInt(document.getElementById('dryer-temp').value, 10);
        const duration = parseInt(document.getElementById('dryer-duration').value, 10);
        if (!Number.isFinite(temp) || temp <= 0) {
            showStatus('Enter a valid temperature', 'error');
            return;
        }
        if (!Number.isFinite(duration) || duration <= 0) {
            showStatus('Enter a valid duration', 'error');
            return;
        }
        try {
            showStatus(`Starting drying @ ${temp}°C for ${duration}min…`, 'info');
            await sendGcode(`ACE_START_DRYING TEMP=${temp} DURATION=${duration}`);
            showStatus('Drying started', 'success');
        } catch (err) {
            showStatus(`Start drying failed: ${err.message}`, 'error');
        }
    });

    document.getElementById('dryer-stop-btn').addEventListener('click', async () => {
        try {
            showStatus('Stopping drying…', 'info');
            await sendGcode('ACE_STOP_DRYING');
            showStatus('Drying stopped', 'success');
        } catch (err) {
            showStatus(`Stop drying failed: ${err.message}`, 'error');
        }
    });
}

function initializeWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/websocket`);

    ws.onopen = () => {
        sendRPC('server.connection.identify', {
            client_name: 'ace-ui',
            version: '1.0.0',
            type: 'web',
            url: location.href,
        }).then(() => {
            wsReady = true;
            setConnectionStatus(true);
            showStatus('');
            loadInitialData();
        }).catch(() => {
            showStatus('Failed to connect to Moonraker', 'error');
        });
    };

    ws.onclose = () => {
        wsReady = false;
        subscribed = false;
        setConnectionStatus(false);
        render();
        showStatus('Disconnected — reconnecting…', 'error');
        setTimeout(initializeWebSocket, 2000);
    };

    ws.onerror = () => showStatus('WebSocket error', 'error');

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);

        if (msg.method === 'notify_status_update') {
            const update = msg.params[0];
            if (update.ace) Object.assign(cachedAce, update.ace);
            if (update.print_stats && update.print_stats.state) {
                printState = update.print_stats.state;
            }
            render();
            return;
        }

        if (msg.id && pendingRequests.has(msg.id)) {
            const { resolve, reject } = pendingRequests.get(msg.id);
            pendingRequests.delete(msg.id);
            if (msg.error) reject(msg.error); else resolve(msg.result);
        }
    };
}

function sendRPC(method, params = {}) {
    return new Promise((resolve, reject) => {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            reject(new Error('WebSocket not connected'));
            return;
        }
        const id = requestId++;
        pendingRequests.set(id, { resolve, reject });
        ws.send(JSON.stringify({ jsonrpc: '2.0', method, params, id }));
        setTimeout(() => {
            if (pendingRequests.has(id)) {
                pendingRequests.delete(id);
                reject(new Error('Request timeout'));
            }
        }, 30000);
    });
}

function setConnectionStatus(connected) {
    const dot = document.getElementById('connection-status');
    const text = document.getElementById('connection-text');
    if (dot) dot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
    if (text) text.textContent = connected ? 'Connected' : 'Disconnected';
}

async function loadInitialData() {
    try {
        if (!subscribed) {
            await sendRPC('printer.objects.subscribe', { objects: QUERY_OBJECTS });
            subscribed = true;
        }
        const result = await sendRPC('printer.objects.query', { objects: QUERY_OBJECTS });
        if (result.status.ace) Object.assign(cachedAce, result.status.ace);
        if (result.status.print_stats && result.status.print_stats.state) {
            printState = result.status.print_stats.state;
        }
        render();
    } catch (err) {
        showStatus(`Load failed: ${err.message}`, 'error');
    }
}

function escHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function colorHex(rgb) {
    if (!Array.isArray(rgb) || rgb.length < 3) return null;
    const [r, g, b] = rgb;
    if (r === 0 && g === 0 && b === 0) return null;
    return [r, g, b].map(c => Math.max(0, Math.min(255, c | 0)).toString(16).padStart(2, '0')).join('').toUpperCase();
}

function render() {
    renderDisconnectedBanner();
    renderSummary();
    renderDryerControls();
    renderGateMap();
    renderGates();
}

// The header dot only reflects the browser's Moonraker WebSocket link -
// the ACE unit itself connects over USB serial and can be unplugged or
// powered off while Moonraker stays perfectly reachable. `cachedAce.connected`
// is ace.py's own `self._connected` (the serial link), so surface it
// separately rather than let the WebSocket dot imply the ACE is present.
function renderDisconnectedBanner() {
    let banner = document.getElementById('ace-disconnected-banner');
    if (cachedAce.connected) {
        if (banner) banner.remove();
        return;
    }
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'ace-disconnected-banner';
        banner.className = 'ace-disconnected-banner';
        banner.textContent = 'ACE not connected (USB serial) — gate status below is stale.';
        document.getElementById('ace-summary').insertAdjacentElement('beforebegin', banner);
    }
}

function renderSummary() {
    const el = document.getElementById('ace-summary');
    const dryer = cachedAce.dryer_status || {};
    const dryerText = dryer.status && dryer.status !== 'stop'
        ? `Drying @ ${dryer.target_temp}°C, ${Math.round((dryer.remain_time || 0) / 60)} min left`
        : 'Idle';

    el.innerHTML = '';
    const addField = (label, html) => {
        const span = document.createElement('span');
        span.innerHTML = `${escHtml(label)}: ${html}`;
        el.appendChild(span);
    };
    const strong = (text, cls) => `<strong${cls ? ` class="${cls}"` : ''}>${escHtml(text)}</strong>`;

    addField('ACE', cachedAce.connected
        ? strong('Connected', 'connected')
        : strong('Disconnected', 'disconnected'));
    addField('Status', strong(cachedAce.status || 'unknown'));
    addField('Temp', strong(`${cachedAce.temp ?? 0}°C`));
    addField('Dryer', strong(dryerText));
}

function isDrying() {
    const dryer = cachedAce.dryer_status || {};
    return Boolean(dryer.status) && dryer.status !== 'stop';
}

// Only offer the action that makes sense for the dryer's current state:
// start (with editable temp/duration) while idle, stop while drying.
function renderDryerControls() {
    const drying = isDrying();
    const available = wsReady && cachedAce.connected;
    document.getElementById('dryer-start-btn').disabled = !available || drying;
    document.getElementById('dryer-stop-btn').disabled = !available || !drying;
    document.getElementById('dryer-temp').disabled = drying;
    document.getElementById('dryer-duration').disabled = drying;
}

function renderGates() {
    const grid = document.getElementById('gates-grid');
    grid.innerHTML = '';

    const gateStatus = cachedAce.gate_status || [0, 0, 0, 0];
    const gateExtruder = cachedAce.gate_extruder || [null, null, null, null];
    const slots = cachedAce.slots || [{}, {}, {}, {}];

    for (let i = 0; i < 4; i++) {
        grid.appendChild(createGateCard(i, gateStatus[i], gateExtruder[i], slots[i] || {}));
    }
}

function createGateCard(index, status, extruder, slot) {
    const loaded = status !== GATE_EMPTY;

    const card = document.createElement('div');
    card.className = 'gate-card' + (loaded ? ' loaded' : '');

    const header = document.createElement('div');
    header.className = 'gate-header';

    const title = document.createElement('span');
    title.className = 'gate-title';
    title.textContent = `Gate ${index + 1}`;
    header.appendChild(title);

    const badge = document.createElement('span');
    badge.className = 'gate-badge ' + (loaded ? 'loaded' : 'empty');
    badge.textContent = loaded ? 'Loaded' : 'Empty';
    header.appendChild(badge);

    card.appendChild(header);

    const extruderLine = document.createElement('div');
    extruderLine.className = 'gate-extruder';
    extruderLine.textContent = extruder != null
        ? `→ Extruder ${extruder + 1}`
        : 'Not mapped to an extruder';
    card.appendChild(extruderLine);
    card.appendChild(createFeedAssistBadge(index));

    if (!loaded) {
        const empty = document.createElement('div');
        empty.className = 'gate-empty-msg';
        empty.textContent = 'No spool detected';
        card.appendChild(empty);
        card.appendChild(createGateActions(index));
        return card;
    }

    const body = document.createElement('div');
    body.className = 'gate-body';

    const addRow = (label, html) => {
        const l = document.createElement('span');
        l.className = 'field-label';
        l.textContent = label;
        const v = document.createElement('span');
        v.className = 'field-value';
        v.innerHTML = html;
        body.appendChild(l);
        body.appendChild(v);
    };

    const na = '<span class="field-unknown">—</span>';
    const type = slot.type && slot.type !== '' ? slot.type : null;
    const brand = slot.brand && slot.brand !== '' ? slot.brand : null;
    const hex = colorHex(slot.color);
    const hasRfid = slot.rfid === 2;

    addRow('Material', type ? escHtml(type) : na);
    addRow('Brand', brand ? escHtml(brand) : na);
    addRow('Color', hex
        ? `<span class="color-swatch" style="background:#${hex}"></span>#${hex}`
        : na);

    card.appendChild(body);

    const rfid = document.createElement('span');
    rfid.className = 'rfid-badge' + (hasRfid ? ' tagged' : '');
    rfid.textContent = hasRfid ? 'RFID tag read' : 'No RFID data';
    card.appendChild(rfid);

    card.appendChild(createGateActions(index));

    return card;
}

function createFeedAssistBadge(index) {
    // Feed assist is a single shared mechanism - only one gate can have it
    // active at a time. desired_feed_assist_index differing from
    // feed_assist_index means a switch is queued/in flight.
    const active = cachedAce.feed_assist_index === index;
    const pending = !active && cachedAce.desired_feed_assist_index === index;

    const badge = document.createElement('div');
    badge.className = 'feed-assist-badge'
        + (active ? ' active' : pending ? ' pending' : '');
    badge.textContent = active
        ? 'Feed Assist: On'
        : pending ? 'Feed Assist: switching…' : 'Feed Assist: Off';
    return badge;
}

const JOG_LENGTH_MM = 20; // 2cm

function createGateActions(index) {
    const actions = document.createElement('div');
    actions.className = 'gate-actions';

    const mkBtn = (text, handler, wide, active) => {
        const btn = document.createElement('button');
        btn.className = 'gate-action-btn' + (wide ? ' wide' : '') + (active ? ' active' : '');
        btn.textContent = text;
        btn.disabled = !wsReady;
        btn.addEventListener('click', handler);
        return btn;
    };

    const feedAssistActive = cachedAce.feed_assist_index === index;

    actions.appendChild(mkBtn('↧ Feed 2cm', () => feedGate(index)));
    actions.appendChild(mkBtn('↥ Retract 2cm', () => retractGate(index)));
    actions.appendChild(mkBtn(
        feedAssistActive ? '⚡ Disable Feed Assist' : '⚡ Enable Feed Assist',
        () => toggleFeedAssist(index, feedAssistActive),
        true,
        feedAssistActive));
    return actions;
}

async function sendGcode(script) {
    try {
        return await sendRPC('printer.gcode.script', { script });
    } catch (error) {
        if (error.message && error.message.includes('!!')) {
            const m = error.message.match(/!!\s*(.+)/);
            if (m) throw new Error(m[1]);
        }
        throw error;
    }
}

async function feedGate(index) {
    try {
        showStatus(`Feeding gate ${index + 1}…`, 'info');
        await sendGcode(`ACE_FEED INDEX=${index} LENGTH=${JOG_LENGTH_MM}`);
        showStatus(`Gate ${index + 1} fed ${JOG_LENGTH_MM}mm`, 'success');
    } catch (err) {
        showStatus(`Feed failed: ${err.message}`, 'error');
    }
}

async function retractGate(index) {
    try {
        showStatus(`Retracting gate ${index + 1}…`, 'info');
        await sendGcode(`ACE_RETRACT INDEX=${index} LENGTH=${JOG_LENGTH_MM}`);
        showStatus(`Gate ${index + 1} retracted ${JOG_LENGTH_MM}mm`, 'success');
    } catch (err) {
        showStatus(`Retract failed: ${err.message}`, 'error');
    }
}

async function toggleFeedAssist(index, currentlyActive) {
    try {
        if (currentlyActive) {
            showStatus(`Disabling feed assist for gate ${index + 1}…`, 'info');
            await sendGcode(`ACE_DISABLE_FEED_ASSIST INDEX=${index}`);
            showStatus(`Feed assist disabled for gate ${index + 1}`, 'success');
        } else {
            showStatus(`Enabling feed assist for gate ${index + 1}…`, 'info');
            await sendGcode(`ACE_ENABLE_FEED_ASSIST INDEX=${index}`);
            showStatus(`Feed assist enabled for gate ${index + 1}`, 'success');
        }
    } catch (err) {
        showStatus(`Toggle feed assist failed: ${err.message}`, 'error');
    }
}

function isPrinting() {
    return printState === 'printing' || printState === 'paused';
}

function savedGateMap() {
    return (cachedAce.pending_extruder_gate || [null, null, null, null]).slice(0, 4);
}

function currentGateMapSelection() {
    return gateMapDraft ? gateMapDraft.slice() : savedGateMap();
}

function duplicateGates(mapping) {
    const seen = new Set();
    const dupes = new Set();
    mapping.forEach(gate => {
        if (gate == null) return;
        if (seen.has(gate)) dupes.add(gate);
        seen.add(gate);
    });
    return dupes;
}

function initializeGateMapControls() {
    document.getElementById('gate-map-save-btn').addEventListener('click', saveGateMap);
    document.getElementById('gate-map-reset-btn').addEventListener('click', resetGateMap);
    document.getElementById('gate-map-restart-btn').addEventListener('click', restartKlipper);
}

let lastGateMapRowsKey = null;

function renderGateMap() {
    const selection = currentGateMapSelection();
    const dupes = duplicateGates(selection);
    const locked = !wsReady || isPrinting();

    // render() runs on every ACE status update (about once a second);
    // rebuilding the selects each time would close an open dropdown.
    const rowsKey = JSON.stringify([selection, locked]);
    if (rowsKey !== lastGateMapRowsKey) {
        lastGateMapRowsKey = rowsKey;
        renderGateMapRows(selection, dupes, locked);
    }
    renderGateMapState(selection, dupes, locked);
}

function renderGateMapRows(selection, dupes, locked) {
    const rows = document.getElementById('gate-map-rows');
    rows.innerHTML = '';
    for (let e = 0; e < 4; e++) {
        const row = document.createElement('label');
        row.className = 'gate-map-row' + (dupes.has(selection[e]) ? ' invalid' : '');

        const name = document.createElement('span');
        name.className = 'gate-map-extruder';
        name.textContent = `Extruder ${e + 1}`;
        const tool = document.createElement('span');
        tool.className = 'gate-map-tool';
        tool.textContent = `T${e}`;
        name.appendChild(tool);
        row.appendChild(name);

        const select = document.createElement('select');
        select.disabled = locked;
        const options = [['', 'Built-in feeder']];
        for (let g = 0; g < 4; g++) options.push([String(g), `ACE Gate ${g + 1}`]);
        options.forEach(([value, text]) => {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = text;
            select.appendChild(opt);
        });
        select.value = selection[e] == null ? '' : String(selection[e]);
        select.addEventListener('change', () => {
            const draft = currentGateMapSelection();
            draft[e] = select.value === '' ? null : parseInt(select.value, 10);
            gateMapDraft = draft;
            renderGateMap();
        });
        row.appendChild(select);
        rows.appendChild(row);
    }
}

function renderGateMapState(selection, dupes, locked) {
    const source = document.getElementById('gate-map-source');
    const fromUi = cachedAce.gate_map_source === 'ui';
    source.textContent = fromUi ? 'Saved in UI' : 'From ace.cfg';
    source.className = 'gate-map-source' + (fromUi ? ' ui' : '');

    const unsaved = gateMapDraft !== null &&
        JSON.stringify(gateMapDraft) !== JSON.stringify(savedGateMap());
    const hint = document.getElementById('gate-map-hint');
    if (isPrinting()) {
        hint.textContent = 'Mapping can only be changed while no print is running.';
        hint.className = 'gate-map-hint';
    } else if (dupes.size) {
        hint.textContent = 'Each ACE gate can only feed one extruder.';
        hint.className = 'gate-map-hint error';
    } else if (unsaved) {
        hint.textContent = 'Unsaved changes.';
        hint.className = 'gate-map-hint warning';
    } else {
        hint.textContent = '';
        hint.className = 'gate-map-hint';
    }

    document.getElementById('gate-map-save-btn').disabled = locked || dupes.size > 0 || !unsaved;
    document.getElementById('gate-map-reset-btn').disabled = locked || !fromUi;

    const restart = document.getElementById('gate-map-restart');
    restart.hidden = !cachedAce.gate_map_restart_required;
    document.getElementById('gate-map-restart-btn').disabled = locked;
}

function formatGateMap(mapping) {
    const entries = mapping
        .map((gate, extruder) => gate == null ? null : `${extruder}=${gate}`)
        .filter(Boolean);
    return entries.length ? entries.join(',') : 'none';
}

async function saveGateMap() {
    const mapping = currentGateMapSelection();
    if (duplicateGates(mapping).size) return;
    try {
        showStatus('Saving gate mapping…', 'info');
        await sendGcode(`ACE_SET_GATE_MAP MAP="${formatGateMap(mapping)}"`);
        gateMapDraft = null;
        showStatus('Gate mapping saved — restart Klipper to apply', 'success');
    } catch (err) {
        showStatus(`Saving gate mapping failed: ${err.message}`, 'error');
    }
    render();
}

async function resetGateMap() {
    if (!confirm('Drop the mapping saved in the UI and use extruder_gate_map from ace.cfg again?')) return;
    try {
        showStatus('Resetting gate mapping…', 'info');
        await sendGcode('ACE_RESET_GATE_MAP');
        gateMapDraft = null;
        showStatus('Gate mapping reset to ace.cfg — restart Klipper to apply', 'success');
    } catch (err) {
        showStatus(`Reset failed: ${err.message}`, 'error');
    }
    render();
}

async function restartKlipper() {
    if (isPrinting()) return;
    if (!confirm('Restart Klipper now to apply the new gate mapping?')) return;
    try {
        showStatus('Restarting Klipper…', 'info');
        await sendRPC('printer.firmware_restart');
        showStatus('Klipper is restarting…', 'success');
        // Klipper's objects are recreated, so the existing subscription
        // goes stale - re-subscribe once it is back.
        subscribed = false;
        setTimeout(loadInitialData, 8000);
    } catch (err) {
        showStatus(`Restart failed: ${err.message}`, 'error');
    }
}

let statusClearTimer = null;

function showStatus(message, type = 'info') {
    const el = document.getElementById('status-message');
    if (!el) return;
    el.textContent = message;
    el.className = `status-message status-${type}`;

    clearTimeout(statusClearTimer);
    if (type !== 'error' && message) {
        statusClearTimer = setTimeout(() => { el.textContent = ''; }, 4000);
    }
}
