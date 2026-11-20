# Verified status and remaining limitations (15 November 2026)

## Verified hardware requirements

- Matched dual-ID gateway and ESP32 B: original ESP32 A part ID is unchanged across the TCP boundary; persistent job ID is separate.
- Final 10-part test (N=4, K=1, 1000 ms production, 200 ms inspection) returned 8 PASS / 2 FAIL; parts 3 and 7 failed; pending=0, balanced=true.
- Final screenshot audit reports duplicates=0, CRC errors=0, bad frames=0.
- Queue-full behavior and fault recovery previously documented in timestamped logs; disconnected ESP32 A/B screenshots are available.

## Important remaining notes

1. The original Wireshark pcapng documents the **legacy single-ID TCP messages** but valid two-byte length prefix. The final dual-ID TCP exchange is supported by terminal screenshots, NOT yet by a new raw pcapng. Recommend a quick fresh capture for completeness.
2. Actual team contribution tasks still need to be supplied. No percentages.
3. Live demo and individual oral explanations are still required by the course.
4. Accepted inspection queue/batch counters reside in gateway RAM; a gateway process crash does not promise lossless batch recovery. Persistent job IDs survive gateway restarts.
5. ESP32 B duplicate suppression cache is finite (256 entries). Raw production part IDs restart following MCU reboot; job IDs distinguish independent occurrences.
6. Benchmark protocol comparisons across MQTT, Modbus, TCP, UDP etc. are Milestone 2 work and not claimed as completed.
7. Port assignments can change: production COM9 was last verified, but always check current USB port.
8. Wi-Fi password placeholders in public source need PRIVATE local values when rebuilding.

POST-FIX TCP EVIDENCE: New raw file Evidence/TCP/inspection_tcp_identifier_fixed.pcapng verifies 10 matched INSPECT, ACCEPT and RESULT transactions; full TCP framing checked. See Evidence/TCP/post_fix_capture_validation.txt.
