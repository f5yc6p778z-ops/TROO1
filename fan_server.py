from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from bs4 import BeautifulSoup


KBO_BASE = "https://www.koreabaseball.com"
KBO_TEAMS = [
    {"id": "KT", "name": "KT 위즈", "short": "KT"},
    {"id": "SS", "name": "삼성 라이온즈", "short": "삼성"},
    {"id": "HT", "name": "KIA 타이거즈", "short": "KIA"},
    {"id": "LG", "name": "LG 트윈스", "short": "LG"},
    {"id": "OB", "name": "두산 베어스", "short": "두산"},
    {"id": "SK", "name": "SSG 랜더스", "short": "SSG"},
    {"id": "LT", "name": "롯데 자이언츠", "short": "롯데"},
    {"id": "NC", "name": "NC 다이노스", "short": "NC"},
    {"id": "HH", "name": "한화 이글스", "short": "한화"},
    {"id": "WO", "name": "키움 히어로즈", "short": "키움"},
]
TEAM_BY_ID = {team["id"]: team for team in KBO_TEAMS}
POSITION_TABLES = {"투수", "포수", "내야수", "외야수"}
CACHE: dict[str, tuple[float, object]] = {}
CACHE_LOCK = threading.Lock()


def cached(key: str, ttl: int, loader):
    now = time.monotonic()
    with CACHE_LOCK:
        item = CACHE.get(key)
        if item and item[0] > now:
            return item[1]
    value = loader()
    with CACHE_LOCK:
        CACHE[key] = (time.monotonic() + ttl, value)
    return value


def create_kbo_opener():
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def open_kbo(path: str, data: dict[str, str] | None = None, referer: str | None = None, opener=None) -> bytes:
    opener = opener or create_kbo_opener()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MyPlayerFanArchive/1.0)",
        "Referer": referer or KBO_BASE + "/",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": KBO_BASE,
        })
    request = urllib.request.Request(KBO_BASE + path, data=body, headers=headers)
    with opener.open(request, timeout=15) as response:
        return response.read()


def parse_player_id(href: str) -> str:
    match = re.search(r"[?&]playerId=(\d+)", href)
    return match.group(1) if match else ""


def search_roster(team_id: str, opener) -> list[dict]:
    path = "/Player/Search.aspx"
    page = BeautifulSoup(open_kbo(path, opener=opener), "html.parser")
    form = page.find("form", id="mainForm")
    if not form:
        return []
    team_select = next((select for select in form.select("select") if (select.get("name") or "").endswith("ddlTeam")), None)
    if not team_select:
        return []
    data = {
        field.get("name"): field.get("value", "")
        for field in form.select("input[type=hidden]")
        if field.get("name")
    }
    data[team_select["name"]] = team_id
    data["__EVENTTARGET"] = team_select["name"]
    data["__EVENTARGUMENT"] = ""
    page = BeautifulSoup(open_kbo(path, data, KBO_BASE + path, opener), "html.parser")
    players = {}
    page_numbers = set()
    for anchor in page.select(".paging a[id*='btnNo']"):
        match = re.search(r"btnNo(\d+)$", anchor.get("id", ""))
        if match:
            page_numbers.add(int(match.group(1)))
    page_numbers.discard(1)

    def add_rows(result_page):
        for row in result_page.select("table.tEx tr")[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 6:
                continue
            link = cells[1].find("a", href=True)
            if not link:
                continue
            player_id = parse_player_id(link["href"])
            if not player_id or player_id in players:
                continue
            birth = cells[4].get_text(" ", strip=True)
            size = cells[5].get_text(" ", strip=True)
            height_match = re.search(r"(\d+)\s*cm", size)
            position = cells[3].get_text(" ", strip=True)
            players[player_id] = {
                "id": player_id,
                "name": link.get_text(" ", strip=True),
                "number": "#" + cells[0].get_text(" ", strip=True),
                "position": position,
                "hand": "",
                "birth": birth if re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth) else "",
                "height": f"{height_match.group(1)} cm" if height_match else "",
                "origin": "KBO 공식 선수 검색",
                "debut": "",
                "profileUrl": urllib.parse.urljoin(KBO_BASE, link["href"]),
                "type": "pitcher" if position == "투수" else "hitter",
            }

    add_rows(page)
    for page_number in sorted(page_numbers):
        form = page.find("form", id="mainForm")
        if not form:
            break
        page_data = {
            field.get("name"): field.get("value", "")
            for field in form.select("input[type=hidden]")
            if field.get("name")
        }
        button = page.select_one(f".paging a[id$='btnNo{page_number}']")
        target_match = re.search(r"__doPostBack\('([^']+)'", button.get("href", "")) if button else None
        if not target_match:
            continue
        page_data["__EVENTTARGET"] = target_match.group(1)
        page_data["__EVENTARGUMENT"] = ""
        page = BeautifulSoup(open_kbo(path, page_data, KBO_BASE + path, opener), "html.parser")
        add_rows(page)
    return list(players.values())


