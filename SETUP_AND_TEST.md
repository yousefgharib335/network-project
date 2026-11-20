# Reproducible setup and test instructions

## Devices and connection topology
- ESP32 A (production) -> ASUS laptop via USB UART, 115200 baud, 8N1, SLIP/ATR.
- ESP32 B (inspection) -> ASUS laptop over 2-byte length-prefixed TCP, Wi-Fi port 9000.
- ASUS laptop -> HP operator browser over HTTP/WebSocket port 8765.
- The sample gateway LAN IP is `192.168.1.113`; replace as needed for other networks.

## Firmware builds (Windows, ESP-IDF 6.1)
1. Open the `Source/esp32_production/production_station_atr` folder in an ESP-IDF terminal.
2. Run `idf.py set-target esp32`, then `idf.py build`, then `idf.py -p COMx flash`.
3. Open `Source/esp32_inspection/inspection_station_lengthprefix/main/inspection_main.c`.
   Replace `YOUR_WIFI_SSID`, `YOUR_WIFI_PASSWORD` privately, and set `GATEWAY_IP` to ASUS Wi-Fi IP.
4. Open the inspection project in the ESP-IDF terminal. Run `idf.py set-target esp32`,
   `idf.py build`, `idf.py -p COMy flash`. Close monitors before using serial ports.
5. Verify both boards have their intended firmware: inspection logs show Wi-Fi/TCP, production is binary-only.
   Windows COM numbers may change when you move/reconnect USB cables. Do NOT assume COM7 always means ESP32 A.

## Gateway and operator
1. Place the contents of `Source/` in your working project root.
2. In PowerShell from the root: `py -m venv .venv`.
3. Install dependencies: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
4. Edit `gateway/server.py` `SERIAL_PORT = "COMx"` to the COM port of the *production* ESP32 A.
5. Verify `GATEWAY_IP` in ESP32 B matches ASUS Wi-Fi IPv4 address.
6. Start: `.\.venv\Scripts\python.exe -u .\gateway\server.py`.
7. Open the ASUS firewall for inbound TCP ports 8765 and 9000 on the trusted/private LAN.
8. On HP, visit `http://ASUS_WIFI_IP:8765`.
9. Keep the gateway and test_atr_uart.py from owning the same COM port simultaneously.

## Normal batch acceptance test
- Apply target=10, production_cycle_ms=1000, inspection N=4, K=1, inspection_ms=200.
- START and wait for COMPLETE. Check `http://ASUS_WIFI_IP:8765/api/audit`.
- Verify `produced=10`, `completed=10`, `pending=0`, `balanced=true`.
- In the VERIFIED corrected protocol, ESP32 B applies the rule to the original ESP32 A part ID, not the separate job ID.
  For fresh raw part IDs 1–10 and N=4, K=1, exactly parts 3 and 7 must FAIL: 8 PASS / 2 FAIL.
  If ESP32 A has already produced earlier batches and its part ID is larger than 10, calculate the expected split from the actual part IDs rather than assuming the counter restarted.
- Timing counters: `http://ASUS_WIFI_IP:8765/api/timing`.

## ATR loss/retry test
- Stop the gateway. Close the ESP-IDF monitor.
- Edit `PORT` in `test_atr_uart.py` for ESP32 A's actual COM port.
- Run `python test_atr_uart.py`; it deliberately withholds one ACK and verifies deduplication.
- Run `python test_atr_uart.py --test-fault`; it withholds ACKs and checks bounded-retry FAULT.
- The UART session will be reset for subsequent production use if required.

## Queue-full test (requires manual reset after each fault)
- QUEUE_CAPACITY=5 in this demonstration gateway; includes queued plus in-flight.
- Apply target=20, cycle=100ms, inspection_ms=3000ms (N=4 K=1).
- START. Expect FAULT when 5 outstanding inspections accumulate; ESP32 A receives STOP.
- Check `/api/queue`; RESET FAULT after both stations are connected, allow jobs to drain to 0,
  then START again. The remaining batch should finish with balanced audit.
- This intentionally demonstrates *multiple faults* per batch; it is not a fault-free run.

## Disconnect/reconnect fault demonstrations
- ESP32 A: unplug its USB data/power cable during RUNNING; expect COM disconnect and FAULT.
  Windows may change COM number after reconnection. Verify port before reset/restart.
- ESP32 B: disconnect its power/Wi-Fi during RUNNING; expect heartbeat/socket failure and FAULT.
  Reconnect B; wait for `ESP32 B connected`, RESET FAULT, drain pending, START.
- Save raw logs, screenshots and packet captures, and verify balanced audit at completion.

## Capture TCP framing
- In Wireshark capture ASUS Wi-Fi interface; display filter `tcp.port == 9000`.
- `Follow -> TCP Stream` illustrates CONFIG/CONFIG_ACK, INSPECT/ACCEPT/RESULT, PING/PONG.
- Historical pre-correction payload: `00 12` + 18-byte ASCII `INSPECT 1794690331` (original .pcapng, packet 474).
- FINAL revised payload: `[uint16 big-endian length] INSPECT <job_id> <original_part_id>`; corresponding ACCEPT and RESULT echo BOTH IDs. The screenshots verify the final message fields; a fresh .pcapng would strengthen wire-level evidence.
- TCP is a stream: the 2-byte header and payload can appear in distinct TCP packets.
