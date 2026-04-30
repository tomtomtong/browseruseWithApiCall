# Browser Use Desktop Runner

Simple Python UI to:

- set your OpenRouter API key
- choose a model
- set a task prompt
- connect Browser Use to your **existing Chrome session** (so you stay logged in)

## 1) Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2) Start Chrome with remote debugging

Close other Chrome instances first (recommended), then run:

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

Log in to your websites in this Chrome window one time.

## 3) Run app

```bash
python app.py
```

In the UI:

- paste your OpenRouter API key
- set model (example: `anthropic/claude-3.5-sonnet`)
- keep CDP URL as `http://localhost:9222` unless changed
- enter your task prompt
- click **Run Task**

## Record browser actions as instructions (no video)

You can capture manual browser actions and reuse them as text instructions for the LLM:

1. Launch Chrome with debugging and keep that browser open.
2. In the app, click **Start Recording**.
3. Perform your flow manually in Chrome (clicks, field changes, submits, navigation).
4. Click **Stop Recording**.
5. Review steps in **Recorded browser instructions**.
6. Click **Use Recording in Task** to insert them into the task prompt.

Notes:

- Password field values are stored as `"[REDACTED]"`.
- Recorded instructions are saved in `config.json` under `recorded_instructions`.
- Use **Clear Recording** to reset captured steps.

## Notes

- Config is saved in `config.json`.
- If you disable "Store API key in local config.json", the app keeps key only in memory.
- Browser Use uses OpenRouter through its native `ChatOpenRouter` integration.
