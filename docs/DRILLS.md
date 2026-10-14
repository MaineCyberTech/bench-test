# Drills

Exercises for recovery paths that a normal run never reaches. Each drill has
**steps**, the **expected output**, and how to **roll the drill itself back** so a
test stick or runner is left in a known-good state. Commands are the real ones
from `bench/live_update.py` and the live scripts — nothing here is invented.

Audit references: `RES-P2-004` (these paths were never exercised).

---

## Drill 1 — A/B auto-revert after three unconfirmed boots

**What it proves.** `iso_write_slot` writes the *inactive* slot and flips the grub
`active` flag; if the new slot never reaches multi-user, GRUB flips the flag back
and the stick boots the previous image. The mechanism lives in two places:

- `live/ab-layout.sh` (and `benchreport._write_ab_grub_cfg`) write the GRUB env:
  `if [ "${attempts}" -ge 3 ]; then flip active; set attempts=0; save_env; fi`.
- On the live system, `bench-boot-attempt.service` runs `bench-slot attempt`
  early (`Before=multi-user.target`) and `bench-boot-confirm.service` runs
  `bench-slot confirm` at multi-user. An interrupted boot only increments
  `attempts`; a clean boot resets it to `0` and pins `active` to the booted slot.

**Prerequisites.** An A/B stick built with `live/ab-layout.sh`, a staged ISO
(`python bench/live_update.py iso-fetch <tag>`, which on a live system lands in
`/var/tmp/bench-iso-stage` — RAM, never the target stick, which `iso_write_slot`
refuses), and root.

### Steps

1. Baseline the slot state (booted from the good slot, e.g. A):

   ```bash
   sudo bench-slot status
   ```

2. Stage the new image into the inactive slot and flip the flag:

   ```bash
   sudo python bench/live_update.py iso-write-slot \
       /var/tmp/bench-iso-stage/bench-live-nvidia-legacy.iso --yes
   sudo bench-slot status
   ```

3. Drive three unconfirmed boots. On real hardware: boot the stick and cut power
   before the login prompt, three times. For a deterministic dry run (no power
   cycling) use the same helper the boot service uses — the counter is what GRUB
   reads:

   ```bash
   sudo bench-slot attempt   # attempts=1
   sudo bench-slot attempt   # attempts=2
   sudo bench-slot attempt   # attempts=3
   sudo bench-slot status
   ```

4. Let GRUB run once more (reboot, or power-cycle the stick). At menu time it
   sees `attempts >= 3`, flips `active` back to A, resets `attempts=0`, and boots
   slot A; the boot service then increments to 1.

   ```bash
   sudo reboot
   # after it comes back:
   sudo bench-slot status
   ```

### Expected output

```text
# step 1 (baseline)
slot: active=A attempts=0 booted=A

# step 2 (after staging to the inactive slot)
[ab] active=A -> writing slot B (/dev/sdX3)
[ab] slot B updated - reboot to try it (auto-revert after 3 failed boots)
{'ok': True, 'slot': 'B', 'partition': '/dev/sdX3', 'bytes': ..., 'previous': 'A', ...}
slot: active=B attempts=0 booted=A        # still running the old slot until reboot

# step 3 (three unconfirmed boots emulated)
slot: active=B attempts=3 booted=A

# step 4 (GRUB flipped back)
slot: active=A attempts=1 booted=A        # attempts=1 is this boot's increment
```

A clean boot of the *new* slot instead gives `slot: active=B attempts=0 booted=B`
(confirm service ran) — the revert did **not** trigger, which is also a valid
outcome to record.

### Roll the drill back

```bash
# force the known-good slot without another reboot
sudo bench-slot set A                     # active=A attempts=0

# or re-lay the stick from a known-good ISO (rewrites both slots + resets grubenv)
sudo ./live/ab-layout.sh --iso /path/to/known-good.iso /dev/sdX

# drop the staged ISO copy if it was written for the drill
sudo rm -f /var/tmp/bench-iso-stage/bench-live-nvidia-legacy.iso
```

