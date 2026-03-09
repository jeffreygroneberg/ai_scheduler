# 🤖 Page Change Checker

An autonomous AI agent that monitors any webpage for content changes and sends email notifications with screenshots when differences are detected.

Built with the **GitHub Copilot SDK**, **Playwright MCP**, and **GitHub Actions** — the entire agent runs serverlessly on a cron schedule with zero infrastructure to manage.

---

## How It Works

```
GitHub Actions (cron every 5 min)
  │
  ▼
┌─────────────────────────────────────────────┐
│  Copilot SDK Agent (gpt-5.2)                │
│                                             │
│  1. web_fetch → get page content            │
│  2. CompareContentOfPage → SHA-256 diff     │
│  3. Decision: changed?                      │
│  4. Playwright MCP → full-page screenshot   │
│  5. SendMailTo → SMTP email + attachment    │
│  6. ReportResult → structured output        │
└─────────────────────────────────────────────┘
  │
  ▼
Snapshot persisted on `data` branch
```

The AI agent **reasons through** a structured prompt and decides autonomously whether changes are meaningful (ignoring whitespace-only diffs). When real content changes are found, it takes a screenshot of the page via a headless browser, summarizes the diff, and emails the notification.

---

## Architecture

### Copilot SDK

The agent is constructed via a single `create_session()` call:

```python
session = await client.create_session({
    "model": "gpt-5.2",
    "reasoning_effort": "high",
    "streaming": True,
    "tools": [CompareContentOfPage, SendMailTo, ReportResult],
    "mcp_servers": {
        "playwright": {
            "type": "local",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless", "--output-dir", ...],
            "tools": ["browser_navigate", "browser_take_screenshot"],
        },
    },
})
```

### Built-in Tools

By default, the Copilot SDK operates with `--allow-all`, enabling all first-party tools from the Copilot CLI. This gives the agent access to a rich set of capabilities out of the box:

| Category | Tools | Description |
|----------|-------|-------------|
| **File System** | `view`, `edit`, `create_file`, `glob` | Read, write, create, and find files in the working directory |
| **Shell** | `bash` | Execute shell commands |
| **Search** | `grep` | Search file contents with pattern matching |
| **Web** | `web_fetch` | Fetch webpage content via HTTP (used by this agent for page monitoring) |
| **User Interaction** | `ask_user` | Request input from the user (enabled via `on_user_input_request` handler) |

You can control which tools are available using `available_tools` (whitelist) or `excluded_tools` (blacklist) in the session config. Setting `available_tools: []` disables all built-in tools — useful when you want to provide only custom tools.

This agent uses `web_fetch` to retrieve page content and relies on three additional custom tools for its domain-specific logic.

### Custom Tools (`@define_tool`)

| Tool | Purpose |
|------|---------|
| `CompareContentOfPage` | Compares current page text against stored snapshot using SHA-256 hashing and unified diff |
| `SendMailTo` | Sends email notifications via SMTP with optional screenshot attachment |
| `ReportResult` | Returns a structured Pydantic result (changes detected, added/removed lines, email status) |

### MCP Server

The **Playwright MCP server** runs as a local process and exposes two whitelisted tools to the agent:

- `browser_navigate` — opens the URL in a headless Chromium browser
- `browser_take_screenshot` — captures a full-page screenshot for the email

Content fetching uses the built-in `web_fetch` tool (not the browser), keeping browser usage strictly for screenshots.

### State Management

The page snapshot (`previous_snapshot.txt`) is persisted on a separate `data` branch in the repository. This keeps the `main` branch clean while giving the agent persistent state across runs.

---

## Fork & Customize

### 1. Fork the Repository

Click **Fork** on GitHub to create your own copy.

### 2. Create the `data` Branch

```bash
git checkout --orphan data
git rm -rf .
git commit --allow-empty -m "init data branch"
git push origin data
```

### 3. Set the Page URL

The URL to monitor is configured via the `PAGE_URL` environment variable.

**Option A — Repository secret (recommended):**

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `PAGE_URL` | The full URL of the page you want to monitor |

Then add it to the workflow's `env` block:

```yaml
- name: Run agent
  env:
    PAGE_URL: ${{ secrets.PAGE_URL }}
    # ... other secrets
```

**Option B — Edit the default directly** in `checkPageChanges.py`:

```python
URL = os.getenv("PAGE_URL", "https://example.com/your-page")
```

### 4. Configure Email (SMTP)

Add these **repository secrets** under **Settings → Secrets and variables → Actions**:

