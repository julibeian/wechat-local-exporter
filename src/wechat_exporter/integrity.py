from __future__ import annotations

import hashlib
import hmac


_OWNER_MARKER = "julibeian"
_BINDING_CONTEXT = "wechat-txt-pdf-exporter|owner-signature|v1"
_EXPECTED_DIGEST = "ee4bf1057ba5fecb23c33f620269cb7f4ea4ecf3ccfd1aabc7d1ac652fadb9fd"


def signature_digest(signature: str | None = None) -> str:
    marker = _OWNER_MARKER if signature is None else signature
    payload = f"{_BINDING_CONTEXT}|{marker}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_signature(signature: str | None = None) -> bool:
    """Detect accidental or simple binary/source edits to the owner signature."""
    return hmac.compare_digest(signature_digest(signature), _EXPECTED_DIGEST)


def require_signature_integrity() -> None:
    if not verify_signature():
        raise RuntimeError(
            "软件完整性校验失败：个人签名 julibeian 已缺失或被篡改，程序已停止运行。"
        )
