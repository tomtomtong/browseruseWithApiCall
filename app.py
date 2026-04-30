import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from tkinter import END, BOTH, LEFT, RIGHT, X, Y, BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter import scrolledtext


CONFIG_PATH = Path(__file__).with_name("config.json")
_REPO_ROOT = Path(__file__).resolve().parent


def get_git_version() -> str:
    """Human-readable git revision for title bar and logs (tags when possible)."""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return out
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


# Separate profile so Chrome always exposes CDP even when your normal Chrome is already open.
CHROME_CDP_USER_DATA = CONFIG_PATH.with_name("chrome_cdp_profile")
DEFAULT_CDP_URL = "http://localhost:9222"
DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"
DEFAULT_LLM_PROVIDER = "openrouter"
INWORLD_MODELS_URL = "https://api.inworld.ai/llm/v1alpha/models"
INWORLD_CHAT_BASE_URL = "https://api.inworld.ai/v1"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_TASK = "Open the requested website and complete the task safely."
DEFAULT_MAX_STEPS = 100
RECORDER_POLL_SECONDS = 0.75
# Shown in Model combobox before “List … models”; also used for substring autocomplete.
DEFAULT_MODEL_AUTOCOMPLETE = (
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3.7-sonnet",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
    "google/gemini-pro-1.5",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
)
# Cap combobox values length so the UI stays responsive with huge provider lists.
MAX_MODEL_COMBO_VALUES = 500


def cdp_port_from_url(cdp_url: str) -> int:
    raw = (cdp_url or "").strip() or DEFAULT_CDP_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.port is not None:
        return parsed.port
    return 9222


def iter_chrome_exe_paths():
    if os.name == "nt":
        yield Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        yield Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
    elif sys.platform == "darwin":
        yield Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            yield Path(found)


def inworld_authorization_header(raw_key: str) -> str:
    """Build Authorization header per Inworld Basic auth (see list-models / chat-completions docs)."""
    key = (raw_key or "").strip()
    if not key:
        return ""
    if key.lower().startswith("basic "):
        return key
    return f"Basic {key}"


def fetch_inworld_models(api_key: str, timeout: float = 45.0) -> list[str]:
    """GET /llm/v1alpha/models — returns provider-prefixed ids suitable for /v1/chat/completions."""
    import httpx

    auth = inworld_authorization_header(api_key)
    if not auth:
        raise ValueError("Inworld API key is empty.")

    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        response = client.get(INWORLD_MODELS_URL, headers={"Authorization": auth})
        response.raise_for_status()
        payload = response.json()

    models = payload.get("models") or []
    raw: list[tuple[str, bool | None]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = (m.get("model") or "").strip()
        prov = (m.get("provider") or "").strip()
        if not mid:
            continue
        supported = m.get("isSupported")
        if prov and "/" not in mid:
            raw.append((f"{prov}/{mid}", supported if isinstance(supported, bool) else None))
        else:
            raw.append((mid, supported if isinstance(supported, bool) else None))

    if any(s is True for _, s in raw):
        raw = [(c, s) for c, s in raw if s is not False]

    choices = [c for c, _ in raw]
    return sorted(set(choices), key=str.lower)


def fetch_openrouter_models(api_key: str, timeout: float = 45.0) -> list[str]:
    """GET /api/v1/models — returns model ids (e.g. anthropic/claude-3.5-sonnet)."""
    import httpx

    key = (api_key or "").strip()
    if not key:
        raise ValueError("OpenRouter API key is empty.")

    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        response = client.get(OPENROUTER_MODELS_URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    data = payload.get("data") or []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = (item.get("id") or "").strip()
            if mid:
                ids.append(mid)
        elif isinstance(item, str) and item.strip():
            ids.append(item.strip())

    return sorted(set(ids), key=str.lower)


async def check_cdp_reachable(cdp_url: str, timeout: float = 12.0) -> None:
    """Fail fast with a clear message when DevTools HTTP is not listening (matches browser_use)."""
    import httpx

    raw = (cdp_url or "").strip() or DEFAULT_CDP_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path.endswith("/json/version"):
        path = path + "/json/version"
    version_url = urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment)
    )
    host = parsed.hostname or ""
    is_localhost = host in ("localhost", "127.0.0.1", "::1", "")
    port = cdp_port_from_url(raw)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=not is_localhost) as client:
            response = await client.get(version_url)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("webSocketDebuggerUrl"):
                raise ConnectionError("CDP replied but webSocketDebuggerUrl was missing.")
    except httpx.HTTPStatusError as exc:
        raise ConnectionError(
            f"CDP HTTP error from {version_url}: {exc.response.status_code}\n"
            "Check the Chrome CDP URL and that Chrome was started with remote debugging."
        ) from exc
    except httpx.RequestError as exc:
        raise ConnectionError(
            f"Cannot reach Chrome DevTools at port {port} ({version_url}).\n\n"
            "Nothing is listening there yet. Common causes:\n"
            "• Chrome was opened without --remote-debugging-port (or merged into an existing "
            "Chrome that was not started with debugging).\n\n"
            "What to do:\n"
            "1. In this app, click “Launch Chrome (debug)” — it uses a separate profile folder "
            f"({CHROME_CDP_USER_DATA.name}) so debugging works even if normal Chrome is running.\n"
            "2. Wait until the window appears, then click Run Task again.\n"
            "3. Or quit all Chrome windows and start Chrome manually with "
            f"--remote-debugging-port={port}.\n\n"
            f"Underlying error: {exc}"
        ) from exc


