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

    # --- Paragraph A: the cull ---
    a = "<b>The cull.</b> %d team%s gone home" % (len(elim), " has" if len(elim) == 1 else "s have")
    if casualty:
        a += ", the biggest scalp so far being %s" % tnf(casualty)
    a += ". "
    if leaders:
        a += "%s %s the most into the knockouts with %d of six still breathing. " % (
            _join_names(leaders), "carry" if len(leaders) != 1 else "carries", most_n)
    if wiped:
        a += "%s %s nothing left &mdash; every last pick eliminated, now reduced to %s. " % (
            _join_names(wiped), "have" if len(wiped) != 1 else "has",
            "spectators" if len(wiped) != 1 else "a spectator")

    # --- Paragraph B: routes & grudge matches ---
    advanced = {}  # abbr -> (depth, round, owner, opp_or_None)
    for depth, col in enumerate(bracket):
        if col["round"] == "Round of 32":
            continue
        for m in col["matches"]:
            for ab, own, opp in ((m["homeAbbr"], m.get("hOwn"), m["awayAbbr"]),
                                 (m["awayAbbr"], m.get("aOwn"), m["homeAbbr"])):
                if ab != "TBC" and ab in teams:
                    advanced[ab] = (depth, col["round"], own, None if opp == "TBC" else opp)
    adv_list = sorted(advanced.items(), key=lambda kv: (-kv[1][0], kv[0]))[:3]

    self_clash = None
    pvp = None
    for col in bracket:
        if col["round"] != "Round of 32":
            continue
        for m in col["matches"]:
            ho, ao = m.get("hOwn"), m.get("aOwn")
            ha, aa = m["homeAbbr"], m["awayAbbr"]
            if ho and ao and ha != "TBC" and aa != "TBC" and m.get("state") != "ft":
                if ho == ao and self_clash is None:
                    self_clash = (ho, ha, aa)
                elif ho != ao and pvp is None:
                    pvp = (ho, ha, ao, aa)

    b = []
    if adv_list:
        bits = []
        for ab, (depth, rnd, own, opp) in adv_list:
            who = ("<b>%s</b>'s " % own) if own else ""
            if opp:
                bits.append("%s%s have a date with %s" % (who, tn(ab), tn(opp)))
            else:
                bits.append("%s%s are through and waiting on an opponent" % (who, tn(ab)))
        b.append("Into the knockouts proper: " + "; ".join(bits) + ".")
    if self_clash:
        o, h, aw = self_clash
        b.append("Spare a thought for <b>%s</b>, who owns both %s and %s in the same R32 tie "
                 "&mdash; a guaranteed win and a guaranteed funeral." % (o, tn(h), tn(aw)))
    if pvp:
        o1, h, o2, aw = pvp
        b.append("<b>%s</b>'s %s versus <b>%s</b>'s %s is the pick of the all-in-the-family "
                 "R32 grudge matches." % (o1, tn(h), o2, tn(aw)))

    out = []
    if a.strip():
        out.append(a.strip())
    if b:
        out.append(" ".join(b))
    return out


def build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches, flair):
    """A funny, flowing 3-paragraph round-up. Each participant (first to last) gets a
    distinct 'beyond the table' quirk, stitched together with connective tissue.
    Deterministic so it only changes when the stats do, not on every refresh."""
    if played == 0:
        return ["Not a ball kicked, not a point banked &mdash; just eight grown adults who should know "
                "better, about to be exposed as the fools they are. Pull up a chair."]
    n = len(ranked)
    used = set()
    beats = []
    for idx, rp in enumerate(ranked):
        cands = player_insights(rp, idx + 1, n, players_cfg, stat, teams, team_matches, flair)
        cands.sort(key=lambda c: -c[0])
        chosen = next((c for c in cands if c[1] not in used), cands[0] if cands else None)
        if chosen:
            used.add(chosen[1])
            beats.append(chosen[2])
        else:
            beats.append("<b>%s</b> is doing absolutely nothing worth the keystrokes." % rp["name"])

    p1 = ["Lording it at the top, ", "Smugly in tow, ", "Rounding out the insufferable elite, "]
    p2 = ["Wallowing in mid-table sludge, ", "Equally forgettable, ", "Barely clinging to relevance, "]
    p3 = ["Down in the dregs, ", "And scraping the very bottom of the barrel, "]

    def para(slice_, leads):
        out = []
        for i, b in enumerate(slice_):
            out.append((leads[i] if i < len(leads) else "Then ") + b)
        return " ".join(out)

    paras = [para(beats[0:3], p1), para(beats[3:6], p2), para(beats[6:], p3)]
    return [p for p in paras if p]


def tname(teams, ab):
    return teams.get(ab, {}).get("name", ab)


