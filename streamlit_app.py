"""
Football Venue Form Predictor — Streamlit App
Powered by live-score-api.com

Run locally:  streamlit run streamlit_app.py
Deploy:       push to a repo, deploy on Streamlit Community Cloud,
              and set API_KEY / API_SECRET in the app's Secrets manager.
"""

import io
import time
from datetime import date, datetime

import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Venue Form Predictor", layout="wide")

BASE_URL = "https://livescore-api.com/api-client"
FETCH_LAST_N = 10

ALLOWED_COMPETITIONS = {
    1: "Bundesliga", 2: "Premier League", 3: "LaLiga Santander",
    5: "Ligue 1", 6: "Super Lig turkey",
    11: "Premier Division", 15: "Super League switzerland ", 16: "Premier League",
    35: "Ligue 1", 64: "Premier League",
    71: "First Professional League",
    82: "League 1", 83: "League 2", 93: "2nd Bundesliga",
    154: "National League",
    155: "National League South", 161: "National 1", 166: "3. Liga",
    189: "Premier League", 196: "Eredivisie",
    199: "Eerste Divisie", 338: "Challenge League switzerland ",
    447: "Tweede Divisie", 483: "First Amateur League", 75: "Scotland", 10: "iceland ",
    133: "autria ", 204: "1st divison norway", 207: "norway 2 ",
    314: "saudi 1", 345: "turkey 2",
}

SHOTS_COMPONENT_TYPES = ("shots_on_target", "shots_off_target", "shots_blocked")

# ─────────────────────────────────────────────────────────────
#  SIDEBAR — SECRETS / SETTINGS
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")

    api_key = st.secrets.get("API_KEY", "") if hasattr(st, "secrets") else ""
    api_secret = st.secrets.get("API_SECRET", "") if hasattr(st, "secrets") else ""

    if not api_key or not api_secret:
        st.warning("API credentials not found in secrets.toml — enter them below (session only, not stored).")
        api_key = st.text_input("API Key", type="password", value=api_key)
        api_secret = st.text_input("API Secret", type="password", value=api_secret)
    else:
        st.success("API credentials loaded from secrets.")

    target_date = st.date_input("Match date", value=date.today())
    max_calls_per_hour = st.number_input("Max API calls/hour", value=1800, min_value=60, step=60)
    shots_threshold = st.number_input("Combined shots threshold (2+ Goals pick)", value=30, min_value=1)

    run_button = st.button("Run Analysis", type="primary", use_container_width=True)

CALL_INTERVAL = 3600 / max_calls_per_hour

# ─────────────────────────────────────────────────────────────
#  API LAYER (cached — one fetch per team/match per session)
# ─────────────────────────────────────────────────────────────
def _api_get(endpoint: str, params: dict, key: str, secret: str) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    params = {**params, "key": key, "secret": secret}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After", 60))
                time.sleep(retry)
                continue
            r.raise_for_status()
            body = r.json()
            if not body.get("success"):
                raise ValueError(f"API success=false: {body}")
            return body
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"All retries failed for endpoint: {endpoint}")


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fixtures(target_date_str: str, key: str, secret: str) -> list:
    results = []
    page = 1
    while True:
        body = _api_get("fixtures/matches.json", {"date": target_date_str, "page": page}, key, secret)
        payload = body.get("data", {})
        fixtures = payload.get("fixtures", [])
        if not fixtures:
            break
        for f in fixtures:
            comp_id = int(f.get("competition_id") or (f.get("competition") or {}).get("id") or 0)
            if comp_id in ALLOWED_COMPETITIONS:
                results.append(f)
        if not payload.get("next_page"):
            break
        page += 1
        time.sleep(CALL_INTERVAL)
    return results


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_last_matches(team_id: int, key: str, secret: str) -> list:
    body = _api_get("teams/matches.json", {"team_id": team_id, "number": FETCH_LAST_N}, key, secret)
    return body.get("data", [])


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_match_stats(match_id, key: str, secret: str) -> list:
    if not match_id:
        return []
    try:
        body = _api_get("statistics/matches.json", {"match_id": match_id}, key, secret)
        return body.get("data", []) or []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
#  ANALYSIS LOGIC (unchanged from original script)
# ─────────────────────────────────────────────────────────────
def venue_form(matches: list, team_id: int, venue: str) -> list:
    tid = str(team_id)
    filtered = []
    for m in matches:
        comp_id = int(m.get("competition_id") or 0)
        if comp_id not in ALLOWED_COMPETITIONS:
            continue
        h = str(m.get("home_id", ""))
        a = str(m.get("away_id", ""))
        if venue == "home" and h == tid:
            filtered.append(m)
        elif venue == "away" and a == tid:
            filtered.append(m)
        if len(filtered) == 2:
            break
    return filtered


def parse_match(m: dict, venue: str):
    score = (m.get("ft_score") or m.get("score") or "").replace(" ", "")
    if not score or "-" not in score:
        return None
    try:
        hg, ag = map(int, score.split("-", 1))
    except ValueError:
        return None
    if venue == "home":
        gf, ga = hg, ag
    else:
        gf, ga = ag, hg
    result = "W" if gf > ga else ("D" if gf == ga else "L")
    return {"gf": gf, "ga": ga, "result": result}