def format_agent_summary(history) -> str:
    """Readable summary from browser_use AgentHistoryList (after agent.run())."""
    lines = [
        "── Task result summary ──",
        f"Successful: {history.is_successful()}",
        f"Agent marked done: {history.is_done()}",
        f"Steps: {history.number_of_steps()}",
        f"Duration: {history.total_duration_seconds():.1f} s",
    ]
    if history.has_errors():
        errs = [e for e in history.errors() if e]
        if errs:
            lines.append("")
            lines.append("Errors in trace:")
            for e in errs[:8]:
                lines.append(f"  • {e}")
            if len(errs) > 8:
                lines.append(f"  … and {len(errs) - 8} more (see log)")
    fr = history.final_result()
    lines.append("")
    lines.append("Final result:")
    lines.append(str(fr) if fr is not None else "(none)")
    judgement = history.judgement()
    if judgement is not None:
        lines.append("")
        lines.append("Judge:")
        lines.append(json.dumps(judgement, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def format_recorded_instruction(event: dict) -> str:
    etype = event.get("type")
    url = event.get("url", "")
    target = event.get("target", "").strip()
    text = event.get("text", "").strip()
    value = event.get("value", "")

    if etype == "page_loaded":
        return f"Open {url}"
    if etype == "click":
        label = text or target or "element"
        return f"Click {label} ({target}) on {url}" if target and text else f"Click {label} on {url}"
    if etype == "change":
        if value == "[REDACTED]":
            return f"Fill {target or 'field'} with your secret value on {url}"
        return f"Set {target or 'field'} to \"{value}\" on {url}"
    if etype == "submit":
        return f"Submit form {target or ''} on {url}".strip()
    if etype == "navigate":
        return f"Wait for navigation to {url}"
    return ""


class BrowserActionRecorder:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.loop = None
        self.task = None
        self.stop_requested = False

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, cdp_url: str):
        if self.is_running():
            self.app.log("Recorder is already running.")
            return
        self.stop_requested = False
        self.thread = threading.Thread(target=self._thread_main, args=(cdp_url,), daemon=True)
        self.thread.start()

    def stop(self):
        if not self.is_running():
            self.app.log("Recorder is not running.")
            return
        self.stop_requested = True
        self.app.log("Stopping recorder...")
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)

    def _thread_main(self, cdp_url: str):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self._record(cdp_url))
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            self.app.log("Recorder stopped.")
        except Exception as exc:
            self.app.log(f"Recorder failed: {exc}")
        finally:
            self.task = None
            self.loop.close()
            self.loop = None
            self.app.on_recording_finished()

    async def _record(self, cdp_url: str):
        self.app.log("Recorder: checking CDP endpoint...")
        await check_cdp_reachable(cdp_url)
        self.app.log("Recorder: connected. Perform actions in Chrome now.")

        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        install_script = """
(() => {
  if (window.__buRecorderInstalled) return;
  window.__buRecorderInstalled = true;
  window.__buRecordedEvents = window.__buRecordedEvents || [];
  const MAX_EVENTS = 700;
  const textOf = (el) => ((el && (el.innerText || el.value || el.getAttribute('aria-label') || '')) + '').trim().slice(0, 90);
  const cssPath = (el) => {
    if (!el || !el.tagName) return '';
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
      let part = cur.tagName.toLowerCase();
      if (cur.name) part += `[name="${cur.name}"]`;
      if (cur.getAttribute && cur.getAttribute('data-testid')) part += `[data-testid="${cur.getAttribute('data-testid')}"]`;
      const cls = (cur.className || '').toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.');
      if (cls) part += '.' + cls;
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const push = (type, data = {}) => {
    window.__buRecordedEvents.push({
      type,
      t: Date.now(),
      url: location.href,
      title: document.title,
      ...data
    });
    if (window.__buRecordedEvents.length > MAX_EVENTS) {
      window.__buRecordedEvents.splice(0, window.__buRecordedEvents.length - MAX_EVENTS);
    }
  };

  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest ? (e.target.closest('a,button,input,textarea,select,[role="button"],[onclick]') || e.target) : e.target;
    push('click', { target: cssPath(el), text: textOf(el) });
  }, true);

  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || !('value' in el)) return;
    let value = '';
    const type = (el.type || '').toLowerCase();
    if (type === 'password') value = '[REDACTED]';
    else if (type === 'checkbox' || type === 'radio') value = el.checked ? 'true' : 'false';
    else value = (el.value || '').toString().slice(0, 140);
    push('change', { target: cssPath(el), value, fieldType: type || el.tagName.toLowerCase() });
  }, true);

  document.addEventListener('submit', (e) => {
    push('submit', { target: cssPath(e.target) });
  }, true);

  window.addEventListener('hashchange', () => push('navigate', { reason: 'hashchange' }));
  window.addEventListener('popstate', () => push('navigate', { reason: 'popstate' }));
  push('page_loaded', {});

  window.__buDrainRecordedEvents = () => {
    const out = window.__buRecordedEvents || [];
    window.__buRecordedEvents = [];
    return out;
  };
})();
"""

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            installed_contexts = set()
            installed_pages = set()

            while not self.stop_requested:
                if not browser.is_connected():
                    raise RuntimeError("Recorder lost connection to Chrome.")

                for context in browser.contexts:
                    context_key = id(context)
                    if context_key not in installed_contexts:
                        try:
                            await context.add_init_script(install_script)
                        except PlaywrightError:
                            pass
                        installed_contexts.add(context_key)

                    for page in context.pages:
                        page_key = id(page)
                        if page_key not in installed_pages:
                            try:
                                await page.evaluate(install_script)
                            except PlaywrightError:
                                pass
                            installed_pages.add(page_key)

                        try:
                            events = await page.evaluate(
                                "(() => (window.__buDrainRecordedEvents ? window.__buDrainRecordedEvents() : []))()"
                            )
                        except PlaywrightError:
                            events = []
                        for event in events or []:
                            self.app.queue_recorded_event(event)

                await asyncio.sleep(RECORDER_POLL_SECONDS)

            try:
                await browser.close()
            except PlaywrightError:
                pass