| Secret | Description | Example |
|--------|-------------|---------|
| `MAIL_SERVER` | SMTP server hostname | `smtp.gmail.com` |
| `MAIL_PORT` | SMTP port (TLS) | `587` |
| `MAIL_USERNAME` | SMTP login username | `you@gmail.com` |
| `MAIL_PASSWORD` | SMTP login password or app password | `abcd efgh ijkl mnop` |
| `MAIL_FROM` | Sender address | `you@gmail.com` |
| `NOTIFY_EMAIL` | Recipient address(es) | `alert@example.com` |

> **Gmail users:** Use an [App Password](https://support.google.com/accounts/answer/185833) instead of your regular password.

### 5. Configure the Copilot Token

Add a `COPILOT_TOKEN` secret with a GitHub token that has Copilot access.

| Secret | Description |
|--------|-------------|
| `COPILOT_TOKEN` | GitHub token with Copilot API access |

#### Why is the Copilot CLI required?

The Python package (`from copilot import CopilotClient`) is a thin wrapper — it does **not** bundle the Copilot runtime itself. Instead, it communicates with a **local CLI binary** (`@github/copilot`) that handles authentication, token exchange, and the actual API calls to the Copilot backend. Without the CLI binary present, `CopilotClient` has no way to reach the Copilot service.

That's why the workflow (and local setup) both run:

```bash
npm install @github/copilot @playwright/mcp@latest
```

The `@github/copilot` npm package contains the platform-specific CLI binary. In GitHub Actions the path is set explicitly via `COPILOT_CLI_PATH` so the Python SDK knows where to find it:

```yaml
COPILOT_CLI_PATH: ${{ github.workspace }}/node_modules/@github/copilot-linux-x64/copilot
```

On macOS/local dev the SDK auto-detects the binary, so `COPILOT_CLI_PATH` is optional.

### 6. Adjust the Schedule

Edit `.github/workflows/checkPageChanges.yml` to change the cron frequency:

```yaml
on:
  schedule:
    - cron: "*/5 * * * *"   # Every 5 minutes
    # - cron: "0 * * * *"   # Every hour
    # - cron: "0 9 * * *"   # Daily at 9 AM UTC
```

### 7. Customize the Prompt (Optional)

The agent's behavior is controlled by the `_build_prompt()` function in `checkPageChanges.py`. You can modify it to:

- Change the language of summaries (currently German)
- Adjust what counts as a "meaningful" change
- Customize the email subject line and body format
- Add additional decision logic

---

## Run Locally

```bash
# Clone and set up
git clone https://github.com/<your-username>/ai_scheduler.git
cd ai_scheduler
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install @github/copilot @playwright/mcp@latest
npx playwright install chrome

# Configure environment
cp .env.example .env   # or create .env manually
# Add: PAGE_URL, COPILOT_TOKEN (or GITHUB_TOKEN), MAIL_* variables

# Run
python checkPageChanges.py
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PAGE_URL` | No | URL to monitor (has a default fallback) |
| `COPILOT_TOKEN` or `GITHUB_TOKEN` | Yes | GitHub token with Copilot access |
| `MAIL_SERVER` | Yes | SMTP server hostname |
| `MAIL_PORT` | Yes | SMTP port |
| `MAIL_USERNAME` | Yes | SMTP username |
| `MAIL_PASSWORD` | Yes | SMTP password |
| `MAIL_FROM` | No | Sender address (defaults to `MAIL_USERNAME`) |
| `NOTIFY_EMAIL` | Yes | Comma-separated recipient email(s) |
| `COPILOT_CLI_PATH` | No | Path to Copilot CLI binary (auto-detected if not set) |

---

## Project Structure

```
ai_scheduler/
├── checkPageChanges.py           # AI agent — tools, prompt, and main loop
├── requirements.txt              # Python dependencies
├── previous_snapshot.txt         # Last known page content (persisted on data branch)
├── screenshots/                  # Playwright screenshots (gitignored)
├── architecture.excalidraw       # Architecture diagram (Excalidraw)
└── .github/
    └── workflows/
        └── checkPageChanges.yml  # GitHub Actions cron workflow
```

---

## How the Diff Works

1. The agent fetches the page content via `web_fetch` (plain text, no browser)
2. `CompareContentOfPage` normalizes whitespace, computes SHA-256 hashes of old and new content
3. If hashes differ → generates a unified diff, categorizes lines as **removed** or **added**
4. The snapshot file is updated immediately
5. The AI decides: if only whitespace changed → no notification. If real content changed → screenshot + email

---

## License

MIT
