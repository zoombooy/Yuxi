"""治理层时间归一化：把原文中的时间表达解析成双时态世界轴字段。

遵循 utopia ADR 0044 的边界划分：抽取模型只负责"摘录原文的时间表述"并标注
它是开始/结束/时点（time_role），本模块做确定性的解析与归一，不调用 LLM。

归一规则：
- 精度只到年/月时，存储的时间戳取该周期的起点（起点语义）或终点（终点语义），
  真实语义由 *_precision 列承载——"没量过"和"量到日"不能长得一样。
- role='end' 且原文说结束了但给不出日期：valid_to_precision='unknown'，
  与 valid_to IS NULL（仍在持续）严格区分。
- 解析失败返回 None，调用方保持字段为空——宁可没有时间，不可编造时间。
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, UTC
from typing import Any

TIME_ROLES = ("start", "end", "point")

_PRECISION_DAY = "day"
_PRECISION_MONTH = "month"
_PRECISION_YEAR = "year"
_PRECISION_UNKNOWN = "unknown"

# 2023年3月5日 / 2023-03-05 / 2023/3/5 / 2023.3.5
_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})\s*[年\-/.]\s*(?:(?P<month>\d{1,2})\s*[月\-/.]\s*)?(?:(?P<day>\d{1,2})\s*日?)?"
)

# 2020年到2023年 / 2020年至2023年 / 2020-2023（避免误匹配 2023-03-05 这类完整日期）
_RANGE_PATTERN = re.compile(r"(?P<from_year>\d{4})\s*(?:年\s*)?[至到~～—\-]{1,2}\s*(?P<to_year>\d{4})\s*年?")

_ONGOING_PATTERN = re.compile(r"至今|截至目前|到目前为止|目前|现在|迄今")


def _precision_of(year: int, month: int | None, day: int | None) -> str:
    if day is not None:
        return _PRECISION_DAY
    if month is not None:
        return _PRECISION_MONTH
    return _PRECISION_YEAR


def _period_start(year: int, month: int | None, day: int | None) -> datetime:
    month_value = month or 1
    day_value = day or 1
    return datetime(year, month_value, day_value, tzinfo=UTC)


def _period_end(year: int, month: int | None, day: int | None) -> datetime:
    month_value = month or 12
    if day is None:
        day_value = calendar.monthrange(year, month_value)[1]
    else:
        day_value = day
    return datetime(year, month_value, day_value, 23, 59, 59, tzinfo=UTC)


def _parse_date_match(match: re.Match) -> tuple[datetime, str] | None:
    year = int(match.group("year"))
    month = int(match.group("month")) if match.group("month") else None
    day = int(match.group("day")) if match.group("day") else None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and month is not None and day > calendar.monthrange(year, month)[1]:
        return None
    return _period_start(year, month, day), _precision_of(year, month, day)


def _resolve_role(raw_role: Any) -> str:
    role = str(raw_role or "").strip().lower()
    return role if role in TIME_ROLES else "point"


def normalize_time_expression(text: Any, role: Any = None) -> dict[str, Any] | None:
    """把原文时间表述归一成世界轴字段。

    返回 {
        valid_from, valid_to,               # aware datetime 或 None
        valid_from_precision, valid_to_precision,
        # 'day'/'month'/'year'；role='end' 且原文明确结束但无日期时
        # valid_to_precision='unknown'
    }；没有可解析的时间信息时返回 None。
    """
    if not isinstance(text, str):
        return None
    expression = text.strip()
    if not expression:
        return None
    resolved_role = _resolve_role(role)

    result: dict[str, Any] = {
        "valid_from": None,
        "valid_to": None,
        "valid_from_precision": None,
        "valid_to_precision": None,
    }

    range_match = _RANGE_PATTERN.search(expression)
    date_matches = list(_DATE_PATTERN.finditer(expression))

    if range_match:
        from_year = int(range_match.group("from_year"))
        to_year = int(range_match.group("to_year"))
        if from_year <= to_year:
            result["valid_from"] = _period_start(from_year, None, None)
            result["valid_from_precision"] = _PRECISION_YEAR
            result["valid_to"] = _period_end(to_year, None, None)
            result["valid_to_precision"] = _PRECISION_YEAR
            return result

    parsed: list[tuple[datetime, str]] = []
    for match in date_matches:
        parsed_date = _parse_date_match(match)
        if parsed_date is not None:
            parsed.append(parsed_date)

    ongoing = bool(_ONGOING_PATTERN.search(expression))

    if not parsed:
        if resolved_role == "end":
            # 「结束了，但原文没说哪天」：unknown 结束端。只有当表述确实
            # 带有完结语气才标记，否则宁可没有时间。
            completed_tone = bool(re.search(r"已(经)?|了|不再|免去|卸任|离职|离任|结束|终止|停止|退出", expression))
            if completed_tone or ongoing:
                result["valid_to_precision"] = _PRECISION_UNKNOWN
                return result
        return None

    if len(parsed) >= 2:
        # 表述里自带两个日期：按时间先后当作起止区间
        first, second = sorted(parsed, key=lambda item: item[0])
        result["valid_from"], result["valid_from_precision"] = first
        result["valid_to"], result["valid_to_precision"] = second
        return result

    moment, precision = parsed[0]
    if resolved_role == "end":
        end_year = moment.year
        end_month = moment.month if precision in (_PRECISION_MONTH, _PRECISION_DAY) else None
        end_day = moment.day if precision == _PRECISION_DAY else None
        result["valid_to"] = _period_end(end_year, end_month, end_day)
        result["valid_to_precision"] = precision
        return result

    # start / point：都落到起点端（point 表示"自此为真"）
    result["valid_from"] = moment
    result["valid_from_precision"] = precision
    if ongoing:
        result["valid_to"] = None
        result["valid_to_precision"] = None
    return result


def fact_holds_at(fact: dict[str, Any], as_of: datetime) -> bool:
    """判断一条已批准事实在 as_of 时刻是否为真（世界轴判定，纯函数）。

    - 认知轴：invalidated_at 非空且 <= as_of → 已被推翻，不成立；
    - 起点：valid_from 为空或 <= as_of；
    - 终点：valid_to 为空 → 仍持续；precision='unknown'（结束但不知何时）→
      无法证伪，按成立处理；否则 valid_to > as_of。
    """
    invalidated_at = fact.get("invalidated_at")
    if invalidated_at is not None and _aware(invalidated_at) <= as_of:
        return False

    valid_from = fact.get("valid_from")
    if valid_from is not None and _aware(valid_from) > as_of:
        return False

    valid_to = fact.get("valid_to")
    if valid_to is not None:
        if fact.get("valid_to_precision") != _PRECISION_UNKNOWN and _aware(valid_to) <= as_of:
            return False
    return True


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