class BrowserUseRunner:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.loop = None
        self.task = None
        self.stop_requested = False

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg):
        if self.is_running():
            self.app.log("An agent run is already in progress.")
            return

        self.stop_requested = False
        self.thread = threading.Thread(target=self._thread_main, args=(cfg,), daemon=True)
        self.thread.start()

    def stop(self):
        if not self.is_running():
            self.app.log("No active run to stop.")
            return

        self.stop_requested = True
        self.app.log("Stop requested. Waiting for current step to finish...")
        if self.loop and self.task:
            self.loop.call_soon_threadsafe(self.task.cancel)

    def _thread_main(self, cfg):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self._run_agent(cfg))

        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            self.app.log("Run cancelled.")
            self.app.queue_summary("Run cancelled by user.\n\nNo final agent result was produced.")
        except Exception as exc:
            self.app.log(f"Run failed: {exc}")
            self.app.queue_summary(f"Run failed:\n\n{exc}")
        finally:
            self.task = None
            self.loop.close()
            self.loop = None
            self.app.on_run_finished()

    async def _run_agent(self, cfg):
        self.app.log("Importing Browser Use modules...")
        try:
            from browser_use import Agent, Browser
            from browser_use.llm import ChatOpenRouter
        except ImportError as exc:
            self.app.log("Missing dependencies. Install with: pip install browser-use")
            raise exc

        self.app.log("Checking Chrome DevTools (CDP) endpoint...")
        await check_cdp_reachable(cfg["cdp_url"])
        self.app.log("CDP endpoint OK.")

        self.app.log(f"Connecting to existing Chrome session: {cfg['cdp_url']}")
        browser = Browser(cdp_url=cfg["cdp_url"])
        if cfg.get("provider") == "inworld":
            llm = ChatOpenRouter(
                model=cfg["model"],
                api_key="",
                base_url=INWORLD_CHAT_BASE_URL,
                default_headers={"Authorization": inworld_authorization_header(cfg["inworld_api_key"])},
            )
        else:
            llm = ChatOpenRouter(
                model=cfg["model"],
                api_key=cfg["openrouter_api_key"],
            )

        # Some Browser Use versions support browser=, others browser_session=.
        # Try both for better compatibility across releases.
        kwargs_candidates = [{"browser": browser}, {"browser_session": browser}]

        last_error = None
        for kwargs in kwargs_candidates:
            if self.stop_requested:
                raise asyncio.CancelledError()

            try:
                agent = Agent(
                    task=cfg["task"],
                    llm=llm,
                    **kwargs,
                )
                self.app.log("Agent started. Executing task...")
                result = await agent.run(max_steps=cfg["max_steps"])
                self.app.log("Agent finished successfully.")
                self.app.log(f"Result: {result}")
                self.app.queue_summary(format_agent_summary(result))
                return
            except TypeError as exc:
                last_error = exc
                continue

        if last_error is not None:
            raise last_error


