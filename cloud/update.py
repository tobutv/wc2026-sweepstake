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


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    teams = cfg["teams"]
    players_cfg = cfg["players"]
    total_matches = cfg["totalMatches"]

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
            if st.get("name") != "STATUS_FULL_TIME":
                continue
            finished.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "ko": ko})

    print("ESPN finished=%d live=%d" % (len(finished), len(live)))

    # --- team standings from finished matches (3/1/0 on FT score) ---
    stat = {ab: {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0} for ab in teams}
    team_matches = {ab: [] for ab in teams}   # per-team match log for "beyond the table" quirks
    for m in finished:
        ha, aa = resolve(m["home"]), resolve(m["away"])
        hs, as_ = m["hs"], m["as"]
        if ha in stat:
            s = stat[ha]; s["Pld"] += 1; s["GF"] += hs; s["GA"] += as_
            if hs > as_: s["W"] += 1
            elif hs == as_: s["D"] += 1
            team_matches[ha].append({"gf": hs, "ga": as_, "opp": m["away"]})
        if aa in stat:
            s = stat[aa]; s["Pld"] += 1; s["GF"] += as_; s["GA"] += hs
            if as_ > hs: s["W"] += 1
            elif as_ == hs: s["D"] += 1
            team_matches[aa].append({"gf": as_, "ga": hs, "opp": m["home"]})
    for ab, s in stat.items():
        s["Pts"] = s["W"] * 3 + s["D"]

    team_pts = {ab: s["Pts"] for ab, s in stat.items()}

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
        if meta:
            return {"abbr": ab, "name": meta["name"], "iso": meta["iso"], "hex": meta["hex"],
                    "fg": text_on(meta["hex"]), "pts": tp, "live": is_live}
        return {"abbr": ab, "name": ab, "iso": None, "hex": None, "fg": None, "pts": tp, "live": is_live}

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
                         "state": e["status"]["type"].get("name", ""),
                         "hs": h.get("score"), "as": a.get("score")})
        for m in sorted(rows, key=lambda r: r["ko"]):
            hw = aw = 0
            state = "ko"; hs = as_ = None
            if m["state"] == "STATUS_FULL_TIME":
                hs, as_ = int(m["hs"]), int(m["as"]); state = "ft"
                hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
                aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
            elif "HALF" in m["state"] or m["state"] == "STATUS_IN_PROGRESS" or "FIRST" in m["state"] or "SECOND" in m["state"]:
                hs, as_ = int(m["hs"]), int(m["as"]); state = "live"
            o = match_obj(m["home"], m["away"], hw, aw)
            o["state"] = state; o["ko"] = m["ko"].strftime("%H:%M")
            if hs is not None:
                o["hs"] = hs; o["as"] = as_
            fixtures.append(o)
    except Exception as e:
        print("WARN fixtures: %s" % e)

    # --- The Story So Far ---
    played = len(finished)
    story = build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches)

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
    }

    out = os.path.join(OUT_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("DATA written: %s (played=%d, leader=%s %d)" % (out, played, leader["name"], leader["ProjPts"]))


def build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches):
    """One paragraph per participant, ordered first to last, each surfacing a distinct
    'beyond the table' quirk. Deterministic (no randomness) so it only changes when the
    underlying stats change, not on every refresh."""
    if played == 0:
        return ["Nothing's kicked off yet. Eight brave souls, 48 teams, and a frankly "
                "unhealthy amount of office trash talk waiting to be unleashed. Strap in."]
    n = len(ranked)
    used = set()
    bits = []
    for idx, rp in enumerate(ranked):
        cands = player_insights(rp, idx + 1, n, players_cfg, stat, teams, team_matches)
        cands.sort(key=lambda c: -c[0])
        chosen = next((c for c in cands if c[1] not in used), cands[0] if cands else None)
        if chosen:
            used.add(chosen[1])
            bits.append(chosen[2])
        else:
            bits.append("<b>%s</b> is quietly keeping their powder dry." % rp["name"])
    return bits


def tname(teams, ab):
    return teams.get(ab, {}).get("name", ab)


