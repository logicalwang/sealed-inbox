#!/usr/bin/env bash
# Quick cloudflared tunnel on Termux → the dashboard.
#
# The interesting bit: cloudflared is a Go binary that reads CA certs at
# /etc/ssl/certs/ca-certificates.crt (Debian layout). Termux keeps them at
# $PREFIX/etc/tls/cert.pem, which cloudflared can't find. We bind the
# Termux CA into the Debian paths via proot and export SSL_CERT_FILE —
# this is what makes `cloudflared` work on a bare Termux install.
#
# Requires: pkg install proot; cloudflared binary in ~/.local/bin
#           (aarch64 build from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
set -euo pipefail

TERMUX_CA="$PREFIX/etc/tls/cert.pem"
CF="$HOME/.local/bin/cloudflared"
PORT="${PORT:-8086}"

exec proot \
    -b "$PREFIX/etc/tls/cert.pem:/etc/ssl/certs/ca-certificates.crt" \
    -b "$PREFIX/etc/tls/cert.pem:/etc/pki/tls/certs/ca-bundle.crt" \
    -b "$PREFIX/etc/tls/cert.pem:/etc/ssl/ca-bundle.pem" \
    env SSL_CERT_FILE="$TERMUX_CA" \
        "$CF" tunnel --url "http://localhost:${PORT}"