def team_roster(team_id: str) -> dict:
    if team_id not in TEAM_BY_ID:
        raise ValueError("알 수 없는 KBO 구단입니다.")

    def load():
        path = "/Player/Register.aspx"
        opener = create_kbo_opener()
        html = BeautifulSoup(open_kbo(path, opener=opener), "html.parser")
        form = html.find("form", id="mainForm")
        if not form:
            raise RuntimeError("KBO 선수 명단 페이지 형식을 확인할 수 없습니다.")
        data = {
            field.get("name"): field.get("value", "")
            for field in form.select("input[type=hidden]")
            if field.get("name")
        }
        team_field = next((name for name in data if name.endswith("hfSearchTeam")), None)
        event_button = next((field.get("name") for field in form.select("input") if (field.get("id") or "").endswith("btnCalendarSelect")), None)
        if not team_field or not event_button:
            raise RuntimeError("KBO 선수 명단 팀 선택 항목을 찾을 수 없습니다.")
        data[team_field] = team_id
        data["__EVENTTARGET"] = event_button
        data["__EVENTARGUMENT"] = ""
        response = BeautifulSoup(open_kbo(path, data, KBO_BASE + path, opener), "html.parser")
        players = []
        for table in response.select("table.tNData"):
            rows = table.select("tr")
            if not rows:
                continue
            headings = rows[0].find_all(["th", "td"])
            if len(headings) < 2:
                continue
            position = headings[1].get_text(" ", strip=True)
            if position not in POSITION_TABLES:
                continue
            for row in rows[1:]:
                cells = row.find_all(["th", "td"])
                if len(cells) < 5:
                    continue
                name_link = cells[1].find("a", href=True)
                if not name_link:
                    continue
                player_id = parse_player_id(name_link["href"])
                if not player_id:
                    continue
                birth = cells[3].get_text(" ", strip=True)
                size = cells[4].get_text(" ", strip=True)
                height_match = re.search(r"(\d+)\s*cm", size)
                players.append({
                    "id": player_id,
                    "name": name_link.get_text(" ", strip=True),
                    "number": "#" + cells[0].get_text(" ", strip=True),
                    "position": position,
                    "hand": cells[2].get_text(" ", strip=True),
                    "birth": birth if re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth) else "",
                    "height": f"{height_match.group(1)} cm" if height_match else "",
                    "origin": "KBO 공식 선수 프로필",
                    "debut": "",
                    "profileUrl": urllib.parse.urljoin(KBO_BASE, name_link["href"]),
                    "type": "pitcher" if position == "투수" else "hitter",
                })
        source = "KBO 공식 선수 등록 명단"
        if not players:
            players = search_roster(team_id, opener)
            source = "KBO 공식 선수 검색 · 등록명단 대체"
        return {
            "team": TEAM_BY_ID[team_id],
            "players": players,
            "source": source,
            "fetchedAt": datetime.now().isoformat(timespec="minutes"),
        }

    return cached(f"roster:{team_id}", 3600, load)


