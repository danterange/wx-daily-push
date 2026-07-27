import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import main as weather_push
from main import (
    RAIN_PROBABILITY_THRESHOLD,
    daily_risk_labels,
    future_risk_summary,
    hourly_risk_label,
    tomorrow_forecast,
)


def hourly(time: str, text: str = "晴", precip: str = "0", pop: str = "0") -> dict:
    return {"fxTime": time, "text": text, "precip": precip, "pop": pop}


class WeatherRiskTests(unittest.TestCase):
    def test_monitor_mode_does_not_send_when_all_cities_are_safe(self):
        environment = {
            "APPID": "appid",
            "APPSECRET": "secret",
            "TEMPLATE_ID": "template",
            "OPENID": "openid",
            "QWEATHER_KEY": "weather-key",
            "QWEATHER_HOST": "example.qweatherapi.com",
            "CITY": "北京",
            "RUN_MODE": "monitor",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("main.build_monitor_alert", return_value=None),
            patch("main.send_weather_message") as send_message,
        ):
            self.assertEqual(weather_push.main(), 0)

        send_message.assert_not_called()

    def test_clear_weather_does_not_trigger_an_alert(self):
        forecast = [
            hourly("2026-07-28T05:00+08:00", "晴", "0", "0"),
            hourly("2026-07-28T06:00+08:00", "阴", "0", str(RAIN_PROBABILITY_THRESHOLD - 1)),
        ]

        self.assertIsNone(future_risk_summary(forecast))

    def test_probability_threshold_triggers_possible_rain_alert(self):
        forecast = [hourly("2026-07-28T10:00+08:00", "多云", "0", "30")]

        self.assertEqual(hourly_risk_label(forecast[0]), "降水")
        self.assertEqual(
            future_risk_summary(forecast),
            "未来 24 小时可能有降水，最早 07-28 10:00，降水概率最高 30%",
        )

    def test_thunderstorm_triggers_even_without_precipitation_probability(self):
        forecast = [hourly("2026-07-28T18:00+08:00", "雷阵雨", "0", "0")]

        self.assertEqual(hourly_risk_label(forecast[0]), "雷阵雨")
        self.assertIn("雷阵雨", future_risk_summary(forecast))

    def test_tomorrow_summary_uses_date_not_list_position(self):
        now_bj = datetime(2026, 7, 27, 23, 3, tzinfo=timezone.utc)
        daily = [
            {"fxDate": "2026-07-27", "textDay": "晴", "textNight": "晴"},
            {"fxDate": "2026-07-28", "textDay": "多云", "textNight": "冰雹"},
        ]

        forecast = tomorrow_forecast(daily, now_bj)
        self.assertEqual(forecast["fxDate"], "2026-07-28")
        self.assertEqual(daily_risk_labels(forecast), ["冰雹"])


if __name__ == "__main__":
    unittest.main()
