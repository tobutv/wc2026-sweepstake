#!/usr/bin/env python3
"""
FIFA World Cup 2026 Sweepstake — cloud data layer (GitHub Actions).

Self-contained: fetches finished + live matches from ESPN's public World Cup API
(no key), computes team standings (3/1/0 on the full-time score), sums them into
player totals, applies the live "as it stands" overlay, builds the story/fixtures/
results, and writes data.json. The static front-end (index.html/styles.css/app.js)
fetches data.json and renders itself.

ESPN is the source of truth (the local Excel workbook is not available in CI).
Standard library only.
"""

import json
import os
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# Output to repo root by default (where index.html / app.js live), override with OUT_DIR.
OUT_DIR = os.environ.get("OUT_DIR", os.path.abspath(os.path.join(HERE, "..")))
CONFIG = os.path.join(HERE, "config.json")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={}"

UK = timezone(timedelta(hours=1))  # BST during the tournament (Jun–Jul)

# Knockout bracket layout order (ESPN gameIds, top-to-bottom bracket-tree/leaf order). The forward
# feeders are NOT adjacent pairs (e.g. R16 M90 = winners of R32 #1 and #3), so to draw a tree where
# every match sits between its two feeders we order each round by this fixed sequence. Stable for
# this tournament; any id not listed sorts to the end.
BRACKET_ORDER = [
    # Round of 32 (ESPN bracket-page leaf order, top-to-bottom)
    760489, 760492, 760486, 760488, 760496, 760497, 760494, 760493,
    760487, 760490, 760491, 760495, 760500, 760499, 760498, 760501,
    # Round of 16
    760503, 760502, 760506, 760507, 760504, 760505, 760509, 760508,
    # Quarter-finals
    760510, 760511, 760512, 760513,
    # Semi-finals
    760514, 760515,
    # Final, then 3rd-place
    760517, 760516,
]
BRACKET_POS = {gid: i for i, gid in enumerate(BRACKET_ORDER)}

ALIASES = {
    "southkorea": "korearepublic", "korearepublic": "korearepublic",
    "unitedstates": "usa", "usa": "usa",
    "turkiye": "turkiye", "turkey": "turkiye",
    "cotedivoire": "ivorycoast", "ivorycoast": "ivorycoast",
    "caboverde": "capeverde", "capeverde": "capeverde",
    "drcongo": "congorepublic", "congodr": "congorepublic",
    "congorepublic": "congorepublic", "congo": "congorepublic",
    "iriran": "iran", "iran": "iran",
    "czechia": "czechia", "czechrepublic": "czechia",
}


