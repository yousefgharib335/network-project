# Verified identifier fix — reproducible test

The corrected ESP32 B firmware and matching ASUS gateway were verified in hardware on 15 November 2026. No ESP32 A firmware change was needed.

## Message formats
- `[2-byte big-endian length] INSPECT <job_id> <original_part_id>`
- `[2-byte big-endian length] ACCEPT <job_id> <original_part_id>`
- `[2-byte big-endian length] RESULT <job_id> <original_part_id> PASS|FAIL`

Gateway checks BOTH returned IDs. ESP32 B computes FAIL iff `(original_part_id + K) % N == 0`. Persistent job IDs avoid reused cached results on gateway restart.

## Replicate the observed validation
1. Stop gateway. Flash the bundled ESP32 B project to its current COM port after entering Wi-Fi credentials in your private copy; keep ESP32 A on bundled ATR firmware.
2. Use the bundled `Source/gateway/server.py` and set `SERIAL_PORT` to the port of ESP32 A (COM9 in the verified demo).
3. Start the gateway from MCT431_Milestone1: `.\.venv\Scripts\python.exe .\gateway\server.py`. Both stations must show Connected.
4. On HP operator panel configure target 10, cycle 1000 ms; N=4, K=1, inspection time 200 ms. Apply settings. Reset ESP32 A if a fresh 1..10 part ID sequence is needed.
5. START. Expected: part IDs 3 and 7 rejected, 8 PASS / 2 FAIL, pending 0, COMPLETE.
6. Open `/api/audit`: balanced=true, produced=10, completed=10, pending=0.
7. Observe both identifiers in production, ACCEPT and RESULT logs; save screenshots. Actual verified screenshots are already in `Evidence/Identifier_Fix_Verified`.
8. **Recommended evidence completion:** Capture a fresh Wireshark `tcp.port == 9000` session; Follow TCP Stream and save the `.pcapng`. The old capture is correctly labeled pre-fix.

Do not mistakenly substitute the old single-ID TCP capture for a new one.
