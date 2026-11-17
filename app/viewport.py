"""冻结的采集视口配置。

视口在领取任务时生成快照，整条链路共用同一份；每一跳的收据都记录该快照，
因此“手机上多跳一层”这类差异可以和具体视口一起复核。配置不允许在采集过程
中修改。
"""
from __future__ import annotations

from dataclasses import dataclass

MOBILE_VIEWPORT = {
    "name": "mobile-390x844",
    "width": 390,
    "height": 844,
    "device_scale_factor": 3,
    "is_mobile": True,
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "reduced_motion": True,
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
}

_ALLOWED = frozenset(MOBILE_VIEWPORT)
_INT_FIELDS = {
    "width": (200, 4096),
    "height": (200, 4096),
}
_FLOAT_FIELDS = {"device_scale_factor": (1.0, 8.0)}


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    device_scale_factor: float
    is_mobile: bool
    locale: str
    timezone: str
    reduced_motion: bool
    user_agent: str

    def as_dict(self):
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "device_scale_factor": self.device_scale_factor,
            "is_mobile": self.is_mobile,
            "locale": self.locale,
            "timezone": self.timezone,
            "reduced_motion": self.reduced_motion,
            "user_agent": self.user_agent,
        }

    @property
    def request_headers(self):
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{self.locale},zh;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DPR": f"{self.device_scale_factor:g}",
            "Viewport-Width": str(self.width),
            "Sec-CH-UA-Mobile": "?1" if self.is_mobile else "?0",
        }


def validate_viewport(value):
    """校验并冻结视口配置；未给出的字段用移动视口默认值补齐。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("视口配置必须是对象")
    unknown = set(value) - _ALLOWED
    if unknown:
        raise ValueError(f"视口配置含未知字段: {sorted(unknown)[0]}")
    merged = {**MOBILE_VIEWPORT, **value}
    for key, (low, high) in _INT_FIELDS.items():
        item = merged[key]
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"视口字段 {key} 无效")
    for key, (low, high) in _FLOAT_FIELDS.items():
        item = merged[key]
        if type(item) not in (int, float) or isinstance(item, bool) or not low <= float(item) <= high:
            raise ValueError(f"视口字段 {key} 无效")
    for key in ("is_mobile", "reduced_motion"):
        if type(merged[key]) is not bool:
            raise ValueError(f"视口字段 {key} 无效")
    for key in ("name", "locale", "timezone", "user_agent"):
        if not isinstance(merged[key], str) or not merged[key].strip():
            raise ValueError(f"视口字段 {key} 无效")
        if any(ch in merged[key] for ch in "\r\n\x00"):
            raise ValueError(f"视口字段 {key} 含控制字符")
    return Viewport(
        name=merged["name"],
        width=merged["width"],
        height=merged["height"],
        device_scale_factor=float(merged["device_scale_factor"]),
        is_mobile=merged["is_mobile"],
        locale=merged["locale"],
        timezone=merged["timezone"],
        reduced_motion=merged["reduced_motion"],
        user_agent=merged["user_agent"],
    )
