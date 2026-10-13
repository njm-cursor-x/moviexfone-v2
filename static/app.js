const screen = document.querySelector("#screen");
const cards = document.querySelector("#cards");
const modeEl = document.querySelector("#mode");
const clockEl = document.querySelector("#clock");
const text = document.querySelector("#text");
const mic = document.querySelector("#mic");

function addLine(who, message) {
  const wrap = document.createElement("p");
  wrap.className = "line";
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  wrap.append(label, document.createTextNode(" " + message));
  screen.append(wrap);
  screen.scrollTop = screen.scrollHeight;
}

function renderPayload(payload) {
  cards.replaceChildren();
  const rows = payload.results || [];
  const alt = payload.nearest_alternative;
  const list = rows.length ? rows : alt ? [alt] : [];
  for (const row of list) {
    const card = document.createElement("article");
    card.className = "card";
    const title = document.createElement("h2");
    title.textContent = `${row.movie || "Now playing"} · ${row.theater}`;
    const meta = document.createElement("p");
    meta.className = "meta";
    const bits = [];
    if (row.times) bits.push(row.times.join(", "));
    if (row.format) bits.push(row.format);
    if (row.distance_mi != null) bits.push(`${row.distance_mi} mi`);
    meta.textContent = bits.join(" · ");
    card.append(title, meta);
    if (row.ticket_url) {
      const link = document.createElement("a");
      link.href = row.ticket_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "Tickets at the theater";
      card.append(link);
    }
    cards.append(card);
  }
}

function speak(textToSay) {
  if (!textToSay || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(textToSay);
  utter.rate = 1.05;
  window.speechSynthesis.speak(utter);
}

let socket;
let mode = "mock";

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/voice`);
  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "session.ready") {
      mode = msg.mode;
      modeEl.textContent = mode === "live" ? "Live voice" : "Demo voice";
      addLine("MovieXFone", msg.greeting || "");
      if (mode !== "live") speak(msg.greeting);
    } else if (msg.type === "transcript" && msg.role === "assistant") {
      addLine("MovieXFone", msg.text || "");
      if (mode !== "live") speak(msg.text);
    } else if (msg.type === "transcript" && msg.role === "user") {
      addLine("You", msg.text || "");
    } else if (msg.type === "showtimes") {
      renderPayload(msg.payload || {});
    } else if (msg.type === "notice" || msg.type === "error") {
      addLine("MovieXFone", msg.message || "");
    }
  };
  socket.onclose = () => {
    modeEl.textContent = "reconnecting";
    setTimeout(connect, 1200);
  };
}

function ask(value) {
  const cleaned = value.trim();
  if (!cleaned || !socket || socket.readyState !== WebSocket.OPEN) return;
  if (mode !== "live") addLine("You", cleaned);
  socket.send(JSON.stringify({ type: "text", text: cleaned }));
  text.value = "";
}

document.querySelector("#send").addEventListener("click", () => ask(text.value));
text.addEventListener("keydown", (event) => {
  if (event.key === "Enter") ask(text.value);
});

const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!Speech) {
  mic.textContent = "Mic unavailable";
  mic.disabled = true;
} else {
  const recognition = new Speech();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = "en-US";
  recognition.onresult = (event) => {
    const said = event.results[0][0].transcript;
    ask(said);
  };
  recognition.onerror = () => addLine("MovieXFone", "I couldn't hear that. Type it instead.");
  mic.addEventListener("click", () => recognition.start());
}

fetch("/health")
  .then((response) => response.json())
  .then((body) => {
    clockEl.textContent = body.now_ny || "";
  })
  .catch(() => {
    clockEl.textContent = "";
  });

connect();
