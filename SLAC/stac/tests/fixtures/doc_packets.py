"""Byte-exact example packets printed in Campbell Scientific
"BMP5 Transparent Commands" rev. 9/08. These are the ground-truth
vectors for framing, header layout and signature tests.

Each WIRE_* constant is the complete on-the-wire frame including the
0xBD delimiters, exactly as printed in the document (no byte in any of
these examples happens to need quoting).

Live captures from the bench CR6 should be added to this package as
they are recorded (acceptance criterion 5).
"""


def _hex(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", "").replace("\n", ""))


# sec. 3.2 / 4.2 -- Ring from app 4094 to logger 1
WIRE_RING = _hex("BD 90 01 0F FE 71 D2 BD")
# sec. 3.2 / 4.2 -- Ready from logger 1 to app 4094
WIRE_READY = _hex("BD AF FE 00 01 5A 89 BD")

# sec. 3.5 / 4.5 -- Clock check command, app 4094 -> logger 1
WIRE_CLOCK_CMD = _hex(
    "BD A0 01 4F FE 10 01 0F FE 17 17 00 00 00 00 00 00 00 00 00 00"
    " B2 B3 BD")
# sec. 3.5 / 4.5 -- Clock response, logger 1 -> app 4094
WIRE_CLOCK_RESP = _hex(
    "BD AF FE 00 01 1F FE 00 01 97 17 00 1B FA 2A 61 C8 00 00 00"
    " 04 FA BD")

# sec. 3.9 / 4.9 -- File Upload command for "CPU:Def.tdf", app 4 -> logger 1
WIRE_TDF_UPLOAD_CMD = _hex(
    "BD A0 01 70 04 10 01 00 04 1D 1D 00 00 43 50 55 3A 44 65 66"
    " 2E 74 64 66 00 00 00 00 00 00 00 80 27 EA BD")

# sec. 3.10 / 4.10 -- Collect Data command, mode 5 (most recent 60 records),
# table 3, table signature 0x4315, app 4 -> logger 1
WIRE_COLLECT_CMD = _hex(
    "BD A0 01 70 04 10 01 00 04 09 09 00 00 05 00 03 43 15 00 00"
    " 00 3C 00 00 C7 DF BD")

ALL_WIRE_EXAMPLES = {
    "ring": WIRE_RING,
    "ready": WIRE_READY,
    "clock_cmd": WIRE_CLOCK_CMD,
    "clock_resp": WIRE_CLOCK_RESP,
    "tdf_upload_cmd": WIRE_TDF_UPLOAD_CMD,
    "collect_cmd": WIRE_COLLECT_CMD,
}
