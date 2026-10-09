# Auto-start the bench-test TUI on the live console (tty1, or serial for VMs).
# Set NO_BENCH_TUI=1 to get a plain shell instead.
TTY="$(tty 2>/dev/null)"
if [ -t 0 ] && { [ "$TTY" = "/dev/tty1" ] || [ "$TTY" = "/dev/ttyS0" ]; } && [ -z "${NO_BENCH_TUI:-}" ]; then
    exec /usr/local/bin/bench-tui
fi
