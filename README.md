# Woogles issue bot

Tag the bot in Discord (usually as a **reply** to someone's bug report) and it:

1. reads the report plus recent channel context,
2. picks the right repo, searches GitHub for duplicates,
3. asks you a question in a thread if anything is unclear,
4. files a clean issue, or comments on the existing one if it's a dupe,
5. optionally asks Claude Code (GitHub Action) to open a PR when you say "fix it".

It runs Claude Haiku 5.5 through the Anthropic SDK's Tool Runner, roughly half a cent per report.

```
You (reply to a report):  @IssueBot file this
Bot (in a new thread):    Filed woogles-io/liwords#1234: "Timer keeps running after resign on mobile".
You (in thread):          also have Claude fix it
Bot:                      Asked Claude for a PR on #1234.
```

## 1. Create the Discord bot

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token**, copy it. Turn **Public Bot** off. Turn **Message Content Intent** on.
3. **OAuth2 → URL Generator**: scopes `bot`; permissions **View Channels, Send Messages,
   Send Messages in Threads, Create Public Threads, Read Message History, Add Reactions**.
4. Open the generated URL and add the bot to your server.

## 2. Get the other two keys

- **Anthropic API key**: console.anthropic.com → API Keys. If you're on Max, claim your monthly
  API credits into the same Console organization.
- **GitHub token**: github.com/settings/personal-access-tokens → *Fine-grained*.
  Repository access: only the repos you list in `config.toml`. Permission: **Issues: Read and write**.
  Issues will be filed as you.

## 3. Configure

```bash
cp .env.example .env                 # paste the three secrets
cp config.example.toml config.toml   # your Discord user ID + repos
```

In `config.toml`, the repo descriptions matter: they're how the agent routes a report.

## 4. Run on the Raspberry Pi

Use 64-bit Raspberry Pi OS (Pi 3/4/5). Docker is easiest:

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # once, then log out/in
git clone https://github.com/domino14/discord-issuebot && cd discord-issuebot
docker compose up -d --build
docker compose logs -f               # should say "Logged in as IssueBot#1234"
```

It restarts on crash and on reboot. Update after editing code: `docker compose up -d --build`.
After editing only `config.toml`: `docker compose restart`.

**Without Docker** (Python 3.11+):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a; .venv/bin/python issuebot.py
```

For always-on, use `issuebot.service` (instructions inside the file).

## 5. Try it safely first

Set `DRY_RUN=1` in `.env` and restart. The bot searches GitHub and drafts everything, but
"creates" fake `DRY-RUN` links instead of writing. Set it back to `0` when happy.

## 6. Optional: Claude Code PRs

Copy `github-workflow-claude.yml` to `.github/workflows/claude.yml` in each repo, set your
GitHub username in it, and add the `CLAUDE_CODE_OAUTH_TOKEN` secret (from `claude setup-token`)
or an API key. Running `/install-github-app` in Claude Code does the app + secret for you.
Then add yourself to `pr_user_ids` in `config.toml`.

The bot's comment is posted with your GitHub token, so it triggers the workflow as you.

## How it behaves

- Instead of replying, you can paste message links: `@IssueBot https://discord.com/channels/…`.
  It reads the linked message plus a few before it (up to 3 links, same server, and only from
  channels both you and the bot can read).
- Only users in `allowed_user_ids` can trigger it; everyone else is ignored.
- It replies in a thread. For 24 hours, your follow-ups in that thread need no tag.
- It can search, read, create issues and comment. It can't close, edit, label-manage or delete
  anything, and it treats quoted community messages as data, not instructions.
- Bot replies never ping anyone (`@everyone` and user mentions are disabled).
- Sessions live in memory, so a restart forgets open threads; just tag the bot again.

## Files

| File | What it is |
|---|---|
| `issuebot.py` | Discord listener, threads, follow-ups |
| `agent.py` | System prompt, GitHub tools, Tool Runner loop |
| `github_api.py` | Small async GitHub REST client |
| `config.py` | Loads `config.toml` + `.env` |

## License

MIT, see [LICENSE](LICENSE).
