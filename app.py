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
# Separate profile so Chrome always exposes CDP even when your normal Chrome is already open.
CHROME_CDP_USER_DATA = CONFIG_PATH.with_name("chrome_cdp_profile")
DEFAULT_CDP_URL = "http://localhost:9222"
DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"
DEFAULT_TASK = "Open the requested website and complete the task safely."
DEFAULT_MAX_STEPS = 100


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
        llm = ChatOpenRouter(
            model=cfg["model"],
            api_key=cfg["api_key"],
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
        self.root.title("Browser Use + OpenRouter (Reuse Chrome Session)")
        self.root.geometry("900x780")

        self.api_key_var = StringVar()
        self.model_var = StringVar(value=DEFAULT_MODEL)
        self.cdp_url_var = StringVar(value=DEFAULT_CDP_URL)
        self.keep_key_var = BooleanVar(value=True)
        self.max_steps_var = StringVar(value=str(DEFAULT_MAX_STEPS))

        self.log_queue = queue.Queue()
        self.summary_queue = queue.Queue()
        self.runner = BrowserUseRunner(self)

        self._build_ui()
        self._load_config()
        self._start_log_poller()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=BOTH, expand=True)

        title = ttk.Label(
            main,
            text="Browser Use Task Runner (OpenRouter + Existing Chrome)",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(fill=X, pady=(0, 10))

        form = ttk.Frame(main)
        form.pack(fill=X)

        ttk.Label(form, text="OpenRouter API Key").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.api_key_entry = ttk.Entry(form, textvariable=self.api_key_var, show="*", width=70)
        self.api_key_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Model").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.model_var, width=70).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Chrome CDP URL").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.cdp_url_var, width=70).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(
            form,
            text="Store API key in local config.json",
            variable=self.keep_key_var,
        ).grid(row=3, column=1, sticky="w", pady=(2, 4))

        ttk.Label(form, text="Max steps (loop limit)").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        self.max_steps_spin = ttk.Spinbox(
            form,
            from_=1,
            to=5000,
            textvariable=self.max_steps_var,
            width=12,
        )
        self.max_steps_spin.grid(row=4, column=1, sticky="w", pady=(4, 8))

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

        self.api_key_var.set(data.get("api_key", ""))
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
        self.log("Loaded config.json")

    def save_config(self):
        try:
            ms = int(self.max_steps_var.get().strip())
        except ValueError:
            ms = DEFAULT_MAX_STEPS
        ms = max(1, min(ms, 5000))
        self.max_steps_var.set(str(ms))

        data = {
            "api_key": self.api_key_var.get().strip() if self.keep_key_var.get() else "",
            "keep_api_key": self.keep_key_var.get(),
            "model": self.model_var.get().strip() or DEFAULT_MODEL,
            "cdp_url": self.cdp_url_var.get().strip() or DEFAULT_CDP_URL,
            "task": self.task_text.get().strip() or DEFAULT_TASK,
            "max_steps": ms,
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.log(f"Saved config to {CONFIG_PATH.name}")

    def validate(self):
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        cdp_url = self.cdp_url_var.get().strip()
        task = self.task_text.get().strip()

        if not api_key:
            messagebox.showerror("Missing API key", "Please enter your OpenRouter API key.")
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
            "api_key": api_key,
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
        self.log(f"Max steps: {cfg['max_steps']}")
        self.runner.start(cfg)

    def stop_task(self):
        self.runner.stop()

    def on_run_finished(self):
        self.root.after(0, self._set_idle)

    def _set_idle(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

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
