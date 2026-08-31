"""Translate failures at UI boundaries without exposing exceptions or memory."""


class UserFacingError(RuntimeError):
    pass


def user_message(error: BaseException) -> str:
    if isinstance(error, UserFacingError):
        return str(error)
    if isinstance(error, PermissionError):
        return "无法读取文件或进程。请确认目录可写，且微信与本工具以相同用户权限运行。"
    if isinstance(error, TimeoutError):
        return "等待超时。请确认微信已完成登录，稍后重试。"
    if isinstance(error, FileNotFoundError):
        return "未找到所需本地文件。请先打开并登录微信，再重新连接。"
    return "本次操作未完成，当前微信数据未被修改。请稍后重试；若仍失败，请确认微信版本受支持。"
