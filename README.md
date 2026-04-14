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

## Notes

- Config is saved in `config.json`.
- If you disable "Store API key in local config.json", the app keeps key only in memory.
- Browser Use uses OpenRouter through its native `ChatOpenRouter` integration.
