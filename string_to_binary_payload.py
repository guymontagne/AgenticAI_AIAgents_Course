"""Convert a string key into a binary payload."""

key = "pokemon"

# UTF-8 binary payload (bytes)
payload: bytes = key.encode("utf-8")

# Hex and bit-string views of the same payload
payload_hex = payload.hex()
payload_bits = " ".join(f"{byte:08b}" for byte in payload)

if __name__ == "__main__":
    print(f"key:          {key!r}")
    print(f"payload:      {payload!r}")
    print(f"payload hex:  {payload_hex}")
    print(f"payload bits: {payload_bits}")
    print(f"length:       {len(payload)} bytes")
