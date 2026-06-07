from __future__ import annotations

import copy
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template


app = Flask(__name__)


# ESPN's public soccer API is used as the primary live source for the 2026 World Cup.
# The fallback simulator below keeps the UI alive if a network, CORS, bot, or provider
# issue prevents fresh data from being fetched.
WC_LEAGUE = os.getenv("WC2026_LEAGUE", "fifa.world")
COMPETITION_LABEL = os.getenv("WC2026_COMPETITION_LABEL", "FIFA World Cup 2026")
SCOREBOARD_DATES = os.getenv("WC2026_SCOREBOARD_DATES", "20260611-20260719")
SCOREBOARD_URL = os.getenv(
    "WC2026_SCOREBOARD_URL",
    (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{WC_LEAGUE}/scoreboard?dates={SCOREBOARD_DATES}&limit=250"
    ),
)
SCOREBOARD_TODAY_URL = os.getenv(
    "WC2026_SCOREBOARD_TODAY_URL",
    f"https://site.api.espn.com/apis/site/v2/sports/soccer/{WC_LEAGUE}/scoreboard",
)
STANDINGS_URL = os.getenv(
    "WC2026_STANDINGS_URL",
    f"https://site.web.api.espn.com/apis/v2/sports/soccer/{WC_LEAGUE}/standings?level=3",
)
STANDINGS_HTML_URL = os.getenv(
    "WC2026_STANDINGS_HTML_URL",
    f"https://global.espn.com/football/standings/_/league/{WC_LEAGUE}",
)

REQUEST_TIMEOUT_SECONDS = int(os.getenv("WC2026_REQUEST_TIMEOUT", "14"))
SCOREBOARD_CACHE_TTL_SECONDS = int(os.getenv("WC2026_SCOREBOARD_CACHE_TTL", "15"))
STANDINGS_CACHE_TTL_SECONDS = int(os.getenv("WC2026_STANDINGS_CACHE_TTL", "300"))
MAX_DASHBOARD_MATCHES = int(os.getenv("WC2026_MAX_DASHBOARD_MATCHES", "18"))
FORCE_SIMULATOR = os.getenv("WC2026_FORCE_SIMULATOR", "0").strip() == "1"


SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,text/html;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.7",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "DNT": "1",
        "Origin": "https://www.espn.com",
        "Pragma": "no-cache",
        "Referer": "https://www.espn.com/soccer/",
        "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).lower())


def _parse_int(value: Any) -> Optional[int]:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace("%", ""))
    if not match:
        return None
    return int(round(float(match.group(0))))


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_date_local(value: Any) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return _clean_text(value)
    return parsed.astimezone().strftime("%b %d, %Y")


def _format_time_local(value: Any) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return ""
    return parsed.astimezone().strftime("%H:%M")


COUNTRY_CODE_ALIASES = {
    "algeria": "dz",
    "argentina": "ar",
    "australia": "au",
    "austria": "at",
    "belgium": "be",
    "bosnia and herzegovina": "ba",
    "bosnia-herzegovina": "ba",
    "brazil": "br",
    "canada": "ca",
    "cape verde": "cv",
    "colombia": "co",
    "croatia": "hr",
    "curacao": "cw",
    "curaçao": "cw",
    "czech republic": "cz",
    "czechia": "cz",
    "democratic republic of congo": "cd",
    "dr congo": "cd",
    "ecuador": "ec",
    "egypt": "eg",
    "england": "gb-eng",
    "france": "fr",
    "germany": "de",
    "ghana": "gh",
    "haiti": "ht",
    "iran": "ir",
    "iraq": "iq",
    "ivory coast": "ci",
    "cote d ivoire": "ci",
    "côte d ivoire": "ci",
    "japan": "jp",
    "jordan": "jo",
    "korea republic": "kr",
    "mexico": "mx",
    "morocco": "ma",
    "morroco": "ma",
    "netherlands": "nl",
    "new zealand": "nz",
    "norway": "no",
    "panama": "pa",
    "paraguay": "py",
    "portugal": "pt",
    "qatar": "qa",
    "saudi arabia": "sa",
    "scotland": "gb-sct",
    "senegal": "sn",
    "south africa": "za",
    "south korea": "kr",
    "spain": "es",
    "sweden": "se",
    "switzerland": "ch",
    "tunisia": "tn",
    "turkey": "tr",
    "turkiye": "tr",
    "türkiye": "tr",
    "united states": "us",
    "usa": "us",
    "uruguay": "uy",
    "uzbekistan": "uz",
}
COUNTRY_CODES = {_normalize_key(name): code for name, code in COUNTRY_CODE_ALIASES.items()}


