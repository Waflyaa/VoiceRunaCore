# VoiceRunaCore — internal contracts (frozen)

This file is the single source of truth for module boundaries. Every module below is owned by
exactly one author; other modules may only call the signatures listed here. If a signature must
change, the change is made here first.

Runtime layout (sidecar):

    sidecar/voiceruna/
      __init__.py        version
      paths.py           app data locations                    [done]
      config.py          config.json + .env store              [done]
      state.py           state machine + listeners             [done]
      events.py          WS event envelope                     [done]
      logging_setup.py   rotating logs, off by default         [done]
      apps.py            apps.json registry                    [core]
      security.py        argument/path/URI/.lnk validators     [core]
      tools/             five tools + executor                 [core]
      router/            fast path (no LLM)                    [core]
      llm/               OpenAI-compatible adapter + cascade   [llm]
      voice/             wake/VAD/STT/TTS/audio pipeline       [voice]
      runtime.py         orchestrator: text -> tool/LLM -> TTS [lead]
      ws.py              WebSocket fan-out                     [lead]
      app.py             FastAPI app, auth, REST routes        [lead]
      __main__.py        uvicorn entry, --port, VOX_TOKEN      [lead]

Common conventions:

- Python 3.12, 'from __future__ import annotations', no blocking calls in async paths
  (long work runs in asyncio.to_thread).
- Everything importable without optional heavy deps: faster_whisper, vosk, onnxruntime,
  sounddevice, edge_tts are imported lazily inside functions, never at module import time.
  'pytest sidecar/tests' must pass on a machine without a microphone.
- Errors never escape into asyncio tasks: tools return ToolResult(ok=False, ...).
- Speech strings are Russian, short, no markdown.
- All public modules re-export their contract names in __all__.

## contracts

### apps.py — [core]

    DEFAULT_APPS: dict[str, dict]                     # template from TZ §9

    class AppsRegistry:
        def __init__(self, path: Path | None = None) -> None
        def load(self) -> dict[str, dict]             # creates apps.json from DEFAULT_APPS when missing
        def save(self) -> None                        # atomic write
        def snapshot(self) -> dict[str, dict]
        def replace(self, data: dict[str, dict]) -> dict[str, dict]   # validate + persist (PUT /apps)
        def get(self, app_id: str) -> dict | None
        def ids(self) -> list[str]
        def aliases(self) -> dict[str, str]           # normalized alias -> app_id
        def describe_for_prompt(self) -> str          # 'dota2 (Dota 2), factorio (Factorio), ...'

Entry validation: display_name: str required; exactly one of path/uri; aliases: list[str]
optional. Invalid entries raise ValueError.

### security.py — [core]

    class ValidationError(Exception): ...

    def normalize_alias(text: str) -> str                       # lower, ё->е, collapse spaces
    def known_folder(folder_id: str) -> Path                    # desktop|downloads|documents
    def validate_folder_id(folder_id: str, allowed: list[str]) -> str
    def validate_filename(filename: str) -> str                 # rejects .., abs, and \ / : * ? " < > |
    def resolve_file(folder_id: str, filename: str, *, allowed_folders: list[str],
                     allowed_extensions: list[str], lnk_allowed_folders: list[str]) -> Path
    def validate_lnk_target(path: Path) -> None                 # refuses .bat/.ps1/.cmd and system dirs
    def validate_uri(uri: str, allowed_schemes: list[str]) -> str
    def expand_app_command(entry: dict) -> tuple[str, list[str]]  # %VAR% expansion + arg split

resolve_file enforces every rule of TZ §8.3 and raises ValidationError with a short
Russian reason. .lnk handling: only from files.lnk_allowed_folders, target read from the
Shell Link binary (MS-SHLLINK LinkInfo), unreadable target = refusal.

### tools/ — [core]

    @dataclass
    class ToolResult:
        ok: bool
        speech: str            # what the assistant says
        payload: dict = field(default_factory=dict)
        error: str | None = None

    class ToolExecutor:
        def __init__(self, config: ConfigManager, apps: AppsRegistry) -> None
        async def execute(self, name: str, args: dict) -> ToolResult
        def specs(self) -> list[dict]        # OpenAI tools JSON schema, exactly the five tools
        def app_ids(self) -> list[str]

    class WeatherService:
        def __init__(self, config: ConfigManager) -> None
        async def forecast(self, city: str | None) -> ToolResult
        @staticmethod
        def describe(code: int) -> str

Unknown tool or invalid args -> ToolResult(ok=False, speech=<errors.tool_refused_phrase>, error=...).

### router/ — [core]

    @dataclass
    class RouteResult:
        tool: str | None            # None -> LLM
        args: dict
        kind: str                   # alias|fuzzy|time|weather|folder|file|none
        score: float = 0.0

    class Router:
        def __init__(self, config: ConfigManager, apps: AppsRegistry) -> None
        def route(self, text: str) -> RouteResult

