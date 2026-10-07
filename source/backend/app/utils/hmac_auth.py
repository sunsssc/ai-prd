from __future__ import annotations

import hashlib
import hmac


def verify_sha256_hmac(*, secret: str, payload: bytes, signature_header: str | None) -> bool:
    """校验 `sha256=<hex>` 形式的 HMAC-SHA256 签名，用于服务间内部调用的共享密钥鉴权。"""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
