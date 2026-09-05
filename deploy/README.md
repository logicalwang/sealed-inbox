# deploy/ — production deployment recipes

These are the sanitized versions of the scripts that run the author's own
deployment on a Termux phone (the receiver, dashboard and tunnel have been
running there since 2026). Copy what you need; every credential lives in a
git-ignored env file, never in the scripts.

## Layout on the device

```
~/apps/sealed-inbox/          a plain `git clone` of this repo (production)
  config.yaml                 real config (git-ignored)
  keys/                       receiver keypair (git-ignored)
  data/                       records.csv, state files, logs, PID files
~/.config/secure-record/      app password, dashboard access key, notify.env
~/.local/bin/cloudflared      the cloudflared binary
```

## The three long-running pieces

| piece | command | what it does |
|---|---|---|
| watcher | `python3 -m src.watcher` | IMAP IDLE → triggers the receiver pipeline |
| dashboard | `python3 -m src.dashboard` | local web UI on `dashboard.port` |
| tunnel | `cloudflared-tunnel.sh` | quick tunnel → public HTTPS URL for the dashboard |

`termux/bg-watchdog.sh` manages all three (start / stop / status / restart),
reports the quick-tunnel URL to Telegram whenever it changes (quick tunnels
rotate), and `termux/start-bg-watchdog.sh` starts everything on device boot
via the Termux:Boot app.

## Setup

1. `cp deploy/termux/env.example ~/.config/secure-record/notify.env` and fill
   in the Telegram token / chat id (leave empty to disable notifications).
2. `chmod +x deploy/termux/*.sh`
3. `deploy/termux/bg-watchdog.sh start` — then `status` should show all three
   running and print the tunnel URL.
4. Boot autostart: install [Termux:Boot](https://wiki.termux.com/wiki/Termux:Boot),
   `mkdir -p ~/.termux/boot && cp deploy/termux/start-bg-watchdog.sh ~/.termux/boot/`.

## Security notes

* The dashboard speaks plain HTTP — the cloudflared quick tunnel is what
  gives you HTTPS. Don't expose the raw port to the internet.
* The tunnel URL + access key is the only thing an outsider needs; the
  dashboard locks an IP out after `dashboard.rate_limit_max` failures.
* `cloudflared-tunnel.sh` contains the proot CA-binding trick that makes
  the Go binary work on bare Termux (cert paths) — see the comments inside.
