"""微信天气推送。

运行模式由 RUN_MODE 决定：
  * monitor: 检查未来 24 小时；只有可能降水或出现恶劣天气时才发送。
  * summary: 固定发送一次简洁的明日最高/最低温及天气风险摘要。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from test_push import get_access_token, send_template


# 降水概率达到该阈值时，即使预报文字尚未写明下雨，也作为"可能降水"提醒。
RAIN_PROBABILITY_THRESHOLD = 30

# 微信测试号模板的单个关键词字段最多显示 20 个字符；超出后客户端会省略。
TEMPLATE_FIELD_MAX_LENGTH = 20

# 风险天气名称保留的最大长度，给时间和降水概率留出足够空间。
RISK_LABEL_MAX_LENGTH = 8

# 和风天气的 text 字段中，包含这些词即视为需要提醒的天气。
ADVERSE_WEATHER_KEYWORDS = (
    "冰雹",
    "雷",
    "雨",
    "雪",
    "冻",
    "雾",
    "霾",
    "沙尘",
    "扬沙",
    "浮尘",
    "大风",
    "龙卷风",
)


def parse_cities(raw: str) -> list[str]:
    """把 CITY 环境变量解析成城市列表，支持中英文逗号和顿号。"""
    for separator in ("，", "、"):
        raw = raw.replace(separator, ",")
    return [city.strip() for city in raw.split(",") if city.strip()]


def qweather(host: str, key: str, path: str, params: dict) -> dict:
    """调用和风天气接口；网络错误重试三次，业务错误立即失败。"""
    url = f"https://{host}{path}"
    last_error = None
    for _ in range(3):
        try:
            response = requests.get(url, params={"key": key, **params}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            break
        except requests.RequestException as error:
            last_error = error
    else:
        raise RuntimeError(f"{path} 网络请求失败（已重试 3 次）：{last_error}")

    if payload.get("code") != "200":
        raise RuntimeError(f"{path} 返回异常：{payload.get('code')}")
    return payload


def resolve_location_id(host: str, key: str, city: str) -> tuple[str, str]:
    """将城市名解析为和风天气 LocationID 和标准城市名。"""
    payload = qweather(host, key, "/geo/v2/city/lookup", {"location": city})
    locations = payload.get("location") or []
    if not locations:
        raise RuntimeError(f"城市解析无结果：{city}")
    location = locations[0]
    return location["id"], location["name"]


def number(value: object, converter, default: float | int = 0):
    """将天气 API 中可能为空的数字字段安全转换。"""
    try:
        return converter(value or 0)
    except (TypeError, ValueError):
        return default


def fit_template_field(value: object, max_length: int = TEMPLATE_FIELD_MAX_LENGTH) -> str:
    """将模板字段规范为单行，并确保不会被微信客户端自动截断。"""
    text = " ".join(str(value).split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"


def compact_risk_label(label: str) -> str:
    """压缩过长的天气描述，保留开头的主要天气现象。"""
    return fit_template_field(label, RISK_LABEL_MAX_LENGTH)


def weather_risk_label(weather_text: str) -> str | None:
    """返回预报文字中的恶劣天气标签；晴、多云、阴等返回 None。"""
    weather_text = weather_text.strip()
    if any(keyword in weather_text for keyword in ADVERSE_WEATHER_KEYWORDS):
        return weather_text or "恶劣天气"
    return None


def hourly_risk_label(hour: dict) -> str | None:
    """判断一个逐小时预报是否需要提醒，并返回对应的风险标签。"""
    text_label = weather_risk_label(str(hour.get("text") or ""))
    if text_label:
        return text_label

    precipitation = number(hour.get("precip"), float)
    if precipitation > 0:
        return "降水"

    precipitation_probability = number(hour.get("pop"), int)
    if precipitation_probability >= RAIN_PROBABILITY_THRESHOLD:
        return "降水"
    return None


def risk_start_time(hour: dict) -> str:
    """将风险预报时间压缩为时刻，例如“07 时”压缩为“7 时”。"""
    value = str(hour.get("fxTime") or "")
    if len(value) >= 13 and value[11:13].isdigit():
        return f"{int(value[11:13])}时"
    return "近期"


def future_risk_summary(hourly: list[dict]) -> str | None:
    """汇总未来 24 小时的首个风险，生成可完整显示的短文案。"""
    risks = [(hour, label) for hour in hourly if (label := hourly_risk_label(hour))]
    if not risks:
        return None

    labels: list[str] = []
    for _, label in risks:
        if label not in labels:
            labels.append(label)

    highest_probability = max(number(hour.get("pop"), int) for hour, _ in risks)
    more_text = "等" if len(labels) > 1 else ""
    probability_text = f"，{highest_probability}%" if highest_probability else ""
    return fit_template_field(
        f"{risk_start_time(risks[0][0])}起{compact_risk_label(labels[0])}{more_text}{probability_text}"
    )


def tomorrow_forecast(daily: list[dict], now_bj: datetime) -> dict:
    """从 7 日预报中找出北京日期为明天的那一项。"""
    tomorrow = (now_bj + timedelta(days=1)).date().isoformat()
    for forecast in daily:
        if forecast.get("fxDate") == tomorrow:
            return forecast

    # 和风 7 日预报通常以今天为第一个元素；保留此回退以兼容未带 fxDate 的测试数据。
    if len(daily) >= 2:
        return daily[1]
    raise RuntimeError("7 日预报中没有明天的数据")


def daily_risk_labels(forecast: dict) -> list[str]:
    """提取明天白天和夜间预报里的不良天气标签。"""
    labels: list[str] = []
    for field in ("textDay", "textNight"):
        label = weather_risk_label(str(forecast.get(field) or ""))
        if label and label not in labels:
            labels.append(label)
    return labels


def build_monitor_alert(host: str, key: str, city: str) -> tuple[str, str] | None:
    """构建一个城市的监测告警；未来 24 小时无风险时不返回消息。"""
    location_id, name = resolve_location_id(host, key, city)
    hourly = qweather(host, key, "/v7/weather/24h", {"location": location_id}).get("hourly") or []
    summary = future_risk_summary(hourly)
    if not summary:
        return None
    return f"{name} 天气提醒", summary


def build_summary_lines(
    host: str, key: str, city: str, now_bj: datetime
) -> tuple[str, str]:
    """构建一个城市简洁的明日天气摘要。"""
    location_id, name = resolve_location_id(host, key, city)
    daily = qweather(host, key, "/v7/weather/7d", {"location": location_id}).get("daily") or []
    forecast = tomorrow_forecast(daily, now_bj)
    risks = daily_risk_labels(forecast)

    high = forecast.get("tempMax", "?")
    low = forecast.get("tempMin", "?")
    line1 = f"{name}明日{low}~{high}度"
    line2 = "、".join(compact_risk_label(risk) for risk in risks) if risks else "无降水和恶劣天气"
    return line1, line2


def template_fields(lines: list[tuple[str, str]]) -> dict[str, str]:
    """将最多两个城市的两行内容映射到当前微信模板字段并控制字数。"""
    fields: dict[str, str] = {}
    for index, (line1, line2) in enumerate(lines[:2], start=1):
        fields[f"c{index}"] = fit_template_field(line1)
        fields[f"c{index}r"] = fit_template_field(line2)
    return fields


def required_environment() -> tuple[dict[str, str], list[str]]:
    """读取必需的密钥配置，并返回缺失变量名称。"""
    values = {
        "APPID": os.environ.get("APPID", ""),
        "APPSECRET": os.environ.get("APPSECRET", ""),
        "TEMPLATE_ID": os.environ.get("TEMPLATE_ID", ""),
        "OPENID": os.environ.get("OPENID", ""),
        "QWEATHER_KEY": os.environ.get("QWEATHER_KEY", ""),
        "QWEATHER_HOST": os.environ.get("QWEATHER_HOST", ""),
    }
    return values, [name for name, value in values.items() if not value]


def send_weather_message(config: dict[str, str], data: dict[str, str]) -> int:
    """在确认需要发送后再获取微信 token 并推送模板消息。"""
    print("推送内容:\n" + "\n".join(f"{key}={value}" for key, value in data.items()))
    token = get_access_token(config["APPID"], config["APPSECRET"])
    result = send_template(token, config["OPENID"], config["TEMPLATE_ID"], data)
    print("推送结果：", result)
    return 0 if result.get("errcode") == 0 else 1


def main() -> int:
    """根据 RUN_MODE 运行监测告警或晚间明日天气摘要。"""
    config, missing = required_environment()
    if missing:
        print(f"缺少环境变量：{', '.join(missing)}")
        return 1

    mode = os.environ.get("RUN_MODE", "summary").strip().lower()
    if mode not in {"monitor", "summary"}:
        print(f"RUN_MODE 必须是 monitor 或 summary，当前为：{mode}")
        return 1

    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    cities = parse_cities(os.environ.get("CITY", "北京"))
    if not cities:
        print("CITY 没有可用城市")
        return 1
    if len(cities) > 2:
        print(f"提示：当前微信模板只有两个城市槽位，将只显示前两个城市：{cities[:2]}")
        cities = cities[:2]

    print(f"北京时间 {now_bj:%Y-%m-%d %H:%M}，模式={mode}")
    if mode == "monitor":
        alerts: list[tuple[str, str]] = []
        for city in cities:
            try:
                alert = build_monitor_alert(config["QWEATHER_HOST"], config["QWEATHER_KEY"], city)
                if alert:
                    alerts.append(alert)
            except Exception as error:
                print(f"{city} 监测失败：{error}")

        if not alerts:
            print("未来 24 小时未监测到降水或不良天气，不发送消息")
            return 0

        data = {
            "date": f"天气提醒 {now_bj:%m月%d日 %H:%M}",
            **template_fields(alerts),
            "tip": "请提前安排出行，注意安全",
        }
        return send_weather_message(config, data)

    summaries: list[tuple[str, str]] = []
    for city in cities:
        try:
            summaries.append(build_summary_lines(config["QWEATHER_HOST"], config["QWEATHER_KEY"], city, now_bj))
        except Exception as error:
            print(f"{city} 明日预报获取失败：{error}")
            summaries.append((f"{city} 明日天气", "天气数据获取失败"))

    data = {
        "date": f"明日天气 {now_bj:%m月%d日 %H:%M}",
        **template_fields(summaries),
        "tip": "明天出门前留意天气变化",
    }
    return send_weather_message(config, data)


if __name__ == "__main__":
    sys.exit(main())
