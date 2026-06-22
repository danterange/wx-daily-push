"""每日推送主程序 —— 第一版:真实天气。

复用 test_push.py 里已验证通过的推送函数(get_access_token / send_template),
新增"和风天气"抓取,把测试消息换成真实天气。

需要的环境变量:
    微信:   APPID / APPSECRET / TEMPLATE_ID / OPENID
    和风:   QWEATHER_KEY / QWEATHER_HOST(形如 xxxx.qweatherapi.com)
    城市:   CITY(可选,默认"北京";多个城市用英文逗号或顿号分隔,
                  如 "北京市昌平区,北京市海淀区")

本地运行(PowerShell):
    $env:QWEATHER_KEY="xxx"; $env:QWEATHER_HOST="xxxx.qweatherapi.com"; $env:CITY="北京市昌平区,北京市海淀区"; py main.py
(微信那四个环境变量沿用 test_push 验证时设的即可)
"""

import os
import sys
import requests

from test_push import get_access_token, send_template


def resolve_location_id(host: str, key: str, city: str) -> tuple[str, str]:
    """用和风 GeoAPI 把城市名解析成 LocationID。

    返回 (location_id, 标准化城市名)。和风天气接口要求传 LocationID
    或经纬度,所以先用城市名查一次。查不到时抛 RuntimeError。
    """
    # 1. 调用城市查询接口
    url = f"https://{host}/geo/v2/city/lookup"
    resp = requests.get(url, params={"location": city, "key": key}, timeout=10).json()

    # 2. 校验:code=="200" 且 location 列表非空才算成功
    if resp.get("code") != "200" or not resp.get("location"):
        raise RuntimeError(f"城市解析失败: {resp}")

    # 3. 取第一个匹配结果
    top = resp["location"][0]
    return top["id"], top["name"]


def fetch_weather(host: str, key: str, location_id: str) -> dict:
    """抓取指定 LocationID 的实时天气,返回和风的 now 字段字典。

    失败(code 非 "200")时抛 RuntimeError 带出原始响应。
    """
    # 1. 调用实时天气接口
    url = f"https://{host}/v7/weather/now"
    resp = requests.get(url, params={"location": location_id, "key": key}, timeout=10).json()

    # 2. 校验返回码
    if resp.get("code") != "200":
        raise RuntimeError(f"天气抓取失败: {resp}")

    # 3. 返回实时天气对象(含 text/temp/feelsLike/humidity/windDir 等)
    return resp["now"]


def build_weather_text(now: dict) -> str:
    """把和风的 now 字典拼成一句人类可读的天气描述。"""
    return (f"{now['text']} {now['temp']}℃"
            f"(体感{now['feelsLike']}℃,湿度{now['humidity']}%,"
            f"{now['windDir']}{now['windScale']}级)")


def parse_cities(raw: str) -> list[str]:
    """把 CITY 环境变量解析成城市列表,支持英文逗号 / 中文逗号 / 顿号分隔。"""
    # 1. 统一各种分隔符为英文逗号
    for sep in ("，", "、"):
        raw = raw.replace(sep, ",")
    # 2. 拆分、去空白、过滤空项
    return [c.strip() for c in raw.split(",") if c.strip()]


def collect_weather(host: str, key: str, cities: list[str]) -> str:
    """依次抓取多个城市的天气,拼成多行文本;单个城市失败不影响其他。"""
    # 1. 逐个城市抓取,失败的城市单独标注、继续下一个
    lines = []
    for city in cities:
        try:
            # 1.1 城市名 → LocationID → 实时天气 → 文本
            location_id, city_name = resolve_location_id(host, key, city)
            now = fetch_weather(host, key, location_id)
            lines.append(f"{city_name}:{build_weather_text(now)}")
        except Exception as exc:
            # 1.2 单城失败降级:记一行错误,不中断整体
            lines.append(f"{city}:获取失败({exc})")
    # 2. 多行合并
    return "\n".join(lines)


def main() -> int:
    """读环境变量 → 查城市 → 抓天气 → 推送到微信。"""
    # 1. 读取并校验环境变量
    appid = os.environ.get("APPID")
    secret = os.environ.get("APPSECRET")
    template_id = os.environ.get("TEMPLATE_ID")
    openid = os.environ.get("OPENID")
    qkey = os.environ.get("QWEATHER_KEY")
    qhost = os.environ.get("QWEATHER_HOST")
    city_raw = os.environ.get("CITY", "北京")
    required = {"APPID": appid, "APPSECRET": secret, "TEMPLATE_ID": template_id,
                "OPENID": openid, "QWEATHER_KEY": qkey, "QWEATHER_HOST": qhost}
    missing = [name for name, val in required.items() if not val]
    if missing:
        print(f"缺少环境变量: {', '.join(missing)}")
        return 1

    # 2. 解析城市列表 → 逐个抓实时天气 → 合并成多行文本
    cities = parse_cities(city_raw)
    weather_text = collect_weather(qhost, qkey, cities)
    print(f"天气:\n{weather_text}")

    # 3. 换微信 token 并推送(字段名需与测试号模板的 {{关键词.DATA}} 对应)
    token = get_access_token(appid, secret)
    data = {
        "city": "、".join(cities),
        "weather": weather_text,
        "news": "(新闻模块待接入)",
    }
    result = send_template(token, openid, template_id, data)

    # 4. 打印结果
    print("推送结果:", result)
    return 0 if result.get("errcode") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