def _sum_shots_from_stats_list(stats_list: list, is_home: bool):
    if not stats_list:
        return None
    side = "home" if is_home else "away"
    total = 0
    found_any = False
    for entry in stats_list:
        if entry.get("type") in SHOTS_COMPONENT_TYPES:
            val = entry.get(side)
            if val not in (None, ""):
                try:
                    total += int(val)
                    found_any = True
                except (TypeError, ValueError):
                    pass
    return total if found_any else None


def extract_shots(match: dict, team_id: int, key: str, secret: str):
    home_id = match.get("home_id")
    is_home = (str(home_id) == str(team_id))
    match_id = match.get("id") or match.get("match_id")
    stats_list = fetch_match_stats(match_id, key, secret)
    return _sum_shots_from_stats_list(stats_list, is_home)


def shots_pick(home_last_venue_match, away_last_venue_match, home_id, away_id, key, secret, threshold):
    if not home_last_venue_match or not away_last_venue_match:
        return "NO PICK"
    h_shots = extract_shots(home_last_venue_match, home_id, key, secret)
    a_shots = extract_shots(away_last_venue_match, away_id, key, secret)
    if h_shots is None or a_shots is None:
        return "NO PICK"
    return "2+ GOALS" if (h_shots + a_shots) >= threshold else "NO PICK"


def predict(home_games: list, away_games: list, home_name: str, away_name: str) -> dict:
    if len(home_games) != 2 or len(away_games) != 2:
        return {"win_pick": "NO PICK"}

    h_unbeaten = all(g["result"] in ("W", "D") for g in home_games)
    a_unbeaten = all(g["result"] in ("W", "D") for g in away_games)
    h_scored_2_plus = all(g["gf"] >= 2 for g in home_games)
    a_scored_2_plus = all(g["gf"] >= 2 for g in away_games)
    h_conceded_2_plus = all(g["ga"] >= 2 for g in home_games)
    a_conceded_2_plus = all(g["ga"] >= 2 for g in away_games)
    h_total_ga = sum(g["ga"] for g in home_games)
    a_total_ga = sum(g["ga"] for g in away_games)

    home_win_valid = (
        h_unbeaten and not a_unbeaten and
        h_scored_2_plus and a_conceded_2_plus and
        h_total_ga < a_total_ga
    )
    away_win_valid = (
        a_unbeaten and not h_unbeaten and
        a_scored_2_plus and h_conceded_2_plus and
        a_total_ga < h_total_ga
    )

    win_pick = "NO PICK"
    if home_win_valid and not away_win_valid:
        win_pick = home_name
    elif away_win_valid and not home_win_valid:
        win_pick = away_name

    return {"win_pick": win_pick}


def process_fixture(fix: dict, key: str, secret: str, threshold: int):
    home_id = int(fix.get("home_id") or fix.get("home", {}).get("id", 0))
    away_id = int(fix.get("away_id") or fix.get("away", {}).get("id", 0))
    home_name = fix.get("home_name") or fix.get("home", {}).get("name", "Unknown")
    away_name = fix.get("away_name") or fix.get("away", {}).get("name", "Unknown")
    comp_id = int(fix.get("competition_id") or (fix.get("competition") or {}).get("id") or 0)
    comp_name = ALLOWED_COMPETITIONS.get(comp_id, "?")

    if not home_id or not away_id:
        return None

    home_all = fetch_last_matches(home_id, key, secret)
    away_all = fetch_last_matches(away_id, key, secret)

    home_venue = venue_form(home_all, home_id, "home")
    away_venue = venue_form(away_all, away_id, "away")

    home_games = [g for g in (parse_match(m, "home") for m in home_venue) if g]
    away_games = [g for g in (parse_match(m, "away") for m in away_venue) if g]

    pred = predict(home_games, away_games, home_name, away_name)

    g2_pick = shots_pick(
        home_venue[0] if home_venue else None,
        away_venue[0] if away_venue else None,
        home_id, away_id, key, secret, threshold
    )

    if pred["win_pick"] == "NO PICK" and g2_pick == "NO PICK":
        return None

    return {
        "time": (fix.get("time") or "00:00")[:5],
        "home": home_name,
        "away": away_name,
        "competition": comp_name,
        "win_pick": pred["win_pick"],
        "goals2_pick": g2_pick,
    }


