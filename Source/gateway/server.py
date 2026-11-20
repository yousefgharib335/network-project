# MCT431 ATR UART upgrade: match production_station_atr firmware; replace both together.
import asyncio
import json
import time
import os
from pathlib import Path
from datetime import datetime

import serial
from serial.tools import list_ports
from aiohttp import WSMsgType, web

# ASUS Gateway configuration
HOST = "0.0.0.0"
PORT = 8765

# ESP32 A (Production Station)
SERIAL_PORT = "COM7"
BAUD_RATE = 115200

# Serial protocol: SLIP-framed 26-byte ATR [test_id:u16][seq:u32][len:u16][payload:16][crc:u16]
SLIP_END, SLIP_ESC = 0xC0, 0xDB
SLIP_ESC_END, SLIP_ESC_ESC = 0xDC, 0xDD
TYPE_PART, TYPE_ACK = 1, 2
TYPE_COMMAND, TYPE_COMMAND_ACK = 3, 4
TYPE_SET_CYCLE, TYPE_SET_CYCLE_ACK = 5, 6
TYPE_FAULT = 7
ATR_TEST_ID = 0x4311
ATR_PAYLOAD_LEN = 16
ATR_FRAME_LEN = 8 + ATR_PAYLOAD_LEN + 2
next_uart_cmd_seq = int(time.time()) & 0xFFFFFFFF
cycle_waiters = {}
COMMAND_CODES = {"start": 1, "pause": 2, "resume": 3, "stop": 4}
command_waiters = {}
command_lock = asyncio.Lock()
inspection_writer = None
HEARTBEAT_INTERVAL = 3
HEARTBEAT_TIMEOUT = 5
serial_write_lock = asyncio.Lock()
active_serial = None
seen_part_ids = set()  # Raw part IDs, scoped to the current USB session
# Persistent, globally increasing job identifiers. Without this, restarting the
# gateway resets IDs to 1 and ESP32 B's 256-entry duplicate-result cache may
# return an old verdict instantly (skipping its configured inspection delay).
ID_COUNTER_FILE = Path(__file__).resolve().parent / "inspection_next_id.txt"