GROUPS_2026: List[Tuple[str, List[str]]] = [
    ("Group A", ["Mexico", "South Africa", "South Korea", "Czechia"]),
    ("Group B", ["Canada", "Switzerland", "Qatar", "Bosnia and Herzegovina"]),
    ("Group C", ["Brazil", "Morocco", "Haiti", "Scotland"]),
    ("Group D", ["United States", "Paraguay", "Australia", "Turkiye"]),
    ("Group E", ["Germany", "Curacao", "Ivory Coast", "Ecuador"]),
    ("Group F", ["Netherlands", "Japan", "Tunisia", "Sweden"]),
    ("Group G", ["Belgium", "Egypt", "Iran", "New Zealand"]),
    ("Group H", ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"]),
    ("Group I", ["France", "Senegal", "Norway", "Iraq"]),
    ("Group J", ["Argentina", "Algeria", "Austria", "Jordan"]),
    ("Group K", ["Portugal", "Uzbekistan", "Colombia", "DR Congo"]),
    ("Group L", ["England", "Croatia", "Ghana", "Panama"]),
]
GROUP_BY_TEAM = {
    _normalize_key(team): group_name
    for group_name, teams in GROUPS_2026
    for team in teams
}

SCORER_POOL = {
    "argentina": ["Lionel Messi", "Julian Alvarez", "Lautaro Martinez"],
    "belgium": ["Kevin De Bruyne", "Romelu Lukaku", "Jeremy Doku"],
    "brazil": ["Endrick", "Vinicius Junior", "Rodrygo"],
    "egypt": ["Mohamed Salah", "Omar Marmoush", "Mostafa Mohamed"],
    "france": ["Kylian Mbappe", "Antoine Griezmann", "Ousmane Dembele"],
    "mexico": ["Santiago Gimenez", "Edson Alvarez", "Hirving Lozano"],
    "morocco": ["Achraf Hakimi", "Youssef En-Nesyri", "Hakim Ziyech"],
    "saudi arabia": ["Salem Al-Dawsari", "Firas Al-Buraikan", "Saleh Al-Shehri"],
    "south africa": ["Percy Tau", "Evidence Makgopa", "Themba Zwane"],
    "spain": ["Lamine Yamal", "Alvaro Morata", "Nico Williams"],
}


CACHE_LOCK = threading.Lock()
CACHE: Dict[str, Dict[str, Any]] = {
    "scoreboard": {"data": None, "expires_at": 0.0},
    "standings": {"data": None, "expires_at": 0.0},
}

SIM_LOCK = threading.Lock()
SIM_STATE: Dict[str, Any] = {
    "matches": [],
    "standings": [],
    "updated_at": _utc_now_iso(),
}


def _country_code(team_name: str) -> str:
    normalized = _normalize_key(team_name)
    if normalized in COUNTRY_CODES:
        return COUNTRY_CODES[normalized]

    # ESPN occasionally returns variants such as "Korea Republic" or "USA".
    for alias_key, code in COUNTRY_CODES.items():
        if alias_key and (alias_key in normalized or normalized in alias_key):
            return code
    return ""


def _flag_url(code: str, width: int = 40) -> str:
    return f"https://flagcdn.com/w{width}/{code.lower()}.png" if code else ""


def _team_payload(team_name: str) -> Dict[str, str]:
    code = _country_code(team_name)
    return {
        "name": team_name,
        "code": code,
        "flag": _flag_url(code),
        "flag_large": _flag_url(code, width=80),
    }


def _stat_row(team_name: str) -> Dict[str, Any]:
    team = _team_payload(team_name)
    return {
        "team": team["name"],
        "code": team["code"],
        "flag": team["flag"],
        "played": 0,
        "won": 0,
        "drawn": 0,
        "lost": 0,
        "goals_for": 0,
        "goals_against": 0,
        "goal_diff": 0,
        "points": 0,
    }


def _initial_world_cup_groups() -> List[Dict[str, Any]]:
    return [
        {
            "group": group_name,
            "teams": [_stat_row(team_name) for team_name in teams],
        }
        for group_name, teams in GROUPS_2026
    ]


def _group_shells(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_label: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        label = _clean_text(group.get("group"))
        if not label:
            continue
        suffix = label.upper().replace("GROUP", "").strip()
        key = f"GROUP {suffix}" if suffix else label.upper()
        by_label[key] = group

    ordered = []
    for idx in range(12):
        letter = chr(ord("A") + idx)
        key = f"GROUP {letter}"
        ordered.append(by_label.get(key, {"group": f"Group {letter}", "teams": []}))
    return ordered


def _get_json(url: str) -> Dict[str, Any]:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected JSON payload from {url}")
    return payload


def _get_html(url: str) -> str:
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def _load_with_cache(
    cache_key: str,
    ttl_seconds: int,
    loader: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    now = time.time()
    with CACHE_LOCK:
        cached = CACHE[cache_key]
        if cached["data"] is not None and now < cached["expires_at"]:
            return copy.deepcopy(cached["data"])

    data = loader()
    with CACHE_LOCK:
        CACHE[cache_key] = {
            "data": copy.deepcopy(data),
            "expires_at": now + ttl_seconds,
        }
    return data


def _extract_stat_value(stat_items: List[Dict[str, Any]], *needles: str) -> Optional[int]:
    target_keys = [_normalize_key(needle) for needle in needles]
    for item in stat_items or []:
        blob = " ".join(
            _clean_text(item.get(field))
            for field in ("name", "abbreviation", "displayName", "shortDisplayName")
        )
        blob_key = _normalize_key(blob)
        if not any(target in blob_key for target in target_keys):
            continue
        parsed = _parse_int(item.get("displayValue", item.get("value")))
        if parsed is not None:
            return parsed
    return None


def _team_display_name(competitor: Dict[str, Any]) -> str:
    team = competitor.get("team") or {}
    return _clean_text(
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or competitor.get("displayName")
        or competitor.get("name")
    )


def _competitor_id(competitor: Dict[str, Any]) -> str:
    team = competitor.get("team") or {}
    return _clean_text(competitor.get("id") or team.get("id") or team.get("uid"))


def _score_value(competitor: Dict[str, Any]) -> int:
    parsed = _parse_int(competitor.get("score"))
    return parsed if parsed is not None else 0


def _default_stats(seed: str, status_state: str) -> Dict[str, List[int]]:
    if status_state == "pre":
        return {
            "possession": [50, 50],
            "shots": [0, 0],
            "shots_on_target": [0, 0],
            "fouls": [0, 0],
        }

    rng = random.Random(_normalize_key(seed))
    home_possession = rng.randint(44, 57)
    away_possession = 100 - home_possession
    home_shots = rng.randint(4, 14)
    away_shots = rng.randint(4, 14)
    return {
        "possession": [home_possession, away_possession],
        "shots": [home_shots, away_shots],
        "shots_on_target": [
            rng.randint(1, max(1, min(home_shots, 7))),
            rng.randint(1, max(1, min(away_shots, 7))),
        ],
        "fouls": [rng.randint(4, 13), rng.randint(4, 13)],
    }


def _extract_match_stats(
    home_stats: List[Dict[str, Any]],
    away_stats: List[Dict[str, Any]],
    *,
    seed: str,
    status_state: str,
) -> Dict[str, List[int]]:
    home_possession = _extract_stat_value(home_stats, "possessionPct", "possession")
    away_possession = _extract_stat_value(away_stats, "possessionPct", "possession")

    if home_possession is None and away_possession is not None:
        home_possession = max(0, 100 - away_possession)
    if away_possession is None and home_possession is not None:
        away_possession = max(0, 100 - home_possession)

    stats = {
        "possession": [
            home_possession if home_possession is not None else 50,
            away_possession if away_possession is not None else 50,
        ],
        "shots": [
            _extract_stat_value(home_stats, "totalShots", "shots", "shot attempts") or 0,
            _extract_stat_value(away_stats, "totalShots", "shots", "shot attempts") or 0,
        ],
        "shots_on_target": [
            _extract_stat_value(home_stats, "shotsOnTarget", "shots on target", "sot", "sog") or 0,
            _extract_stat_value(away_stats, "shotsOnTarget", "shots on target", "sot", "sog") or 0,
        ],
        "fouls": [
            _extract_stat_value(home_stats, "foulsCommitted", "fouls", "fouls committed") or 0,
            _extract_stat_value(away_stats, "foulsCommitted", "fouls", "fouls committed") or 0,
        ],
    }

    has_real_numbers = any(sum(values) > 0 for key, values in stats.items() if key != "possession")
    return stats if has_real_numbers or status_state == "pre" else _default_stats(seed, status_state)


def _goal_payload(
    match_id: str,
    team_side: str,
    scorer: str,
    minute: int | str,
    index: int,
) -> Dict[str, Any]:
    minute_text = str(minute)
    if minute_text and not minute_text.endswith("'"):
        minute_text = f"{minute_text}'"
    return {
        "id": f"{match_id}-{team_side}-{_normalize_key(scorer)}-{minute_text}-{index}",
        "team": team_side,
        "scorer": scorer,
        "minute": minute_text,
        "text": f"{scorer} {minute_text}".strip(),
    }


def _parse_goal_details(
    competition: Dict[str, Any],
    home: Dict[str, Any],
    away: Dict[str, Any],
    match_id: str,
) -> List[Dict[str, Any]]:
    goals = []
    home_id = _competitor_id(home)
    away_id = _competitor_id(away)

    # ESPN puts goal/play events in "details" for matches that have live play-by-play.
    for index, detail in enumerate(competition.get("details") or []):
        if not isinstance(detail, dict):
            continue
        type_info = detail.get("type") or {}
        text_blob = " ".join(
            [
                _clean_text(type_info.get("text")),
                _clean_text(type_info.get("displayName")),
                _clean_text(detail.get("text")),
            ]
        )
        if not detail.get("scoringPlay") and "goal" not in text_blob.lower():
            continue

        detail_team = detail.get("team") or {}
        detail_team_id = _clean_text(detail.get("teamId") or detail_team.get("id"))
        team_side = "home" if detail_team_id == home_id else "away" if detail_team_id == away_id else ""

        athletes = detail.get("athletesInvolved") or detail.get("participants") or []
        scorer = ""
        if athletes and isinstance(athletes[0], dict):
            scorer = _clean_text(
                athletes[0].get("displayName")
                or athletes[0].get("shortName")
                or athletes[0].get("fullName")
            )
        if not scorer:
            scorer = _clean_text(detail.get("athlete", {}).get("displayName") if isinstance(detail.get("athlete"), dict) else "")
        if not scorer:
            scorer = "Goal"

        clock = detail.get("clock") if isinstance(detail.get("clock"), dict) else {}
        minute = _clean_text(clock.get("displayValue") or detail.get("displayClock") or detail.get("time"))
        minute = minute.replace("'", "") or str(index + 1)
        goals.append(_goal_payload(match_id, team_side or "home", scorer, minute, index))

    return goals


def _parse_scoreboard_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    competitions = event.get("competitions") or []
    if not competitions:
        return None

    competition = competitions[0]
    competitors = competition.get("competitors") or []
    if len(competitors) < 2:
        return None

    home = next((item for item in competitors if item.get("homeAway") == "home"), competitors[0])
    away = next((item for item in competitors if item.get("homeAway") == "away"), competitors[1])

    status = competition.get("status") or event.get("status") or {}
    status_type = status.get("type") or {}
    status_state = _clean_text(status_type.get("state") or "pre")
    status_text = _clean_text(
        status_type.get("shortDetail")
        or status_type.get("detail")
        or status_type.get("description")
        or status.get("displayClock")
        or "Scheduled"
    )
    minute_display = _clean_text(status.get("displayClock") if status_state == "in" else status_text)

    date_iso = _clean_text(event.get("date") or competition.get("date"))
    kickoff_time = _format_time_local(date_iso)
    display_date = _format_date_local(date_iso)
    home_name = _team_display_name(home)
    away_name = _team_display_name(away)
    home_score = _score_value(home)
    away_score = _score_value(away)
    match_id = _clean_text(event.get("id") or competition.get("id") or f"{home_name}-{away_name}-{date_iso}")

    if status_state == "pre":
        score_text = kickoff_time or "Scheduled"
    else:
        score_text = f"{home_score} - {away_score}"

    home_team = _team_payload(home_name)
    away_team = _team_payload(away_name)
    venue = competition.get("venue") or event.get("venue") or {}

    return {
        "id": match_id,
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "home_code": home_team["code"],
        "away_code": away_team["code"],
        "home_flag": home_team["flag"],
        "away_flag": away_team["flag"],
        "home_flag_large": home_team["flag_large"],
        "away_flag_large": away_team["flag_large"],
        "home_score": home_score,
        "away_score": away_score,
        "score_text": score_text,
        "status": status_text,
        "status_state": status_state,
        "minute_display": minute_display,
        "competition": COMPETITION_LABEL,
        "venue": _clean_text(
            venue.get("fullName")
            or venue.get("displayName")
            or venue.get("name")
            or venue.get("address", {}).get("city")
        ),
        "date": date_iso,
        "display_date": display_date,
        "kickoff_time": kickoff_time,
        "stats": _extract_match_stats(
            home.get("statistics") or [],
            away.get("statistics") or [],
            seed=f"{match_id}-{home_name}-{away_name}",
            status_state=status_state,
        ),
        "goals": _parse_goal_details(competition, home, away, match_id),
        "group": GROUP_BY_TEAM.get(_normalize_key(home_name), ""),
        "is_live": status_state == "in",
        "is_finished": status_state == "post",
        "is_scheduled": status_state == "pre",
        "source": "espn-json",
    }


def _parse_scoreboard_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    matches = []
    for event in payload.get("events") or []:
        if isinstance(event, dict):
            match = _parse_scoreboard_event(event)
            if match:
                matches.append(match)

    matches.sort(
        key=lambda item: (
            0 if item["is_live"] else 1 if item["is_scheduled"] else 2,
            item.get("date") or "",
            item.get("home_team") or "",
        )
    )
    return matches


def _select_dashboard_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    live = [match for match in matches if match.get("is_live")]
    today_matches = []
    for match in matches:
        parsed = _parse_datetime(match.get("date"))
        if parsed and parsed.astimezone(timezone.utc).date() == today:
            today_matches.append(match)

    selected = live + [match for match in today_matches if match not in live]
    if not selected:
        selected = matches[:MAX_DASHBOARD_MATCHES]
    return selected[:MAX_DASHBOARD_MATCHES]


def _fetch_scoreboard() -> Dict[str, Any]:
    errors = []
    for url in (SCOREBOARD_URL, SCOREBOARD_TODAY_URL):
        try:
            payload = _get_json(url)
            matches = _parse_scoreboard_payload(payload)
            if matches:
                return {
                    "updated_at": _utc_now_iso(),
                    "source_mode": "espn-live",
                    "message": "Live World Cup data loaded from ESPN.",
                    "live_matches": _select_dashboard_matches(matches),
                    "all_matches_count": len(matches),
                }
        except Exception as exc:
            errors.append(_clean_text(exc))

    raise ValueError("ESPN scoreboard failed or returned no matches. " + " | ".join(errors))


def _parse_standings_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    team = entry.get("team") or {}
    team_name = _clean_text(
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or entry.get("displayName")
        or entry.get("name")
    )
    stats = entry.get("stats") or []

    def stat(*names: str) -> int:
        parsed = _extract_stat_value(stats, *names)
        return parsed if parsed is not None else 0

    goals_for = stat("goalsFor", "gf", "goals for")
    goals_against = stat("goalsAgainst", "ga", "goals against")
    team_payload = _team_payload(team_name)
    return {
        "team": team_payload["name"],
        "code": team_payload["code"],
        "flag": team_payload["flag"],
        "played": stat("gamesPlayed", "played", "mp", "p"),
        "won": stat("wins", "won", "w"),
        "drawn": stat("ties", "draws", "drawn", "d"),
        "lost": stat("losses", "lost", "l"),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_diff": stat("goalDifferential", "gd", "goal diff") or goals_for - goals_against,
        "points": stat("points", "pts"),
    }


def _parse_json_standings(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups = []
    for index, child in enumerate(payload.get("children") or []):
        standings = child.get("standings") or {}
        entries = standings.get("entries") or child.get("entries") or []
        if not entries:
            continue

        label = _clean_text(
            child.get("name")
            or child.get("displayName")
            or child.get("abbreviation")
            or f"Group {chr(ord('A') + index)}"
        )
        if re.fullmatch(r"[A-L]", label.upper()):
            label = f"Group {label.upper()}"
        teams = [_parse_standings_entry(entry) for entry in entries if isinstance(entry, dict)]
        groups.append({"group": label.title() if label.lower().startswith("group") else label, "teams": teams})
    return _group_shells(_sort_group_tables(groups))


def _find_column_index(headers: List[str], *needles: str) -> Optional[int]:
    normalized_headers = [_normalize_key(header) for header in headers]
    for index, header in enumerate(normalized_headers):
        if any(_normalize_key(needle) in header for needle in needles):
            return index
    return None


def _parse_html_standings(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    groups = []

    # The backup scraper targets semantic standings tables: headers identify columns,
    # tbody rows carry team names and numeric table data.
    for table_index, table in enumerate(soup.select("table")):
        headers = [_clean_text(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
        rows = []
        for row in table.select("tbody tr"):
            cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th,td")]
            if cells:
                rows.append(cells)
        if not rows:
            continue

        team_idx = _find_column_index(headers, "team", "country", "nation")
        played_idx = _find_column_index(headers, "played", "mp", "p")
        won_idx = _find_column_index(headers, "won", "wins", "w")
        drawn_idx = _find_column_index(headers, "draw", "drawn", "d", "ties")
        lost_idx = _find_column_index(headers, "lost", "losses", "l")
        gf_idx = _find_column_index(headers, "gf", "goals for")
        ga_idx = _find_column_index(headers, "ga", "goals against")
        gd_idx = _find_column_index(headers, "gd", "goal diff")
        points_idx = _find_column_index(headers, "pts", "points")

        heading = table.find_previous(["h2", "h3", "h4"])
        label = _clean_text(heading.get_text(" ", strip=True)) if heading else f"Group {chr(ord('A') + table_index)}"
        teams = []
        for cells in rows:
            team_name = cells[team_idx] if team_idx is not None and team_idx < len(cells) else cells[0]
            if not team_name:
                continue
            team_payload = _team_payload(team_name)
            goals_for = _parse_int(cells[gf_idx]) if gf_idx is not None and gf_idx < len(cells) else 0
            goals_against = _parse_int(cells[ga_idx]) if ga_idx is not None and ga_idx < len(cells) else 0
            teams.append(
                {
                    "team": team_payload["name"],
                    "code": team_payload["code"],
                    "flag": team_payload["flag"],
                    "played": _parse_int(cells[played_idx]) if played_idx is not None and played_idx < len(cells) else 0,
                    "won": _parse_int(cells[won_idx]) if won_idx is not None and won_idx < len(cells) else 0,
                    "drawn": _parse_int(cells[drawn_idx]) if drawn_idx is not None and drawn_idx < len(cells) else 0,
                    "lost": _parse_int(cells[lost_idx]) if lost_idx is not None and lost_idx < len(cells) else 0,
                    "goals_for": goals_for or 0,
                    "goals_against": goals_against or 0,
                    "goal_diff": (
                        _parse_int(cells[gd_idx])
                        if gd_idx is not None and gd_idx < len(cells)
                        else (goals_for or 0) - (goals_against or 0)
                    ),
                    "points": _parse_int(cells[points_idx]) if points_idx is not None and points_idx < len(cells) else 0,
                }
            )

        if teams:
            groups.append({"group": label, "teams": teams})

    return _group_shells(_sort_group_tables(groups))


def _sort_group_tables(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sorted_groups = []
    for group in groups:
        teams = group.get("teams") or []
        teams.sort(
            key=lambda row: (
                -int(row.get("points", 0)),
                -int(row.get("goal_diff", 0)),
                -int(row.get("goals_for", 0)),
                row.get("team", ""),
            )
        )
        sorted_groups.append({"group": group.get("group", ""), "teams": teams})
    return sorted_groups


def _fetch_standings() -> Dict[str, Any]:
    errors = []

    try:
        payload = _get_json(STANDINGS_URL)
        groups = _parse_json_standings(payload)
        if any(group.get("teams") for group in groups):
            return {
                "updated_at": _utc_now_iso(),
                "source_mode": "espn-standings",
                "message": "World Cup standings loaded from ESPN.",
                "standings": groups,
            }
    except Exception as exc:
        errors.append(f"JSON standings: {_clean_text(exc)}")

    try:
        html = _get_html(STANDINGS_HTML_URL)
        groups = _parse_html_standings(html)
        if any(group.get("teams") for group in groups):
            return {
                "updated_at": _utc_now_iso(),
                "source_mode": "espn-html-standings",
                "message": "World Cup standings parsed from ESPN HTML tables.",
                "standings": groups,
            }
    except Exception as exc:
        errors.append(f"HTML standings: {_clean_text(exc)}")

    return {
        "updated_at": _utc_now_iso(),
        "source_mode": "verified-static-groups",
        "message": "Using the current 2026 World Cup group structure while live standings are unavailable.",
        "standings": _initial_world_cup_groups(),
        "errors": errors,
    }


def _apply_match_results_to_groups(
    groups: List[Dict[str, Any]],
    matches: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows_by_team: Dict[str, Dict[str, Any]] = {}
    updated_groups = copy.deepcopy(groups)
    for group in updated_groups:
        for row in group.get("teams") or []:
            rows_by_team[_normalize_key(row.get("team"))] = row

    for match in matches:
        if match.get("status_state") == "pre":
            continue
        home_key = _normalize_key(match.get("home_team"))
        away_key = _normalize_key(match.get("away_team"))
        home_row = rows_by_team.get(home_key)
        away_row = rows_by_team.get(away_key)
        if not home_row or not away_row:
            continue

        home_score = int(match.get("home_score") or 0)
        away_score = int(match.get("away_score") or 0)
        for row, goals_for, goals_against in (
            (home_row, home_score, away_score),
            (away_row, away_score, home_score),
        ):
            row["played"] = 1
            row["goals_for"] = goals_for
            row["goals_against"] = goals_against
            row["goal_diff"] = goals_for - goals_against
        if home_score > away_score:
            home_row["won"], home_row["points"] = 1, 3
            away_row["lost"] = 1
        elif away_score > home_score:
            away_row["won"], away_row["points"] = 1, 3
            home_row["lost"] = 1
        else:
            home_row["drawn"] = away_row["drawn"] = 1
            home_row["points"] = away_row["points"] = 1

    return _sort_group_tables(updated_groups)


def _public_match(match: Dict[str, Any]) -> Dict[str, Any]:
    public = copy.deepcopy(match)
    for key in list(public.keys()):
        if key.startswith("_"):
            public.pop(key, None)
    return public


def _sim_stats(possession: Tuple[int, int], shots: Tuple[int, int], target: Tuple[int, int], fouls: Tuple[int, int]) -> Dict[str, List[int]]:
    return {
        "possession": [possession[0], possession[1]],
        "shots": [shots[0], shots[1]],
        "shots_on_target": [target[0], target[1]],
        "fouls": [fouls[0], fouls[1]],
    }


def _make_sim_match(
    match_id: str,
    home: str,
    away: str,
    *,
    minute: int,
    score: Tuple[int, int],
    venue: str,
    stats: Dict[str, List[int]],
    goals: Optional[List[Tuple[str, str, int]]] = None,
    scheduled: bool = False,
    kickoff_offset_hours: int = 2,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=kickoff_offset_hours)
    home_team = _team_payload(home)
    away_team = _team_payload(away)
    status_state = "pre" if scheduled else "in"
    score_text = _format_time_local(kickoff.isoformat()) if scheduled else f"{score[0]} - {score[1]}"
    goal_events = []
    for index, goal in enumerate(goals or []):
        side, scorer, goal_minute = goal
        goal_events.append(_goal_payload(match_id, side, scorer, goal_minute, index))

    return {
        "id": match_id,
        "home_team": home,
        "away_team": away,
        "home_code": home_team["code"],
        "away_code": away_team["code"],
        "home_flag": home_team["flag"],
        "away_flag": away_team["flag"],
        "home_flag_large": home_team["flag_large"],
        "away_flag_large": away_team["flag_large"],
        "home_score": score[0],
        "away_score": score[1],
        "score_text": score_text,
        "status": "Scheduled" if scheduled else "Live",
        "status_state": status_state,
        "minute_display": "Scheduled" if scheduled else f"{minute}'",
        "competition": COMPETITION_LABEL,
        "venue": venue,
        "date": kickoff.isoformat() if scheduled else now.isoformat(),
        "display_date": _format_date_local(kickoff.isoformat() if scheduled else now.isoformat()),
        "kickoff_time": _format_time_local(kickoff.isoformat()),
        "stats": copy.deepcopy(stats),
        "goals": goal_events,
        "group": GROUP_BY_TEAM.get(_normalize_key(home), ""),
        "is_live": not scheduled,
        "is_finished": False,
        "is_scheduled": scheduled,
        "source": "simulator-fallback",
        "_minute": minute,
        "_ticks": 0,
        "_last_goal_tick": 0,
    }


def _init_simulator() -> None:
    with SIM_LOCK:
        if SIM_STATE["matches"]:
            return

        SIM_STATE["matches"] = [
            _make_sim_match(
                "sim-mex-rsa",
                "Mexico",
                "South Africa",
                minute=18,
                score=(0, 0),
                venue="Estadio Banorte",
                stats=_sim_stats((57, 43), (4, 2), (1, 0), (3, 4)),
            ),
            _make_sim_match(
                "sim-bra-mar",
                "Brazil",
                "Morocco",
                minute=52,
                score=(1, 0),
                venue="MetLife Stadium",
                stats=_sim_stats((55, 45), (12, 8), (5, 3), (7, 9)),
                goals=[("home", "Endrick", 52)],
            ),
            _make_sim_match(
                "sim-bel-egy",
                "Belgium",
                "Egypt",
                minute=37,
                score=(0, 1),
                venue="Mercedes-Benz Stadium",
                stats=_sim_stats((49, 51), (6, 7), (2, 3), (5, 4)),
                goals=[("away", "Mohamed Salah", 29)],
            ),
            _make_sim_match(
                "sim-esp-sau",
                "Spain",
                "Saudi Arabia",
                minute=64,
                score=(1, 1),
                venue="SoFi Stadium",
                stats=_sim_stats((61, 39), (13, 6), (4, 2), (8, 10)),
                goals=[("home", "Lamine Yamal", 22), ("away", "Salem Al-Dawsari", 58)],
            ),
            _make_sim_match(
                "sim-arg-alg",
                "Argentina",
                "Algeria",
                minute=0,
                score=(0, 0),
                venue="Hard Rock Stadium",
                stats=_sim_stats((50, 50), (0, 0), (0, 0), (0, 0)),
                scheduled=True,
                kickoff_offset_hours=4,
            ),
        ]
        SIM_STATE["standings"] = _apply_match_results_to_groups(
            _initial_world_cup_groups(),
            [_public_match(match) for match in SIM_STATE["matches"]],
        )
        SIM_STATE["updated_at"] = _utc_now_iso()


def _choose_scorer(team_name: str) -> str:
    key = _normalize_key(team_name)
    pool = SCORER_POOL.get(key) or [f"{team_name} scorer"]
    return random.choice(pool)


def _advance_live_match(match: Dict[str, Any]) -> None:
    if match.get("status_state") != "in":
        return

    match["_ticks"] = int(match.get("_ticks") or 0) + 1
    if match["_ticks"] % 2 == 0:
        match["_minute"] = min(90, int(match.get("_minute") or 0) + 1)

    minute = int(match.get("_minute") or 0)
    match["minute_display"] = "90+'" if minute >= 90 else f"{minute}'"
    match["status"] = "Live"

    stats = match["stats"]
    shift = random.choice([-1, 0, 1])
    home_possession = max(38, min(64, int(stats["possession"][0]) + shift))
    stats["possession"] = [home_possession, 100 - home_possession]

    attacking_side = 0 if random.random() < home_possession / 100 else 1
    if random.random() < 0.48:
        stats["shots"][attacking_side] += 1
        if random.random() < 0.36:
            stats["shots_on_target"][attacking_side] += 1
    if random.random() < 0.32:
        stats["fouls"][random.choice([0, 1])] += 1

    can_score = (
        minute < 90
        and (match["_ticks"] - int(match.get("_last_goal_tick") or 0)) >= 7
        and len(match.get("goals") or []) < 5
    )
    if can_score and random.random() < 0.08:
        side = "home" if random.random() < home_possession / 100 else "away"
        if side == "home":
            match["home_score"] += 1
            team_name = match["home_team"]
        else:
            match["away_score"] += 1
            team_name = match["away_team"]

        scorer = _choose_scorer(team_name)
        match["goals"].append(_goal_payload(match["id"], side, scorer, minute, len(match["goals"])))
        match["score_text"] = f"{match['home_score']} - {match['away_score']}"
        match["_last_goal_tick"] = match["_ticks"]

    if minute >= 90:
        match["status"] = "Full Time"
        match["status_state"] = "post"
        match["is_live"] = False
        match["is_finished"] = True
        match["is_scheduled"] = False


def _simulator_loop() -> None:
    _init_simulator()
    while True:
        time.sleep(5)
        with SIM_LOCK:
            for match in SIM_STATE["matches"]:
                _advance_live_match(match)
            SIM_STATE["standings"] = _apply_match_results_to_groups(
                _initial_world_cup_groups(),
                [_public_match(match) for match in SIM_STATE["matches"]],
            )
            SIM_STATE["updated_at"] = _utc_now_iso()


def _sim_dashboard(message: str) -> Dict[str, Any]:
    _init_simulator()
    with SIM_LOCK:
        matches = [_public_match(match) for match in SIM_STATE["matches"]]
        standings = copy.deepcopy(SIM_STATE["standings"])
        updated_at = SIM_STATE["updated_at"]

    return {
        "updated_at": updated_at,
        "source_mode": "simulator-fallback",
        "message": message,
        "live_matches": matches,
        "standings": standings,
        "source": {
            "scoreboard": SCOREBOARD_URL,
            "scoreboard_today": SCOREBOARD_TODAY_URL,
            "standings": STANDINGS_URL,
            "standings_html": STANDINGS_HTML_URL,
            "flags": "https://flagcdn.com/",
        },
    }


def _load_dashboard() -> Dict[str, Any]:
    if FORCE_SIMULATOR:
        return _sim_dashboard("Simulator mode is enabled with WC2026_FORCE_SIMULATOR=1.")

    try:
        scoreboard = _load_with_cache("scoreboard", SCOREBOARD_CACHE_TTL_SECONDS, _fetch_scoreboard)
    except Exception as exc:
        return _sim_dashboard(f"Live source unavailable, running resilient simulator fallback. {_clean_text(exc)}")

    standings = _load_with_cache("standings", STANDINGS_CACHE_TTL_SECONDS, _fetch_standings)
    standings_rows = standings.get("standings") or _initial_world_cup_groups()
    if standings.get("source_mode") == "verified-static-groups":
        standings_rows = _apply_match_results_to_groups(standings_rows, scoreboard.get("live_matches") or [])

    return {
        "updated_at": _utc_now_iso(),
        "source_mode": scoreboard.get("source_mode", "espn-live"),
        "message": scoreboard.get("message") or "World Cup dashboard refreshed.",
        "live_matches": scoreboard.get("live_matches") or [],
        "standings": standings_rows,
        "source": {
            "scoreboard": SCOREBOARD_URL,
            "scoreboard_today": SCOREBOARD_TODAY_URL,
            "standings": STANDINGS_URL,
            "standings_html": STANDINGS_HTML_URL,
            "flags": "https://flagcdn.com/",
        },
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/dashboard")
def api_dashboard():
    response = jsonify(_load_dashboard())
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/api/health")
def api_health():
    return jsonify(
        {
            "status": "ok",
            "timestamp": _utc_now_iso(),
            "league": WC_LEAGUE,
            "competition": COMPETITION_LABEL,
            "force_simulator": FORCE_SIMULATOR,
        }
    )


threading.Thread(target=_simulator_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