# ─────────────────────────────────────────────────────────────
#  PDF BUILDER — returns an in-memory buffer, not a disk file
# ─────────────────────────────────────────────────────────────
def build_pdf(target_date_str: str, win_rows: list, g2_rows: list, threshold: int) -> io.BytesIO:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_CENTER

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                             topMargin=15 * mm, bottomMargin=15 * mm)
    styles = getSampleStyleSheet()

    def mk(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    title_s = mk("T1", fontSize=18, textColor=colors.white, fontName="Helvetica-Bold", alignment=TA_CENTER)
    header_s = mk("H1", fontSize=12, textColor=colors.white, fontName="Helvetica-Bold", alignment=TA_CENTER)
    cell_s = mk("C1", fontSize=10, textColor=colors.black, fontName="Helvetica", alignment=TA_CENTER)
    pick_s = mk("P1", fontSize=10, textColor=colors.HexColor("#145c2e"), fontName="Helvetica-Bold", alignment=TA_CENTER)

    story = []

    banner = Table([[Paragraph(f"QUALIFIED PICKS — {target_date_str}", title_s)]], colWidths=[180 * mm])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#111827")),
                                 ("PADDING", (0, 0), (-1, -1), 12)]))
    story.append(banner)
    story.append(Spacer(1, 10 * mm))

    def render_section(title, bg_color, rows, pick_key):
        sec_title = Table([[Paragraph(title, header_s)]], colWidths=[180 * mm])
        sec_title.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg_color),
                                        ("PADDING", (0, 0), (-1, -1), 6)]))
        story.append(sec_title)

        if not rows:
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph("No matches qualified for this metric today.", cell_s))
            story.append(Spacer(1, 8 * mm))
            return

        table_data = [[
            Paragraph("<b>TIME</b>", cell_s),
            Paragraph("<b>LEAGUE</b>", cell_s),
            Paragraph("<b>MATCH</b>", cell_s),
            Paragraph("<b>PICK</b>", cell_s),
        ]]
        for r in rows:
            match_str = f"{r['home']} vs {r['away']}"
            table_data.append([
                Paragraph(r["time"], cell_s),
                Paragraph(r["competition"], cell_s),
                Paragraph(match_str, cell_s),
                Paragraph(r[pick_key], pick_s),
            ])

        t = Table(table_data, colWidths=[20 * mm, 45 * mm, 75 * mm, 40 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 10 * mm))

    render_section("WIN PICKS", colors.HexColor("#16a34a"), win_rows, "win_pick")
    render_section(f"2+ GOALS PICKS (combined shots >= {threshold})",
                    colors.HexColor("#d97706"), g2_rows, "goals2_pick")

    doc.build(story)
    buffer.seek(0)
    return buffer


# ─────────────────────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────────────────────
st.title("⚽ Football Venue Form Predictor")
st.caption("Data via livescore-api.com. WIN picks: unbeaten venue form + goal criteria. 2+ GOALS picks: combined shots over threshold.")

if run_button:
    if not api_key or not api_secret:
        st.error("Enter your API key and secret in the sidebar first.")
        st.stop()

    target_date_str = target_date.isoformat()

    with st.spinner("Fetching schedule..."):
        try:
            fixtures = fetch_fixtures(target_date_str, api_key, api_secret)
        except Exception as exc:
            st.error(f"Schedule fetch failed: {exc}")
            st.stop()

    if not fixtures:
        st.info(f"No matches in whitelisted leagues for {target_date_str}.")
        st.stop()

    fixtures.sort(key=lambda f: f.get("time") or "00:00:00")
    st.write(f"Found **{len(fixtures)}** matches in whitelisted leagues. Analyzing...")

    win_rows, g2_rows = [], []
    progress = st.progress(0.0)
    status = st.empty()

    for i, fix in enumerate(fixtures, 1):
        status.text(f"Processing {i}/{len(fixtures)}: "
                    f"{fix.get('home_name', '?')} vs {fix.get('away_name', '?')}")
        try:
            row = process_fixture(fix, api_key, api_secret, shots_threshold)
        except Exception as exc:
            row = None
            st.warning(f"Skipped a fixture due to error: {exc}")

        if row:
            if row["win_pick"] != "NO PICK":
                win_rows.append(row)
            if row["goals2_pick"] != "NO PICK":
                g2_rows.append(row)

        progress.progress(i / len(fixtures))

    status.empty()
    progress.empty()

    st.success(f"Done. Checked {len(fixtures)} matches.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("WIN Picks")
        if win_rows:
            st.dataframe(
                [{"Time": r["time"], "League": r["competition"],
                  "Match": f"{r['home']} vs {r['away']}", "Pick": r["win_pick"]} for r in win_rows],
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No matches qualified for this metric today.")

    with col2:
        st.subheader(f"2+ Goals Picks (shots ≥ {shots_threshold})")
        if g2_rows:
            st.dataframe(
                [{"Time": r["time"], "League": r["competition"],
                  "Match": f"{r['home']} vs {r['away']}", "Pick": r["goals2_pick"]} for r in g2_rows],
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No matches qualified for this metric today.")

    try:
        pdf_buffer = build_pdf(target_date_str, win_rows, g2_rows, shots_threshold)
        st.download_button(
            "Download PDF report",
            data=pdf_buffer,
            file_name=f"Qualified_Picks_{target_date_str}.pdf",
            mime="application/pdf",
        )
    except ImportError:
        st.warning("`reportlab` not installed — add it to requirements.txt to enable PDF export.")

else:
    st.info("Set your date and credentials in the sidebar, then click **Run Analysis**.")
