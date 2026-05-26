// GABORA landing page: mock login + aquifer selector.
// NOTE: This is a UI mockup. The credentials are checked client-side
// only; there is no real authentication. Do not rely on this for any
// access control.

const MOCK_USER = "Regulator";
const MOCK_PASS = "GABORA2027";
const SESSION_KEY = "gabora_session";

const AQUIFERS = [
  {
    id: "precipice",
    name: "Precipice Sandstone",
    desc: "Lower Jurassic confined sandstone. Calibrated MODFLOW 6 model with spring complex receptors.",
    status: "ok",
    href: "precipice.html",
  },
  {
    id: "hutton",
    name: "Hutton Sandstone",
    desc: "Middle Jurassic. Model under development — calibration in progress.",
    status: "soon",
    href: "coming-soon.html?aquifer=Hutton%20Sandstone",
  },
  {
    id: "adori-springbok",
    name: "Adori–Springbok Sandstone",
    desc: "Upper Jurassic. Receptor inventory being compiled.",
    status: "soon",
    href: "coming-soon.html?aquifer=Adori%E2%80%93Springbok%20Sandstone",
  },
  {
    id: "hooray",
    name: "Hooray Sandstone",
    desc: "Lower Cretaceous regional aquifer. Awaiting parent-model inputs.",
    status: "soon",
    href: "coming-soon.html?aquifer=Hooray%20Sandstone",
  },
  {
    id: "walloon",
    name: "Walloon Coal Measures",
    desc: "Coal seam unit — outside scope of GAB consolidated regulation but included for cumulative impact context.",
    status: "soon",
    href: "coming-soon.html?aquifer=Walloon%20Coal%20Measures",
  },
  {
    id: "gubberamunda",
    name: "Gubberamunda Sandstone",
    desc: "Upper Jurassic. Model framework pending.",
    status: "soon",
    href: "coming-soon.html?aquifer=Gubberamunda%20Sandstone",
  },
];

function $(id) { return document.getElementById(id); }

function showSelector(username) {
  $("login-pane").hidden = true;
  $("selector-pane").hidden = false;
  $("user-chip").textContent = username;
  $("user-chip").hidden = false;
  $("logout-btn").hidden = false;
  renderAquifers();
}

function showLogin() {
  $("login-pane").hidden = false;
  $("selector-pane").hidden = true;
  $("user-chip").hidden = true;
  $("logout-btn").hidden = true;
  $("username").value = "";
  $("password").value = "";
  $("login-error").hidden = true;
}

function renderAquifers() {
  const grid = $("aquifer-grid");
  grid.innerHTML = "";
  for (const a of AQUIFERS) {
    const card = document.createElement("a");
    card.className = "aquifer-card" + (a.status !== "ok" ? " disabled" : "");
    card.href = a.href;
    if (a.status === "ok") card.title = `Open the ${a.name} module`;
    else card.title = `${a.name} — under development`;
    card.innerHTML = `
      <span class="badge ${a.status}">${a.status === "ok" ? "Available" : "Coming soon"}</span>
      <div class="name">${a.name}</div>
      <div class="desc">${a.desc}</div>
    `;
    grid.appendChild(card);
  }
}

function init() {
  // Restore session if present.
  const saved = sessionStorage.getItem(SESSION_KEY);
  if (saved) {
    showSelector(saved);
  } else {
    showLogin();
  }

  $("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const u = $("username").value.trim();
    const p = $("password").value;
    if (u === MOCK_USER && p === MOCK_PASS) {
      sessionStorage.setItem(SESSION_KEY, u);
      showSelector(u);
    } else {
      const err = $("login-error");
      err.textContent = "Invalid username or password.";
      err.hidden = false;
    }
  });

  $("logout-btn").addEventListener("click", () => {
    sessionStorage.removeItem(SESSION_KEY);
    showLogin();
  });
}

init();
