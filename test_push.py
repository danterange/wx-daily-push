"""微信测试号推送 —— 通道连通性测试脚本。

单独验证"拿 access_token → 发模板消息"这条链路是否打通。
所有敏感信息从环境变量读取,不写进代码:
    APPID / APPSECRET / TEMPLATE_ID / OPENID

运行方式(在终端里一次性带上环境变量):
    APPID=xxx APPSECRET=xxx TEMPLATE_ID=xxx OPENID=xxx python test_push.py

成功时微信会收到一条测试推送,终端打印 {'errcode': 0, 'errmsg': 'ok', ...}
"""

import os
import sys
import requests

def get_access_token(appid: str, secret: str) -> str:
    """用 appid/secret 换取 access_token。

    失败(errcode 非 0 或无 token 字段)时抛出 RuntimeError,
    把微信返回的原始内容带出来方便排查。
    """
    # 1. 调用微信凭证接口
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {"grant_type": "client_credential", "appid": appid, "secret": secret}
    resp = requests.get(url, params=params, timeout=10).json()

    # 2. 校验返回:正常应有 access_token,异常会带 errcode/errmsg
    token = resp.get("access_token")
    if not token:
        raise RuntimeError(f"拿 access_token 失败: {resp}")
    return token


def send_template(token: str, openid: str, template_id: str, data: dict) -> dict:
    """发送一条模板消息给指定 openid,返回微信的 JSON 响应。

    data 的字段名必须和你在测试号后台模板里写的 {{关键词.DATA}} 对应;
    多余字段会被忽略,缺失字段在消息里显示为空,不影响发送本身。
    """
    # 1. 拼接发送接口地址(access_token 走 query)
    url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}"

    # 2. 组装请求体:touser=收件人,template_id=模板,data=各字段值
    payload = {
        "touser": openid,
        "template_id": template_id,
        "data": {k: {"value": v} for k, v in data.items()},
    }

    # 3. POST 发送并返回结果
    return requests.post(url, json=payload, timeout=10).json()


def main() -> int:
    """读取环境变量 → 换 token → 发一条测试消息 → 打印结果。"""
    # 1. 读取并校验四个必填环境变量
    appid = os.environ.get("APPID")
    secret = os.environ.get("APPSECRET")
    template_id = os.environ.get("TEMPLATE_ID")
    openid = os.environ.get("OPENID")
    missing = [n for n, v in
               [("APPID", appid), ("APPSECRET", secret),
                ("TEMPLATE_ID", template_id), ("OPENID", openid)] if not v]
    if missing:
        print(f"缺少环境变量: {', '.join(missing)}")
        return 1

    # 2. 换取 access_token
    token = get_access_token(appid, secret)
    print("access_token 获取成功")

    # 3. 发送测试消息(字段名按需改成你模板里的关键词)
    data = {
        "city": "北京",
        "weather": "晴 26℃",
        "news": "这是一条通道测试消息",
    }
    result = send_template(token, openid, template_id, data)

    # 4. 打印结果:errcode 为 0 即成功,非 0 按 errmsg 排查
    print("发送结果:", result)
    return 0 if result.get("errcode") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
