const screen = document.querySelector("#screen");
const cards = document.querySelector("#cards");
const modeEl = document.querySelector("#mode");
const clockEl = document.querySelector("#clock");
const text = document.querySelector("#text");
const mic = document.querySelector("#mic");

let socket = null;
let mode = "mock";
let sessionWanted = false;
let retries = 0;
let micStream = null;
let micNode = null;
let audioCtx = null;
let nextPlay = 0;
const playing = [];
let assistant = "";
let talkRequested = false;
let lastLine = "";

function addLine(who, message) {
  const wrap = document.createElement("p");
  wrap.className = "line";
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  wrap.append(label, document.createTextNode(" " + message));
  screen.append(wrap);
  screen.scrollTop = screen.scrollHeight;
  lastLine = message;
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

function speak(textToSay, force) {
  if (!textToSay || !window.speechSynthesis) return;
  if (mode === "live" && !force) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(textToSay);
  utter.rate = 1.05;
  window.speechSynthesis.speak(utter);
}

function ensureAudio() {
  if (!audioCtx) {
    const Context = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Context();
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

function stopPlayback() {
  for (const source of playing) {
    try {
      source.stop();
    } catch (_err) {
      /* already stopped */
    }
  }
  playing.length = 0;
  nextPlay = 0;
}

function playPcm(b64) {
  if (!b64) return;
  const ctx = ensureAudio();
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const samples = Math.floor(bytes.length / 2);
  if (!samples) return;
  const view = new DataView(bytes.buffer);
  const buffer = ctx.createBuffer(1, samples, 24000);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples; i += 1) channel[i] = view.getInt16(i * 2, true) / 32768;
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  const start = Math.max(ctx.currentTime + 0.02, nextPlay);
  source.start(start);
  nextPlay = start + buffer.duration;
  playing.push(source);
  source.onended = () => {
    const index = playing.indexOf(source);
    if (index >= 0) playing.splice(index, 1);
  };
}

function floatTo16(samples) {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < samples.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return new Uint8Array(buf);
}

function toBase64(bytes) {
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

function downsample(input, inRate, outRate) {
  if (inRate === outRate) return input;
  const ratio = inRate / outRate;
  const length = Math.floor(input.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) out[i] = input[Math.floor(i * ratio)];
  return out;
}

async function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let auth = "";
  let ticketFailed = false;
  try {
    const response = await fetch("/v1/voice/ticket", { method: "POST" });
    if (response.status === 401 || response.status === 429) {
      ticketFailed = true;
      sessionWanted = false;
      modeEl.textContent = "idle";
      addLine("MovieXFone", response.status === 429 ? "Too many tries. Wait a minute." : "Not authorized.");
    } else if (response.ok) {
      const body = await response.json();
      if (body.ticket) auth = `?ticket=${encodeURIComponent(body.ticket)}`;
    } else {
      ticketFailed = true;
      sessionWanted = false;
      addLine("MovieXFone", "The line didn't open.");
    }
  } catch (_err) {
    ticketFailed = true;
    sessionWanted = false;
    addLine("MovieXFone", "The line didn't open.");
  }
  if (ticketFailed) return;
  socket = new WebSocket(`${proto}://${location.host}/ws/voice${auth}`);
  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "session.ready") {
      retries = 0;
      mode = msg.mode;
      modeEl.textContent = mode === "live" ? "Live voice" : "Demo voice";
      addLine("MovieXFone", msg.greeting || "");
      speak(msg.greeting);
      if (talkRequested) {
        talkRequested = false;
        if (mode === "live") {
          startMic().catch(() => addLine("MovieXFone", "I can't use the microphone. Type it instead."));
        } else {
          startBrowserSpeech();
        }
      }
    } else if (msg.type === "transcript.delta" && msg.role === "assistant") {
      assistant += msg.text || "";
    } else if (msg.type === "transcript" && msg.role === "assistant") {
      const line = msg.text || assistant;
      assistant = "";
      if (line) addLine("MovieXFone", line);
      speak(line);
    } else if (msg.type === "transcript" && msg.role === "user") {
      addLine("You", msg.text || "");
    } else if (msg.type === "audio") {
      if (mode === "live") playPcm(msg.data || "");
    } else if (msg.type === "transcript.done") {
      if (assistant && assistant.trim() !== lastLine) addLine("MovieXFone", assistant.trim());
      assistant = "";
    } else if (msg.type === "speech_started") {
      stopPlayback();
      assistant = "";
    } else if (msg.type === "showtimes") {
      renderPayload(msg.payload || {});
      if (mode === "live" && assistant) {
        addLine("MovieXFone", assistant);
        assistant = "";
      }
    } else if (msg.type === "goodbye") {
      addLine("MovieXFone", msg.text || "Enjoy the show.");
      speak(msg.text || "Enjoy the show.", true);
    } else if (msg.type === "notice" || msg.type === "error") {
      addLine("MovieXFone", msg.message || "");
      speak(msg.message || "", true);
      socket._statusShown = true;
    }
  };
  socket.onopen = () => {
    modeEl.textContent = "connected";
  };
  socket.onclose = (event) => {
    stopMic();
    if (event.code === 1008) {
      sessionWanted = false;
      modeEl.textContent = "idle";
      if (!socket._statusShown) addLine("MovieXFone", "Not authorized.");
      return;
    }
    if (event.code === 1013) {
      sessionWanted = false;
      modeEl.textContent = "idle";
      if (!socket._statusShown) addLine("MovieXFone", "The line is busy. Try again in a minute.");
      return;
    }
    if (!sessionWanted || mode === "live") {
      sessionWanted = false;
      modeEl.textContent = "idle";
      if (mode === "live") addLine("MovieXFone", "The line closed. Press Talk to start again.");
      return;
    }
    retries += 1;
    if (retries > 4) {
      sessionWanted = false;
      modeEl.textContent = "disconnected";
      addLine("MovieXFone", "The line dropped. Press Talk to try again.");
      return;
    }
    const delay = Math.min(8000, 500 * 2 ** retries);
    modeEl.textContent = "reconnecting";
    setTimeout(connect, delay);
  };
}

