# Voice Agent Builder

MovieXFone's phone path is an xAI Voice Agent Builder agent. The agent does the talking. These HTTP tools do the looking up. Do not enable web search on the agent.

## 1. Run the backend where Builder can reach it

Builder calls the tools from xAI's network, so localhost is not enough for a phone number. For a local trial, use the browser demo instead (`python -m moviexfone`). When you expose the API, set a shared secret and bind it deliberately:

```bash
export MOVIEXFONE_TOOL_SECRET='a-long-random-string'
export MOVIEXFONE_BIND=0.0.0.0
python -m moviexfone
```

Every Builder request must send `X-MovieXFone-Secret: <that string>`.

## 2. Create the agent

In the xAI console, open Voice Agent Builder and create an agent.

- Voice: `rex` (or audition `sal`, `leo`, `ara`, `eve`)
- Speed: `1.15`
- Turn detection: server VAD, idle timeout 8000 ms
- Greeting, interruptible: `Hi, you've reached MovieXFone. Tell me a movie, a theater, or a neighborhood.`
- Pronunciations: `Nitehawk` → `Night hawk`, `IMAX` → `eye max`

Paste the system prompt from `GET /v1/builder/bundle` (the `prompt` field). It already has the current New York time filled in. Refresh it when you publish, or replace the time line with the console's time variable if the console offers one.

## 3. Add three API Request tools

Method `POST`. Header `X-MovieXFone-Secret`. Body is the JSON arguments object. Paths are relative to your public origin.

### search_showtimes

`POST /v1/tools/search_showtimes`

```json
{
  "type": "function",
  "name": "search_showtimes",
  "description": "Find movie showtimes at New York City theaters. Call ONCE per caller question. Never invent times; only read times returned here. Read the speak field aloud. If results are empty, read nearest_alternative or say no screenings are listed. Do not search again for the same question.",
  "parameters": {
    "type": "object",
    "properties": {
      "location": {"type": "string", "description": "Theater, neighborhood, landmark, subway stop, ZIP, or borough exactly as the caller said it."},
      "movie": {"type": "string", "description": "Title as the caller said it. Omit for what's playing."},
      "date": {"type": "string", "description": "YYYY-MM-DD in America/New_York, or a phrase such as tonight, Friday, or this weekend. Default today."},
      "after_time": {"type": "string", "description": "HH:MM 24-hour earliest start. Tonight means 17:00."},
      "format": {"type": "string", "enum": ["any", "imax", "dolby", "3d", "70mm", "35mm"], "default": "any"},
      "page": {"type": "integer", "default": 1, "description": "2, 3, ... only when the caller asks for more."}
    },
    "required": ["location"]
  }
}
```

### get_movie_info

`POST /v1/tools/get_movie_info`

```json
{
  "type": "function",
  "name": "get_movie_info",
  "description": "Rating, runtime, and a one-line synopsis for a movie playing in NYC.",
  "parameters": {
    "type": "object",
    "properties": {"movie": {"type": "string"}},
    "required": ["movie"]
  }
}
```

### end_call

`POST /v1/tools/end_call`

```json
{
  "type": "function",
  "name": "end_call",
  "description": "Hang up after a short goodbye.",
  "parameters": {"type": "object", "properties": {}}
}
```

The same JSON is served live at `GET /v1/builder/bundle`.

## 4. What the tool returns

`search_showtimes` returns a short `speak` line (about ten seconds), at most two theaters, and at most three times each. Read `speak`. If `results` is empty, offer `nearest_alternative` or say no screenings are listed. Never say a movie is not playing. If `coverage_note` is set, say it and do not invent times. Do not read `ticket_url` aloud.

## 5. Try it, then attach a number

Use Builder's browser tester first. Ask "What's playing near Union Square tonight?" and "Times at Film Forum?". Then attach Builder's free phone number. Telephony is billed by xAI. Twilio is not part of this demo.

Do not add a web search tool. It invents showtimes.
