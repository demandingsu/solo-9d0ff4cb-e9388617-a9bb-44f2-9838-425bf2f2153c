"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """业务校验失败（缺字、文件格式错、资源不存在等），HTTP 400/404。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MissingGlyphError(DomainError):
    """存在没有任何字库收录的字，无法交印。"""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"缺字（无任何字库收录）: {''.join(missing)}", 422)


class SaveError(DomainError):
    """保存失败：事务回滚，各册仍沿用原清单。"""

    def __init__(self, message: str = "保存失败，已回滚，仍沿用原清单"):
        super().__init__(message, 500)
