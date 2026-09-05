#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot entry — starts the whole stack on device boot.
# Install: mkdir -p ~/.termux/boot && cp start-bg-watchdog.sh ~/.termux/boot/
# Requires the Termux:Boot app.
termux-wake-lock
"$HOME/apps/sealed-inbox/deploy/termux/bg-watchdog.sh" start
"$HOME/apps/sealed-inbox/deploy/termux/bg-watchdog.sh" watchdog &