def select_values(form) -> dict[str, str]:
    data = {
        field.get("name"): field.get("value", "")
        for field in form.select("input[type=hidden]")
        if field.get("name")
    }
    for select in form.select("select"):
        selected = select.select_one("option[selected]") or select.select_one("option")
        if select.get("name") and selected:
            data[select["name"]] = selected.get("value", "")
    return data


def player_stats(team_id: str, player_id: str, player_type: str, season: int) -> dict:
    if team_id not in TEAM_BY_ID or not re.fullmatch(r"\d{1,10}", player_id):
        raise ValueError("선수 조회 값이 올바르지 않습니다.")
    is_pitcher = player_type == "pitcher"
    detail_page = "PitcherDetail" if is_pitcher else "HitterDetail"
    path = f"/Record/Player/{detail_page}/Basic.aspx?playerId={player_id}"

    def load():
        profile = BeautifulSoup(open_kbo(path), "html.parser")
        tables = [
            table for table in profile.select("table")
            if any(key in (table.get("summary") or "") for key in ("타율", "평균자책점"))
            and "최근" not in (table.get("summary") or "")
        ][:2]
        if not tables:
            raise RuntimeError("KBO 공식 선수 프로필 기록을 찾을 수 없습니다.")
        values = {}
        for table in tables:
            rows = table.select("tr")
            if len(rows) < 2:
                continue
            headings = [cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])]
            data_row = [cell.get_text(" ", strip=True) for cell in rows[1].find_all(["th", "td"])]
            values.update(dict(zip(headings, data_row)))
        if is_pitcher:
            wanted = [("평균자책점", "ERA"), ("경기", "G"), ("승", "W"), ("세이브", "SV"), ("탈삼진", "SO"), ("이닝", "IP"), ("WHIP", "WHIP")]
        else:
            wanted = [("타율", "AVG"), ("경기", "G"), ("안타", "H"), ("홈런", "HR"), ("타점", "RBI"), ("출루율", "OBP"), ("OPS", "OPS")]
        filtered = [{"label": label, "value": values[key]} for label, key in wanted if values.get(key) not in {None, "", "-"}]
        if not filtered:
            return {"season": season, "values": [], "status": "no-data", "source": "KBO 공식 기록실"}
        return {
            "season": season,
            "values": filtered,
            "status": "ok",
            "source": "KBO 공식 선수 프로필",
            "fetchedAt": datetime.now().isoformat(timespec="minutes"),
        }

    return cached(f"stats:{team_id}:{player_id}:{player_type}:{season}", 900, load)


def parse_schedule_cell(value: str) -> BeautifulSoup:
    return BeautifulSoup(value or "", "html.parser")


