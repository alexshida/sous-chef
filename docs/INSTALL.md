# Installing sous-chef

About ten minutes, start to finish. You don't need to know how to code. You'll
copy a few lines into Terminal, and this guide says exactly which ones.

**You need:** a Mac (macOS 13 or newer) or a Linux computer. On Windows, use
[WSL](https://learn.microsoft.com/windows/wsl/install) and follow the Linux notes.

**Optional:**
- A Claude subscription, for ✨ recipe ideas written for you. Without it, you still
  get the built-in recipe library and everything else.
- An iPhone, to use sous-chef from the grocery store.

---

## 1. Open Terminal

Press **⌘ Space**, type **Terminal**, press **Return**. A window with a text prompt
opens. Everything below is typed (or pasted) there and run with **Return**.

## 2. Download sous-chef

Copy this line, paste it into Terminal (⌘ V) and press Return:

```bash
git clone https://github.com/alexshida/sous-chef.git ~/sous-chef && cd ~/sous-chef
```

> **First time using git on this Mac?** A box may pop up asking to install
> "command line developer tools". Click **Install**, wait for it to finish (a few
> minutes), then paste the line above again.

This puts sous-chef in a folder called `sous-chef` in your home folder.

## 3. Install

```bash
./install.sh
```

It finds Python, sets up a private environment inside the `sous-chef` folder,
installs everything, and checks the result. When it asks:

> Keep sous-chef running in the background, starting at login? [Y/n]

press **Return** for yes. sous-chef then runs quietly whenever your Mac is on, and
the installer opens it in your browser.

> **"sous-chef needs Python 3.10 or newer"?** Macs come with an older Python.
> Download the current one from [python.org/downloads](https://www.python.org/downloads/)
> (the big yellow button), install it, open a **new** Terminal window, then
> `cd ~/sous-chef` and run `./install.sh` again.

## 4. Open it

Go to **[http://localhost:8766](http://localhost:8766)** in your browser. You'll see
next week's plan with a few suggestions already waiting. Pick some, and the Shop tab
has your grocery list.

If you said no to the background service, start sous-chef yourself each time with:

```bash
cd ~/sous-chef && .venv/bin/sous-chef web
```

Keep that window open while you use it.

---

## Optional: ✨ recipe ideas from Claude

The ✨ buttons (*New ideas*, *Craft*, *Import*) use [Claude Code](https://code.claude.com/docs/en/setup)
on your Claude subscription, so no API key and nothing extra to pay. Install it:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then run `claude` once and sign in when it asks. Type `/exit` to leave. That's all:
sous-chef finds it from then on. If you installed the background service *before*
installing Claude Code, run `./install.sh --service` once more so the service can find it.

## Optional: use it on your iPhone

sous-chef runs on your computer. Your phone reaches it through
[Tailscale](https://tailscale.com/download), a free private network between your own
devices. Nothing is opened to the internet.

1. Install Tailscale on your computer and on your iPhone (App Store), and sign in
   to **the same account** on both.
2. On the computer, run `~/sous-chef/.venv/bin/sous-chef doctor`. Under
   *Phone access* it prints a link like `http://100.101.102.103:8766`.
3. Open that link in **Safari** on your phone, tap **Share → Add to Home Screen**.

It now opens like an app. Your computer needs to be on and awake for it to load.
The shopping list keeps a copy on the phone, though, so it still opens in a store
with no signal.

## Optional: connect your calendar

So sous-chef knows which evenings you're busy or out, paste your calendar's private
feed link into **Settings → Calendar**:

- **Google Calendar:** calendar.google.com → ⚙️ Settings → click your calendar →
  *Integrate calendar* → copy **Secret address in iCal format**.
- **iCloud:** Calendar app → right-click the calendar → *Share Calendar…* → tick
  **Public Calendar** → copy the link.

---

## Updating

```bash
cd ~/sous-chef && git pull && ./install.sh
```

The installer updates everything and restarts the background service.

## Checking that everything works

```bash
~/sous-chef/.venv/bin/sous-chef doctor
```

It checks Python, your data, Claude, the server, phone access and the background
service. For anything missing or broken, it says what to do.

## Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: sous-chef` | Use the full path, `~/sous-chef/.venv/bin/sous-chef`, or run `source .venv/bin/activate` inside the folder first. Your prompt then shows `(sous-chef)`. |
| The page won't load | Run `sous-chef doctor`. If *Server* says nothing is answering, run `sous-chef restart` (background service) or start `sous-chef web`. |
| "Could not listen on port 8766" | sous-chef is already running, probably as the background service. Just open the page. |
| ✨ buttons say Claude wasn't found | Install Claude Code (above), sign in with `claude`, then run `./install.sh --service` so the background service picks it up. |
| Phone says it can't connect | Is Tailscale on and signed in on both devices, with the same account? Is the computer awake? Run `sous-chef doctor` for the right link. |
| Something else | Check the log: `tail -50 ~/.sous-chef/web.log` |

## Uninstalling

```bash
~/sous-chef/.venv/bin/sous-chef install-service --remove   # stop the background service
rm -rf ~/sous-chef                                         # the app
rm -rf ~/.sous-chef                                        # your plans, prices and settings
```