class App:
    def __init__(self, root):
        self.root = root
        self.git_version = get_git_version()
        self.root.title(
            f"Browser Use + OpenRouter / Inworld (Reuse Chrome Session) — {self.git_version}"
        )
        self.root.geometry("900x820")

        self.provider_var = StringVar(value=DEFAULT_LLM_PROVIDER)
        self.openrouter_key_var = StringVar()
        self.inworld_key_var = StringVar()
        self.model_var = StringVar(value=DEFAULT_MODEL)
        self.cdp_url_var = StringVar(value=DEFAULT_CDP_URL)
        self.keep_key_var = BooleanVar(value=True)
        self.max_steps_var = StringVar(value=str(DEFAULT_MAX_STEPS))

        self.log_queue = queue.Queue()
        self.summary_queue = queue.Queue()
        self.recorded_event_queue = queue.Queue()
        self.runner = BrowserUseRunner(self)
        self.recorder = BrowserActionRecorder(self)
        self.recorded_instructions = []
        self._last_instruction = ""
        self._model_choices_full = tuple(DEFAULT_MODEL_AUTOCOMPLETE)

        self._build_ui()
        self.log(f"Git version: {self.git_version}")
        self._load_config()
        self._on_provider_changed()
        self._apply_model_filter()
        self._start_log_poller()

    def _on_provider_changed(self):
        prov = (self.provider_var.get() or DEFAULT_LLM_PROVIDER).strip().lower()
        if prov not in ("openrouter", "inworld"):
            prov = DEFAULT_LLM_PROVIDER
            self.provider_var.set(prov)
        use_or = prov == "openrouter"
        use_iw = prov == "inworld"
        self.refresh_openrouter_models_btn.configure(state="normal" if use_or else "disabled")
        self.refresh_inworld_models_btn.configure(state="normal" if use_iw else "disabled")

    def _set_model_choices_full(self, choices: list[str]):
        self._model_choices_full = (
            tuple(choices) if choices else tuple(DEFAULT_MODEL_AUTOCOMPLETE)
        )
        self._apply_model_filter()

    def _apply_model_filter(self):
        """Filter Model combobox values by current text (substring match, case-insensitive)."""
        full = self._model_choices_full
        text = self.model_var.get()
        t = text.lower().strip()
        if not t:
            shown = full[:MAX_MODEL_COMBO_VALUES] if len(full) > MAX_MODEL_COMBO_VALUES else full
            self.model_combo["values"] = tuple(shown)
            return
        filtered = [m for m in full if t in m.lower()]
        if len(filtered) > MAX_MODEL_COMBO_VALUES:
            filtered = filtered[:MAX_MODEL_COMBO_VALUES]
        self.model_combo["values"] = tuple(filtered)

    def _on_model_keyrelease(self, event):
        if event.keysym in ("Up", "Down", "Prior", "Next", "Left", "Right", "Home", "End"):
            return
        if event.state & 0x4:
            if event.keysym.lower() in ("v", "x"):
                self.root.after_idle(self._apply_model_filter)
            return
        self.root.after_idle(self._apply_model_filter)

    def _on_model_selected(self, _event=None):
        self.root.after_idle(self._apply_model_filter)

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=BOTH, expand=True)

        title = ttk.Label(
            main,
            text="Browser Use Task Runner (OpenRouter or Inworld + Existing Chrome)",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(fill=X, pady=(0, 10))

        form = ttk.Frame(main)
        form.pack(fill=X)

        ttk.Label(form, text="LLM provider").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.provider_combo = ttk.Combobox(
            form,
            textvariable=self.provider_var,
            values=("openrouter", "inworld"),
            state="readonly",
            width=20,
        )
        self.provider_combo.grid(row=0, column=1, sticky="w", pady=4)
        self.provider_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_provider_changed())

        ttk.Label(form, text="OpenRouter API key").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.openrouter_key_entry = ttk.Entry(form, textvariable=self.openrouter_key_var, show="*", width=70)
        self.openrouter_key_entry.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Inworld API key").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.inworld_key_entry = ttk.Entry(form, textvariable=self.inworld_key_var, show="*", width=70)
        self.inworld_key_entry.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Model").grid(row=3, column=0, sticky="nw", padx=(0, 8), pady=4)
        model_row = ttk.Frame(form)
        model_row.grid(row=3, column=1, sticky="ew", pady=4)
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, width=58)
        self.model_combo.pack(side=LEFT, fill=X, expand=True)
        self.model_combo.bind("<KeyRelease>", self._on_model_keyrelease)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.refresh_openrouter_models_btn = ttk.Button(
            model_row,
            text="List OpenRouter models",
            command=self.refresh_openrouter_models,
        )
        self.refresh_openrouter_models_btn.pack(side=LEFT, padx=(8, 0))
        self.refresh_inworld_models_btn = ttk.Button(
            model_row,
            text="List Inworld models",
            command=self.refresh_inworld_models,
        )
        self.refresh_inworld_models_btn.pack(side=LEFT, padx=(8, 0))

        ttk.Label(form, text="Chrome CDP URL").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.cdp_url_var, width=70).grid(row=4, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(
            form,
            text="Store API keys in local config.json",
            variable=self.keep_key_var,
        ).grid(row=5, column=1, sticky="w", pady=(2, 4))

        ttk.Label(form, text="Max steps (loop limit)").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        self.max_steps_spin = ttk.Spinbox(
            form,
            from_=1,
            to=5000,
            textvariable=self.max_steps_var,
            width=12,
        )
        self.max_steps_spin.grid(row=6, column=1, sticky="w", pady=(4, 8))

        form.columnconfigure(1, weight=1)

        task_frame = ttk.LabelFrame(main, text="Task Prompt", padding=8)
        task_frame.pack(fill=BOTH, expand=True, pady=(8, 8))
        self.task_text = ttk.Entry(task_frame)
        self.task_text.pack(fill=X)
        self.task_text.insert(0, DEFAULT_TASK)

        controls = ttk.Frame(main)
        controls.pack(fill=X, pady=(0, 8))

        self.save_btn = ttk.Button(controls, text="Save Config", command=self.save_config)
        self.save_btn.pack(side=LEFT)

        self.start_btn = ttk.Button(controls, text="Run Task", command=self.run_task)
        self.start_btn.pack(side=LEFT, padx=6)

        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop_task, state="disabled")
        self.stop_btn.pack(side=LEFT)

        self.record_btn = ttk.Button(controls, text="Start Recording", command=self.start_recording)
        self.record_btn.pack(side=LEFT, padx=(12, 0))

        self.stop_record_btn = ttk.Button(
            controls,
            text="Stop Recording",
            command=self.stop_recording,
            state="disabled",
        )
        self.stop_record_btn.pack(side=LEFT, padx=6)

        self.clear_record_btn = ttk.Button(controls, text="Clear Recording", command=self.clear_recording)
        self.clear_record_btn.pack(side=LEFT)

        self.use_record_btn = ttk.Button(controls, text="Use Recording in Task", command=self.use_recording_in_task)
        self.use_record_btn.pack(side=LEFT, padx=6)

        launch_chrome_btn = ttk.Button(controls, text="Launch Chrome (debug)", command=self.launch_chrome_debug)
        launch_chrome_btn.pack(side=LEFT, padx=(12, 0))

        help_btn = ttk.Button(controls, text="Show Chrome Launch Help", command=self.show_launch_help)
        help_btn.pack(side=RIGHT)

        summary_frame = ttk.LabelFrame(main, text="Last run summary", padding=8)
        summary_frame.pack(fill=BOTH, expand=False, pady=(0, 8))
        self.summary_text = scrolledtext.ScrolledText(
            summary_frame,
            height=10,
            wrap="word",
            font=("Segoe UI", 9),
            state="disabled",
        )
        self.summary_text.pack(fill=BOTH, expand=True)

        record_frame = ttk.LabelFrame(main, text="Recorded browser instructions", padding=8)
        record_frame.pack(fill=BOTH, expand=False, pady=(0, 8))
        self.record_text = scrolledtext.ScrolledText(
            record_frame,
            height=10,
            wrap="word",
            font=("Consolas", 9),
            state="disabled",
        )
        self.record_text.pack(fill=BOTH, expand=True)

        log_frame = ttk.LabelFrame(main, text="Log", padding=8)
        log_frame.pack(fill=BOTH, expand=True)

        self.log_text = ttk.Treeview(log_frame, columns=("msg",), show="tree")
        self.log_text.pack(fill=BOTH, expand=True)

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.log("Could not parse config.json, using defaults.")
            return

        prov = (data.get("llm_provider") or DEFAULT_LLM_PROVIDER).strip().lower()
        if prov not in ("openrouter", "inworld"):
            prov = DEFAULT_LLM_PROVIDER
        self.provider_var.set(prov)
        or_key = (data.get("openrouter_api_key") or data.get("api_key", "")).strip()
        self.openrouter_key_var.set(or_key)
        self.inworld_key_var.set((data.get("inworld_api_key") or "").strip())
        self.model_var.set(data.get("model", DEFAULT_MODEL))
        self.cdp_url_var.set(data.get("cdp_url", DEFAULT_CDP_URL))
        self.keep_key_var.set(bool(data.get("keep_api_key", True)))
        task = data.get("task", DEFAULT_TASK)
        self.task_text.delete(0, END)
        self.task_text.insert(0, task)
        max_steps = data.get("max_steps", DEFAULT_MAX_STEPS)
        try:
            ms = int(max_steps)
        except (TypeError, ValueError):
            ms = DEFAULT_MAX_STEPS
        self.max_steps_var.set(str(max(1, min(ms, 5000))))
        recorded_instructions = data.get("recorded_instructions", [])
        if isinstance(recorded_instructions, list):
            self.recorded_instructions = [str(x) for x in recorded_instructions if str(x).strip()]
            self._refresh_recording_text()
        self.log("Loaded config.json")

    def save_config(self):
        try:
            ms = int(self.max_steps_var.get().strip())
        except ValueError:
            ms = DEFAULT_MAX_STEPS
        ms = max(1, min(ms, 5000))
        self.max_steps_var.set(str(ms))

        prov = (self.provider_var.get() or DEFAULT_LLM_PROVIDER).strip().lower()
        if prov not in ("openrouter", "inworld"):
            prov = DEFAULT_LLM_PROVIDER
        keep = self.keep_key_var.get()
        data = {
            "llm_provider": prov,
            "openrouter_api_key": self.openrouter_key_var.get().strip() if keep else "",
            "inworld_api_key": self.inworld_key_var.get().strip() if keep else "",
            "api_key": self.openrouter_key_var.get().strip() if keep else "",
            "keep_api_key": keep,
            "model": self.model_var.get().strip() or DEFAULT_MODEL,
            "cdp_url": self.cdp_url_var.get().strip() or DEFAULT_CDP_URL,
            "task": self.task_text.get().strip() or DEFAULT_TASK,
            "max_steps": ms,
            "recorded_instructions": self.recorded_instructions[-300:],
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.log(f"Saved config to {CONFIG_PATH.name}")

    def validate(self):
        prov = (self.provider_var.get() or DEFAULT_LLM_PROVIDER).strip().lower()
        if prov not in ("openrouter", "inworld"):
            prov = DEFAULT_LLM_PROVIDER
        openrouter_key = self.openrouter_key_var.get().strip()
        inworld_key = self.inworld_key_var.get().strip()
        model = self.model_var.get().strip()
        cdp_url = self.cdp_url_var.get().strip()
        task = self.task_text.get().strip()

        if prov == "openrouter":
            if not openrouter_key:
                messagebox.showerror("Missing API key", "Please enter your OpenRouter API key.")
                return None
        else:
            if not inworld_key:
                messagebox.showerror("Missing API key", "Please enter your Inworld API key.")
                return None
        if not model:
            messagebox.showerror("Missing model", "Please enter a model name.")
            return None
        if not cdp_url:
            messagebox.showerror("Missing CDP URL", "Please enter a Chrome CDP URL.")
            return None
        if not task:
            messagebox.showerror("Missing task", "Please enter a task prompt.")
            return None

        try:
            max_steps = int(self.max_steps_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid max steps", "Max steps must be a whole number (e.g. 50–500).")
            return None
        if max_steps < 1 or max_steps > 5000:
            messagebox.showerror("Invalid max steps", "Max steps must be between 1 and 5000.")
            return None

        return {
            "provider": prov,
            "openrouter_api_key": openrouter_key,
            "inworld_api_key": inworld_key,
            "model": model,
            "cdp_url": cdp_url,
            "task": task,
            "max_steps": max_steps,
        }

    def run_task(self):
        cfg = self.validate()
        if not cfg:
            return

        self.save_config()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_summary_text(f"Running… (max {cfg['max_steps']} steps)\n")
        self.log("Starting Browser Use task...")
        self.log(f"LLM: {cfg['provider']} — model {cfg['model']}")
        self.log(f"Max steps: {cfg['max_steps']}")
        self.runner.start(cfg)

    def refresh_openrouter_models(self):
        key = self.openrouter_key_var.get().strip()
        if not key:
            messagebox.showerror("Missing API key", "Enter your OpenRouter API key first.")
            return
        self.log("Fetching OpenRouter model list (api/v1/models)...")
        self.refresh_openrouter_models_btn.configure(state="disabled")

        def work():
            try:
                choices = fetch_openrouter_models(key)
            except Exception as exc:
                self.root.after(
                    0,
                    lambda: self._openrouter_models_failed(exc),
                )
                return
            self.root.after(0, lambda: self._openrouter_models_ok(choices))

        threading.Thread(target=work, daemon=True).start()

    def _openrouter_models_failed(self, exc: Exception):
        self.refresh_openrouter_models_btn.configure(state="normal")
        self._on_provider_changed()
        self.log(f"OpenRouter model list failed: {exc}")
        messagebox.showerror("OpenRouter models", f"Could not list models:\n\n{exc}")

    def _openrouter_models_ok(self, choices: list[str]):
        self.refresh_openrouter_models_btn.configure(state="normal")
        self._on_provider_changed()
        self._set_model_choices_full(choices)
        if choices:
            current = self.model_var.get().strip()
            if current not in choices:
                self.model_var.set(choices[0])
                self._apply_model_filter()
            self.log(f"OpenRouter: loaded {len(choices)} models — type to filter, then pick from the Model dropdown.")
        else:
            self.log("OpenRouter: model list was empty.")
            messagebox.showinfo("OpenRouter models", "The API returned no models.")

    def refresh_inworld_models(self):
        key = self.inworld_key_var.get().strip()
        if not key:
            messagebox.showerror("Missing API key", "Enter your Inworld API key first.")
            return
        self.log("Fetching Inworld model list (llm/v1alpha/models)...")
        self.refresh_inworld_models_btn.configure(state="disabled")

        def work():
            try:
                choices = fetch_inworld_models(key)
            except Exception as exc:
                self.root.after(
                    0,
                    lambda: self._inworld_models_failed(exc),
                )
                return
            self.root.after(0, lambda: self._inworld_models_ok(choices))

        threading.Thread(target=work, daemon=True).start()

    def _inworld_models_failed(self, exc: Exception):
        self.refresh_inworld_models_btn.configure(state="normal")
        self._on_provider_changed()
        self.log(f"Inworld model list failed: {exc}")
        messagebox.showerror("Inworld models", f"Could not list models:\n\n{exc}")

    def _inworld_models_ok(self, choices: list[str]):
        self.refresh_inworld_models_btn.configure(state="normal")
        self._on_provider_changed()
        self._set_model_choices_full(choices)
        if choices:
            current = self.model_var.get().strip()
            if current not in choices:
                self.model_var.set(choices[0])
                self._apply_model_filter()
            self.log(f"Inworld: loaded {len(choices)} models — type to filter, then pick from the Model dropdown.")
        else:
            self.log("Inworld: model list was empty.")
            messagebox.showinfo("Inworld models", "The API returned no models.")

    def stop_task(self):
        self.runner.stop()

    def on_run_finished(self):
        self.root.after(0, self._set_idle)

    def _set_idle(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def start_recording(self):
        cdp_url = self.cdp_url_var.get().strip()
        if not cdp_url:
            messagebox.showerror("Missing CDP URL", "Please enter a Chrome CDP URL before recording.")
            return
        self.record_btn.configure(state="disabled")
        self.stop_record_btn.configure(state="normal")
        self.log("Starting browser action recorder...")
        self.recorder.start(cdp_url)

    def stop_recording(self):
        self.recorder.stop()

    def on_recording_finished(self):
        self.root.after(0, self._set_recording_idle)

    def _set_recording_idle(self):
        self.record_btn.configure(state="normal")
        self.stop_record_btn.configure(state="disabled")
        self.save_config()

    def clear_recording(self):
        self.recorded_instructions.clear()
        self._last_instruction = ""
        self._refresh_recording_text()
        self.log("Cleared recorded instructions.")

    def use_recording_in_task(self):
        if not self.recorded_instructions:
            messagebox.showinfo("No recording", "Record at least one browser action first.")
            return
        compiled = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(self.recorded_instructions))
        task = (
            "Follow these recorded browser instructions exactly, then continue with user intent:\n\n"
            f"{compiled}"
        )
        self.task_text.delete(0, END)
        self.task_text.insert(0, task)
        self.log("Inserted recording into Task Prompt.")

    def queue_recorded_event(self, event: dict):
        self.recorded_event_queue.put(event)

    def _refresh_recording_text(self):
        body = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(self.recorded_instructions))
        self.record_text.configure(state="normal")
        self.record_text.delete("1.0", END)
        self.record_text.insert("1.0", body)
        self.record_text.configure(state="disabled")

    def _apply_recorded_event(self, event: dict):
        instruction = format_recorded_instruction(event)
        if not instruction:
            return
        if instruction == self._last_instruction:
            return
        self._last_instruction = instruction
        self.recorded_instructions.append(instruction)
        if len(self.recorded_instructions) > 300:
            self.recorded_instructions = self.recorded_instructions[-300:]
        self._refresh_recording_text()

    def show_launch_help(self):
        command = (
            "Start Chrome with remote debugging:\n\n"
            "Windows:\n"
            "  \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" "
            "--remote-debugging-port=9222\n\n"
            "Then keep that Chrome window open and use CDP URL:\n"
            "  http://localhost:9222"
        )
        messagebox.showinfo("How to reuse Chrome login", command)

    def launch_chrome_debug(self):
        port = cdp_port_from_url(self.cdp_url_var.get())
        chrome = next((p for p in iter_chrome_exe_paths() if p.is_file()), None)
        if chrome is None:
            messagebox.showerror(
                "Chrome not found",
                "Could not find chrome.exe. Install Google Chrome or use "
                "\"Show Chrome Launch Help\" and start it manually.",
            )
            self.log("Launch Chrome (debug): chrome.exe not found.")
            return
        try:
            CHROME_CDP_USER_DATA.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    str(chrome),
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={CHROME_CDP_USER_DATA}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            messagebox.showerror("Could not start Chrome", str(exc))
            self.log(f"Launch Chrome (debug): failed — {exc}")
            return
        self.log(
            f"Launched Chrome with remote debugging on port {port} "
            f"(profile: {CHROME_CDP_USER_DATA})."
        )
        self.log("When the window is up, click Run Task. Use this Chrome for logins you need automated.")

    def log(self, msg):
        self.log_queue.put(msg)

    def queue_summary(self, text: str):
        """Thread-safe: worker calls this; main thread applies via poller."""
        self.summary_queue.put(text)

    def _set_summary_text(self, text: str):
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", END)
        self.summary_text.insert("1.0", text)
        self.summary_text.configure(state="disabled")

    def _start_log_poller(self):
        self._drain_log_queue()

    def _drain_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("", "end", text=msg)
            self.log_text.yview_moveto(1.0)

        while True:
            try:
                summary = self.summary_queue.get_nowait()
            except queue.Empty:
                break
            self._set_summary_text(summary)

        while True:
            try:
                event = self.recorded_event_queue.get_nowait()
            except queue.Empty:
                break
            self._apply_recorded_event(event)

        self.root.after(150, self._drain_log_queue)


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    root = Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
