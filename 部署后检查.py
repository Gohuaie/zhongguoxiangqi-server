import json
import sys
from urllib.request import urlopen


BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "https://zhong-guo-xiang-qi-yun-duan-fu-wu-qi.onrender.com").rstrip("/")


def read(path):
    with urlopen(BASE_URL + path, timeout=30) as response:
        return response.read().decode("utf-8")


health = read("/health")
if health != "OK":
    raise SystemExit(f"/health 异常: {health!r}")

status = json.loads(read("/security-status"))
required = ("wechat_content_security_enabled", "wechat_credentials_configured", "fail_closed")
if not all(status.get(key) is True for key in required):
    raise SystemExit(f"内容安全配置未完成: {status}")
if int(status.get("forbidden_words", 0)) < 15000:
    raise SystemExit(f"违禁词库未正确加载: {status}")

print("Render 健康检查：通过")
print("微信内容安全配置：通过")
print(f"已加载违禁词：{status['forbidden_words']} 条")