def canon(s):
    if not s:
        return ""
    t = unicodedata.normalize("NFD", s)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = "".join(c for c in t if c.isalnum())
    return ALIASES.get(t, t)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sweepstake-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_iso(s):
    """Robust ISO-8601 parse for ESPN dates (handles trailing 'Z' and missing seconds)
    across Python versions. Returns an aware UTC-based datetime or None."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings?season=2026"


def fetch_eliminated(resolve):
    """Read ESPN's authoritative group standings and return a set of abbrs ESPN marks
    'Eliminated'. ESPN does the full qualification maths (incl. best-third), so we just
    trust its note. Returns empty set on any failure (fail-safe: nobody greyed)."""
    out = set()
    try:
        data = fetch(STANDINGS)
    except Exception as e:
        print("WARN standings fetch failed: %s" % e)
        return out
    for grp in data.get("children", []):
        for entry in grp.get("standings", {}).get("entries", []):
            note = (entry.get("note") or {}).get("description", "") or ""
            if "eliminat" in note.lower():
                ab = resolve(entry.get("team", {}).get("displayName", ""))
                if ab:
                    out.add(ab)
    return out


def text_on(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#0a1730" if lum > 150 else "#ffffff"


def heat_bg(val, mx, target_hex):
    f = 0.0 if mx <= 0 else float(val) / float(mx)
    f = max(0.0, min(1.0, f))
    base = (15, 37, 70)
    t = target_hex.lstrip("#")
    tr, tg, tb = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
    r = int(base[0] + (tr - base[0]) * f)
    g = int(base[1] + (tg - base[1]) * f)
    b = int(base[2] + (tb - base[2]) * f)
    return "#%02X%02X%02X" % (r, g, b)


def round_info(slug, name=""):
    """Map an ESPN knockout slug to (display-order, human label). Uses the slug ONLY — the
    `name` of a forward fixture contains its feeder labels (e.g. 'Round of 32 5 Winner'),
    which would otherwise misclassify the round."""
    s = (slug or "").lower()
    if "32" in s: return (1, "Round of 32")
    if "16" in s: return (2, "Round of 16")
    if "quarter" in s: return (3, "Quarter-finals")
    if "semi" in s: return (4, "Semi-finals")
    if "3rd" in s or "third" in s: return (5, "Third-place play-off")
    if "final" in s: return (6, "Final")
    return (9, (slug or "Knockout").replace("-", " ").title())


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    teams = cfg["teams"]
    players_cfg = cfg["players"]
    total_matches = cfg["totalMatches"]
    flair = cfg.get("flair", {})
    groups = cfg.get("groups", {})

    # canonical full-name -> abbr
    canon_to_abbr = {canon(meta["name"]): ab for ab, meta in teams.items()}

    def resolve(name):
        return canon_to_abbr.get(canon(name))

    # --- date window: tournament start .. today (UTC) ---
    start = datetime.strptime(cfg["tournamentStart"], "%Y%m%d").date()
    today = datetime.now(timezone.utc).date()
    days = []
    d = start
    while d <= today:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    finished = []   # {home,away,hs,as,ko(datetime utc)}
    live = []       # {home,away,hs,as,clock,detail}
    for ds in days:
        try:
            data = fetch(ESPN.format(ds))
        except Exception as e:
            print("WARN fetch %s failed: %s" % (ds, e))
            continue
        for e in data.get("events", []):
            comp = e["competitions"][0]
            cs = comp["competitors"]
            h = next((c for c in cs if c["homeAway"] == "home"), None)
            a = next((c for c in cs if c["homeAway"] == "away"), None)
            if not h or not a:
                continue
            hn, an = h["team"]["displayName"], a["team"]["displayName"]
            ko = parse_iso(e.get("date"))
            st = e["status"]["type"]
            state = st.get("state", "")
            if state == "in":
                live.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "clock": e["status"].get("displayClock", ""),
                             "detail": st.get("shortDetail", "")})
                continue
            if not (state == "post" and st.get("completed")):
                continue
            pen = None
            hsh, ash = h.get("shootoutScore"), a.get("shootoutScore")
            if hsh is not None or ash is not None:
                try:
                    if int(hsh or 0) > int(ash or 0): pen = hn
                    elif int(ash or 0) > int(hsh or 0): pen = an
                except Exception:
                    pass
            # manner: penalties (shootout present), extra time (period > 2 regulation, or AET text),
            # otherwise a 90-minute result. ESPN's `score` already counts ET goals and excludes the
            # shootout, so GF/GA are correct as-is (ET counts, pens don't).
            period = 0
            try:
                period = int(e["status"].get("period", 0) or 0)
            except Exception:
                pass
            dtxt = ("%s %s %s" % (st.get("detail", ""), st.get("shortDetail", ""),
                                  st.get("description", ""))).upper()
            if pen is not None:
                manner = "pen"
            elif period > 2 or "AET" in dtxt or "EXTRA" in dtxt:
                manner = "et"
            else:
                manner = "90"
            finished.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "ko": ko, "penWinner": pen, "manner": manner})

    print("ESPN finished=%d live=%d" % (len(finished), len(live)))

    # group membership (built first: drives group-vs-knockout scoring AND elimination)
    team_group = {}
    for g, abs_ in groups.items():
        for ab in abs_:
            team_group[ab] = g

    def is_ko_tie(ha, aa):
        """A knockout tie is any match between teams from different groups."""
        return bool(ha and aa and team_group.get(ha) and team_group.get(aa)
                    and team_group[ha] != team_group[aa])

    # --- team standings from finished matches ---
    # Group stage: 3 win / 1 draw / 0 loss. Knockouts: 3 win in 90, 2 win in ET or pens,
    # 1 loss in ET or pens, 0 loss in 90. GF/GA use ESPN's `score` everywhere (ET goals count,
    # shootout goals don't). Points are summed per match (a flat W*3+D can't express 3/2/1/0).
    stat = {ab: {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0} for ab in teams}
    team_matches = {ab: [] for ab in teams}   # per-team match log for "beyond the table" quirks
    for m in finished:
        ha, aa = resolve(m["home"]), resolve(m["away"])
        hs, as_ = m["hs"], m["as"]
        ko_tie = is_ko_tie(ha, aa)
        # winner: by score, else (knockouts only) by shootout
        if hs > as_:
            hwin = True
        elif as_ > hs:
            hwin = False
        elif ko_tie and m.get("penWinner"):
            hwin = (m["penWinner"] == m["home"])
        else:
            hwin = None                       # genuine draw (group stage)
        manner = m.get("manner", "90")
        if ko_tie and hwin is not None:
            if hwin:
                hp, ap = (3, 0) if manner == "90" else (2, 1)
            else:
                hp, ap = (0, 3) if manner == "90" else (1, 2)
        else:
            hp = 3 if hwin is True else (1 if hwin is None else 0)
            ap = 3 if hwin is False else (1 if hwin is None else 0)
        if ha in stat:
            s = stat[ha]; s["Pld"] += 1; s["GF"] += hs; s["GA"] += as_; s["Pts"] += hp
            if hwin is True: s["W"] += 1
            elif hwin is None: s["D"] += 1
            team_matches[ha].append({"gf": hs, "ga": as_, "opp": m["away"]})
        if aa in stat:
            s = stat[aa]; s["Pld"] += 1; s["GF"] += as_; s["GA"] += hs; s["Pts"] += ap
            if hwin is False: s["W"] += 1
            elif hwin is None: s["D"] += 1
            team_matches[aa].append({"gf": as_, "ga": hs, "opp": m["home"]})

    team_pts = {ab: s["Pts"] for ab, s in stat.items()}

    # --- eliminated teams ---
    # ESPN's standings *notes* proved unreliable in the knockouts (kept labelling cut third-placed
    # teams "Best 8 advance"). The bracket is the truth: once R32 is drawn, any group team not in a
    # knockout fixture is OUT (catches bottoms + cut thirds), plus anyone who loses a knockout tie.
    elim = fetch_eliminated(resolve)              # (1) pre-bracket fallback: ESPN group-stage notes
    # (2) bracket truth: teams appearing in any inter-group knockout fixture (scheduled OR finished)
    in_knockout = set()
    bracket_events = []                           # reused below to build the visual bracket
    try:
        br = fetch("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260628-20260720")
        bracket_events = br.get("events", [])
        for e in bracket_events:
            cs = e["competitions"][0]["competitors"]
            h = next((c for c in cs if c["homeAway"] == "home"), None)
            a = next((c for c in cs if c["homeAway"] == "away"), None)
            if not h or not a:
                continue
            hi, ai = resolve(h["team"]["displayName"]), resolve(a["team"]["displayName"])
            if is_ko_tie(hi, ai):
                in_knockout.add(hi); in_knockout.add(ai)
    except Exception as ex:
        print("WARN bracket fetch failed: %s" % ex)
    # only trust the bracket once it's substantially drawn (>=24 of 32), so we never wrongly grey
    # everyone during the group->R32 transition.
    if len(in_knockout) >= 24:
        for ab in teams:
            if ab not in in_knockout:
                elim.add(ab)
    # (3) knockout losers: any finished inter-group tie -> loser is out (incl. shootout defeats)
    for m in finished:
        ha, aa = resolve(m["home"]), resolve(m["away"])
        if not is_ko_tie(ha, aa):
            continue
        hs, as_ = m["hs"], m["as"]
        loser = None
        if hs > as_: loser = aa
        elif as_ > hs: loser = ha
        elif m.get("penWinner"): loser = aa if m["penWinner"] == m["home"] else ha
        if loser:
            elim.add(loser)
    print("eliminated (%d): %s" % (len(elim), ", ".join(sorted(elim)) if elim else "(none)"))

    # --- live overlay: provisional pts/goals from in-progress matches ---
    owner_of = {}
    for p in players_cfg:
        for ab in p["picks"]:
            owner_of[ab] = p["name"]

    players = []
    for p in players_cfg:
        agg = {"Pts": 0, "Pld": 0, "GF": 0, "GA": 0}
        for ab in p["picks"]:
            s = stat.get(ab)
            if s:
                agg["Pts"] += s["Pts"]; agg["Pld"] += s["Pld"]
                agg["GF"] += s["GF"]; agg["GA"] += s["GA"]
        players.append({"name": p["name"], "picks": list(p["picks"]),
                        "Pts": agg["Pts"], "Pld": agg["Pld"], "GF": agg["GF"], "GA": agg["GA"],
                        "LivePts": 0, "LivePld": 0, "LiveGF": 0, "LiveGA": 0, "LiveTeams": []})
    by_name = {p["name"]: p for p in players}

    live_cards = []
    for lm in live:
        ha, aa = resolve(lm["home"]), resolve(lm["away"])
        hs, as_ = lm["hs"], lm["as"]
        hp = 3 if hs > as_ else (1 if hs == as_ else 0)
        ap = 3 if as_ > hs else (1 if as_ == hs else 0)
        h_own = owner_of.get(ha); a_own = owner_of.get(aa)
        if h_own and ha:
            p = by_name[h_own]; p["LivePts"] += hp; p["LivePld"] += 1; p["LiveGF"] += hs; p["LiveGA"] += as_; p["LiveTeams"].append(ha)
        if a_own and aa:
            p = by_name[a_own]; p["LivePts"] += ap; p["LivePld"] += 1; p["LiveGF"] += as_; p["LiveGA"] += hs; p["LiveTeams"].append(aa)
        live_cards.append({"home": lm["home"], "away": lm["away"], "hs": hs, "as": as_,
                           "clock": lm["clock"], "detail": lm["detail"], "hOwn": h_own, "aOwn": a_own,
                           "ha": ha, "aa": aa})
    any_live = len(live) > 0

    # projected (as-it-stands) + sort
    for p in players:
        p["ProjPts"] = p["Pts"] + p["LivePts"]
        p["ProjPld"] = p["Pld"] + p["LivePld"]
        p["ProjGF"] = p["GF"] + p["LiveGF"]
        p["ProjGA"] = p["GA"] + p["LiveGA"]
        p["ProjGD"] = p["ProjGF"] - p["ProjGA"]
    ranked = sorted(players, key=lambda p: (-p["ProjPts"], -p["ProjGD"], -p["ProjGF"]))

    max_pts = max([p["ProjPts"] for p in ranked] + [1])
    max_gf = max([p["ProjGF"] for p in ranked] + [1])
    max_ga = max([p["ProjGA"] for p in ranked] + [1])

    def pick_obj(ab, live_teams):
        meta = teams.get(ab)
        tp = team_pts.get(ab, 0)
        is_live = ab in live_teams
        out_ = ab in elim
        if meta:
            return {"abbr": ab, "name": meta["name"], "iso": meta["iso"], "hex": meta["hex"],
                    "fg": text_on(meta["hex"]), "pts": tp, "live": is_live, "out": out_}
        return {"abbr": ab, "name": ab, "iso": None, "hex": None, "fg": None, "pts": tp, "live": is_live, "out": out_}

    standings = []
    for i, p in enumerate(ranked):
        rank = i + 1
        pts, gf, ga = p["ProjPts"], p["ProjGF"], p["ProjGA"]
        gd, pld = p["ProjGD"], p["ProjPld"]
        standings.append({
            "rank": rank, "name": p["name"], "isLeader": rank == 1,
            "pts": pts, "pld": pld, "gd": gd, "gf": gf, "ga": ga,
            "livePts": p["LivePts"], "livePld": p["LivePld"],
            "hasLive": (p["LivePts"] > 0 or len(p["LiveTeams"]) > 0), "delta": 0,
            "ptsBg": heat_bg(pts, max_pts, "#D4AF37"), "ptsFg": text_on(heat_bg(pts, max_pts, "#D4AF37")),
            "gfBg": heat_bg(gf, max_gf, "#63BE7B"), "gfFg": text_on(heat_bg(gf, max_gf, "#63BE7B")),
            "gaBg": heat_bg(ga, max_ga, "#F8696B"), "gaFg": text_on(heat_bg(ga, max_ga, "#F8696B")),
            "picks": [pick_obj(ab, p["LiveTeams"]) for ab in p["picks"]],
        })

    # --- match-object builder (results / live / fixtures) ---
    def match_obj(home, away, hwin, awin):
        hi, ai = teams.get(resolve(home) or ""), teams.get(resolve(away) or "")
        return {
            "home": home, "away": away,
            "homeAbbr": resolve(home) or home, "awayAbbr": resolve(away) or away,
            "homeIso": hi["iso"] if hi else None, "awayIso": ai["iso"] if ai else None,
            "hOwn": owner_of.get(resolve(home)), "aOwn": owner_of.get(resolve(away)),
            "hWin": hwin, "aWin": awin,
        }

    # --- recent results: finished sorted by kickoff desc, last 8 ---
    fin_sorted = sorted(finished, key=lambda m: m["ko"] or datetime(2000, 1, 1, tzinfo=timezone.utc), reverse=True)[:8]
    results = []
    for m in fin_sorted:
        hs, as_ = m["hs"], m["as"]
        hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
        aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
        o = match_obj(m["home"], m["away"], hw, aw)
        o["hs"] = hs; o["as"] = as_
        results.append(o)

    # --- live cards as match objects ---
    live_data = []
    for lc in live_cards:
        hs, as_ = lc["hs"], lc["as"]
        hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
        aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
        clk = lc["clock"] or lc["detail"] or "LIVE"
        o = match_obj(lc["home"], lc["away"], hw, aw)
        o["hs"] = hs; o["as"] = as_; o["clock"] = clk; o["detail"] = lc["detail"]
        live_data.append(o)

    # --- today's fixtures (BST kickoff times, player matchup) ---
    fixtures = []
    try:
        sb = fetch(ESPN.format(today.strftime("%Y%m%d")))
        rows = []
        for e in sb.get("events", []):
            comp = e["competitions"][0]
            cs = comp["competitors"]
            h = next((c for c in cs if c["homeAway"] == "home"), None)
            a = next((c for c in cs if c["homeAway"] == "away"), None)
            if not h or not a:
                continue
            ko = parse_iso(e.get("date"))
            ko = ko.astimezone(UK) if ko else datetime.now(UK)
            rows.append({"ko": ko, "home": h["team"]["displayName"], "away": a["team"]["displayName"],
                         "state": e["status"]["type"].get("state", ""),
                         "hs": h.get("score"), "as": a.get("score")})
        for m in sorted(rows, key=lambda r: r["ko"]):
            hw = aw = 0
            state = "ko"; hs = as_ = None
            if m["state"] == "post":
                hs, as_ = int(m["hs"]), int(m["as"]); state = "ft"
                hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
                aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
            elif m["state"] == "in":
                hs, as_ = int(m["hs"]), int(m["as"]); state = "live"
            o = match_obj(m["home"], m["away"], hw, aw)
            o["state"] = state; o["ko"] = m["ko"].strftime("%H:%M")
            if hs is not None:
                o["hs"] = hs; o["as"] = as_
            fixtures.append(o)
    except Exception as e:
        print("WARN fixtures: %s" % e)

    # --- knockout bracket (round columns): R32 (real ties) + forward rounds with placeholders ---
    # R32 comes from the elimination fetch (real teams). The forward rounds (R16..Final) come from a
    # second fetch; ESPN auto-advances qualified teams into their next slot and labels undecided slots
    # ("Round of 32 3 Winner"), which we render as "TBC" so it's clear who's through and who they meet.
    fwd_events = []
    try:
        fwd = fetch("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260704-20260719")
        fwd_events = fwd.get("events", [])
    except Exception as ex:
        print("WARN forward bracket fetch failed: %s" % ex)

    def bracket_side(comp):
        ab = resolve(comp["team"]["displayName"])
        if ab and ab in teams:
            return ab, teams[ab]["iso"], owner_of.get(ab)
        return "TBC", None, None

    brk = {}
    seen_ids = set()
    for e in list(bracket_events) + list(fwd_events):
        eid = str(e.get("id"))
        if eid in seen_ids:
            continue
        cs = e["competitions"][0]["competitors"]
        h = next((c for c in cs if c["homeAway"] == "home"), None)
        a = next((c for c in cs if c["homeAway"] == "away"), None)
        if not h or not a:
            continue
        order, rname = round_info(e.get("season", {}).get("slug", ""), e.get("name", ""))
        if order <= 1 and not is_ko_tie(resolve(h["team"]["displayName"]), resolve(a["team"]["displayName"])):
            continue  # R32: only real inter-group ties (skip any stray group match)
        seen_ids.add(eid)
        habbr, hiso, hown = bracket_side(h)
        aabbr, aiso, aown = bracket_side(a)
        st = e["status"]["type"]
        state = st.get("state", "")
        period = 0
        try:
            period = int(e["status"].get("period", 0) or 0)
        except Exception:
            pass
        hs = as_ = None
        manner = ""
        hwin = awin = 0
        if state in ("post", "in"):
            hs = int(h.get("score", 0) or 0); as_ = int(a.get("score", 0) or 0)
        if state == "post":
            pen = None
            hsh, ash = h.get("shootoutScore"), a.get("shootoutScore")
            if hsh is not None or ash is not None:
                try:
                    if int(hsh or 0) > int(ash or 0): pen = "h"
                    elif int(ash or 0) > int(hsh or 0): pen = "a"
                except Exception:
                    pass
            dtxt = ("%s %s %s" % (st.get("detail", ""), st.get("shortDetail", ""),
                                  st.get("description", ""))).upper()
            manner = "pen" if pen else ("et" if (period > 2 or "AET" in dtxt or "EXTRA" in dtxt) else "90")
            if hs > as_: hwin, awin = 1, -1
            elif as_ > hs: hwin, awin = -1, 1
            elif pen == "h": hwin, awin = 1, -1
            elif pen == "a": hwin, awin = -1, 1
        elif state == "in":
            if hs > as_: hwin, awin = 1, -1
            elif as_ > hs: hwin, awin = -1, 1
        state_code = "ft" if state == "post" else ("live" if state == "in" else "ko")
        ko = parse_iso(e.get("date"))
        ko_lbl = ""
        if ko:
            kt = ko.astimezone(UK)
            ko_lbl = "%d %s %02d:%02d" % (kt.day, kt.strftime("%b"), kt.hour, kt.minute)
        try:
            sort_id = int(eid)
        except Exception:
            sort_id = 0
        brk.setdefault(order, {"round": rname, "matches": []})
        brk[order]["matches"].append({
            "home": habbr, "away": aabbr, "homeAbbr": habbr, "awayAbbr": aabbr,
            "homeIso": hiso, "awayIso": aiso,
            "hs": hs, "as": as_, "state": state_code, "manner": manner,
            "hWin": hwin, "aWin": awin, "hOwn": hown, "aOwn": aown,
            "ko": ko_lbl, "_id": sort_id,
        })
    bracket = []
    for order in sorted(brk.keys()):
        col = brk[order]
        col["matches"].sort(key=lambda m: BRACKET_POS.get(m["_id"], 10000))
        for m in col["matches"]:
            m.pop("_id", None)
        bracket.append(col)

    # --- The Story So Far ---
    played = len(finished)
    story = build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches, flair)
    story += build_knockout_story(players_cfg, stat, teams, elim, bracket, flair)

    leader = ranked[0]
    stamp = datetime.now(UK).strftime("%d %b %Y %H:%M")
    og = ("%s leads on %d pts after %d/%d matches" % (leader["name"], leader["ProjPts"], played, total_matches)) \
        if played > 0 else "8 players, 48 teams, 104 matches. Kicks off 11 June."

    payload = {
        "meta": {"stamp": stamp, "playedCount": played, "totalMatches": total_matches,
                 "leaderName": leader["name"], "leaderPts": leader["ProjPts"], "ogDesc": og},
        "anyLive": any_live,
        "standings": standings,
        "live": live_data,
        "story": story,
        "fixtures": fixtures,
        "results": results,
        "bracket": bracket,
    }

    out = os.path.join(OUT_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("DATA written: %s (played=%d, leader=%s %d)" % (out, played, leader["name"], leader["ProjPts"]))


def _join_names(items):
    """Join names with Oxford-style 'and': [a]->a, [a,b]->'a and b', [a,b,c]->'a, b and c'."""
    items = ["<b>%s</b>" % x for x in items]
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return items[0] + " and " + items[1]
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_knockout_story(players_cfg, stat, teams, elim, bracket, flair):
    """Knockout-aware paragraphs appended to the story: who's surviving vs eliminated, the biggest
    casualties, who's already through, and the juicy bracket routes/grudge matches ahead. Returns
    0-2 paragraph strings. Fully deterministic (explicit tie-breaks) so the PS engine can mirror it
    byte-for-byte and the two never flip-flop data.json."""
    if not elim:
        return []

    def tn(ab):
        return teams.get(ab, {}).get("name", ab)

    def tnf(ab):
        fl = flair.get(ab, "")
        return ("%s (%s)" % (tn(ab), fl)) if fl else tn(ab)

    # survival count per player
    surv = []
    for p in players_cfg:
        alive = sum(1 for ab in p["picks"] if ab not in elim)
        surv.append((p["name"], alive))
    surv.sort(key=lambda s: (-s[1], s[0]))
    most_n = surv[0][1] if surv else 0
    leaders = sorted([n for (n, c) in surv if c == most_n and most_n > 0])
    wiped = sorted([n for (n, c) in surv if c == 0])

    # biggest casualty: eliminated team with most points (tie-break: abbr)
    elim_teams = sorted([ab for ab in stat if ab in elim], key=lambda ab: (-stat[ab]["Pts"], ab))
    casualty = elim_teams[0] if elim_teams else None

    # --- Paragraph A: the cull (survivors + biggest casualty) ---
    a = "<b>The cull.</b> %d team%s out" % (len(elim), " is" if len(elim) == 1 else "s are")
    if casualty:
        a += " &mdash; %s the marquee casualty" % tnf(casualty)
    a += ". "
    if leaders:
        a += "%s still %s the most skin in the game, %d of six alive. " % (
            _join_names(leaders), "have" if len(leaders) != 1 else "has", most_n)
    if wiped:
        a += "%s %s out cold, every last pick gone. " % (
            _join_names(wiped), "are" if len(wiped) != 1 else "is")

    # --- Paragraph B: the ties in the deepest round that's taken shape (each listed once) ---
    def side(ab, own):
        if ab == "TBC":
            return "TBC"
        return ("<b>%s</b>'s %s" % (own, tn(ab))) if own else tn(ab)

    live_round = None
    for col in bracket:
        if col["round"] == "Round of 32":
            continue
        if any(m["homeAbbr"] != "TBC" or m["awayAbbr"] != "TBC" for m in col["matches"]):
            live_round = col
    b = []
    if live_round:
        ties = []
        for m in live_round["matches"]:
            if m["homeAbbr"] == "TBC" and m["awayAbbr"] == "TBC":
                continue
            ties.append("%s v %s" % (side(m["homeAbbr"], m.get("hOwn")), side(m["awayAbbr"], m.get("aOwn"))))
        if ties:
            b.append("Next up, the %s: %s." % (live_round["round"].lower(), "; ".join(ties)))

    out = []
    if a.strip():
        out.append(a.strip())
    if b:
        out.append(" ".join(b))
    return out


def build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches, flair):
    """One tight, current scene-setter: the race at the top and the wooden spoon. The knockout
    detail (survivors, casualties, the ties taking shape) lives in build_knockout_story so the
    write-up stays short and stops rehashing stale group-stage stats. Deterministic."""
    if played == 0:
        return ["Not a ball kicked, not a point banked &mdash; just eight grown adults about to be "
                "exposed as the fools they are. Pull up a chair."]

    def nm(p):
        return "<b>%s</b>" % p["name"]

    lead = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    last = ranked[-1]
    lp = lead["ProjPts"]
    if second is None:
        top = "%s stands alone at the top on %d." % (nm(lead), lp)
    else:
        gap = lp - second["ProjPts"]
        if gap == 0:
            top = "%s and %s are locked together at the summit on %d, split only by the fine print." % (nm(lead), nm(second), lp)
        elif gap <= 3:
            top = "%s leads on %d, but %s is right on their shoulder &mdash; %d back and sharpening the knives." % (nm(lead), lp, nm(second), gap)
        else:
            top = "%s has bolted clear on %d, %d ahead of %s and out of sight." % (nm(lead), lp, gap, nm(second))
    tail = " Down at the bottom, %s is nailed to the foot of the table on %d." % (nm(last), last["ProjPts"])
    return [top + tail]


if __name__ == "__main__":
    main()
