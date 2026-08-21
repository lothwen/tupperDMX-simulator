// ABOUTME: Web-remote client: WebSocket sync with the TupperDMX app, rAF-
// ABOUTME: interpolated clock, setlist/section/waveform rendering and commands.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // Internal song-part names are already English; this only expands the
  // abbreviated ones for display.
  var SECTION_NAMES = {
    INTRO: "INTRO", VERSE: "VERSE", CHORUS: "CHORUS", BREAK: "BREAK",
    BRIDGE: "BRIDGE", INST: "INSTRUMENTAL", SOLO: "SOLO", OUTRO: "OUTRO",
    END: "END"
  };

  var state = null;                    // last full state message
  var pos = { t: 0, wall: 0, playing: false };
  var beat = 0;
  var waveBins = null;                 // peaks for the current song
  var waveSong = -1;                   // setlist index the peaks belong to
  var waveDuration = 0;                // seconds of audio those peaks cover
  var ws = null;
  var reconnectDelay = 500;
  var seeking = false;                 // true while dragging the phone seek bar

  function paintSeek(f) {
    $("seekbar-fill").style.width = (f * 100) + "%";
    $("seekbar-knob").style.left = (f * 100) + "%";
  }

  // ---- helpers -------------------------------------------------------------

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function fmtDur(t) {
    if (t == null || !isFinite(t)) return "--:--";
    t = Math.max(0, Math.round(t));
    return Math.floor(t / 60) + ":" + pad2(t % 60);
  }

  function fmtClock(t) {
    // Negative time = the count-in running down toward the song start.
    var neg = (t || 0) < 0;
    t = Math.abs(t || 0);
    var ms = Math.floor((t % 1) * 1000);
    var s = Math.floor(t);
    return (neg ? "−" : "") + pad2(Math.floor(s / 60)) + ":" + pad2(s % 60) +
      '<span class="clock-ms">.' + String(ms).padStart(3, "0") + "</span>";
  }

  function sectionLabel(name) { return SECTION_NAMES[name] || name; }

  function currentTime() {
    // May be negative while the count-in runs (the countdown to beat 1).
    var t = pos.t;
    if (pos.playing) t += (performance.now() - pos.wall) / 1000;
    if (state && state.duration) t = Math.min(t, state.duration);
    if (!pos.playing) t = Math.max(0, t);
    return t;
  }

  function send(cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(Object.assign({ type: "cmd" }, cmd)));
    }
  }

  // ---- WebSocket -----------------------------------------------------------

  function setConnected(on) {
    var pill = $("sync-pill");
    pill.className = "pill " + (on ? "pill-on" : "pill-off");
    pill.innerHTML = '<span class="pill-dot"></span>' +
      (on ? "SYNCED" : "DISCONNECTED");
    // On phone the pill is hidden; the header live-dot reflects the link.
    document.body.classList.toggle("offline", !on);
  }

  function connect() {
    ws = new WebSocket("ws://" + location.host + "/ws");
    ws.onopen = function () {
      reconnectDelay = 500;
      setConnected(true);
    };
    ws.onclose = ws.onerror = function () {
      if (!ws) return;
      ws = null;
      setConnected(false);
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "state") onState(msg);
      else if (msg.type === "pos") onPos(msg);
      else if (msg.type === "beat") onBeat(msg);
    };
  }

  // ---- message handlers ------------------------------------------------------

  function onState(s) {
    state = s;
    pos = { t: s.position, wall: performance.now(), playing: s.playing };
    $("show-name").textContent = s.show_name || "—";
    $("show-name").classList.toggle("placeholder-text", !s.show_name);
    renderShows(s.shows);
    renderSetlist(s);
    renderNowPlaying(s);
    renderCues(s.cues);
    renderSectionStrip(s);
    renderCountin(s);
    $("btn-blackout").classList.toggle("active", !!s.blackout);
    updatePlayIcon(s.playing);
    if (s.setlist.current !== waveSong) {
      waveSong = s.setlist.current;
      waveBins = null;
      waveDuration = 0;
      drawWaveform();
      fetchWaveform(waveSong);
    } else {
      drawWaveform();
    }
  }

  function onPos(msg) {
    pos = { t: msg.t, wall: performance.now(), playing: msg.playing };
    updatePlayIcon(msg.playing);
    renderMeters(msg.levels);
  }

  function onBeat(msg) {
    beat = msg.beat;
    var dots = document.querySelectorAll(".beat-dot");
    dots.forEach(function (dot) {
      var active = Number(dot.dataset.beat) === beat;
      dot.classList.toggle("active", active);
      dot.classList.toggle("downbeat", active && beat === 1);
    });
  }

  // ---- rendering -------------------------------------------------------------

  function songMeta(song) {
    var parts = [];
    if (song.bpm != null) parts.push(song.bpm + " BPM");
    parts.push(song.duration != null ? fmtDur(song.duration) : "—");
    return parts.join(" · ");
  }

  function closeShowMenu() {
    $("show-picker").classList.remove("open");
    $("show-btn").setAttribute("aria-expanded", "false");
  }

  function renderShows(shows) {
    shows = shows || [];
    var menu = $("show-menu");
    menu.innerHTML = "";
    // One show (or none) means nothing to switch to: the chevron goes away.
    $("show-picker").classList.toggle("single", shows.length < 2);
    if (shows.length < 2) closeShowMenu();
    shows.forEach(function (show) {
      var li = document.createElement("li");
      if (show.current) li.className = "current";

      var tick = document.createElement("span");
      tick.className = "tick";
      tick.textContent = show.current ? "✓" : "";
      li.appendChild(tick);

      var label = document.createElement("span");
      label.className = "show-label";
      label.textContent = show.name;
      li.appendChild(label);

      // The open show is inert (same rule as the current setlist row): only
      // the name goes back to the app, which resolves it to a folder.
      if (!show.current) {
        li.addEventListener("click", function () {
          closeShowMenu();
          send({ action: "open_show", name: show.name });
        });
      }
      menu.appendChild(li);
    });
  }

  function renderSetlist(s) {
    var list = $("setlist");
    var current = s.setlist.current;
    var songs = s.setlist.songs;
    $("setlist-count").textContent = songs.length + " tracks";
    list.innerHTML = "";
    songs.forEach(function (song, i) {
      var li = document.createElement("li");
      if (i < current) li.className = "done";
      else if (i === current) li.className = "current";
      else if (i === current + 1) li.className = "next";

      var num = document.createElement("span");
      num.className = "num";
      num.textContent = i < current ? "✓" : pad2(i + 1);
      li.appendChild(num);

      var main = document.createElement("div");
      main.className = "song-main";
      var title = document.createElement("div");
      title.className = "song-title";
      title.textContent = song.title;
      main.appendChild(title);
      var meta = document.createElement("div");
      meta.className = "song-meta";
      meta.textContent = i === current + 1
        ? "NEXT · " + songMeta(song) : songMeta(song);
      main.appendChild(meta);
      li.appendChild(main);

      if (i === current) {
        var eq = document.createElement("span");
        eq.className = "eq" + (pos.playing ? "" : " paused");
        eq.innerHTML = "<i></i><i></i><i></i>";
        li.appendChild(eq);
      } else {
        // Tap any other song to jump to it (the current one is left inert so
        // a stray tap can't restart the playing track). Also closes the phone
        // drawer.
        li.addEventListener("click", function (idx) {
          return function () {
            send({ action: "select_song", index: idx });
            document.body.classList.remove("setlist-open");
          };
        }(i));
      }
      list.appendChild(li);
    });
  }

  function renderNowPlaying(s) {
    var songs = s.setlist.songs;
    var current = s.setlist.current;
    var song = songs[current];
    var total = songs.length;

    var indexText = pad2(current + 1) + " / " + pad2(total);
    $("song-title").textContent = song ? song.title : "—";
    $("chip-index").textContent = indexText;
    $("now-index").textContent = song ? indexText : "— / —";
    $("chip-key").textContent = "KEY · " + (song && song.key ? song.key : "—");
    $("chip-bpm").textContent = s.bpm + " BPM";
    $("top-bpm").textContent = s.bpm + " BPM";
    $("top-song").textContent = song
      ? pad2(current + 1) + " / " + pad2(total) + " · " + song.title.toUpperCase()
      : "— / — · —";
    $("wave-total").textContent = fmtDur(s.duration);

    var next = songs[current + 1];
    $("next-title").textContent = next ? next.title : "— (end of setlist)";
    $("next-meta").textContent = next ? songMeta(next) : "";
    var bars = s.countin ? s.countin.bars : null;
    $("next-countin").textContent = !next ? "—"
      : next.precount ? "COUNT-IN · " + bars + " BARS" : "SEGUE";
  }

  function renderCountin(s) {
    var countin = s.countin || { enabled: false, bars: null };
    $("countin-toggle").classList.toggle("on", !!countin.enabled);
    $("countin-bars").textContent = countin.bars != null ? countin.bars : "—";
  }

  function renderMeters(levels) {
    ["click", "backing"].forEach(function (bus) {
      var level = levels && levels[bus] ? levels[bus] : 0;
      // RMS → lit segments; sqrt lifts quiet material into the visible range.
      var lit = Math.min(8, Math.round(Math.sqrt(Math.min(1, level * 1.6)) * 8));
      document.querySelectorAll('[data-meter="' + bus + '"]').forEach(function (meter) {
        for (var i = 0; i < meter.children.length; i++) {
          meter.children[i].classList.toggle("off", i >= lit);
        }
      });
    });
  }

  function renderCues(cues) {
    var grid = $("cue-grid");
    grid.innerHTML = "";
    (cues || []).forEach(function (cue) {
      var btn = document.createElement("button");
      btn.className = "cue-btn";
      btn.style.borderColor = cue.color;
      btn.style.background =
        "linear-gradient(160deg, " + cue.color + "22, var(--card) 60%)";

      var dot = document.createElement("span");
      dot.className = "cue-dot";
      dot.style.background = cue.color;
      btn.appendChild(dot);

      var id = document.createElement("span");
      id.className = "cue-id";
      id.textContent = "C" + (cue.index + 1);
      btn.appendChild(id);

      var label = document.createElement("span");
      label.textContent = cue.label;
      btn.appendChild(label);

      btn.addEventListener("click", function () {
        send({ action: "cue", index: cue.index });
      });
      grid.appendChild(btn);
    });
  }

  function renderSectionStrip(s) {
    var strip = $("section-strip");
    strip.innerHTML = "";
    if (!s.duration) return;
    (s.sections || []).forEach(function (sec) {
      var seg = document.createElement("span");
      seg.className = "seg";
      // Position by fraction of the song so the strip tiles the full width
      // regardless of device size (contiguous sections abut exactly).
      seg.style.left = (sec.start / s.duration * 100) + "%";
      seg.style.width = ((sec.end - sec.start) / s.duration * 100) + "%";
      seg.style.background = sec.color + "55";
      seg.style.color = "#fffc";
      seg.textContent = sectionLabel(sec.name);
      strip.appendChild(seg);
    });
  }

  function updatePlayIcon(playing) {
    $("icon-play").classList.toggle("hidden", playing);
    $("icon-pause").classList.toggle("hidden", !playing);
    var eq = document.querySelector(".eq");
    if (eq) eq.classList.toggle("paused", !playing);
  }

  // ---- waveform --------------------------------------------------------------

  function fetchWaveform(index) {
    if (index == null || index < 0) return;
    fetch("/api/waveform?song=" + index)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.song === waveSong) {
          waveBins = data.bins;
          waveDuration = data.duration || 0;
          drawWaveform();
        }
      })
      .catch(function () { /* song without audio: leave the canvas empty */ });
  }

  function drawWaveform() {
    var canvas = $("waveform");
    var rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * devicePixelRatio;
    canvas.height = rect.height * devicePixelRatio;
    var ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!waveBins) return;

    // The bins cover the track the engine decoded, which can be shorter than
    // the song (a 20 s backing under a full-length guide). They get their own
    // share of the canvas, so the peaks stay under the playhead instead of
    // being stretched over a song they don't fill.
    var span = (state && state.duration && waveDuration)
      ? Math.min(1, waveDuration / state.duration) : 1;
    var n = waveBins.length;
    var w = canvas.width * span / n;
    var mid = canvas.height / 2;
    var playedFrac = state && state.duration ? currentTime() / state.duration : 0;
    for (var i = 0; i < n; i++) {
      var h = Math.max(1, waveBins[i] * mid * 0.95);
      ctx.fillStyle = (i / n) * span <= playedFrac ? "#5ac8fa" : "#3a4453";
      ctx.fillRect(i * w, mid - h, Math.max(1, w * 0.7), h * 2);
    }
  }

  // ---- rAF clock loop ----------------------------------------------------------

  var lastDrawnFrac = -1;

  function tick() {
    var t = currentTime();
    var counting = t < 0;
    document.body.classList.toggle("counting-in", counting);
    var clock = fmtClock(t);
    $("top-clock").innerHTML = clock;
    $("transport-clock").innerHTML = clock;
    $("wave-elapsed").textContent = fmtDur(Math.max(0, t));

    if (state) {
      var dur = state.duration;
      $("transport-remaining").textContent = dur ? fmtDur(dur - Math.max(0, t)) : "--:--";

      if (dur) {
        var f = Math.min(1, Math.max(0, t) / dur);
        $("playhead").style.left = (f * 100) + "%";
        if (!seeking) paintSeek(f);
        // Repaint the played/unplayed split at most a few times per second.
        var frac = Math.floor(f * 200);
        if (frac !== lastDrawnFrac) { lastDrawnFrac = frac; drawWaveform(); }
      }

      if (counting) {
        $("section-name").textContent = "COUNT-IN";
        $("section-count").textContent = "";
        $("section-remaining").textContent = (-t).toFixed(1) + "s to start";
        $("bar-beat").textContent =
          "COUNT-IN · BEAT " + (beat || "—") + "/" + state.beats_per_bar;
      } else {
        renderSection(t);
        var beatDur = 60 / (state.bpm || 120);
        var bar = Math.floor(t / (state.beats_per_bar * beatDur)) + 1;
        $("bar-beat").textContent =
          "BAR " + bar + " · BEAT " + (beat || "—") + "/" + state.beats_per_bar;
      }
    }
    requestAnimationFrame(tick);
  }

  function renderSection(t) {
    var sections = state.sections || [];
    var current = null;
    var i, occurrence = 0, total = 0;
    for (i = 0; i < sections.length; i++) {
      if (sections[i].start <= t) current = sections[i];
    }
    if (!current) {
      $("section-name").textContent = "—";
      $("section-count").textContent = "";
      $("section-remaining").textContent = "";
      return;
    }
    for (i = 0; i < sections.length; i++) {
      if (sections[i].name === current.name) {
        total++;
        if (sections[i].start <= current.start) occurrence++;
      }
    }
    $("section-name").textContent = sectionLabel(current.name);
    $("section-count").textContent = total > 1 ? occurrence + "/" + total : "";
    $("section-remaining").textContent =
      fmtDur(Math.max(0, current.end - t)) + " left";
  }

  // ---- controls ------------------------------------------------------------------

  $("btn-play").addEventListener("click", function () { send({ action: "play_pause" }); });
  $("btn-stop").addEventListener("click", function () { send({ action: "stop" }); });
  $("btn-prev").addEventListener("click", function () { send({ action: "prev" }); });
  $("btn-next").addEventListener("click", function () { send({ action: "next" }); });
  $("btn-blackout").addEventListener("click", function () { send({ action: "blackout" }); });

  // AUTO COUNT-IN card: toggle = current song's precount, stepper = global bars.
  $("countin-toggle").addEventListener("click", function () {
    if (state && state.countin) send({ action: "set_precount", on: !state.countin.enabled });
  });
  $("countin-minus").addEventListener("click", function () {
    if (state && state.countin && state.countin.bars > 1) {
      send({ action: "set_countin_bars", bars: state.countin.bars - 1 });
    }
  });
  $("countin-plus").addEventListener("click", function () {
    if (state && state.countin && state.countin.bars < 8) {
      send({ action: "set_countin_bars", bars: state.countin.bars + 1 });
    }
  });

  // PANIC requires a 600 ms hold so a stray tap can't kill the show.
  (function () {
    var btn = $("btn-panic");
    var timer = null;
    function arm() {
      btn.classList.add("arming");
      timer = setTimeout(function () {
        disarm();
        send({ action: "panic" });
      }, 600);
    }
    function disarm() {
      btn.classList.remove("arming");
      if (timer) { clearTimeout(timer); timer = null; }
    }
    btn.addEventListener("pointerdown", arm);
    btn.addEventListener("pointerup", disarm);
    btn.addEventListener("pointerleave", disarm);
    btn.addEventListener("pointercancel", disarm);
  })();

  $("wave-wrap").addEventListener("pointerdown", function (ev) {
    if (!state || !state.duration) return;
    var rect = this.getBoundingClientRect();
    var frac = (ev.clientX - rect.left) / rect.width;
    send({ action: "seek", time: Math.max(0, frac) * state.duration });
  });

  // Show picker: the header show name deploys the list of shows sitting in the
  // same folder; a tap anywhere else closes it.
  $("show-btn").addEventListener("click", function (ev) {
    ev.stopPropagation();
    var open = $("show-picker").classList.toggle("open");
    this.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", closeShowMenu);

  // Setlist drawer (phone): the header "+" opens it, the backdrop closes it.
  $("btn-setlist").addEventListener("click", function () {
    document.body.classList.toggle("setlist-open");
  });
  $("drawer-backdrop").addEventListener("click", function () {
    document.body.classList.remove("setlist-open");
  });

  // Phone seek bar: drag the knob to scrub. `seeking` suppresses the rAF loop
  // from fighting the finger; the seek command fires on release.
  (function () {
    var bar = $("seekbar");
    function frac(ev) {
      var r = bar.getBoundingClientRect();
      return Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
    }
    bar.addEventListener("pointerdown", function (ev) {
      if (!state || !state.duration) return;
      seeking = true;
      bar.setPointerCapture(ev.pointerId);
      paintSeek(frac(ev));
    });
    bar.addEventListener("pointermove", function (ev) {
      if (seeking) paintSeek(frac(ev));
    });
    bar.addEventListener("pointerup", function (ev) {
      if (!seeking) return;
      seeking = false;
      send({ action: "seek", time: frac(ev) * state.duration });
    });
    bar.addEventListener("pointercancel", function () { seeking = false; });
  })();

  window.addEventListener("resize", drawWaveform);

  connect();
  requestAnimationFrame(tick);
})();
