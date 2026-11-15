// Second You phone stub. The real agents run on the phones (SecondYouKit, on-device models);
// this page shows the session state the server keeps: sealed-message count, the combined
// decision, both-say-yes, the date confirmation and the rating. Chat text and the date plan
// live only on the phones, so this page shows clearly labeled example text for them.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let token = null;
  try {
    const q = new URLSearchParams(location.search).get("token");
    if (q) { sessionStorage.setItem("vexo-token", q); history.replaceState(null, "", location.pathname); }
    token = sessionStorage.getItem("vexo-token");
  } catch (_) {
    token = new URLSearchParams(location.search).get("token");
  }
  if (!token) {
    $("screen").innerHTML = "<div class='card'>Open the phone link printed by <code>demo/seed.py</code> first.</div>";
    return;
  }

  const api = async (method, path, body) => {
    const r = await fetch(path, {
      method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
    return data;
  };
  const toast = (msg) => { const t = $("toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500); };
  const pct = (x) => `${Math.round(x * 100)}%`;

  // Example text only. On a phone these come from your own agent, decrypted on the device.
  const EXAMPLE_CHAT = [
    [false, "hey! your agent says you climb. indoor or outdoor?"],
    [true, "mostly the gym on weeknights, outside when the weather's good. you?"],
    [false, "same. I'm still scared of lead though lol"],
    [true, "honestly me too. want to try a bouldering session this weekend?"],
    [false, "yes. saturday morning, then coffee after?"],
    [true, "deal. also, real talk: I want kids someday. that ok with you?"],
    [false, "yeah, same page. not in a rush, but yes."],
  ];
  const EXAMPLE_PLAN = { what: "Bouldering, then coffee", when: "Saturday, 10:00", where: "The climbing gym on 5th, café next door" };
  const EXPECTED_MESSAGES = 26;          // 6 + 6 + up to 14 turns across the three scenarios
  const STEPS = [["First chat", 6], ["Plan a date", 12], ["Future talk", 22]];

  let session = null, matchId = null, lastJson = "", rating = { went: null, stars: 0, second: null };

  const meter = (d) => {
    if (!d) {
      return `<div class="meter"><div class="band" style="left:0;width:100%"></div></div>
        <div class="meter-labels small muted"><span>Not sure yet</span><span>Narrows when both phones finish</span></div>`;
    }
    const [lo, hi] = d.interval;
    return `<div class="meter">
        <div class="band" style="left:${lo * 100}%;width:${Math.max(1, (hi - lo) * 100)}%"></div>
        <div class="fill" style="width:${d.score * 100}%;opacity:.85"></div>
        <div class="floor" style="left:${d.floor * 100}%" title="match bar"></div></div>
      <div class="meter-labels small muted"><span>Likely ${pct(lo)} to ${pct(hi)}</span><span>Bar: ${pct(d.floor)}</span></div>`;
  };

  const who = (s) => s.counterpart
    ? `<div class="who"><div class="avatar known">${esc(s.counterpart.display_name[0])}</div>
        <div><b>${esc(s.counterpart.display_name)}</b><div class="small muted">You both said yes</div></div></div>`
    : `<div class="who"><div class="avatar">?</div>
        <div><b>Someone nearby</b><div class="small muted">Name stays hidden until you both say yes</div></div></div>`;

  function talking(s) {
    const n = s.relayed;
    const shown = EXAMPLE_CHAT.slice(0, Math.max(2, Math.min(EXAMPLE_CHAT.length, Math.ceil(n / 3))));
    const progress = s.my_verdict_sent ? 1 : Math.min(1, n / EXPECTED_MESSAGES);
    const wait = s.my_verdict_sent ? "Your agent is done. Waiting for theirs." : "Your agents are getting to know each other.";
    return `<section class="card">
        <h2>Agents are talking</h2>${who(s)}
        <div class="small" style="margin-top:10px">${wait}</div>
        <div class="meter"><div class="fill" style="width:${progress * 100}%"></div></div>
        <div class="small muted">${n} sealed message${n === 1 ? "" : "s"} passed along</div>
        <ul class="steps">${STEPS.map(([name, at]) => {
          const done = s.my_verdict_sent || n >= at;
          return `<li><span>${name}</span><span class="${done ? "" : "muted"}">${done ? "done" : "in progress"}</span></li>`;
        }).join("")}</ul>
      </section>
      <section class="card">
        <h2>How sure we are</h2>${meter(null)}
      </section>
      <section class="card">
        <h2>The chat</h2>
        <div class="label">Example chat. On a real phone you read your agent's actual messages here; the server only passes them along sealed.</div>
        <div class="chat">${shown.map(([mine, t]) => `<div class="bubble ${mine ? "mine" : ""}">${esc(t)}</div>`).join("")}</div>
      </section>`;
  }

  function reveal(s) {
    const d = s.decision;
    const ask = s.you_accepted === true
      ? "<div class='label'>You said yes. Waiting for them.</div>"
      : `<div class="actions"><button class="primary" data-act="yes">Yes, I'm in</button><button data-act="no">Not this time</button></div>`;
    return `<section class="card">
        <h2>It's a match</h2>${who(s)}
        <div class="big">${pct(d.score)}</div>
        <div class="small">Your agents think you two would click. Both phones agreed, nothing clashed and nobody made stuff up.</div>
        ${meter(d)}
        <div class="small muted" style="margin-top:8px">Both of you have to say yes before names are shown.</div>
        ${ask}
      </section>`;
  }

  function noMatch(s) {
    const d = s.decision;
    const why = d.clash ? "Something important didn't line up, like kids or where you want to live."
      : d.invented ? "An agent said something its person never would, so we stopped to be safe."
      : "They got along, but not clearly enough to be sure.";
    return `<section class="card"><h2>Not this one</h2>${who(s)}
        <div class="small" style="margin-top:10px">${why}</div>${meter(d)}
        <div class="small muted">Nothing was shared with them. Your agent keeps looking.</div></section>`;
  }

  function passed() {
    return `<section class="card"><h2>Not this one</h2>
      <div class="small">One of you passed. Names stay hidden, and nobody sees who said no.</div></section>`;
  }

  function datePlan(s) {
    const p = EXAMPLE_PLAN;
    const btn = s.you_confirmed_date
      ? "<div class='label'>You confirmed. Waiting for them.</div>"
      : `<div class="actions"><button class="primary" data-act="confirm">Confirm</button></div>`;
    return `<section class="card"><h2>It's mutual</h2>${who(s)}
        <div class="small" style="margin-top:10px">Your agents already planned something you'd both like.</div></section>
      <section class="card"><h2>Date plan</h2>
        <div class="label">Example plan. On a real phone it comes from your agents' chat and stays on the phones.</div>
        <table style="margin-top:8px">
          <tr><td class="muted">What</td><td>${esc(p.what)}</td></tr>
          <tr><td class="muted">When</td><td>${esc(p.when)}</td></tr>
          <tr><td class="muted">Where</td><td>${esc(p.where)}</td></tr>
        </table>
        ${btn}
      </section>`;
  }

  function rate(s) {
    if (s.you_rated) {
      return `<section class="card"><h2>Thanks</h2>${who(s)}
        <div class="small" style="margin-top:10px">Got it. Real dates teach your agent who you actually click with.</div>
        <div class="small muted" id="cal"></div></section>`;
    }
    const pick = (key, val, label) => `<button data-rate="${key}" data-val="${val}" class="${rating[key] === val ? "on" : ""}">${label}</button>`;
    return `<section class="card"><h2>How did it go?</h2>${who(s)}
        <div class="small" style="margin-top:12px">Did you go?</div>
        <div class="choice">${pick("went", true, "Yes")}${pick("went", false, "No")}</div>
        ${rating.went === true ? `
          <div class="small" style="margin-top:12px">How was it?</div>
          <div class="stars">${[1, 2, 3, 4, 5].map((n) => pick("stars", n, `${n} ★`)).join("")}</div>
          <div class="small" style="margin-top:12px">Want a second date?</div>
          <div class="choice">${pick("second", true, "Yes")}${pick("second", false, "No")}</div>` : ""}
        <div class="actions"><button class="primary" data-act="rate" ${rating.went === null ? "disabled" : ""}>Save</button></div>
        <div class="small muted">Only your answer goes to the server, to tune when we call it a match.</div>
      </section>`;
  }

  function render() {
    const box = $("screen"), stage = $("stage");
    const s = session;
    if (!s) {
      stage.textContent = "idle"; stage.className = "pill";
      box.innerHTML = `<section class="card"><h2>Agents are talking</h2>
        <div>${matchId ? "Starting your agent…" : "Nobody nearby yet."}</div>
        <div class="small muted">When someone compatible is nearby, your agents chat first. You only hear about it if they think you'd click.</div></section>`;
      return;
    }
    const map = {
      TALKING: ["talking", "pill warn", talking], MATCH: ["match", "pill good", reveal], NO_MATCH: ["no match", "pill", noMatch],
      PASSED: ["passed", "pill", passed], REVEALED: ["both said yes", "pill good", datePlan],
      DATE_SET: ["date set", "pill good", rate], DONE: ["done", "pill good", rate],
    };
    const [label, cls, fn] = map[s.state] || ["?", "pill", () => ""];
    stage.textContent = label; stage.className = cls;
    box.innerHTML = fn(s);
    if (s.you_rated) api("GET", "/api/second-you/calibration").then((c) => {
      const el = $("cal"); if (el) el.textContent = `Match bar today: ${pct(c.floor)} (${c.outcomes} of ${c.needed} dates needed to tune it).`;
    }).catch(() => {});
  }

  async function poll() {
    try {
      const cur = await api("GET", "/api/second-you/current");
      matchId = cur.match_id;
      let s = cur.session;
      if (!s && matchId) s = (await api("POST", "/api/second-you/sessions", { match_id: matchId })).session;
      if (s) session = s;
      else if (!matchId && session && ["TALKING", "MATCH"].includes(session.state)) session = null;
      else if (session) session = (await api("GET", `/api/second-you/sessions/${session.id}`)).session;
      const j = JSON.stringify([session, matchId]);
      if (j !== lastJson) { lastJson = j; render(); }
    } catch (e) { /* transient; next poll refreshes */ }
  }

  $("screen").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b || !session) return;
    if (b.dataset.rate) {
      const raw = b.dataset.val;
      rating[b.dataset.rate] = raw === "true" ? true : raw === "false" ? false : +raw;
      render(); return;
    }
    const id = session.id;
    try {
      if (b.dataset.act === "yes" || b.dataset.act === "no") session = (await api("POST", `/api/second-you/sessions/${id}/respond`, { accept: b.dataset.act === "yes" })).session;
      if (b.dataset.act === "confirm") session = (await api("POST", `/api/second-you/sessions/${id}/date`, { confirm: true })).session;
      if (b.dataset.act === "rate") {
        session = (await api("POST", `/api/second-you/sessions/${id}/outcome`, {
          went: rating.went, second_date: rating.went === true && rating.second === true,
          rating: rating.went === true && rating.stars ? rating.stars : null,
        })).session;
        toast("Thanks, saved");
      }
    } catch (e) { toast(e.message); }
    lastJson = ""; render();
  });

  poll();
  setInterval(poll, 1000);
})();
