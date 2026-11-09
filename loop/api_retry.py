"""
api_retry.py — API 错误分类 + 指数退避重试（对应 Claude Code 第 14 课）

核心思想：
  不是所有错误都该重试。先分类，再决定：
    - 暂时性错误（5xx/429/超时/连接错误）→ 退避后重试
    - 永久性错误（401/403/请求过长）→ 立即失败，重试无意义
"""
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

BASE_DELAY_MS = 500      # 首次重试基准
MAX_DELAY_MS = 32_000    # 退避上限
MAX_OVERLOAD_RETRIES = 3


class APIError(Exception):
    """模拟 openai SDK 的错误：带 HTTP 状态码。"""
    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(message or f"HTTP {status}")


def classify_error(error: Exception) -> str:
    """把错误归类，决定后续动作。对应源码 classifyAPIError。"""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "transient"          # 超时/断网：可重试
    if isinstance(error, APIError):
        if error.status in (429, 529) or error.status >= 500:
            return "transient"      # 限流/过载/服务端错误：可重试
        if error.status in (401, 403):
            return "auth"          # 认证失败：立即停，提示换 key
        if error.status == 400:
            return "bad_request"   # 如 prompt 太长：改了才有意义
        return "permanent"
    return "unknown"


def retry_delay_seconds(attempt: int, retry_after: "float | None" = None) -> float:
    """指数退避 + 抖动。attempt 从 1 开始：500ms, 1s, 2s... 封顶 32s。"""
    if retry_after is not None:
        return retry_after         # 服务器明确说了等多久，听它的
    base_ms = min(BASE_DELAY_MS * (2 ** (attempt - 1)), MAX_DELAY_MS)
    jitter_ms = random.random() * 0.25 * base_ms   # 0~25% 抖动，防重试风暴
    return (base_ms + jitter_ms) / 1000


def call_with_retry(
    operation: Callable[[], T],
    max_retries: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: "Callable[[int, str, float], None] | None" = None,
) -> T:
    """
    执行 operation，按错误类型决定是否重试。
    sleep / on_retry 可注入：测试时不等真时间、还能观察重试过程。
    """
    overload_count = 0
    last_error = None

    for attempt in range(1, max_retries + 2):   # 首次 + max_retries 次重试
        try:
            return operation()
        except Exception as error:
            last_error = error
            kind = classify_error(error)

            if kind != "transient":
                # 永久性错误：重试无意义，立即抛出
                raise

            if error.__class__ is APIError and error.status == 529:
                overload_count += 1
                if overload_count >= MAX_OVERLOAD_RETRIES:
                    raise RuntimeError("服务器连续过载，应触发模型回退(fallback)") from error

            if attempt > max_retries:
                raise

            delay = retry_delay_seconds(attempt)
            if on_retry:
                on_retry(attempt, kind, delay)
            sleep(delay)

    raise last_error  # 理论上到不了
