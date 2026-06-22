"""每日天气推送主程序 —— 路线A:测试号模板消息(纯文字多行)。

数据来自和风天气,经微信测试号模板消息推送(复用 test_push.py 的发送函数)。
模板消息坑点规避:
  · 每个字段一行,字段内绝不含换行符(\\n 会被微信截断,这是"海淀消失"的根因)
  · 整条内容 ≤200 字(连续数字/字母算 1 字),我们实际约 90 字,余量充足
  · emoji 是否被去除属待实测项,本版保留少量 emoji 用于验证

特性:
  · 实时天气 + 今日温度区间(逐天)
  · 扫描未来 24 小时逐小时预报,智能生成"降水时段"提醒
  · 生活指数(按时段选:早=穿衣 / 午=紫外线 / 晚=感冒)
  · 时段感知:早间播报今日、午后播报、晚间附带"明早"预览
  · 多城市,单城抓取失败自动降级不影响其他

需要的环境变量:
    微信:   APPID / APPSECRET / TEMPLATE_ID / OPENID
    和风:   QWEATHER_KEY / QWEATHER_HOST(形如 xxxx.qweatherapi.com)
    城市:   CITY(多个用逗号/顿号分隔,默认"北京")
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import requests

from test_push import get_access_token, send_template

# 生活指数 type 代码:3=穿衣, 5=紫外线, 9=感冒(见和风文档)
SLOT_INDEX_TYPE = {"morning": "3", "afternoon": "5", "evening": "9"}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def parse_cities(raw: str) -> list[str]:
    """把 CITY 环境变量解析成城市列表,支持英文逗号 / 中文逗号 / 顿号分隔。"""
    for sep in ("，", "、"):
        raw = raw.replace(sep, ",")
    return [c.strip() for c in raw.split(",") if c.strip()]


def qweather(host: str, key: str, path: str, params: dict) -> dict:
    """调用和风某个接口并返回 JSON;code 非 "200" 时抛 RuntimeError。

    path 形如 "/v7/weather/now";params 不需带 key,函数内部补上。
    """
    url = f"https://{host}{path}"
    resp = requests.get(url, params={"key": key, **params}, timeout=10).json()
    if resp.get("code") != "200":
        raise RuntimeError(f"{path} 返回异常: {resp}")
    return resp


def resolve_location_id(host: str, key: str, city: str) -> tuple[str, str]:
    """城市名 → (LocationID, 标准化城市名),用和风 GeoAPI 查询。"""
    resp = qweather(host, key, "/geo/v2/city/lookup", {"location": city})
    if not resp.get("location"):
        raise RuntimeError(f"城市解析无结果: {city}")
    top = resp["location"][0]
    return top["id"], top["name"]


def scan_rain(hourly: list[dict]) -> tuple[str, bool]:
    """扫描逐小时数据,生成首个降水时段提醒。

    判定某小时"可能下雨"的条件:降水量 > 0 或 降水概率 >= 50%。
    返回 (描述文本, 是否有雨)。无雨时返回正向提示。
    """
    # 1. 把每小时标记为是否降水
    flags = []
    for h in hourly:
        try:
            precip = float(h.get("precip") or 0)
        except ValueError:
            precip = 0.0
        try:
            pop = int(h.get("pop") or 0)
        except ValueError:
            pop = 0
        flags.append(precip > 0 or pop >= 50)

    # 2. 找出所有连续降水区间
    intervals = []
    i, n = 0, len(hourly)
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and flags[j + 1]:
                j += 1
            intervals.append((i, j))
            i = j + 1
        else:
            i += 1

    # 3. 无降水:返回正向提示
    if not intervals:
        return "☔未来24h无降水", False

    # 4. 取首个降水时段,计算起止时刻与最大概率
    a, b = intervals[0]
    start = hourly[a]["fxTime"][11:16]
    end = hourly[b]["fxTime"][11:16]
    pops = []
    for k in range(a, b + 1):
        try:
            pops.append(int(hourly[k].get("pop") or 0))
        except ValueError:
            pass
    max_pop = max(pops) if pops else 0
    text = hourly[a].get("text", "降水")
    more = " 等" if len(intervals) > 1 else ""
    return f"☔{start}-{end}{text}{max_pop}% 带伞{more}", True


def tomorrow_morning_line(hourly: list[dict], tomorrow_md: str) -> str | None:
    """从逐小时数据里挑明天早晨(6-9 点)一条预览,供晚间播报用;无则返回 None。"""
    picks = [h for h in hourly
             if h["fxTime"][5:10] == tomorrow_md and 6 <= int(h["fxTime"][11:13]) <= 9]
    if not picks:
        return None
    h = picks[len(picks) // 2]  # 取中间一条(约 7-8 点)
    return f"🌙明早{h['fxTime'][11:16]}{h['text']}{h['temp']}℃"


def fetch_index_word(host: str, key: str, lid: str, slot: str) -> str | None:
    """按时段抓对应生活指数,返回简短词如 "穿衣:炎热";失败返回 None(降级)。"""
    itype = SLOT_INDEX_TYPE[slot]
    try:
        resp = qweather(host, key, "/indices/1d", {"location": lid, "type": itype})
    except Exception:
        return None
    daily = resp.get("daily") or []
    if not daily:
        return None
    idx = daily[0]
    name = idx.get("name", "").replace("指数", "")
    return f"{name}:{idx.get('category', '')}"


def build_city_lines(host: str, key: str, city: str, slot: str, now_bj: datetime) -> tuple[str, str, bool]:
    """抓取单个城市天气,拼成两行单行文本(行内绝不含换行符)。

    返回 (第一行, 第二行, 是否有雨)。两行分别填进模板两个字段,
    规避"单字段含换行被微信截断"的坑。任一接口失败由上层捕获降级。
    """
    # 1. 城市解析
    lid, name = resolve_location_id(host, key, city)

    # 2. 实时 / 逐天 / 逐小时
    now = qweather(host, key, "/v7/weather/now", {"location": lid})["now"]
    today = qweather(host, key, "/v7/weather/7d", {"location": lid})["daily"][0]
    hourly = qweather(host, key, "/v7/weather/24h", {"location": lid})["hourly"]

    # 3. 第一行:区名 + 温度区间 + 实况 + 风 + 湿度(单行)
    line1 = (f"📍{name} {today['tempMin']}~{today['tempMax']}℃ 现{now['temp']}℃{now['text']} "
             f"{now['windDir']}{now['windScale']}级 湿{now['humidity']}%")

    # 4. 第二行:降水提醒 +(生活指数)+(晚间明早预览),空格拼成单行
    rain_text, has_rain = scan_rain(hourly)
    parts = [rain_text]
    # 4.1 生活指数(失败则跳过)
    idx_word = fetch_index_word(host, key, lid, slot)
    if idx_word:
        parts.append(idx_word)
    # 4.2 晚间附加"明早"预览
    if slot == "evening":
        tomorrow_md = (now_bj + timedelta(days=1)).strftime("%m-%d")
        tm = tomorrow_morning_line(hourly, tomorrow_md)
        if tm:
            parts.append(tm)
    line2 = " ".join(parts)

    return line1, line2, has_rain


def detect_slot(hour: int) -> str:
    """按北京时间小时判定时段:早 / 午后 / 晚。"""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    return "evening"


def build_header(slot: str, now_bj: datetime) -> tuple[str, str]:
    """根据时段生成标题与日期行。返回 (title, date_line)。"""
    titles = {"morning": "🌅早间天气", "afternoon": "☀️午后天气", "evening": "🌙晚间天气"}
    weekday = WEEKDAYS[now_bj.weekday()]
    date_line = f"{now_bj.strftime('%m月%d日')} {weekday} {now_bj.strftime('%H:%M')}"
    return titles[slot], date_line


def build_tip(slot: str, any_rain: bool) -> str:
    """生成底部贴士:有雨优先提醒带伞,否则给时段问候语。"""
    if any_rain:
        return "🌂今日有雨,记得带伞"
    return {"morning": "☕注意早晚温差增减衣物",
            "afternoon": "🍵午后记得多补水",
            "evening": "🛌早点休息,留意明日"}[slot]


def main() -> int:
    """读环境变量 → 判定时段 → 逐城市抓详细天气 → 填模板字段 → 推送给自己。"""
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

    # 2. 计算北京时间与时段(GitHub Actions 跑在 UTC)
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    slot = detect_slot(now_bj.hour)
    print(f"北京时间 {now_bj:%Y-%m-%d %H:%M} · 时段={slot}")

    # 3. 逐城市抓取两行;填进 c1/c1r/c2/c2r 字段,单城失败降级
    cities = parse_cities(city_raw)
    fields, any_rain = {}, False
    for i, city in enumerate(cities):
        c, cr = f"c{i + 1}", f"c{i + 1}r"
        try:
            line1, line2, has_rain = build_city_lines(qhost, qkey, city, slot, now_bj)
            fields[c], fields[cr] = line1, line2
            any_rain = any_rain or has_rain
        except Exception as exc:
            fields[c], fields[cr] = f"📍{city} 获取失败", f"⚠️{exc}"
    # 3.1 城市数超过模板槽位时提示(当前模板 2 个城市)
    if len(cities) > 2:
        print(f"提示:模板目前只有 2 个城市槽位,多出的不会显示:{cities[2:]}")

    # 4. 组装模板字段
    title, date_line = build_header(slot, now_bj)
    data = {"title": title, "date": date_line, **fields, "tip": build_tip(slot, any_rain)}
    print("推送内容:\n" + "\n".join(f"{k}={v}" for k, v in data.items()))

    # 5. 换 token 并推送给自己
    token = get_access_token(appid, secret)
    result = send_template(token, openid, template_id, data)
    print("推送结果:", result)
    return 0 if result.get("errcode") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
