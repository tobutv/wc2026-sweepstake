/* FIFA World Cup 2026 Sweepstake — presentation/render layer.
   Fetches data.json (the data layer, emitted by auto_update_sweepstake.ps1),
   renders the page, and re-fetches on a timer so the page updates without a
   full reload and without regenerating any markup server-side. */
(function () {
  'use strict';

  var POLL_MS = 60000;            // re-fetch data.json every 60s
  var DATA_URL = 'data.json';

  // ---- small helpers -------------------------------------------------------
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function flag(iso, size) {
    if (!iso) return '';
    size = size || 20;
    var h = Math.round(size * 0.75);
    return "<img class='flag' width='" + size + "' height='" + h +
      "' decoding='async' src='https://flagcdn.com/w40/" + iso + ".png'" +
      " srcset='https://flagcdn.com/w80/" + iso + ".png 2x' alt=''>";
  }

  // ---- league table --------------------------------------------------------
  function pickCell(p) {
    if (!p) return "<td class='pick'></td>";
    if (p.hex) {
      var cls = 'chip' + (p.out ? ' out' : '');
      var title = esc(p.name) + (p.out ? ' — eliminated' : '');
      return "<td class='pick' title='" + title + "'>" +
        "<span class='" + cls + "' style='background:" + p.hex + ";color:" + p.fg + "'>" +
        flag(p.iso, 18) + "<span>" + esc(p.abbr) + "</span></span></td>";
    }
    return "<td class='pick'>" + esc(p.abbr) + "</td>";
  }

  function breakdownRow(row) {
    var bd = '';
    (row.picks || []).forEach(function (p) {
      var liveMark = p.live ? "<i class='blive'>LIVE</i>" : '';
      var outMark = p.out ? "<i class='bout'>OUT</i>" : '';
      var cls = 'bchip' + (p.out ? ' out' : '');
      if (p.hex) {
        bd += "<span class='" + cls + "' style='background:" + p.hex + ";color:" + p.fg + "'>" +
          flag(p.iso, 14) + "<span>" + esc(p.name) + "</span><b>" + p.pts + "</b>" + liveMark + outMark + "</span>";
      } else {
        bd += "<span class='" + cls + "'>" + esc(p.abbr) + " <b>" + p.pts + "</b>" + liveMark + outMark + "</span>";
      }
    });
    return "<tr class='brow' id='brow-" + row.rank + "'><td colspan='13'>" +
      "<div class='bwrap'>" + bd + "</div></td></tr>";
  }

  function standingsRow(row) {
    var cls = row.isLeader ? ' leader' : '';
    var delta = row.delta > 0 ? " <span class='delta'>+" + row.delta + "</span>" : '';
    var liveDot = row.hasLive ? " <span class='ldot' title='Has a match live now'></span>" : '';
    var gdTxt = row.gd > 0 ? '+' + row.gd : '' + row.gd;
    var picks = '';
    for (var i = 0; i < 6; i++) { picks += pickCell((row.picks || [])[i]); }
    var ptsCls = row.livePts > 0 ? 'pts live' : 'pts';
    var pldCls = row.livePld > 0 ? " class='pldlive'" : '';
    return "<tr class='prow" + cls + "' data-row='" + row.rank + "'>" +
      "<td class='pos'>" + row.rank + "</td>" +
      "<td class='player'>" + esc(row.name) + liveDot + delta + "</td>" +
      picks +
      "<td class='" + ptsCls + "' style='background:" + row.ptsBg + ";color:" + row.ptsFg + "'>" + row.pts + "</td>" +
      "<td" + pldCls + ">" + row.pld + "</td>" +
      "<td>" + gdTxt + "</td>" +
      "<td style='background:" + row.gfBg + ";color:" + row.gfFg + "'>" + row.gf + "</td>" +
      "<td style='background:" + row.gaBg + ";color:" + row.gaFg + "'>" + row.ga + "</td>" +
      "</tr>";
  }

  function renderLeague(standings) {
    var html = '';
    (standings || []).forEach(function (row) {
      html += standingsRow(row);
      html += breakdownRow(row);
    });
    el('league-body').innerHTML = html;
  }

  // ---- match cards (shared by live / fixtures / results) -------------------
  function matchupLine(m) {
    if (!m.hOwn && !m.aOwn) return '';
    var hC = m.hWin === 1 ? 'won' : (m.hWin === -1 ? 'lost' : '');
    var aC = m.aWin === 1 ? 'won' : (m.aWin === -1 ? 'lost' : '');
    var hO = m.hOwn ? "<b class='" + hC + "'>" + esc(m.hOwn) + "</b>" : "<span class='noown'>&mdash;</span>";
    var aO = m.aOwn ? "<b class='" + aC + "'>" + esc(m.aOwn) + "</b>" : "<span class='noown'>&mdash;</span>";
    return hO + " <span class='vsep'>v</span> " + aO;
  }

  function matchCard(m, midHtml) {
    var hCls = m.hWin === 1 ? ' wn' : (m.hWin === -1 ? ' ls' : '');
    var aCls = m.aWin === 1 ? ' wn' : (m.aWin === -1 ? ' ls' : '');
    var hAb = m.homeAbbr || m.home, aAb = m.awayAbbr || m.away;
    var sub = matchupLine(m);
    var subHtml = sub ? "<div class='msub'>" + sub + "</div>" : '';
    return "<div class='mcard'><div class='mrow'>" +
      "<span class='mt home" + hCls + "'><span class='mab'>" + esc(hAb) + "</span>" + flag(m.homeIso, 20) + "</span>" +
      "<span class='mmid'>" + midHtml + "</span>" +
      "<span class='mt away" + aCls + "'>" + flag(m.awayIso, 20) + "<span class='mab'>" + esc(aAb) + "</span></span>" +
      "</div>" + subHtml + "</div>";
  }

  function renderLive(data) {
    var sec = el('live-section'), banner = el('live-banner');
    if (!data.anyLive || !(data.live && data.live.length)) {
      sec.hidden = true; banner.hidden = true; el('live-cards').innerHTML = ''; banner.innerHTML = '';
      return;
    }
    var n = data.live.length, word = n === 1 ? 'match' : 'matches';
    banner.hidden = false;
    banner.innerHTML = "<div class='livebanner'><span class='bdot'></span>" + n + ' ' + word +
      " live now &middot; table below shows points <b>as it stands</b></div>";
    var html = '';
    data.live.forEach(function (m) {
      var clk = m.clock || m.detail || 'LIVE';
      var mid = "<span class='mscore live'>" + m.hs + "&ndash;" + m.as + "</span>" +
        "<span class='mtag live'>" + esc(clk) + "</span>";
      html += matchCard(m, mid);
    });
    el('live-cards').innerHTML = html;
    sec.hidden = false;
  }

  function renderFixtures(fixtures) {
    var html = '';
    (fixtures || []).forEach(function (m) {
      var mid;
      if (m.state === 'ft') {
        mid = "<span class='mscore'>" + m.hs + "&ndash;" + m.as + "</span><span class='mtag'>FT</span>";
      } else if (m.state === 'live') {
        mid = "<span class='mscore live'>" + m.hs + "&ndash;" + m.as + "</span><span class='mtag live'>LIVE</span>";
      } else {
        mid = "<span class='mscore ko'>" + esc(m.ko) + "</span><span class='mtag'>BST</span>";
      }
      html += matchCard(m, mid);
    });
    if (!html) html = "<div class='mcard empty'>No matches scheduled today</div>";
    el('fixtures').innerHTML = html;
  }

  function renderResults(results) {
    var html = '';
    (results || []).forEach(function (m) {
      var mid = "<span class='mscore'>" + m.hs + "&ndash;" + m.as + "</span><span class='mtag'>FT</span>";
      html += matchCard(m, mid);
    });
    if (!html) html = "<div class='mcard empty'>No results yet &mdash; kicks off 11 June</div>";
    el('results').innerHTML = html;
  }

  // ---- knockout bracket (round columns) ------------------------------------
  function bracketMid(m) {
    if (m.state === 'ft') {
      var tag = m.manner === 'pen' ? 'PENS' : (m.manner === 'et' ? 'AET' : 'FT');
      return "<span class='mscore'>" + m.hs + "&ndash;" + m.as + "</span><span class='mtag'>" + tag + "</span>";
    }
    if (m.state === 'live') {
      return "<span class='mscore live'>" + m.hs + "&ndash;" + m.as + "</span><span class='mtag live'>LIVE</span>";
    }
    return "<span class='mscore ko'>" + esc(m.ko || 'TBD') + "</span><span class='mtag'>BST</span>";
  }

  function renderBracket(bracket) {
    var sec = el('bracket-section');
    if (!sec) return;
    if (!bracket || !bracket.length) { sec.hidden = true; el('bracket').innerHTML = ''; return; }
    var html = '';
    bracket.forEach(function (rd) {
      var cards = (rd.matches || []).map(function (m) { return matchCard(m, bracketMid(m)); }).join('');
      html += "<div class='brkcol'><h3 class='brkrd'>" + esc(rd.round) +
        "</h3>" + cards + "</div>";
    });
    el('bracket').innerHTML = html;
    sec.hidden = false;
  }

  function renderStory(story) {
    el('story').innerHTML = (story || []).map(function (p) { return "<p>" + p + "</p>"; }).join('');
  }

  function renderMeta(meta) {
    el('subline').innerHTML = "Live league table &middot; auto-refreshes &middot; <b>" +
      meta.playedCount + " of " + meta.totalMatches + "</b> matches played";
    el('foot').innerHTML = "Last updated: " + esc(meta.stamp) +
      " &middot; Scoring: 3 pts win / 1 draw / 0 loss, goal difference tiebreaker &middot; all 104 matches count<br>" +
      "Tap a player row for their team breakdown &middot; hover a team to see its full name.";
    if (meta.ogDesc) {
      var og = document.querySelector("meta[property='og:description']");
      if (og) og.setAttribute('content', meta.ogDesc);
    }
  }

  // ---- expand/collapse breakdown (event delegation, bound once) ------------
  function bindExpand() {
    el('league-body').addEventListener('click', function (e) {
      var row = e.target.closest('tr.prow');
      if (!row) return;
      var b = document.getElementById('brow-' + row.getAttribute('data-row'));
      if (b) b.classList.toggle('open');
    });
  }

  // ---- render + poll -------------------------------------------------------
  function render(data) {
    renderMeta(data.meta || {});
    renderLeague(data.standings);
    renderLive(data);
    renderStory(data.story);
    renderFixtures(data.fixtures);
    renderResults(data.results);
    renderBracket(data.bracket);
  }

  function load() {
    fetch(DATA_URL + '?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(render)
      .catch(function (err) {
        var sub = el('subline');
        if (sub) sub.textContent = 'Could not load live data (' + err.message + ') — retrying…';
      });
  }

  bindExpand();
  load();
  setInterval(load, POLL_MS);
})();
