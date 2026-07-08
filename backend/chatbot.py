"""
AI rain chatbot for Garaj Baras.

Gemini 2.5 Flash (free tier) + function calling over the existing nowcast
engine. main.py registers the internal endpoint functions via register_tools()
so tools run in-process (no HTTP self-calls). Provider-specific code is kept
inside chat_stream() so the LLM can be swapped later.

Requires env var GEMINI_API_KEY (free key from https://aistudio.google.com).
"""

import json
import os

import httpx  # type: ignore

GEMINI_MODEL = "gemini-2.5-flash"
MAX_TOOL_ITERATIONS = 6
MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_CHARS = 2000
MAX_OUTPUT_TOKENS = 2048

# Filled by main.py at import time: name -> python callable
_TOOL_FNS = {}


def register_tools(fn_map):
    _TOOL_FNS.update(fn_map)


def is_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


SYSTEM_PROMPT = """You are Garaj Baras Assistant, the AI helper inside Garaj Baras — an Indian rain nowcasting app that reads live IMD doppler radar.

LANGUAGE: Reply in the same language the user writes — English, Hindi, or Hinglish. Keep the tone friendly and desi-casual, never robotic.

WHAT YOU CAN DO:
- Tell whether it will rain at a place within the next ~105 minutes (radar nowcast, 15-minute slots).
- Check rain along a driving route (start to end coordinates).
- Report current rain movement (direction/speed) over Delhi NCR.
- Share the app's verified prediction accuracy stats when asked.

HARD LIMITS — be honest about these:
- Coverage is ONLY within radar range of Delhi NCR, Lucknow, Patna, and Bhopal. For places outside (e.g., Mumbai, Bangalore), say clearly that the location is outside radar coverage right now.
- The horizon is ~105 minutes. You CANNOT forecast "tomorrow", "this evening" (if far away), or weekly weather. Politely decline and offer the next-105-minutes view instead.
- Never invent rain data. If a tool fails or returns nothing, say so.

WORKFLOW:
1. When the user names a place, call geocode_place first to get coordinates, then get_nowcast. If geocoding returns several matches, pick the most likely Indian city-area match; only ask the user if it's genuinely ambiguous.
2. For "should I leave now or wait?" questions: compare slot probabilities across the timeline and recommend a concrete time (IST), e.g. "nikal jao abhi — 30 min baad 78% chance hai".
3. For route questions, geocode both ends, then call get_route_rain.

ANSWER STYLE: Verdict first, then the why. 2–5 sentences. Use the probabilities and timings from the tools. A little personality is good (umbrella jokes allowed), fabricated data is not."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "geocode_place",
        "description": "Convert an Indian place name to latitude/longitude. Returns up to 3 candidate matches. Always call this before get_nowcast or get_route_rain when the user gives place names.",
        "parameters": {
            "type": "object",
            "properties": {
                "place_name": {"type": "string", "description": "Place name, e.g. 'Connaught Place Delhi' or 'Hazratganj Lucknow'"},
            },
            "required": ["place_name"],
        },
    },
    {
        "name": "get_nowcast",
        "description": "Rain nowcast for a point: 8 slots at 0/15/30/.../105 minutes with rain probability and intensity, from live radar. Works only inside Delhi NCR / Lucknow / Patna / Bhopal radar coverage.",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "get_route_rain",
        "description": "Rain prediction along a driving route between two coordinates (Delhi NCR radar). Returns per-waypoint rain expectation and where rain is first expected.",
        "parameters": {
            "type": "object",
            "properties": {
                "start_lat": {"type": "number"},
                "start_lon": {"type": "number"},
                "end_lat": {"type": "number"},
                "end_lon": {"type": "number"},
            },
            "required": ["start_lat", "start_lon", "end_lat", "end_lon"],
        },
    },
    {
        "name": "get_rain_movement",
        "description": "Current rain movement over Delhi NCR: direction it's coming from/heading to and speed in km/h.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_accuracy_stats",
        "description": "Verified accuracy of past predictions (POD/FAR/CSI), graded automatically against later radar frames. Use only when the user asks how accurate the app is.",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback window in days, default 30"},
            },
        },
    },
]


def _geocode_place(place_name: str):
    r = httpx.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place_name, "format": "json", "limit": 3, "countrycodes": "in"},
        headers={"User-Agent": "GarajBaras/1.0 (rain nowcast app)"},
        timeout=10.0,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return {"matches": [], "note": "No match found. Try adding the city name."}
    return {
        "matches": [
            {
                "name": item.get("display_name", "")[:120],
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
            }
            for item in results
        ]
    }


def _compact_nowcast(result: dict) -> dict:
    """Trim the /nowcast payload so it doesn't bloat the LLM context."""
    return {
        "in_radar_bounds": result.get("in_radar_bounds"),
        "summary": result.get("summary"),
        "radar_as_of": result.get("radar_as_of"),
        "slots": [
            {
                "slot_mins": s.get("slot_mins"),
                "has_rain": s.get("has_rain"),
                "probability": s.get("probability"),
                "intensity": s.get("intensity"),
            }
            for s in (result.get("slots") or [])
        ],
    }