def read_next_inspection_id():
    seed = int(time.time())  # Avoid old low-numbered IDs on first upgrade
    try:
        return max(seed, int(ID_COUNTER_FILE.read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        return seed

next_inspection_id = read_next_inspection_id()

def allocate_inspection_id():
    global next_inspection_id
    if next_inspection_id >= 0xFFFFFFFF:
        raise RuntimeError("Inspection job ID exhausted; cannot safely reuse IDs")
    number = next_inspection_id
    next_inspection_id += 1
    # Persist the NEXT ID before accepting the current part; a gateway restart
    # cannot reuse an ID retained by ESP32 B's duplicate suppression cache.
    temporary = ID_COUNTER_FILE.with_name(ID_COUNTER_FILE.name + ".tmp")
    temporary.write_text(str(next_inspection_id), encoding="ascii")
    os.replace(temporary, ID_COUNTER_FILE)
    return number
serial_sessions = 0
serial_metrics = {"duplicates": 0, "crc_errors": 0, "bad_frames": 0}

# ESP32 B connects to the ASUS on this TCP port
INSPECTION_PORT = 9000
# QUEUE-FULL SAFETY DEMONSTRATION: at most 5 outstanding jobs,
# INCLUDING the job currently being inspected. Do not use this small
# limit for the final throughput run unless the examiner asks for it.
QUEUE_CAPACITY = 5
QUEUE_POLICY = "FAULT when 5 jobs are outstanding; STOP ESP32 A; preserve accepted jobs"
part_queue = asyncio.Queue(maxsize=QUEUE_CAPACITY)
queue_full_events = 0
# Diagnostics: monotonic timestamps distinguish real delay from cached results.
last_part_received_at = None
part_intervals_ms = []
inspection_durations_ms = []
inspection_start_times = {}
MAX_TIMING_SAMPLES = 200

def record_sample(samples, number):
    samples.append(round(number, 1))
    if len(samples) > MAX_TIMING_SAMPLES:
        del samples[:-MAX_TIMING_SAMPLES]

def mean_or_none(samples):
    return round(sum(samples) / len(samples), 1) if samples else None

def log_event(message):
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    print(f"[{stamp}] {message}")

# A batch is continued after manual STOP/FAULT; a completed batch can be replaced.
batch_active = False
batch_draining = False
batch_stop_requested = False
batch_target = 10
production_cycle_ms = 2000

# Runtime inspection configuration. ESP32 B keeps this only in RAM, so
# the gateway sends it on each TCP connection before accepting inspection jobs.
inspection_config = {"n": 2, "k": 1, "inspection_ms": 0}
config_requests = asyncio.Queue()
config_request_lock = asyncio.Lock()
next_config_id = int(time.time())  # Increasing uint32 identifiers, no reuse within this run


system_status = {
    "system": "MCT431 Production Line",
    "state": "IDLE",
    "esp32_production": "Not connected",
    "esp32_inspection": "Not connected",
    "produced": 0,
    "batch_target": 10,
    "production_cycle_ms": 2000,
    "batch_complete": False,
    "batch_message": "Configure the batch before START",
    "accepted": 0,
    "rejected": 0,
    "pending": 0,
    "fault_reason": "",
    "inspection_n": 2,
    "inspection_k": 1,
    "inspection_ms": 0,
    "inspection_config_state": "Awaiting ESP32 B",
    "queue_capacity": QUEUE_CAPACITY,
    "queue_full_events": 0,
    "queue_policy": QUEUE_POLICY,
    "last_production_interval_ms": None,
    "average_production_interval_ms": None,
    "last_inspection_duration_ms": None,
    "average_inspection_duration_ms": None,

}

clients = set()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>MCT431 Operator Panel</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #f4f6f9;
            margin: 0;
            padding: 30px 15px;
            color: #222;
        }
        .panel {
            max-width: 650px;
            margin: auto;
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 2px 10px #ddd;
        }
        h1 { text-align: center; }
        .state {
            font-size: 24px;
            font-weight: bold;
            text-align: center;
            margin: 20px 0;
        }
        .buttons {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            justify-content: center;
        }
        button {
            padding: 12px 20px;
            border: none;
            border-radius: 6px;
            color: white;
            cursor: pointer;
            font-size: 15px;
        }
        button:disabled { opacity: 0.35; cursor: not-allowed; }
        .start { background: green; }
        .pause { background: #c88400; }
        .resume { background: #1678c2; }
        .stop { background: #c42929; }
        .info { margin-top: 25px; line-height: 2; }
        .config-panel { margin-top: 22px; padding: 15px; background: #f4f6f9; border-radius: 8px; }
        .config-panel label { display: block; margin: 9px 0 4px; font-weight: 600; }
        .config-panel input { box-sizing: border-box; width: 100%; padding: 9px; font-size: 15px; }
        .config-panel button { background: #1678c2; margin-top: 12px; }

    </style>
</head>
<body>
    <div class="panel">
        <h1>MCT431 Production Line</h1>
        <div class="state">System State: <span id="state">IDLE</span></div>

        <div class="buttons">
            <button id="start" class="start" onclick="control('start')">START</button>
            <button id="pause" class="pause" onclick="control('pause')">PAUSE</button>
            <button id="resume" class="resume" onclick="control('resume')">RESUME</button>
            <button id="stop" class="stop" onclick="control('stop')">STOP</button>
            <button id="reset" class="resume" onclick="control('reset')">RESET FAULT</button>
        </div>

        <div class="config-panel">
            <strong>Production Batch Settings</strong>
            <p style="font-size:13px">Apply while IDLE before starting a new batch.</p>
            <label for="batch_target">Target number of parts (1–10000)</label>
            <input id="batch_target" type="number" min="1" max="10000" value="10">
            <label for="production_cycle_ms">ESP32 A cycle time (ms, 100–10000)</label>
            <input id="production_cycle_ms" type="number" min="100" max="10000" value="2000">
            <button id="apply_batch" onclick="applyBatch()">APPLY BATCH SETTINGS</button>
            <div style="font-size:13px;margin-top:10px">Batch: <span id="batch_message">Not configured</span></div>
            <div style="font-size:13px">Complete: <span id="batch_complete">false</span></div>
        </div>
        <div class="config-panel">
            <strong>ESP32 B Inspection Settings</strong>
            <p style="font-size:13px">Rejected if (part ID + K) mod N = 0. Apply while IDLE or PAUSED.</p>
            <label for="cfg_n">N (1–1000000)</label><input id="cfg_n" type="number" min="1" max="1000000" value="2">
            <label for="cfg_k">K (0 to N−1)</label><input id="cfg_k" type="number" min="0" value="1">
            <label for="cfg_ms">Inspection time (ms, 0–10000)</label><input id="cfg_ms" type="number" min="0" max="10000" value="0">
            <button id="apply_config" onclick="applyConfig()">APPLY INSPECTION SETTINGS</button>
            <div style="font-size:13px;margin-top:10px">Applied on ESP32 B: <span id="inspection_config_state">Awaiting ESP32 B</span></div>
        </div>
        <div class="info">
            <div>Produced: <span id="produced">0</span></div>
            <div>Accepted: <span id="accepted">0</span></div>
            <div>Rejected: <span id="rejected">0</span></div>
            <div>Waiting for inspection (including in-flight): <span id="pending">0</span></div>
            <div>Queue capacity (including in-flight): <span id="queue_capacity">5</span></div>
            <div>Queue-full faults: <span id="queue_full_events">0</span></div>
            <p style="font-size:13px; color:#555">Queue policy: FAULT + hardware STOP when at capacity; accepted jobs are preserved.</p>
            <p style="font-size:13px; color:#555">STOP keeps unfinished inspections for the next START.</p>
            <div>ESP32 A: <span id="esp32_production">Not connected</span></div>
            <div>ESP32 B: <span id="esp32_inspection">Not connected</span></div>
            <div>Gateway connection: <span id="connection">Connecting...</span></div>
            <div>Fault reason: <span id="fault_reason">None</span></div>
        </div>
        <p id="message"></p>
    </div>

    <script>
        let currentState = "IDLE";
        let configLoaded = false;

        function update(data) {
            currentState = data.state;
            for (const key of [
                "state", "produced", "accepted", "rejected", "pending",
                "esp32_production", "esp32_inspection", "fault_reason",
                "queue_capacity", "queue_full_events"
            ]) {
                document.getElementById(key).textContent = data[key];
            }
            document.getElementById("batch_message").textContent = data.batch_message;
            document.getElementById("batch_complete").textContent = String(data.batch_complete);
            document.getElementById("apply_batch").disabled = currentState !== "IDLE" || data.esp32_production !== "Connected";
            if (!configLoaded) {
                document.getElementById("batch_target").value = data.batch_target;
                document.getElementById("production_cycle_ms").value = data.production_cycle_ms;
                document.getElementById("cfg_n").value = data.inspection_n;
                document.getElementById("cfg_k").value = data.inspection_k;
                document.getElementById("cfg_ms").value = data.inspection_ms;
                configLoaded = true;
            }
            document.getElementById("inspection_config_state").textContent = data.inspection_config_state;
            document.getElementById("apply_config").disabled =
                !(currentState === "IDLE" || currentState === "PAUSED") ||
                data.esp32_inspection !== "Connected";
            document.getElementById("start").disabled = currentState !== "IDLE";
            document.getElementById("pause").disabled = currentState !== "RUNNING";
            document.getElementById("resume").disabled = currentState !== "PAUSED";
            document.getElementById("stop").disabled = currentState === "IDLE" || currentState === "FAULT";
            document.getElementById("reset").disabled = currentState !== "FAULT";
        }

        async function control(action) {
            try {
                const response = await fetch("/api/control/" + action, {
                    method: "POST"
                });
                const data = await response.json();
                if (!response.ok) {
                    document.getElementById("message").textContent = data.error;
                    return;
                }
                document.getElementById("message").textContent =
                    "Command accepted: " + action.toUpperCase();
                update(data);
            } catch (error) {
                document.getElementById("message").textContent =
                    "Cannot contact gateway.";
            }
        }

        async function applyBatch() {
            const target = Number(document.getElementById("batch_target").value);
            const cycle_ms = Number(document.getElementById("production_cycle_ms").value);
            if (!Number.isInteger(target) || target < 1 || target > 10000 ||
                !Number.isInteger(cycle_ms) || cycle_ms < 100 || cycle_ms > 10000) {
                document.getElementById("message").textContent = "Invalid batch target or cycle time";
                return;
            }
            document.getElementById("apply_batch").disabled = true;
            try {
                const response = await fetch("/api/batch/config", {
                    method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify({target, cycle_ms})
                });
                const data = await response.json();
                document.getElementById("message").textContent = response.ok ?
                    "ESP32 A confirmed cycle time; batch settings saved" : (data.error || "Error");
                if (response.ok) update(data);
            } catch (error) {
                document.getElementById("message").textContent = "Cannot contact gateway";
            }
        }

        async function applyConfig() {
            const n = Number(document.getElementById("cfg_n").value);
            const k = Number(document.getElementById("cfg_k").value);
            const inspection_ms = Number(document.getElementById("cfg_ms").value);
            if (![n, k, inspection_ms].every(Number.isInteger) ||
                n < 1 || n > 1000000 || k < 0 || k >= n ||
                inspection_ms < 0 || inspection_ms > 10000) {
                document.getElementById("message").textContent = "Invalid N, K or inspection time.";
                return;
            }
            document.getElementById("apply_config").disabled = true;
            try {
                const response = await fetch("/api/inspection/config", {
                    method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({n, k, inspection_ms})
                });
                const data = await response.json();
                document.getElementById("message").textContent = response.ok ?
                    "ESP32 B confirmed settings applied" : (data.error || "Configuration failed");
                if (response.ok) update(data);
            } catch (error) {
                document.getElementById("message").textContent = "Cannot contact gateway";
            }
        }

        async function loadStatus() {
            try {
                const response = await fetch("/api/status");
                update(await response.json());
            } catch (error) {
                document.getElementById("message").textContent =
                    "Could not load gateway status.";
            }
        }

        function connectWebSocket() {
            const protocol = location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(protocol + "//" + location.host + "/ws");

            ws.onopen = () => {
                document.getElementById("connection").textContent = "Connected";
            };
            ws.onmessage = (event) => update(JSON.parse(event.data));
            ws.onclose = () => {
                document.getElementById("connection").textContent = "Disconnected";
                setTimeout(connectWebSocket, 2000);
            };
        }

        loadStatus();
        connectWebSocket();
    </script>
</body>
</html>
"""


async def dashboard(request):
    return web.Response(text=HTML, content_type="text/html")


async def status(request):
    return web.json_response(system_status)


async def audit(request):
    produced = system_status["produced"]
    completed = system_status["accepted"] + system_status["rejected"]
    pending = system_status["pending"]
    return web.json_response({
        "produced": produced,
        "completed": completed,
        "pending": pending,
        "balanced": produced == completed + pending,
        "serial": dict(serial_metrics),
        "note": "Pending includes parts awaiting inspection and a part currently being inspected.",
    })


async def queue_status(request):
    # A job already being inspected occupies one of the capacity slots.
    pending = system_status["pending"]
    waiting = part_queue.qsize()
    return web.json_response({
        "capacity": QUEUE_CAPACITY,
        "outstanding": pending,
        "waiting": waiting,
        "in_flight": max(0, pending - waiting),
        "full_events": queue_full_events,
        "production_interval_ms": system_status["last_production_interval_ms"],
        "inspection_duration_ms": system_status["last_inspection_duration_ms"],
        "policy": QUEUE_POLICY,
        "state": system_status["state"],
        "balanced": system_status["produced"] == system_status["accepted"]
                    + system_status["rejected"] + pending,
    })


async def timing_status(request):
    return web.json_response({
        "configured_production_cycle_ms": production_cycle_ms,
        "configured_inspection_ms": inspection_config["inspection_ms"],
        "last_production_interval_ms": system_status["last_production_interval_ms"],
        "average_production_interval_ms": mean_or_none(part_intervals_ms),
        "production_sample_count": len(part_intervals_ms),
        "last_inspection_duration_ms": system_status["last_inspection_duration_ms"],
        "average_inspection_duration_ms": mean_or_none(inspection_durations_ms),
        "inspection_sample_count": len(inspection_durations_ms),
        "recent_production_intervals_ms": part_intervals_ms[-10:],
        "recent_inspection_durations_ms": inspection_durations_ms[-10:],
        "next_inspection_job_id": next_inspection_id,
        "inspection_id_mapping": "INSPECT <job_id> <original_part_id>; original part ID unchanged",
        "note": "Inspection duration is measured at the gateway from INSPECT send to RESULT receive; verdict is calculated from original part ID."
    })


async def set_inspection_config(request):
    global inspection_config
    if system_status["state"] not in ("IDLE", "PAUSED"):
        return web.json_response({"error": "Apply settings only while IDLE or PAUSED"}, status=409)
    if inspection_writer is None or system_status["esp32_inspection"] != "Connected":
        return web.json_response({"error": "ESP32 B is not connected"}, status=503)
    try:
        data = await request.json()
        n, k, ms = data["n"], data["k"], data["inspection_ms"]
        if any(type(v) is not int for v in (n, k, ms)):
            raise ValueError("Integers required")
        if not (1 <= n <= 1000000 and 0 <= k < n and 0 <= ms <= 10000):
            raise ValueError("N/K/inspection time outside valid range")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, web.HTTPBadRequest) as err:
        return web.json_response({"error": f"Invalid configuration: {err}"}, status=400)

    # Only one runtime CONFIG at a time; the TCP handler owns all socket reads.
    async with config_request_lock:
        if system_status["state"] not in ("IDLE", "PAUSED"):
            return web.json_response({"error": "State changed: pause production first"}, status=409)
        if inspection_writer is None or system_status["esp32_inspection"] != "Connected":
            return web.json_response({"error": "ESP32 B disconnected"}, status=503)
        future = asyncio.get_running_loop().create_future()
        await config_requests.put(({"n": n, "k": k, "inspection_ms": ms}, future))
        try:
            await asyncio.wait_for(future, timeout=12)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError) as error:
            return web.json_response({"error": "ESP32 B did not apply settings: " + str(error)}, status=504)
    return web.json_response(system_status)


async def set_batch_config(request):
    global batch_target, production_cycle_ms
    if system_status["state"] != "IDLE":
        return web.json_response({"error": "Set batch values while IDLE"}, status=409)
    if batch_active and not system_status["batch_complete"]:
        return web.json_response({"error": "An unfinished batch exists; finish it before configuring a new one"}, status=409)
    try:
        data = await request.json()
        target, cycle_ms = data["target"], data["cycle_ms"]
        if type(target) is not int or type(cycle_ms) is not int:
            raise ValueError("Integer values required")
        if not (1 <= target <= 10000 and 100 <= cycle_ms <= 10000):
            raise ValueError("Target must be 1–10000 and cycle 100–10000 ms")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, web.HTTPBadRequest) as error:
        return web.json_response({"error": f"Invalid batch settings: {error}"}, status=400)
    async with command_lock:
        if system_status["state"] != "IDLE":
            return web.json_response({"error": "State changed"}, status=409)
        if not await send_cycle_config(cycle_ms):
            return web.json_response({"error": "ESP32 A did not acknowledge cycle time"}, status=504)
        batch_target, production_cycle_ms = target, cycle_ms
        system_status.update({"batch_target": target, "production_cycle_ms": cycle_ms,
                              "batch_message": f"Ready: {target} parts, {cycle_ms} ms cycle"})
        await broadcast_status()
    return web.json_response(system_status)


async def send_cycle_config(ms):
    if active_serial is None or system_status["esp32_production"] != "Connected":
        return False
    seq = allocate_uart_command_seq()
    ack = asyncio.Event()
    cycle_waiters[seq] = ack
    try:
        for attempt in range(1, 4):
            try:
                print(f"Sending cycle {ms} ms to ESP32 A (attempt {attempt})")
                await write_to_production(encode_serial_frame(TYPE_SET_CYCLE, ms, seq))
                await asyncio.wait_for(ack.wait(), timeout=1.8)
                return True
            except (asyncio.TimeoutError, serial.SerialException, OSError):
                pass
        return False
    finally:
        cycle_waiters.pop(seq, None)


async def finish_batch_if_ready():
    global batch_active, batch_draining
    if (batch_active and system_status["produced"] == batch_target and
        system_status["pending"] == 0 and
        system_status["accepted"] + system_status["rejected"] == batch_target and
        not system_status["batch_complete"]):
        system_status["batch_complete"] = True
        system_status["batch_message"] = (
            f"COMPLETE: {batch_target} parts; "
            f"PASS {system_status['accepted']}, FAIL {system_status['rejected']}")
        batch_draining = False
        batch_active = False
        print("BATCH", system_status["batch_message"])
        await broadcast_status()


async def stop_at_batch_target():
    global batch_draining
    async with command_lock:
        if system_status["state"] != "RUNNING":
            return
        print("Batch target reached. Stopping ESP32 A; draining inspections.")
        if not await send_mcu_command("stop"):
            system_status["state"] = "FAULT"
            system_status["fault_reason"] = "Could not confirm automatic batch STOP"
            await broadcast_status()
            return
        system_status["state"] = "IDLE"
        batch_draining = True
        system_status["batch_message"] = "Production stopped at target; finishing inspections"
        await broadcast_status()
        await finish_batch_if_ready()


async def broadcast_status():
    message = json.dumps(system_status)
    for client in list(clients):
        if client.closed:
            clients.discard(client)
            continue
        try:
            await client.send_str(message)
        except (ConnectionResetError, RuntimeError):
            clients.discard(client)


async def send_mcu_command(action):
    """Caller must own command_lock. Retry if command ACK is lost."""
    if active_serial is None or system_status["esp32_production"] != "Connected":
        return False
    code = COMMAND_CODES[action]
    seq = allocate_uart_command_seq()
    ack_event = asyncio.Event()
    command_waiters[seq] = ack_event
    try:
        for attempt in range(1, 4):
            print(f"Sending {action.upper()} to ESP32 A (attempt {attempt})")
            try:
                await write_to_production(encode_serial_frame(TYPE_COMMAND, code, seq))
                await asyncio.wait_for(ack_event.wait(), timeout=1.8)
                return True
            except (asyncio.TimeoutError, serial.SerialException, OSError):
                pass
        return False
    finally:
        command_waiters.pop(seq, None)


async def enter_fault(reason):
    """Latch a fault, then attempt to stop the real production MCU."""
    if system_status["state"] == "FAULT":
        return
    if system_status["state"] not in ("RUNNING", "PAUSED"):
        return
    system_status["state"] = "FAULT"
    system_status["fault_reason"] = reason
    print("FAULT:", reason)
    await broadcast_status()
    # The serial receive task remains alive, so it can collect the MCU's ACK.
    async with command_lock:
        if not await send_mcu_command("stop"):
            system_status["fault_reason"] += " | WARNING: ESP32 A STOP not acknowledged"
            print("WARNING: Could not confirm ESP32 A stopped")
        await broadcast_status()


async def control(request):
    global batch_active, batch_draining, batch_stop_requested, last_part_received_at
    action = request.match_info["action"]
    async with command_lock:
        state = system_status["state"]
        if action == "reset":
            if state != "FAULT":
                return web.json_response({"error": "No fault to reset"}, status=400)
            if system_status["esp32_inspection"] != "Connected":
                return web.json_response({"error": "ESP32 B must reconnect before RESET"}, status=503)
            if system_status["esp32_production"] != "Connected":
                return web.json_response({"error": "ESP32 A must reconnect before RESET"}, status=503)
            if not await send_mcu_command("stop"):
                return web.json_response({"error": "Could not confirm ESP32 A stopped"}, status=504)
            system_status["state"] = "IDLE"
            system_status["fault_reason"] = ""
            # Drain previously accepted jobs while the MCU stays IDLE.
            # Otherwise an immediate START at 100 ms could fill the queue
            # again before inspection (3000 ms/job) makes any progress.
            batch_draining = system_status["pending"] > 0
            if batch_draining:
                system_status["batch_message"] = (
                    f"Recovery: draining {system_status['pending']} accepted jobs before START")
            print("FAULT RESET -> IDLE; pending inspection jobs preserved and draining")
            await broadcast_status()
            return web.json_response(system_status)

        transitions = {
            "start": ("IDLE", "RUNNING"),
            "pause": ("RUNNING", "PAUSED"),
            "resume": ("PAUSED", "RUNNING"),
        }
        if action == "stop":
            if state not in ("RUNNING", "PAUSED"):
                return web.json_response({"error": "System is not running"}, status=400)
            new_state = "IDLE"
        elif action in transitions:
            expected, new_state = transitions[action]
            if state != expected:
                return web.json_response({"error": "Invalid command for state " + state}, status=400)
        else:
            return web.json_response({"error": "Unknown command"}, status=400)

        if system_status["esp32_inspection"] != "Connected" and action in ("start", "resume"):
            return web.json_response({"error": "ESP32 B inspection station is not connected"}, status=503)
        if active_serial is None or system_status["esp32_production"] != "Connected":
            return web.json_response({"error": "ESP32 A is not connected"}, status=503)

        if action == "start":
            if batch_draining and system_status["pending"] > 0:
                return web.json_response({"error": "Recovery is draining accepted jobs; wait for pending=0 before START"}, status=409)
            if system_status["batch_complete"] or not batch_active:
                # Do not reset the job ID or UART duplicate tracker: those are
                # transport/session identifiers, not per-batch counters.
                if system_status["pending"] != 0:
                    return web.json_response({"error": "Cannot start a fresh batch with unfinished inspections"}, status=409)
                system_status.update({"produced": 0, "accepted": 0, "rejected": 0,
                                      "batch_complete": False,
                                      "batch_message": f"Batch in progress: 0/{batch_target} produced"})
                batch_active = True
                last_part_received_at = None
                batch_stop_requested = False
                batch_draining = False
            # MCU can reboot and reset cycle time, so enforce config at START.
            if not await send_cycle_config(production_cycle_ms):
                return web.json_response({"error": "ESP32 A did not acknowledge cycle settings"}, status=504)

        if not await send_mcu_command(action):
            # An unacknowledged command could still have reached the MCU,
            # including START. Enter FAULT and try to stop production even
            # though its exact state is uncertain.
            system_status["state"] = "FAULT"
            system_status["fault_reason"] = "ESP32 A command " + action.upper() + " not acknowledged; MCU state uncertain"
            await broadcast_status()
            if not await send_mcu_command("stop"):
                system_status["fault_reason"] += " | WARNING: STOP not acknowledged"
                await broadcast_status()
            return web.json_response({"error": "ESP32 A did not acknowledge " + action.upper()}, status=504)
        # A disconnect can trigger FAULT while the command is awaiting an ACK.
        if system_status["state"] == "FAULT":
            return web.json_response({"error": "Fault occurred during command"}, status=409)
        system_status["state"] = new_state
        if action == "stop":
            # STOP halts new production; accepted jobs must not vanish.
            # Pending jobs remain until the next START. An inspection already
            # in progress may complete after STOP and is still recorded.
            print("STOP: preserving", system_status["pending"], "outstanding inspections")
        print("MCU ACK received; COMMAND:", action.upper(), "->", new_state)
        await broadcast_status()
        return web.json_response(system_status)


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    await ws.send_json(system_status)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        clients.discard(ws)
    return ws


# Reliable serial protocol, matching test_reliable_serial.py and ESP32 A firmware.
def crc16(payload):
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def allocate_uart_command_seq():
    global next_uart_cmd_seq
    next_uart_cmd_seq = (next_uart_cmd_seq + 1) & 0xFFFFFFFF
    if next_uart_cmd_seq == 0:
        next_uart_cmd_seq = 1
    return next_uart_cmd_seq


def encode_serial_frame(kind, number, seq=None):
    # ATR: test_id(2), seq(4), payload_len(2), payload(16), CRC16(2)
    if seq is None:
        seq = number
    payload = bytes([kind]) + number.to_bytes(4, "big")
    payload += bytes((seq + i) & 0xFF for i in range(5, ATR_PAYLOAD_LEN))
    content = (ATR_TEST_ID.to_bytes(2, "big") + seq.to_bytes(4, "big") +
               ATR_PAYLOAD_LEN.to_bytes(2, "big") + payload)
    content += crc16(content).to_bytes(2, "big")
    framed = bytearray([SLIP_END])
    for byte in content:
        if byte == SLIP_END:
            framed.extend((SLIP_ESC, SLIP_ESC_END))
        elif byte == SLIP_ESC:
            framed.extend((SLIP_ESC, SLIP_ESC_ESC))
        else:
            framed.append(byte)
    framed.append(SLIP_END)
    return bytes(framed)


def decode_atr(frame):
    if len(frame) != ATR_FRAME_LEN:
        raise ValueError("Incorrect ATR frame length")
    if int.from_bytes(frame[:2], "big") != ATR_TEST_ID:
        raise ValueError("Wrong ATR test ID")
    seq = int.from_bytes(frame[2:6], "big")
    if int.from_bytes(frame[6:8], "big") != ATR_PAYLOAD_LEN:
        raise ValueError("Wrong ATR payload length")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "big"):
        raise ValueError("CRC mismatch")
    kind = frame[8]
    value = int.from_bytes(frame[9:13], "big")
    if frame[13:24] != bytes((seq + i) & 0xFF for i in range(5, ATR_PAYLOAD_LEN)):
        raise ValueError("ATR payload integrity mismatch")
    return kind, seq, value

class SlipDecoder:
    def __init__(self):
        self.content = bytearray()
        self.in_frame = False
        self.escaped = False

    def feed(self, data):
        # Yield complete decoded frames; ignore plain UART console/debug text.
        for b in data:
            if b == SLIP_END:
                if self.in_frame and self.content and not self.escaped:
                    yield bytes(self.content)
                self.content.clear()
                self.in_frame = True
                self.escaped = False
                continue
            if not self.in_frame:
                continue
            if self.escaped:
                if b == SLIP_ESC_END:
                    b = SLIP_END
                elif b == SLIP_ESC_ESC:
                    b = SLIP_ESC
                else:
                    serial_metrics["bad_frames"] += 1
                    self.content.clear()
                    self.in_frame = False
                    self.escaped = False
                    continue
                self.escaped = False
            elif b == SLIP_ESC:
                self.escaped = True
                continue
            if len(self.content) >= 64:
                serial_metrics["bad_frames"] += 1
                self.content.clear()
                self.in_frame = False
                self.escaped = False
                continue
            self.content.append(b)


async def write_to_production(frame):
    async with serial_write_lock:
        port = active_serial
        if port is None:
            raise serial.SerialException("ESP32 A is disconnected")
        await asyncio.to_thread(port.write, frame)


def production_port_present():
    """On Windows, detect physical USB removal even if read() returns b''."""
    return any(port.device.upper() == SERIAL_PORT.upper() for port in list_ports.comports())


async def read_production():
    global active_serial, serial_sessions, batch_stop_requested, queue_full_events, last_part_received_at
    while True:
        try:
            with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.2) as esp32:
                decoder = SlipDecoder()
                serial_sessions += 1
                # ESP32 A reboots when USB power is lost and restarts PART IDs
                # at 1. Deduplicate its raw IDs only within each USB session.
                seen_part_ids.clear()
                active_serial = esp32
                print("ESP32 A reliable UART connected on", SERIAL_PORT,
                      "(USB session", serial_sessions, ")")
                system_status["esp32_production"] = "Connected"
                await broadcast_status()
                if system_status["state"] == "FAULT":
                    asyncio.create_task(stop_after_serial_reconnect())
                while True:
                    # Do not block the aiohttp event loop while receiving serial bytes.
                    data = await asyncio.to_thread(esp32.read, 64)
                    if not data:
                        # Some USB serial drivers silently return no bytes after
                        # unplugging, without raising SerialException.
                        if not production_port_present():
                            raise serial.SerialException(
                                f"{SERIAL_PORT} disappeared from Windows serial ports"
                            )
                        continue
                    for payload in decoder.feed(data):
                        try:
                            kind, seq, part_id = decode_atr(payload)
                        except ValueError as error:
                            if "CRC" in str(error):
                                serial_metrics["crc_errors"] += 1
                            else:
                                serial_metrics["bad_frames"] += 1
                            print("ESP32 A rejected ATR:", error)
                            continue
                        if kind == TYPE_SET_CYCLE_ACK:
                            event = cycle_waiters.get(seq)
                            if event is not None:
                                event.set()
                                print("ESP32 A acknowledged cycle", part_id, "ms", "seq", seq)
                            continue
                        if kind == TYPE_COMMAND_ACK:
                            event = command_waiters.get(seq)
                            if event is not None:
                                if system_status["state"] != "FAULT":
                                    ack_states = {1: "RUNNING", 2: "PAUSED", 3: "RUNNING", 4: "IDLE"}
                                    if part_id in ack_states:
                                        system_status["state"] = ack_states[part_id]
                                event.set()
                                print("ESP32 A acknowledged control code", part_id, "seq", seq)
                            continue
                        if kind == TYPE_FAULT:
                            print("ESP32 A reports bounded-retry failure for part", part_id)
                            await enter_fault(f"ESP32 A exhausted retries for part {part_id}")
                            continue
                        if kind != TYPE_PART or part_id == 0 or seq != part_id:
                            serial_metrics["bad_frames"] += 1
                            continue

                        # Withhold ACK for a not-yet-accepted part during a
                        # full-queue fault; never pretend it entered the queue.
                        if (system_status["state"] == "FAULT" and
                                system_status["fault_reason"].startswith("Inspection capacity") and
                                part_id not in seen_part_ids):
                            print("Queue FAULT: not acknowledging unaccepted part", part_id)
                            continue

                        queue_full_reason = None
                        if part_id in seen_part_ids:
                            # ESP32 A did not receive our previous ACK; resend
                            # the ACK without producing/inspecting the same part twice.
                            serial_metrics["duplicates"] += 1
                            print("ESP32 A duplicate", part_id, "- ACK again, don't recount")
                        else:
                            if system_status["state"] == "RUNNING" and system_status["produced"] < batch_target:
                                if system_status["pending"] >= QUEUE_CAPACITY:
                                    # This part has NOT been accepted. No ACK, no
                                    # increment, and a controlled fault if needed.
                                    print("Inspection capacity reached; refusing unaccepted part", part_id)
                                    asyncio.create_task(enter_fault(
                                        f"Inspection capacity {QUEUE_CAPACITY} reached"))
                                    continue
                                # Allocate and persist ID before acknowledging this
                                # part; never reuse a cached ESP32 B job identifier.
                                inspection_id = allocate_inspection_id()
                                seen_part_ids.add(part_id)
                                part_queue.put_nowait((inspection_id, part_id))
                                system_status["produced"] += 1
                                # Count queued AND in-flight jobs, not just qsize().
                                system_status["pending"] += 1
                                now = time.monotonic()
                                if last_part_received_at is not None:
                                    interval_ms = (now - last_part_received_at) * 1000
                                    record_sample(part_intervals_ms, interval_ms)
                                    system_status["last_production_interval_ms"] = round(interval_ms, 1)
                                    system_status["average_production_interval_ms"] = mean_or_none(part_intervals_ms)
                                last_part_received_at = now
                                log_event(f"PRODUCED part={part_id} inspection_job={inspection_id} "
                                          f"interval_ms={system_status['last_production_interval_ms']} "
                                          f"outstanding={system_status['pending']}/{QUEUE_CAPACITY}")
                                print("Total produced:", system_status["produced"])
                                system_status["batch_message"] = (
                                    f"Producing: {system_status['produced']}/{batch_target}")
                                await broadcast_status()
                                if system_status["produced"] == batch_target and not batch_stop_requested:
                                    batch_stop_requested = True
                                    asyncio.create_task(stop_at_batch_target())
                                elif system_status["pending"] >= QUEUE_CAPACITY:
                                    # The last accepted part is ACKed below. STOP
                                    # is dispatched asynchronously so the UART
                                    # reader remains free to receive the MCU ACK.
                                    queue_full_events += 1
                                    system_status["queue_full_events"] = queue_full_events
                                    log_event(f"INSPECTION QUEUE LIMIT {QUEUE_CAPACITY} REACHED: entering FAULT")
                                    queue_full_reason = f"Inspection capacity {QUEUE_CAPACITY} reached"
                            else:
                                # Matches earlier demo behavior: production is counted
                                # only while RUNNING. Always ACK when paused/stopped so
                                # the MCU does not remain blocked retransmitting a part.
                                seen_part_ids.add(part_id)
                                print("Part", part_id, "ACKed while", system_status["state"],
                                      "(not added to queue)")

                        await write_to_production(encode_serial_frame(TYPE_ACK, part_id))
                        if queue_full_reason:
                            # ACK the accepted fifth job BEFORE issuing STOP.
                            # The control command must run in a separate task:
                            # the UART reader also receives the control ACK.
                            asyncio.create_task(enter_fault(queue_full_reason))

        except asyncio.CancelledError:
            raise
        except (serial.SerialException, OSError) as error:
            print("ESP32 A UART error:", error)
            active_serial = None
            system_status["esp32_production"] = "Disconnected"
            await broadcast_status()
            await enter_fault("ESP32 A UART disconnected")
            await asyncio.sleep(2)
        finally:
            active_serial = None

async def stop_after_serial_reconnect():
    if system_status["state"] != "FAULT":
        return
    async with command_lock:
        if system_status["state"] == "FAULT":
            if await send_mcu_command("stop"):
                print("ESP32 A stopped after UART reconnection")
            else:
                print("WARNING: ESP32 A STOP unconfirmed after reconnection")


async def production_serial_context(app):
    reader_task = asyncio.create_task(read_production())
    try:
        yield
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass



# ESP32 B is a TCP CLIENT. ASUS listens here for its connection.
# 2-byte unsigned big-endian length, then that many ASCII bytes (no delimiter).
# readexactly handles both split and coalesced TCP segments correctly.
MAX_TCP_MESSAGE = 160

async def write_inspection_frame(writer, text):
    payload = text.encode("ascii")
    if not (1 <= len(payload) <= MAX_TCP_MESSAGE):
        raise ValueError("Invalid outgoing ESP32 B message size")
    writer.write(len(payload).to_bytes(2, "big") + payload)
    await asyncio.wait_for(writer.drain(), timeout=5)

async def read_inspection_frame(reader, timeout):
    header = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
    size = int.from_bytes(header, "big")
    if not (1 <= size <= MAX_TCP_MESSAGE):
        raise ValueError(f"Invalid ESP32 B frame length: {size}")
    payload = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    return payload.decode("ascii", "strict")

async def send_config_to_esp32b(reader, writer, config):
    """Application CONFIG_ACK confirms acceptance, distinct from socket delivery."""
    global next_config_id
    cmd_id = next_config_id
    next_config_id += 1
    command = f"CONFIG {cmd_id} {config['n']} {config['k']} {config['inspection_ms']}"
    expected = (f"CONFIG_ACK {cmd_id} APPLIED {config['n']} {config['k']} "
                f"{config['inspection_ms']}")
    for attempt in range(1, 4):
        print(f"Sending framed CONFIG {cmd_id} to ESP32 B (attempt {attempt})")
        await write_inspection_frame(writer, command)
        try:
            reply = await read_inspection_frame(reader, timeout=3)
        except asyncio.TimeoutError:
            continue
        if reply == expected:
            print(f"ESP32 B applied N={config['n']} K={config['k']} delay={config['inspection_ms']}ms")
            return
        raise ValueError(f"ESP32 B rejected CONFIG: {reply!r}")
    raise ConnectionError("ESP32 B CONFIG not acknowledged after 3 attempts")

async def handle_inspection_client(reader, writer):
    global inspection_writer, batch_draining
    peer = writer.get_extra_info("peername")
    print("ESP32 B TCP connection from", peer)
    active_part = None
    is_registered = False
    try:
        greeting = await read_inspection_frame(reader, timeout=10)
        if greeting != "HELLO ESP32_B":
            print("Unexpected ESP32 B greeting:", greeting[:100])
            return

        # Exactly one connected station owns the inspection socket reader.
        if inspection_writer is not None and inspection_writer is not writer:
            print("Replacing old ESP32 B connection")
            inspection_writer.close()
        inspection_writer = writer
        is_registered = True
        system_status["inspection_config_state"] = "Applying..."
        await broadcast_status()
        await send_config_to_esp32b(reader, writer, inspection_config)
        system_status["inspection_config_state"] = "Applied"
        system_status["esp32_inspection"] = "Connected"
        await broadcast_status()
        print("ESP32 B connected with length-prefix TCP framing")
        last_heartbeat = time.monotonic()
        heartbeat_number = 0

        while inspection_writer is writer:
            try:
                requested_config, future = config_requests.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                try:
                    await send_config_to_esp32b(reader, writer, requested_config)
                    inspection_config.update(requested_config)
                    system_status.update({
                        "inspection_n": requested_config["n"],
                        "inspection_k": requested_config["k"],
                        "inspection_ms": requested_config["inspection_ms"],
                        "inspection_config_state": "Applied",
                    })
                    await broadcast_status()
                    if not future.done():
                        future.set_result(True)
                except Exception as error:
                    if not future.done():
                        future.set_exception(ConnectionError(str(error)))
                    raise
                finally:
                    config_requests.task_done()

            if reader.at_eof() or writer.is_closing():
                raise ConnectionError("ESP32 B socket closed")

            if system_status["state"] == "RUNNING" or batch_draining:
                try:
                    active_part = await asyncio.wait_for(part_queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    pass
                else:
                    # pending already includes this in-flight job. The queue
                    # get() changes where it resides, not the outstanding count.
                    await broadcast_status()
                    # Delivery: bytes written to TCP. This does not mean ESP32 B accepted the job.
                    inspect_started_at = time.monotonic()
                    log_event(f"INSPECTION START job={active_part[0]} part={active_part[1]} configured_ms={inspection_config['inspection_ms']}")
                    await write_inspection_frame(writer, f"INSPECT {active_part[0]} {active_part[1]}")
                    accepted = await read_inspection_frame(reader, timeout=5)
                    if accepted != f"ACCEPT {active_part[0]} {active_part[1]}":
                        raise ValueError(f"No application acceptance for {active_part}: {accepted!r}")
                    print(f"ESP32 B accepted inspection job {active_part[0]} for original part {active_part[1]}")
                    # Completion: a separate result frame (matching the unique job ID).
                    result_text = await read_inspection_frame(
                        reader, timeout=max(5, inspection_config["inspection_ms"] / 1000 + 3))
                    result = result_text.split()
                    if (len(result) != 4 or result[:3] != ["RESULT", str(active_part[0]), str(active_part[1])]
                            or result[3] not in ("PASS", "FAIL")):
                        raise ValueError(f"Invalid inspection completion for {active_part}: {result_text!r}")
                    if result[3] == "PASS":
                        system_status["accepted"] += 1
                    else:
                        system_status["rejected"] += 1
                    measured_ms = (time.monotonic() - inspect_started_at) * 1000
                    record_sample(inspection_durations_ms, measured_ms)
                    system_status["last_inspection_duration_ms"] = round(measured_ms, 1)
                    system_status["average_inspection_duration_ms"] = mean_or_none(inspection_durations_ms)
                    log_event(f"INSPECTION COMPLETE job={active_part[0]} part={active_part[1]} result={result[3]} "
                              f"duration_ms={measured_ms:.1f} "
                              f"configured_ms={inspection_config['inspection_ms']}")
                    part_queue.task_done()
                    active_part = None
                    system_status["pending"] -= 1
                    await broadcast_status()
                    await finish_batch_if_ready()
                    if (batch_draining and system_status["state"] == "IDLE" and
                            system_status["pending"] == 0 and not system_status["batch_complete"]):
                        # Recovering an incomplete batch does not restart the MCU.
                        # Wait for the operator to press START explicitly.
                        batch_draining = False
                        system_status["batch_message"] = "Recovery queue drained; press START to continue batch"
                        await broadcast_status()
                    last_heartbeat = time.monotonic()
                    continue
            else:
                await asyncio.sleep(0.3)

            if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat_number += 1
                await write_inspection_frame(writer, f"PING {heartbeat_number}")
                pong = await read_inspection_frame(reader, timeout=HEARTBEAT_TIMEOUT)
                if pong != f"PONG {heartbeat_number}":
                    raise ConnectionError(f"Heartbeat failed: {pong!r}")
                last_heartbeat = time.monotonic()

    except (OSError, ConnectionError, ValueError, asyncio.TimeoutError,
            asyncio.IncompleteReadError, UnicodeDecodeError) as error:
        print("ESP32 B TCP error:", error)
    finally:
        if active_part is not None:
            # Retain the same job ID so ESP32 B's duplicate cache prevents a
            # repeated execution after TCP reconnect.
            if not part_queue.full():
                part_queue.put_nowait(active_part)
            else:
                print("WARNING: inspection queue full; cannot requeue in-flight job", active_part)
            part_queue.task_done()
            # Requeueing is a transfer, not a new part. pending is unchanged.
        if is_registered and inspection_writer is writer:
            inspection_writer = None
            system_status["inspection_config_state"] = "Awaiting ESP32 B"
            system_status["esp32_inspection"] = "Disconnected"
            await broadcast_status()
            await enter_fault("ESP32 B inspection station disconnected or timed out")
        # Unblock pending dashboard configuration requests (if station was lost).
        while True:
            try:
                _, pending_future = config_requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not pending_future.done():
                pending_future.set_exception(ConnectionError("ESP32 B disconnected"))
            config_requests.task_done()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        print("ESP32 B disconnected; waiting for reconnection")


async def inspection_tcp_context(app):
    server = await asyncio.start_server(handle_inspection_client, HOST, INSPECTION_PORT)
    print(f"ESP32 B TCP listener on port {INSPECTION_PORT}")
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()

app = web.Application()
app.cleanup_ctx.append(production_serial_context)
app.cleanup_ctx.append(inspection_tcp_context)

app.router.add_get("/", dashboard)
app.router.add_get("/api/status", status)
app.router.add_get("/api/audit", audit)
app.router.add_get("/api/queue", queue_status)
app.router.add_get("/api/timing", timing_status)
app.router.add_post("/api/inspection/config", set_inspection_config)
app.router.add_post("/api/batch/config", set_batch_config)
app.router.add_post("/api/control/{action}", control)
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    print("Starting MCT431 Gateway...")
    web.run_app(app, host=HOST, port=PORT)
