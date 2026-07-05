import base64
import hashlib
import hmac
import os

from app.config import Config


TOKEN_PREFIX = "rgenc:v1:"


def _secret():
    secret = Config.SECRET_KEY or ""
    if not secret:
        raise ValueError("SECRET_KEY is required to encrypt Google tokens.")
    return secret.encode("utf-8")


def _keystream(nonce, length):
    chunks = []
    counter = 0
    secret = _secret()

    while sum(len(chunk) for chunk in chunks) < length:
        counter += 1
        chunks.append(
            hmac.new(
                secret,
                nonce + counter.to_bytes(4, "big"),
                hashlib.sha256
            ).digest()
        )

    return b"".join(chunks)[:length]


def encrypt_token(value):
    if not value:
        return value

    if isinstance(value, bytes):
        value = value.decode("utf-8")

    if value.startswith(TOKEN_PREFIX):
        return value

    plaintext = value.encode("utf-8")
    nonce = os.urandom(16)
    stream = _keystream(nonce, len(plaintext))
    ciphertext = bytes(
        plain_byte ^ stream_byte
        for plain_byte, stream_byte in zip(plaintext, stream)
    )
    signature = hmac.new(_secret(), nonce + ciphertext, hashlib.sha256).digest()
    payload = base64.urlsafe_b64encode(nonce + signature + ciphertext).decode("ascii")

    return f"{TOKEN_PREFIX}{payload}"


def decrypt_token(value):
    if not value:
        return value

    if isinstance(value, bytes):
        value = value.decode("utf-8")

    if not value.startswith(TOKEN_PREFIX):
        return value

    payload = value[len(TOKEN_PREFIX):]
    raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce = raw[:16]
    signature = raw[16:48]
    ciphertext = raw[48:]
    expected = hmac.new(_secret(), nonce + ciphertext, hashlib.sha256).digest()

    if not hmac.compare_digest(signature, expected):
        raise ValueError("Encrypted token signature is invalid.")

    stream = _keystream(nonce, len(ciphertext))
    plaintext = bytes(
        cipher_byte ^ stream_byte
        for cipher_byte, stream_byte in zip(ciphertext, stream)
    )

    return plaintext.decode("utf-8")
