"""重试层离线测试：不碰网络，用假 operation 模拟各种错误。"""
import api_retry
from api_retry import APIError, call_with_retry, classify_error, retry_delay_seconds


def expect(cond, msg):
    assert cond, msg
    print("✓", msg)


no_sleep = lambda s: None  # 测试不真等

# 1. 暂时性错误重试后成功
calls = {"n": 0}
def flaky():
    calls["n"] += 1
    if calls["n"] <= 2:
        raise APIError(529, "overloaded")
    return "ok"
retries = []
result = call_with_retry(flaky, max_retries=5, sleep=no_sleep,
                         on_retry=lambda a, k, d: retries.append((a, k)))
expect(result == "ok" and calls["n"] == 3, "前两次 529、第三次成功：返回结果且共调用 3 次")
expect([a for a, _ in retries] == [1, 2], "记录到第 1、2 次重试")
expect(all(k == "transient" for _, k in retries), "529 被分类为 transient")

# 2. 401 认证错误：立即失败，绝不重试
calls2 = {"n": 0}
def bad_key():
    calls2["n"] += 1
    raise APIError(401, "invalid api key")
try:
    call_with_retry(bad_key, max_retries=5, sleep=no_sleep)
    expect(False, "401 应当抛出")
except APIError as e:
    expect(e.status == 401 and calls2["n"] == 1, "401 只调用 1 次，不重试（重试无意义）")

expect(classify_error(APIError(403)) == "auth", "403 也归为 auth")
expect(classify_error(APIError(400)) == "bad_request", "400 归为 bad_request（如 prompt 过长）")
expect(classify_error(TimeoutError()) == "transient", "超时归为 transient")
expect(classify_error(ConnectionError()) == "transient", "连接错误归为 transient")

# 3. 连续 3 次 529 → 触发模型回退
calls3 = {"n": 0}
def always_overloaded():
    calls3["n"] += 1
    raise APIError(529, "overloaded")
try:
    call_with_retry(always_overloaded, max_retries=10, sleep=no_sleep)
    expect(False, "连续过载应当抛回退错误")
except RuntimeError as e:
    expect("回退" in str(e) and calls3["n"] == 3, "连续 3 次 529 后停止并提示 fallback（只试 3 次）")

# 4. 退避序列：把抖动固定为 0，验证 0.5s 翻倍到 32s 封顶
api_retry.random.random = lambda: 0.0
seq = [retry_delay_seconds(a) for a in range(1, 8)]
expect(seq == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0],
       f"指数退避序列正确：{seq}")
api_retry.random.random = lambda: 1.0  # 抖动拉满
d = retry_delay_seconds(1)
expect(0.5 <= d <= 0.625, f"抖动使首次延迟落在 0.5~0.625s：{d:.3f}s")

# 5. retry-after 优先：服务器说等 10s 就等 10s
expect(retry_delay_seconds(1, retry_after=10) == 10, "Retry-After 头优先于指数退避")

# 6. 超过最大重试次数仍失败 → 抛出最后一个错误
calls4 = {"n": 0}
def always_500():
    calls4["n"] += 1
    raise APIError(500)
try:
    call_with_retry(always_500, max_retries=2, sleep=no_sleep)
    expect(False, "应抛出")
except APIError as e:
    expect(calls4["n"] == 3, "max_retries=2 时共尝试 3 次（首次+2 重试）")

print("\n全部通过")
