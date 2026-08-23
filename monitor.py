from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

API_URL = "https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation"
PREVIEW_URL = "https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/?l=zh-CN&appId=579"
STATE_PATH = Path(".data/state.json")
HISTORY_PATH = Path(".data/history.jsonl")

OPEN = {"g", "y"}
STATUS_CN = {"g": "充足", "y": "少量", "r": "已满", "x": "不开放"}

DEFAULT_OFFICE_NAMES = {
    "RHK": "湾仔",
    "RKO": "长沙湾",
    "RTK": "将军澳",
    "FTO": "火炭",
    "TMO": "屯门",
    "YLO": "元朗",
}


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必要环境变量: {name}")
    return value


def parse_status(value: Any) -> str:
    if value is None:
        return "x"
    s = str(value).strip().lower()
    if "quota-g" in s or s == "g":
        return "g"
    if "quota-y" in s or s == "y":
        return "y"
    if "quota-r" in s or s == "r":
        return "r"
    if "no-quota" in s or s in {"x", "", "none", "null"}:
        return "x"
    return "x"


def normalize_date(value: str) -> str:
    value = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"无法识别日期: {value!r}")


def office_names_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    names = dict(DEFAULT_OFFICE_NAMES)
    for office in payload.get("office", []) or []:
        if not isinstance(office, dict):
            continue
        oid = str(office.get("officeId") or office.get("id") or "").strip()
        if not oid:
            continue
        candidates = [
            office.get("district"),
            office.get("districtChs"),
            office.get("district_chs"),
            office.get("officeName"),
            office.get("name"),
        ]
        for c in candidates:
            if isinstance(c, str) and c.strip():
                names[oid] = c.strip()
                break
    return names


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("官方响应缺少 data[]")

    quota: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        office = str(row.get("officeId") or row.get("office") or "").strip()
        if not office:
            continue
        date = normalize_date(row.get("date"))
        quota.setdefault(office, {})[date] = {
            "R": parse_status(row.get("quotaR")),
            "K": parse_status(row.get("quotaK")),
        }

    if not quota:
        raise ValueError("官方响应解析后没有任何配额数据")

    return {
        "source_update_time": payload.get("lastUpdateTime"),
        "quota": quota,
        "office_names": office_names_from_payload(payload),
    }


def fetch_snapshot() -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HKIDQuotaMonitor/1.1)",
        "Referer": PREVIEW_URL,
        "Accept": "application/json,text/plain,*/*",
    }
    last_error = None

    for attempt in range(1, 4):
        params = {"svcId": "579", "t": str(int(time.time() * 1000))}
        try:
            r = requests.get(API_URL, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            snap = normalize(r.json())
            snap["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            return snap
        except Exception as e:
            last_error = e
            if attempt < 3:
                time.sleep(attempt * 3)

    raise RuntimeError(f"抓取香港入境处配额失败，已重试3次: {last_error}")


def load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(snap: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_history(events: list[dict[str, Any]], snap: dict[str, Any]) -> None:
    if not events:
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        for e in events:
            record = {
                **e,
                "source_update_time": snap.get("source_update_time"),
                "detected_at": snap.get("checked_at"),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def target_offices() -> set[str]:
    raw = os.getenv("OFFICES", "").strip()
    if not raw:
        return set(DEFAULT_OFFICE_NAMES)
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def target_date_range() -> tuple[str, str]:
    start = env_required("TARGET_START")
    end = env_required("TARGET_END")
    datetime.strptime(start, "%Y-%m-%d")
    datetime.strptime(end, "%Y-%m-%d")
    if start > end:
        raise RuntimeError("TARGET_START 不能晚于 TARGET_END")
    return start, end


def is_target_date(date: str) -> bool:
    start, end = target_date_range()
    return start <= date <= end


def find_open_events(
    old: dict[str, Any] | None,
    new: dict[str, Any],
) -> list[dict[str, Any]]:
    wanted = target_offices()
    events: list[dict[str, Any]] = []
    names = new.get("office_names", DEFAULT_OFFICE_NAMES)

    # 第一次只建基线，不把当前已经开放的格子当成“新放号”
    if not old:
        return events

    old_quota = old.get("quota", {})
    for office, by_date in new.get("quota", {}).items():
        if office not in wanted:
            continue
        old_by_date = old_quota.get(office, {})

        for date, cell in by_date.items():
            if not is_target_date(date):
                continue

            old_cell = old_by_date.get(date, {})
            for session in ("R", "K"):
                now_status = cell.get(session, "x")
                old_status = old_cell.get(session, "x")
                if now_status in OPEN and old_status not in OPEN:
                    events.append({
                        "office": office,
                        "office_name": names.get(office, office),
                        "date": date,
                        "session": session,
                        "from": old_status,
                        "to": now_status,
                    })

    events.sort(key=lambda x: (x["date"], x["office"], x["session"]))
    return events


def bark_push(title: str, body: str) -> None:
    key = env_required("BARK_KEY")
    jump_url = os.getenv("BOOKING_URL", PREVIEW_URL).strip() or PREVIEW_URL

    url = f"https://api.day.app/{quote(key, safe='')}/{quote(title, safe='')}/{quote(body, safe='')}"
    params = {
        "url": jump_url,
        "group": "HKID预约",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()


def build_notification(events: list[dict[str, Any]], snap: dict[str, Any]) -> tuple[str, str]:
    title = f"🔔 香港身份证目标日期有号 ×{len(events)}"
    lines = []
    for e in events[:8]:
        session_name = "一般时段" if e["session"] == "R" else "延长时段"
        lines.append(
            f'{e["date"]}｜{e["office_name"]}｜{session_name}｜{STATUS_CN.get(e["to"], e["to"])}'
        )
    if len(events) > 8:
        lines.append(f"另有 {len(events) - 8} 个变化，请立即查看官网。")
    if snap.get("source_update_time"):
        lines.append(f'官方更新时间：{snap["source_update_time"]}')
    lines.append("点击通知立即打开预约页面。")
    return title, "\n".join(lines)


def main() -> None:
    target_date_range()
    env_required("BARK_KEY")

    old = load_state()
    new = fetch_snapshot()

    if old and new.get("source_update_time") and (
        new.get("source_update_time") == old.get("source_update_time")
    ):
        print(f"NO_CHANGE source_update_time={new.get('source_update_time')}")
        return

    events = find_open_events(old, new)
    save_state(new)
    append_history(events, new)

    if events:
        title, body = build_notification(events, new)
        bark_push(title, body)
        print(f"ALERT events={len(events)} source={new.get('source_update_time')}")
    elif old is None:
        print(f"BASELINE_CREATED source={new.get('source_update_time')}")
    else:
        print(f"OK no_target_openings source={new.get('source_update_time')}")


if __name__ == "__main__":
    main()