def _compact_route(result: dict) -> dict:
    wps = result.get("waypoints") or []
    step = max(1, len(wps) // 20)  # cap detail at ~20 waypoints
    return {
        "route_distance_km": result.get("route_distance_km"),
        "rain_waypoints": result.get("rain_waypoints"),
        "clear_waypoints": result.get("clear_waypoints"),
        "first_rain_eta_mins": result.get("first_rain_eta"),
        "first_rain_intensity": result.get("first_rain_label"),
        "rain_direction_from": result.get("rain_direction_from"),
        "rain_speed_kmh": result.get("rain_speed_kmh"),
        "waypoints": [
            {
                "eta_mins": w.get("eta_mins"),
                "rain_expected": w.get("rain_expected"),
                "label": w.get("label"),
                "in_radar_bounds": w.get("in_radar_bounds"),
            }
            for w in wps[::step]
        ],
    }


def execute_tool(name: str, args: dict) -> dict:
    """Run one tool call; always returns a JSON-safe dict (errors included)."""
    try:
        if name == "geocode_place":
            return _geocode_place(str(args.get("place_name", "")))
        if name == "get_nowcast":
            fn = _TOOL_FNS["get_nowcast"]
            return _compact_nowcast(fn(float(args["lat"]), float(args["lon"])))
        if name == "get_route_rain":
            fn = _TOOL_FNS["get_route_rain"]
            return _compact_route(fn(
                float(args["start_lat"]), float(args["start_lon"]),
                float(args["end_lat"]), float(args["end_lon"]),
            ))
        if name == "get_rain_movement":
            return _TOOL_FNS["get_rain_movement"]()
        if name == "get_accuracy_stats":
            days = int(args.get("days") or 30)
            return _TOOL_FNS["get_accuracy_stats"](days)
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:  # tool errors go back to the model, never crash the stream
        detail = getattr(e, "detail", None) or str(e)
        return {"error": str(detail)[:300]}


# ---------------------------------------------------------------------------
# Chat streaming (Gemini-specific)
# ---------------------------------------------------------------------------

def _sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def chat_stream(messages):
    """
    Sync generator of SSE strings for POST /chat.

    messages: [{"role": "user"|"model", "text": "..."}, ...] (newest last)
    Events: {"type":"text","delta"} | {"type":"tool","name"} | {"type":"done"} | {"type":"error","message"}
    """
    from google import genai  # imported lazily so the app boots without the package/key
    from google.genai import types, errors

    if not is_configured():
        yield _sse({"type": "error", "message": "Chatbot is not configured (GEMINI_API_KEY missing)."})
        return

    client = genai.Client()  # reads GEMINI_API_KEY

    contents = []
    for m in messages[-MAX_HISTORY_MESSAGES:]:
        role = "model" if m.get("role") == "model" else "user"
        text = str(m.get("text", ""))[:MAX_MESSAGE_CHARS]
        if text:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.6,
    )

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            model_parts = []
            function_calls = []

            stream = client.models.generate_content_stream(
                model=GEMINI_MODEL, contents=contents, config=config,
            )
            for chunk in stream:
                if not chunk.candidates:
                    continue
                content = chunk.candidates[0].content
                if not content or not content.parts:
                    continue
                for part in content.parts:
                    model_parts.append(part)
                    if part.text:
                        yield _sse({"type": "text", "delta": part.text})
                    if part.function_call:
                        function_calls.append(part.function_call)

            if not function_calls:
                yield _sse({"type": "done"})
                return

            contents.append(types.Content(role="model", parts=model_parts))
            response_parts = []
            for fc in function_calls:
                yield _sse({"type": "tool", "name": fc.name})
                result = execute_tool(fc.name, dict(fc.args or {}))
                response_parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result},
                ))
            # Gemini expects function responses under role="user" (matches the
            # SDK's own automatic-function-calling behavior).
            contents.append(types.Content(role="user", parts=response_parts))

        yield _sse({"type": "error", "message": "Took too many steps — please rephrase your question."})
    except errors.APIError as e:
        if getattr(e, "code", None) == 429:
            yield _sse({"type": "error", "message": "AI assistant is resting (free quota hit). Try again in a minute. 🌧️"})
        else:
            yield _sse({"type": "error", "message": f"AI error: {getattr(e, 'message', str(e))[:200]}"})
    except Exception as e:
        yield _sse({"type": "error", "message": f"Chat failed: {str(e)[:200]}"})