def player_insights(rp, rank, n, players_cfg, stat, teams, team_matches):
    """Return a list of (salience, type, html) candidate observations for one player."""
    name = rp["name"]
    picks = next(p["picks"] for p in players_cfg if p["name"] == name)
    pts = gf = ga = w = d = l = pld = 0
    contrib = {}
    yet = []
    played_teams = []
    zero_played = []
    best_match = None      # biggest win margin (gf-ga, gf, ga, ab)
    big_rout = None        # most goals in one match (gf, ga, ab)
    worst_def = None       # heaviest defeat (ga-gf, gf, ga, ab)
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

    pos = ("Top of the pile, " if rank == 1 else ("Propping it all up, " if rank == n else ""))
    nm = "<b>%s</b>" % name
    c = []

    # reliance — one nation carrying the campaign
    if pts > 0 and share >= 50 and contrib[best_ab] > 0:
        c.append((share + 20, "reliance",
                  "%s%s is essentially a one-nation portfolio: %s alone accounts for %d%% of their points. If that lot wobble, the whole thing topples." % (pos, nm, tname(teams, best_ab), share)))
    # perfect record
    if pld >= 2 and l == 0 and w >= 2 and d == 0:
        c.append((92, "perfect",
                  "%s%s hasn't put a foot wrong &mdash; all %d of their teams that have played have won. Insufferable, frankly." % (pos, nm, w)))
    # winless despite playing
    if pld >= 2 and w == 0:
        tail = ("just %d draw%s keeping them off the floor" % (d, "" if d == 1 else "s")) if d > 0 else "and nothing yet to show for it"
        c.append((88, "winless",
                  "%s%s is still chasing a first win &mdash; %d games played, not one victory, %s." % (pos, nm, pld, tail)))
    # games in hand
    if len(yet) >= 1:
        names = ", ".join(tname(teams, a) for a in yet)
        c.append((40 + 12 * len(yet), "inhand",
                  "%s%s still has %d of their six yet to kick a ball (%s) &mdash; whatever they've got, there's plenty more in the tank." % (pos, nm, len(yet), names)))
    # dead weight
    if zero_played:
        za = zero_played[0]
        c.append((62, "deadweight",
                  "%s%s is lugging dead weight: %s has played and banked precisely nothing." % (pos, nm, tname(teams, za))))
    # rout / biggest scoreline
    if big_rout and big_rout[0] >= 4:
        c.append((58 + big_rout[0], "rout",
                  "%s%s owns the biggest hammering so far &mdash; %s winning %d-%d." % (pos, nm, tname(teams, big_rout[2]), big_rout[0], big_rout[1])))
    # biggest win margin (if not already a rout)
    elif best_match and best_match[0] >= 2:
        c.append((50 + best_match[0], "bigwin",
                  "%s%s's standout result is %s's %d-%d demolition job." % (pos, nm, tname(teams, best_match[3]), best_match[1], best_match[2])))
    # goal machine
    if gf >= 6:
        c.append((40 + gf, "goals",
                  "%s%s's nations are the entertainers &mdash; %d goals between them, GD of %s%d. Box office." % (pos, nm, gf, ("+" if gd >= 0 else ""), gd)))
    # leaky defence
    if ga >= 7:
        leak = max(played_teams, key=lambda a: stat[a]["GA"]) if played_teams else None
        leaktxt = (" %s alone shipping %d." % (tname(teams, leak), stat[leak]["GA"])) if leak else ""
        c.append((38 + ga, "leaky",
                  "%s%s's defence is more of a suggestion &mdash; %d conceded already,%s" % (pos, nm, ga, leaktxt)))
    # clean sheets
    if clean >= 2:
        c.append((44 + 6 * clean, "cleansheets",
                  "%s%s has quietly kept %d clean sheets &mdash; winning ugly, but winning." % (pos, nm, clean)))
    # draw merchant
    if d >= 3:
        c.append((40 + 6 * d, "draws",
                  "%s%s is the draw specialist of the group: %d stalemates and counting. A point's a point." % (pos, nm, d)))
    # heavy defeat
    if worst_def and worst_def[0] >= 3:
        c.append((42 + worst_def[0], "thumped",
                  "%s%s took the heaviest hiding of their lot &mdash; %s done %d-%d." % (pos, nm, tname(teams, worst_def[3]), worst_def[1], worst_def[2])))
    # even spread (anti-reliance) — points well distributed
    scoring_teams = [a for a in played_teams if contrib[a] > 0]
    if pts >= 6 and len(scoring_teams) >= 4 and share <= 35:
        c.append((46, "spread",
                  "%s%s is the model of diversification &mdash; %d of their teams have chipped in points, no single passenger carrying the load." % (pos, nm, len(scoring_teams))))

    # always-available fallback so everyone gets a line
    c.append((5, "summary",
              "%s%s &mdash; %d pts from %d games (%d W, %d D, %d L), GD %s%d." % (
                  pos, nm, pts, pld, w, d, l, ("+" if gd >= 0 else ""), gd)))
    return c


if __name__ == "__main__":
    main()
