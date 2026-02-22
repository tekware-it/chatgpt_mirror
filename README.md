# ChatGPT Mirror (PySide6 MVP)

Native desktop viewer (left pane) + embedded `chatgpt.com` webview (right pane) for mirroring already-rendered ChatGPT conversation DOM content into a smooth local list UI.

## Features (MVP)

- Left pane native message viewer with:
  - role badge + message index
  - `Copy` and `Collapse/Expand` per message
  - text paragraphs
  - extracted `<pre><code>` code blocks in monospace UI with `Copy code`
- Right pane `QWebEngineView` loading `https://chatgpt.com` for login/SSO/debugging
- JS extractor runs inside the page and sends deltas only via `QWebChannel`
- Best-effort DOM pruning in the webview (keeps last 30 message nodes, replaces older ones with placeholders)

## Requirements

- Ubuntu 22.04
- Python 3
- `pip`

## Install

```bash
python3 -m pip install --upgrade pip
python3 -m pip install PySide6 PySide6-Addons
```

Notes:
- `QtWebEngine` lives in PySide6 addons on many installs.
- If your system is missing runtime Qt/XCB libs, install Ubuntu packages such as `libxcb-cursor0`.

## Run

```bash
python3 chatgpt_mirror.py
```

## Usage

1. Log into ChatGPT in the right pane.
2. Open a conversation on `chatgpt.com`.
3. The left pane mirrors messages as they appear in the DOM.
4. Use `Copy`, `Collapse/Expand`, and `Copy code` buttons in the native viewer.

## Notes / Tuning

- ChatGPT DOM structure changes over time. The extractor uses best-effort selectors and isolates them in JS functions for easy tweaking.
- This MVP intentionally does not call any OpenAI APIs or private endpoints.
- The left pane uses a Qt list model with per-row widgets (pragmatic MVP). For very large histories, a custom painted delegate/editor approach would be the next optimization step.
