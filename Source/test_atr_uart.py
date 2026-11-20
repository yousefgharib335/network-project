"""Standalone ESP32 A ATR/SLIP hardware test. Do NOT run gateway simultaneously.
Usage: python test_atr_uart.py [--test-fault]
--test-fault deliberately withholds all ACKs from one part and verifies the
firmware reports a bounded-retry FAULT. Firmware stops and needs power-cycle
or a START command for further production after this test.
"""
import argparse
import time
import serial
from gateway.server import (SlipDecoder, encode_serial_frame, decode_atr,
    TYPE_PART, TYPE_ACK, TYPE_COMMAND, TYPE_COMMAND_ACK, TYPE_SET_CYCLE,
    TYPE_SET_CYCLE_ACK, TYPE_FAULT)

PORT = "COM9"  # Last verified ESP32 A port. Confirm in Device Manager before running.
BAUD = 115200

def main():
    arg = argparse.ArgumentParser()
    arg.add_argument("--test-fault", action="store_true")
    opts = arg.parse_args()
    decoder = SlipDecoder()
    unique = set()
    dropped_one = False
    retry_seen = False
    stop_seq = 1000003
    with serial.Serial(PORT, BAUD, timeout=0.2) as ser:
        time.sleep(1)
        ser.reset_input_buffer()
        ser.write(encode_serial_frame(TYPE_SET_CYCLE, 200, 1000001))
        print("Sent CYCLE 200ms, command seq=1000001")
        state = "cycle"
        limit = time.monotonic() + 40
        fault_target = None
        while time.monotonic() < limit:
            for payload in decoder.feed(ser.read(256)):
                try:
                    kind, seq, value = decode_atr(payload)
                except ValueError as error:
                    print("INVALID ATR:", error)
                    continue
                if kind == TYPE_SET_CYCLE_ACK and seq == 1000001 and state == "cycle":
                    print("Cycle ACK verified; sending START")
                    ser.write(encode_serial_frame(TYPE_COMMAND, 1, 1000002))
                    state = "start"
                elif kind == TYPE_COMMAND_ACK and seq == 1000002:
                    print("START ACK verified; receiving parts")
                    state = "parts"
                elif kind == TYPE_PART and state in ("parts", "fault"):
                    if seq != value:
                        raise RuntimeError("PART sequence and ID differ")
                    if opts.test_fault and fault_target is not None and value == fault_target:
                        print("Withholding ACK for fault test part", value)
                        continue
                    if value in unique:
                        retry_seen = True
                        print("RETRANSMISSION PART", value, "(not counted twice)")
                        ser.write(encode_serial_frame(TYPE_ACK, value, seq))
                    else:
                        unique.add(value)
                        print("VALID PART", value, "unique count", len(unique))
                        if len(unique) == 3 and not dropped_one:
                            dropped_one = True
                            print("Intentionally withholding FIRST ACK of part", value)
                        elif opts.test_fault and len(unique) == 6:
                            fault_target = value
                            state = "fault"
                            print("Withholding ALL ACKs of part", value)
                        else:
                            ser.write(encode_serial_frame(TYPE_ACK, value, seq))
                    if len(unique) >= 8 and not opts.test_fault:
                        ser.write(encode_serial_frame(TYPE_COMMAND, 4, stop_seq))
                        state = "stop"
                elif kind == TYPE_FAULT and opts.test_fault:
                    print("PASS: ESP32 A reported FAULT after bounded retries for part", value)
                    return
                elif kind == TYPE_COMMAND_ACK and seq == stop_seq and state == "stop":
                    if retry_seen and len(unique) >= 8:
                        print("PASS: ATR CRC/sequence, 8 unique parts, lost ACK retransmitted, STOP acknowledged")
                        return
                    raise RuntimeError("Insufficient parts or no retransmission")
        raise TimeoutError("ATR test did not finish; check COM port, firmware and serial monitor")

if __name__ == "__main__":
    main()
