#!/usr/bin/env python3
"""rasp.rea.ru -> schedule.ics

Забирает расписание группы с rasp.rea.ru (эндпоинт /Schedule/ScheduleCard)
и собирает календарь iCalendar, на который можно подписаться с iPhone.

Зависимости:  pip install beautifulsoup4

Примеры:
  python rea_schedule_to_ics.py                       # вся осень, weekNum 1..22
  python rea_schedule_to_ics.py --from-week 6 --to-week 18 --alarm 30
  python rea_schedule_to_ics.py --from-file week.html # проверка на сохранённом HTML
"""
import argparse
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from bs4 import BeautifulSoup

BASE_URL = "https://rasp.rea.ru/Schedule/ScheduleCard"
DEFAULT_GROUP = "15.27д-би06/25м"
MSK = timezone(timedelta(hours=3))  # Москва: UTC+3 круглый год


# ---------- загрузка ----------

def fetch_week(group, week):
    query = urllib.parse.urlencode({"selection": group, "weekNum": week, "catfilter": 0})
    req = urllib.request.Request(
        f"{BASE_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 (personal schedule sync)",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://rasp.rea.ru/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- разбор ----------

def pretty_place(place):
    """'6 корпус - 336, пл. Основная' -> '6 корпус, ауд. 336 (пл. Основная)'."""
    m = re.match(r"(.+?корпус)\s*-\s*(\S+?)(?:,|\s|$)\s*(.*)$", place)
    if not m:
        return place
    corpus, room, site = m.groups()
    return f"{corpus}, ауд. {room}" + (f" ({site})" if site else "")


def parse_week(html):
    """Список занятий недели: dict(date, pair, start, end, title, kind, place, eid)."""
    soup = BeautifulSoup(html, "html.parser")
    lessons = []
    for table in soup.select("table"):
        head = table.select_one("th.dayh")
        if not head:
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", head.get_text())
        if not m:
            continue
        day = date(int(m[3]), int(m[2]), int(m[1]))

        for row in table.select("tr.slot"):
            link = row.select_one("a.task")
            if not link:  # пустая пара или «Занятия отсутствуют»
                continue
            first_cell = row.select_one("td").get_text(" ")
            pair = re.search(r"(\d+)\s*пара", first_cell)
            times = re.findall(r"\b(\d{1,2}):(\d{2})\b", first_cell)
            if not pair or len(times) < 2:
                continue

            strings = list(link.stripped_strings)
            kind_tag = link.find("i")
            kind = kind_tag.get_text(" ", strip=True) if kind_tag else ""
            rest = [s for s in strings[1:] if s != kind]
            place = " ".join(" ".join(rest).split()).replace(" ,", ",")

            lessons.append({
                "date": day,
                "pair": int(pair[1]),
                "start": dtime(int(times[0][0]), int(times[0][1])),
                "end": dtime(int(times[1][0]), int(times[1][1])),
                "title": strings[0],
                "kind": kind,
                "place": pretty_place(place),
                "eid": link.get("data-elementid", ""),
            })
    return lessons


def merge_adjacent(lessons):
    """Склеивает подряд идущие пары одного предмета (7+8 -> один слот 18:55-22:00)."""
    out = []
    for l in sorted(lessons, key=lambda x: (x["date"], x["pair"])):
        p = out[-1] if out else None
        if (p and p["date"] == l["date"] and p["last_pair"] + 1 == l["pair"]
                and (p["title"], p["kind"], p["place"]) == (l["title"], l["kind"], l["place"])):
            p["end"] = l["end"]
            p["last_pair"] = l["pair"]
        else:
            out.append({**l, "last_pair": l["pair"]})
    return out


# ---------- iCalendar ----------

def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """Перенос строк по RFC 5545: не больше 75 октетов, не режем многобайтные символы."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def utc(d, t):
    return datetime.combine(d, t, tzinfo=MSK).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(lessons, alarm_min=0):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//rasp.rea.ru personal sync//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Учёба РЭУ",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for l in lessons:
        first, last = l.get("first_pair", l["pair"]), l.get("last_pair", l["pair"])
        pairs = f"Пара {first}" if first == last else f"Пары {first}–{last}"
        summary = f"{l['title']} ({l['kind']})" if l["kind"] else l["title"]
        uid = f"{l['eid'] or l['pair']}-{l['date']:%Y%m%d}@rasp.rea.ru"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{utc(l['date'], l['start'])}",
            f"DTEND:{utc(l['date'], l['end'])}",
            f"SUMMARY:{esc(summary)}",
            f"LOCATION:{esc(l['place'])}",
            f"DESCRIPTION:{esc(pairs)}",
        ]
        if alarm_min:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{esc(l['title'])}",
                f"TRIGGER:-PT{alarm_min}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="rasp.rea.ru -> .ics")
    ap.add_argument("--group", default=DEFAULT_GROUP)
    ap.add_argument("--from-week", type=int, default=1)
    ap.add_argument("--to-week", type=int, default=22)
    ap.add_argument("--out", default="schedule.ics")
    ap.add_argument("--no-merge", action="store_true", help="не склеивать подряд идущие пары")
    ap.add_argument("--alarm", type=int, default=0, help="напоминание за N минут (0 = без)")
    ap.add_argument("--from-file", help="разобрать сохранённый HTML вместо похода на сайт")
    args = ap.parse_args()

    found = []
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            found = parse_week(f.read())
    else:
        for week in range(args.from_week, args.to_week + 1):
            try:
                found += parse_week(fetch_week(args.group, week))
            except Exception as e:  # сайт лёг / сменилась вёрстка — идём дальше
                print(f"неделя {week}: {e}", file=sys.stderr)
            time.sleep(1)  # не нагружаем сайт

    unique = {(l["date"], l["pair"]): l for l in found}.values()
    lessons = list(unique) if args.no_merge else merge_adjacent(unique)
    if not lessons:
        sys.exit("Не найдено ни одного занятия — .ics не перезаписан.")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(lessons, args.alarm))
    print(f"Готово: {len(lessons)} событий -> {args.out}")


if __name__ == "__main__":
    main()
