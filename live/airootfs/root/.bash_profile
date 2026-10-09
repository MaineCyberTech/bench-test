# Auto-start the bench-test TUI on the live console (tty1).
# Set NO_BENCH_TUI=1 to get a plain shell instead.
if [ -t 0 ] && [ "$(tty 2>/dev/null)" = "/dev/tty1" ] && [ -z "${NO_BENCH_TUI:-}" ]; then
    exec /usr/local/bin/bench-tui
fi
