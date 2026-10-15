# MovieXFone demo

An internal demo. You ask a New York City showtimes question out loud, and an xAI realtime voice answers from a showtimes database this service owns. The voice model never searches the web.

This is not a public service. Do not brand it with the live "MovieXFone" trademark, and do not present Assemble's showtimes as our own feed.

## Run it locally

Requirements: CPython **3.12** (the lockfile does not install on 3.11 or 3.13), Chrome, a microphone, and an xAI API key.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --require-hashes -r requirements-build.lock
pip install --no-deps --no-build-isolation -e .

# names only; set your own values, never commit them
export XAI_API_KEY=...              # your xAI key (secret)
export MOVIEXFONE_TOOL_SECRET=...   # random string, at least 16 bytes (secret)
export MOVIEXFONE_LIVE_VOICE=1      # real xAI voice instead of the browser demo voice
export MOVIEXFONE_LIVE_DATA=1       # default; set 0 to load fixtures instead

python -m moviexfone                # one process only
```

Open **http://127.0.0.1:8000** in Chrome, press **Talk**, and allow the mic. The page asks `POST /v1/voice/ticket` for a one-minute ticket and connects with `?ticket=`. It does not put the tool secret on the socket.

Optional env vars:

- `MOVIEXFONE_PORT` (default 8000) and `MOVIEXFONE_DB` (SQLite path).
- `MOVIEXFONE_DAILY_SESSIONS` (default 20 calls a day) and `MOVIEXFONE_SESSION_ALLOW_HOSTS`.
- `MOVIEXFONE_ASSEMBLE_TTL` (default 900 s) and `MOVIEXFONE_MAX_SESSION` (default 900 s per call).
- `MOVIEXFONE_CORS_ORIGINS`, `MOVIEXFONE_RATE_LIMIT`, and `MOVIEXFONE_XAI_MODEL`.
- For a non-localhost bind: `MOVIEXFONE_BIND` plus `MOVIEXFONE_BASIC_USER` and `MOVIEXFONE_BASIC_PASSWORD`. The process refuses to start without them.

**Before the demo:**

- **Start the server at least 25 minutes early.** The first cold Assemble warm-up takes on the order of 20 minutes (hundreds of requests, capped at 50 per minute in this process). Until it finishes, AMC and Regal questions say "I don't have times … yet". Later restarts reuse the saved data.
- Check `/health/data` on localhost for row counts once it is warm.

## Voice Agent Builder

See `builder/voice-agent-builder.md`. In short, run the backend somewhere xAI's servers can reach, with `MOVIEXFONE_BIND=0.0.0.0`, a tool secret of at least 16 bytes, and basic auth. Add the three tool POSTs with the `X-MovieXFone-Secret` header. Do not enable web search. Attaching a phone number is billed by xAI. The browser bridge is the path that has been tested live. Builder has not.

## What a live browser call does

Round 7 on this bridge, with live xAI voice and live data:

- Calls of several minutes survive natural pauses. After about 40 seconds of silence the caller hears one "Anything else?".
- "Goodbye" gets "Enjoy the show." and the line closes. If the model already said that, it is not said again.
- Places include theaters, neighborhoods, ZIPs, and subway stations (Columbus Circle, Grand Central, Barclays).
- AMC and Regal matched the Assemble cells that were checked. Alamo matched the drafthouse.com sessions that were checked. Nitehawk times matched the site's day pages for the days that were checked.
- A dead voice upstream hangs up on the first stall, about 20 seconds, and the next caller can connect. A live socket that commits noise and then goes quiet gets one "Sorry, I didn't catch that" before a second stall hangs up.
- One caller at a time. A daily cap survives restarts. Basic auth covers non-local binds. Bogus tickets are rejected.

## Limits

- **AMC and Regal times come from Assemble's showtimes API.** That is fine for an internal demo. The sourcing is not disclosed to callers and is not cleared for public or branded use. The Alamo feed is also undocumented. Do not make this public without asking.
- **Single process only.** Assemble is capped at 50 requests a minute. Those request times are stored in SQLite and loaded again on startup, so a restart inside the same minute does not open a second window of 50. Run one `python -m moviexfone`, or one uvicorn or Docker worker. Two processes can still exceed the limit together.
- Repertory houses get a coverage note instead of times. Some titles have no Assemble film id; Alamo and Nitehawk still cover the ones they publish.
- No runtimes. Ratings and one-line blurbs come from bundled data only.
- One live call at a time. Real phone numbers are only via Builder, and that is billed.
- Alamo, Nitehawk, and Assemble load at startup and again on Monday, Thursday, and Saturday at 07:00 America/New_York, while the process is up. A missed 07:00 is covered by the next start, not by a catch-up timer. Nitehawk uses the dated page for today and the next six days, so a start after midnight still asks for today.