def team_schedule(team_id: str, month: str) -> dict:
    if team_id not in TEAM_BY_ID or not re.fullmatch(r"\d{6}", month):
        raise ValueError("일정 조회 값이 올바르지 않습니다.")
    year, month_number = int(month[:4]), int(month[4:])
    if month_number < 1 or month_number > 12 or year < 2020 or year > datetime.now().year + 1:
        raise ValueError("조회 가능한 일정 범위를 벗어났습니다.")

    def load():
        # Establish the KBO session before calling its same-origin schedule service.
        opener = create_kbo_opener()
        open_kbo("/Schedule/Schedule.aspx", opener=opener)
        form = {
            "leId": "1",
            "srIdList": "0,9,6",
            "seasonId": str(year),
            "gameMonth": str(month_number).zfill(2),
            "teamId": team_id,
        }
        path = "/ws/Schedule.asmx/GetScheduleList"
        body = urllib.parse.urlencode(form).encode()
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MyPlayerFanArchive/1.0)",
            "Referer": KBO_BASE + "/Schedule/Schedule.aspx",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": KBO_BASE,
        }
        request = urllib.request.Request(KBO_BASE + path, data=body, headers=headers)
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
        games = []
        last_date = ""
        target = TEAM_BY_ID[team_id]["short"]
        for item in payload.get("rows", []):
            cells = item.get("row", [])
            day_cell = next((cell for cell in cells if cell.get("Class") == "day"), None)
            if day_cell and day_cell.get("Text"):
                match = re.search(r"(\d{2})\.(\d{2})", BeautifulSoup(day_cell["Text"], "html.parser").get_text(" ", strip=True))
                if match:
                    last_date = f"{year}-{match.group(1)}-{match.group(2)}"
            play_cell = next((cell for cell in cells if cell.get("Class") == "play"), None)
            if not play_cell:
                continue
            play = parse_schedule_cell(play_cell.get("Text", ""))
            teams = [span.get_text(" ", strip=True) for span in play.find_all("span", recursive=False)]
            if len(teams) < 2 or target not in teams:
                continue
            score_cell = play.find("em")
            scores = [span.get_text(" ", strip=True) for span in score_cell.find_all("span", recursive=False)] if score_cell else []
            result = [score for score in scores if score.isdigit()]
            note = cells[8].get("Text", "") if len(cells) > 8 else ""
            note_text = BeautifulSoup(note, "html.parser").get_text(" ", strip=True)
            if len(result) == 2:
                status = "경기 종료"
                score_text = f"{result[0]} : {result[1]}"
            elif re.search(r"취소|우천", note_text):
                status = "취소"
                score_text = ""
            else:
                status = "경기 예정"
                score_text = ""
            venue = BeautifulSoup(cells[7].get("Text", "") if len(cells) > 7 else "", "html.parser").get_text(" ", strip=True)
            time_text = BeautifulSoup(cells[1].get("Text", "") if len(cells) > 1 else "", "html.parser").get_text(" ", strip=True)
            games.append({
                "date": last_date,
                "time": time_text,
                "event": f"{teams[0]} vs {teams[-1]}",
                "venue": venue,
                "status": status,
                "score": score_text,
            })
        return {
            "team": TEAM_BY_ID[team_id]["name"],
            "month": month,
            "games": games,
            "source": "KBO 공식 경기 일정",
            "fetchedAt": datetime.now().isoformat(timespec="minutes"),
        }

    return cached(f"schedule:{team_id}:{month}", 900, load)


class FanArchiveHandler(SimpleHTTPRequestHandler):
    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/kbo/teams":
                return self.send_json(200, {"teams": KBO_TEAMS, "source": "KBO 공식 구단 목록"})
            if parsed.path == "/api/kbo/roster":
                team_id = query.get("team", [""])[0]
                return self.send_json(200, team_roster(team_id))
            if parsed.path == "/api/kbo/stats":
                team_id = query.get("team", [""])[0]
                player_id = query.get("playerId", [""])[0]
                player_type = query.get("type", [""])[0]
                season = int(query.get("season", [str(datetime.now().year)])[0])
                if player_type not in {"hitter", "pitcher"}:
                    raise ValueError("선수 유형이 올바르지 않습니다.")
                return self.send_json(200, player_stats(team_id, player_id, player_type, season))
            if parsed.path == "/api/kbo/schedule":
                team_id = query.get("team", [""])[0]
                month = query.get("month", [datetime.now().strftime("%Y%m")])[0]
                return self.send_json(200, team_schedule(team_id, month))
            if parsed.path.startswith("/api/"):
                return self.send_json(404, {"error": "요청한 API를 찾을 수 없습니다."})
            return super().do_GET()
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(502, {"error": "KBO 공식 자료를 불러오지 못했습니다.", "detail": str(error)})

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))


if __name__ == "__main__":
    address = ("0.0.0.0", int(os.environ.get("PORT", "8000")))
    print(f"Fan archive server listening at http://localhost:{address[1]}")
    ThreadingHTTPServer(address, FanArchiveHandler).serve_forever()