---

## Drill 2 — Live toolkit re-point and rollback

**What it proves.** `update_toolkit` fetches a ref onto BENCHDATA, verifies the
tarball, re-points `/opt/bench-test` at the fetched copy, and keeps the image's
PyTorch reachable; `toolkit_boot_link` repeats the re-point at boot so the update
survives a reboot without reflashing. Rollback re-points at a known-good ref, or
removes the fetched copies so the image's own toolkit is used again.

**Prerequisites.** A live USB (the command refuses a non-live checkout unless
`--force` / `BENCH_ALLOW_TOOLKIT_SWITCH=1`), BENCHDATA mounted, network access,
and root. Know a **known-good ref** (a tag or commit SHA) before you start.

### Steps (forward)

1. Baseline what is in use:

   ```bash
   readlink -f /opt/bench-test
   cat /opt/bench-test/.bench-version
   ```

2. Fetch a ref and switch onto it:

   ```bash
   sudo python bench/live_update.py toolkit --ref main
   ```

3. Verify the re-point and the stamp:

   ```bash
   readlink -f /opt/bench-test
   cat /opt/bench-test/.bench-version
   ```

4. Verify the image's PyTorch is still importable after the re-point:

   ```bash
   sudo python bench/live_update.py torch-link
   python -c "import torch; print(torch.__version__)"
   ```

5. Verify it survives a reboot (`bench-agent-env` runs `boot-link` at boot):

   ```bash
   sudo reboot
   # after it comes back:
   readlink -f /opt/bench-test
   ```

### Expected output

```text
# step 1 (image's own toolkit: a directory, not a symlink)
/opt/bench-test
{"mode":"iso","ref":"main","commit":"...","built":"..."}

# step 2
[update] fetching https://codeload.github.com/MaineCyberTech/bench-test/tar.gz/main
[update] main -> /mnt/benchdata/bench-toolkit/main (switched=True)
{'ok': True, 'ref': 'main', 'dest': '/mnt/benchdata/bench-toolkit/main',
 'current': '/mnt/benchdata/bench-toolkit/current', 'switched': True,
 'commit': '...', 'restart_required': True}

# step 3
/mnt/benchdata/bench-toolkit/main
{"mode":"copy","ref":"main","commit":"...","sha256":"...","fetched_at":...}

# step 4 (torch-link prints nothing when the stable path is already linked)
# a link that had to be made prints:  [torch] <src> -> /opt/bench-pypackages
2.x.y+cu126

# step 5 (after reboot)
/mnt/benchdata/bench-toolkit/main
```

A `[torch] <src> -> /opt/bench-pypackages` line appears during step 2 only when
the image's PyTorch had to be bound to the stable path; newer images already bake
it at `/opt/bench-pypackages`, so that line is a no-op there.

On a non-live checkout step 2 refuses instead, which is the expected guard:

```text
{'kind': 'install', 'toolkit': '/path/to/checkout',
 'error': "not a live runner - update the checkout with 'update' (git pull) "
          "or set BENCH_ALLOW_TOOLKIT_SWITCH=1"}
```

### Roll the drill back

```bash
# (a) re-point at the last known-good ref (fetches into its own dir + flips current)
sudo python bench/live_update.py toolkit --ref <known-good-sha-or-tag>

# (b) or drop the fetched copies and re-point at the newest remaining one
sudo rm -rf /mnt/benchdata/bench-toolkit/<bad-ref>
sudo python bench/live_update.py boot-link

# (c) or return to the image's own toolkit entirely (no reflash): remove the
#     persisted copies and reboot - boot-link finds nothing and leaves
#     /opt/bench-test as the image built it
sudo rm -rf /mnt/benchdata/bench-toolkit
sudo reboot

# leave PyTorch reachable whichever copy is in use
sudo python bench/live_update.py torch-link
```

After (c), `readlink -f /opt/bench-test` prints `/opt/bench-test` (the image
directory, not a symlink) — the machine is back on the toolkit it booted with.