def player_insights(rp, rank, n, players_cfg, stat, teams, team_matches, flair):
    """Return a list of (salience, type, html) candidate observations for one player.
    Texts are name-led and self-contained (paragraph assembly handles position)."""
    name = rp["name"]
    picks = next(p["picks"] for p in players_cfg if p["name"] == name)
    pts = gf = ga = w = d = l = pld = 0
    contrib = {}
    yet = []
    played_teams = []
    zero_played = []
    best_match = None
    big_rout = None
    worst_def = None
    clean = 0
    for ab in picks:
        s = stat.get(ab, {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0})
        pts += s["Pts"]; gf += s["GF"]; ga += s["GA"]; w += s["W"]; d += s["D"]; pld += s["Pld"]
        contrib[ab] = s["Pts"]
        if s["Pld"] == 0:
            yet.append(ab)
        else:
            played_teams.append(ab)
            if s["Pts"] == 0:
                zero_played.append(ab)
        for m in team_matches.get(ab, []):
            margin = m["gf"] - m["ga"]
            if m["ga"] == 0:
                clean += 1
            if m["gf"] > m["ga"] and (best_match is None or margin > best_match[0]):
                best_match = (margin, m["gf"], m["ga"], ab)
            if big_rout is None or m["gf"] > big_rout[0]:
                big_rout = (m["gf"], m["ga"], ab)
            if m["ga"] > m["gf"] and (worst_def is None or (m["ga"] - m["gf"]) > worst_def[0]):
                worst_def = (m["ga"] - m["gf"], m["gf"], m["ga"], ab)
    l = pld - w - d
    gd = gf - ga
    best_ab = max(contrib, key=lambda k: contrib[k]) if contrib else None
    share = round(100.0 * contrib[best_ab] / pts) if (best_ab and pts > 0) else 0
    sign = "+" if gd >= 0 else ""
    nm = "<b>%s</b>" % name

    def tnf(ab):  # team name + national cliché, e.g. "Germany (ruthlessly, tediously efficient)"
        base = tname(teams, ab)
        fl = flair.get(ab, "")
        return ("%s (%s)" % (base, fl)) if fl else base

    c = []

    if pts > 0 and share >= 50 and contrib[best_ab] > 0:
        c.append((share + 20, "reliance",
                  "%s is a one-team con artist &mdash; %s does %d%% of the work while the other five collect appearance money. Take it away and there's nothing left but excuses." % (nm, tnf(best_ab), share)))
    if pld >= 2 and l == 0 and w >= 2 and d == 0:
        c.append((92, "perfect",
                  "%s is winning and being a complete bellend about it &mdash; every team they own that's played has won, and we've all had to hear about every single one. Someone hide their phone." % nm))
    if pld >= 2 and w == 0:
        tail = ("just %d draw%s for company" % (d, "" if d == 1 else "s")) if d > 0 else "and nothing whatsoever to show for it"
        c.append((88, "winless",
                  "%s is still chasing a first win like it owes them money &mdash; %d games, zero wins, %s. Maybe pick a sport you understand." % (nm, pld, tail)))
    if len(yet) >= 1:
        names = ", ".join(tname(teams, a) for a in yet)
        c.append((40 + 12 * len(yet), "inhand",
                  "%s has %d team(s) yet to kick a ball (%s) &mdash; clinging to 'games in hand' like it's a personality. It won't save them." % (nm, len(yet), names)))
    if zero_played:
        c.append((62, "deadweight",
                  "%s saw something in %s that no scout, coach or sane human ever has &mdash; played, did nothing, and is somehow still the jewel of that abysmal squad." % (nm, tnf(zero_played[0]))))
    if big_rout and big_rout[0] >= 4:
        c.append((58 + big_rout[0], "rout",
                  "%s will be replaying %s's %d-%d all tournament because, let's be honest, it's the only good thing their team will ever do. Frame it." % (nm, tnf(big_rout[2]), big_rout[0], big_rout[1])))
    elif best_match and best_match[0] >= 2:
        c.append((50 + best_match[0], "bigwin",
                  "%s's solitary highlight is a %d-%d win for %s &mdash; screenshotted, set as wallpaper, shown to disinterested colleagues. It's all they've got." % (nm, best_match[1], best_match[2], tnf(best_match[3]))))
    if gf >= 6:
        topgf = max(picks, key=lambda a: stat.get(a, {}).get("GF", 0))
        c.append((40 + gf, "goals",
                  "%s's lot defend like the door's been left on the latch, but they'll at least outscore the misery &mdash; %d goals (GD %s%d), led by %s. Thrilling, doomed." % (nm, gf, sign, gd, tnf(topgf))))
    if ga >= 7:
        leak = max(played_teams, key=lambda a: stat[a]["GA"]) if played_teams else None
        leaktxt = (", %s leaking %d of them. Genuinely embarrassing to be associated with." % (tnf(leak), stat[leak]["GA"])) if leak else ". Genuinely embarrassing to be associated with."
        c.append((38 + ga, "leaky",
                  "%s's defence is a public health hazard &mdash; %d shipped%s" % (nm, ga, leaktxt)))
    if clean >= 2:
        c.append((44 + 6 * clean, "cleansheets",
                  "%s is boring everyone into an early grave &mdash; %d clean sheets, zero entertainment. The kind of football that makes people emigrate." % (nm, clean)))
    if d >= 3:
        c.append((40 + 6 * d, "draws",
                  "%s has turned the goalless draw into an art form nobody asked for &mdash; %d of them. A black hole where fun goes to die." % (nm, d)))
    if worst_def and worst_def[0] >= 3:
        c.append((42 + worst_def[0], "thumped",
                  "%s had to sit and watch %s get battered %d-%d &mdash; and we'll be bringing it up at every opportunity for the rest of their natural life." % (nm, tnf(worst_def[3]), worst_def[1], worst_def[2])))
    scoring_teams = [a for a in played_teams if contrib[a] > 0]
    if pts >= 6 and len(scoring_teams) >= 4 and share <= 35:
        c.append((46, "spread",
                  "%s spread their picks out of sheer cowardice &mdash; %d teams all contributing scraps, not one brave enough to actually be good. Death by committee." % (nm, len(scoring_teams))))

    c.append((5, "summary",
              "%s is just... there. %d pts, %dW %dD %dL. A complete non-entity &mdash; we genuinely forgot they were playing until just now." % (nm, pts, w, d, l)))
    return c


if __name__ == "__main__":
    main()
