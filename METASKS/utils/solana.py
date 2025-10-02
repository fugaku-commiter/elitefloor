from __future__ import annotations

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ALPHABET_MAP = {c: i for i, c in enumerate(BASE58_ALPHABET)}


def _b58decode_to_bytes(value: str) -> bytes:
    if not value:
        return b""
    num = 0
    for ch in value:
        try:
            digit = _ALPHABET_MAP[ch]
        except KeyError:
            raise ValueError("Invalid base58 character")
        num = num * 58 + digit
    # Convert to bytes without sign, big-endian
    full = num.to_bytes((num.bit_length() + 7) // 8, byteorder="big") or b"\x00"
    # Account for leading zeros encoded as '1'
    leading_zeros = 0
    for ch in value:
        if ch == '1':
            leading_zeros += 1
        else:
            break
    return b"\x00" * leading_zeros + full


def is_valid_solana_address(address: str) -> bool:
    if not isinstance(address, str):
        return False
    addr = address.strip()
    if len(addr) < 32 or len(addr) > 50:
        return False
    # Must be all base58 chars
    if any(ch not in _ALPHABET_MAP for ch in addr):
        return False
    try:
        decoded = _b58decode_to_bytes(addr)
    except ValueError:
        return False
    # Solana public keys are 32 bytes
    return len(decoded) == 32