function ensureSocket() {
  sessionWanted = true;
  connect();
}

function ask(value) {
  const cleaned = value.trim();
  if (!cleaned) return;
  ensureSocket();
  addLine("You", cleaned);
  const send = () => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setTimeout(send, 80);
      return;
    }
    socket.send(JSON.stringify({ type: "text", text: cleaned }));
  };
  send();
  text.value = "";
}

function stopMic() {
  if (micNode) {
    micNode.disconnect();
    micNode.onaudioprocess = null;
    micNode = null;
  }
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop();
    micStream = null;
  }
  mic.textContent = "Talk";
}

async function startMic() {
  ensureSocket();
  ensureAudio();
  micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = ensureAudio();
  const source = ctx.createMediaStreamSource(micStream);
  micNode = ctx.createScriptProcessor(4096, 1, 1);
  micNode.onaudioprocess = (event) => {
    if (!socket || socket.readyState !== WebSocket.OPEN || mode !== "live") return;
    const raw = event.inputBuffer.getChannelData(0);
    const samples = downsample(raw, ctx.sampleRate, 24000);
    const bytes = floatTo16(samples);
    socket.send(JSON.stringify({ type: "audio", data: toBase64(bytes) }));
  };
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(micNode);
  micNode.connect(mute);
  mute.connect(ctx.destination);
  mic.textContent = "Stop";
}

const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;

function startBrowserSpeech() {
  if (!Speech) {
    addLine("MovieXFone", "This browser can't hear you. Type it instead.");
    return;
  }
  const recognition = new Speech();
  recognition.lang = "en-US";
  recognition.onresult = (event) => {
    ask(event.results[0][0].transcript);
  };
  recognition.onerror = () => addLine("MovieXFone", "I couldn't hear that. Type it instead.");
  recognition.start();
}

document.querySelector("#send").addEventListener("click", () => ask(text.value));
text.addEventListener("keydown", (event) => {
  if (event.key === "Enter") ask(text.value);
});

mic.textContent = "Talk";
mic.addEventListener("click", () => {
  ensureAudio();
  if (micStream) {
    stopMic();
    if (mode === "live" && socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "close" }));
      sessionWanted = false;
      socket.close();
    }
    return;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    if (mode === "live") {
      startMic().catch(() => addLine("MovieXFone", "I can't use the microphone. Type it instead."));
    } else {
      startBrowserSpeech();
    }
    return;
  }
  talkRequested = true;
  ensureSocket();
});

fetch("/health")
  .then((response) => response.json())
  .then((body) => {
    clockEl.textContent = body.now_ny || "";
  })
  .catch(() => {
    clockEl.textContent = "";
  });
