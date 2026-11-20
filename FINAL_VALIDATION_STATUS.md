# Final validation status — 15 November 2026

## Identifier preservation verified on hardware

Original part ID 1..10 carried from ESP32 A through gateway to ESP32 B unchanged. Persistent transaction job IDs 1794701347–1794701356 were separate. Rule N=4 K=1 rejected original part IDs 3 and 7. Batch: 10 produced, 8 accepted, 2 rejected, 0 pending. /api/audit balanced=true, serial duplicates=0 / CRC errors=0 / bad frames=0. ESP32 A acknowledged STOP on automatic completion.

## Provenance and raw data

The verified 15 Nov data come from five screenshots in Evidence/Identifier_Fix_Verified, not from a complete raw terminal-log file. The per-part CSV records only durations visible in those screenshots. Historical Wireshark capture and timestamps refer to the older one-ID protocol; they remain valuable as evidence of two-byte framing, but cannot prove the new two-ID on-wire format.

## Submission status


POST-FIX TCP EVIDENCE: New raw file Evidence/TCP/inspection_tcp_identifier_fixed.pcapng verifies 10 matched INSPECT, ACCEPT and RESULT transactions; full TCP framing checked. See Evidence/TCP/post_fix_capture_validation.txt.