Order per TZ §6: exact alias -> fuzzy >= threshold -> templates -> none. Two or more fuzzy
candidates above the threshold, or a template that does not parse -> tool=None.

### llm/ — [llm]

    @dataclass
    class LLMResult:
        text: str = ""
        tool_calls: list[dict] = field(default_factory=list)   # [{'name': str, 'args': dict}]
        provider_id: str | None = None
        error: str | None = None        # 'no_connection' | 'unauthorized' | None
        kind: str = 'ok'                # 'ok' | 'not_understood'

    class CascadeClient:
        def __init__(self, config: ConfigManager) -> None
        async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult
        async def test_provider(self, provider_id: str) -> tuple[bool, str]
        async def list_models(self, provider_id: str) -> list[str]
        def disabled_ids(self) -> list[str]
        def reset(self) -> None
        async def aclose(self) -> None

    def build_system_prompt(config: ConfigManager, apps: AppsRegistry) -> str   # TZ §7.5

Cascade: priority order, 429/402/5xx/timeout -> next silently, 401 -> disable until restart,
all failed -> LLMResult(error='no_connection'). Only the first tool call of a reply is kept.

### voice/ — [voice]

    class VoicePipeline:
        def __init__(self, config: ConfigManager, state: StateManager, emit: EventSink,
                     on_utterance: Callable[[str], Awaitable[None]]) -> None
        async def start(self) -> None            # background workers; never raises for missing mic
        async def stop(self) -> None
        async def pause(self) -> None            # mic off, state paused
        async def resume(self) -> None
        async def listen_now(self) -> None       # /listen, hotkey: listen without wake word
        async def speak(self, text: str) -> None # returns when playback finished or interrupted
        async def stop_speaking(self) -> None    # barge-in
        async def preview(self, voice: str, text: str) -> None
        async def list_voices(self) -> list[dict]   # [{'engine','name','label'}]
        def apply_config(self) -> None           # hot reload tts/audio/vad/wake/errors

The pipeline owns state transitions idle->listening->thinking->speaking->idle (TZ §3.2) and
emits transcript events. Barge-in: a wake hit during TTS stops playback immediately
(TZ §2.6). Offline: TTS failure must degrade to text-only, not raise.

### runtime.py, ws.py, app.py, __main__.py — [lead]

runtime.AssistantRuntime wires ConfigManager, AppsRegistry, Router, ToolExecutor, CascadeClient,
VoicePipeline and StateManager. Public surface used by the API:

    class AssistantRuntime:
        def __init__(self, config, state, emit) -> None
        async def startup(self) -> None
        async def shutdown(self) -> None
        async def handle_text(self, text: str, source: str) -> str   # returns answer text
        async def repeat(self) -> str
        async def set_paused(self, paused: bool) -> None
        def history(self) -> list[dict]

### HTTP/WS surface — [lead], consumed by UI

    GET  /health                  -> {'status':'ok','version':str,'state':str}
    POST /command {text}          -> {'answer': str}
    POST /listen                  -> {'ok': true}
    POST /repeat                  -> {'answer': str}
    GET  /state                   -> {'state': str, 'paused': bool}
    GET/PUT /config               -> full config dict (PUT accepts a partial deep merge)
    GET  /providers/{id}/models   -> {'models': [str]}
    POST /providers/{id}/test     -> {'ok': bool, 'message': str}
    GET  /voices                  -> {'voices': [{'engine','name','label'}]}
    POST /tts/preview {voice,text}-> {'ok': bool, 'message': str}   # honest playback result
    GET/PUT /apps                 -> apps.json dict
    PUT  /env {name, value}       -> {'ok': true}            # API key into .env
    POST /pause                   -> {'ok': true, 'paused': true}
    POST /resume                  -> {'ok': true, 'paused': false}
    GET  /audio/devices           -> {'input': [{'index':int,'name':str,'channels':int,'hostapi':str,'default':bool}],
                                     'output': [...], 'current': {'input':str,'output':str},
                                     'playback': {'ok':bool,'backend':str,'message':str}}
    POST /audio/test {kind,input|output, device?} -> {'ok':bool,'message':str,'level':float|null}
    POST /stop_speaking           -> {'ok': true}            # barge-in from the UI
    POST /shutdown                -> {'ok': true}
    WS   /ws?token=...            -> events: state | transcript | answer | tool_call | error | download_progress

Auth: header X-Vox-Token must equal the process token; bind 127.0.0.1 only.

### Events

    {'type':'state','state':'idle'|'listening'|'thinking'|'speaking'|'paused'|'downloading'|'error'}
    {'type':'transcript','text':str,'source':'voice'|'window'}
    {'type':'answer','text':str,'replaced_transcript':str|None}
    {'type':'tool_call','name':str,'args':dict,'ok':bool}
    {'type':'error','message':str,'kind':str}
    {'type':'download_progress','stage':str,'percent':float,'message':str}