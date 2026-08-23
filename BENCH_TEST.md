# BENCH_TEST.md — hardware-in-the-loop checklist

TL;DR: nothing in this file runs in `run_tests.py`. These are manual,
operator-present checks on the RHEL9 bench. Sections are cumulative:
**no specimen of value is energized before §T passes.** Check boxes get
initials + date, same as the Digital Multitool convention.

Probes live in `bench/` and print PASS/FAIL evidence; each section names
its probe.

## §P — BK4055B burst semantics (probe: `bench/probe_burst_idle.py`, no HV: Trek OFF or disconnected)

- [ ] STPS accepted: `C<ch>:BTWV STPS,270` readback shows 270 (or probe
      reports firmware rejects it → burst mode is BANNED on this box,
      engine auto-selects `timed`)
- [ ] Idle level: burst armed, output ON, scope MEAN at SG output ≈
      waveform minimum (NOT the offset midpoint) before and after a
      fired burst
- [ ] NCYC max: probe binary-search result recorded here: ______
- [ ] MTRIG-to-first-edge latency recorded at 1 Hz / 10 Hz: ______
- [ ] FREQUENCY verification: scope reads f within ±2% during bursts at
      0.1 / 1 / 5 / 10 Hz

## §Q — kill paths (probe: `bench/probe_kill_path.py`, dummy load, low voltage)

- [ ] Abort mid-burst → SG output OFF then DC 0; scope MEAN confirms 0 V
- [ ] SIGTERM during RUNNING → engine ramps/zeroes within 3 s grace,
      checkpoint written, exit code nonzero-clean
- [ ] USB/LAN cable pulled mid-run → NOT_ZEROED terminal state, loud in
      console + status.json; operator instruction displayed
- [ ] Resume after deliberate kill: RECHAR gate runs before any HV re-arm

## §R — dry rehearsal on hardware

- [ ] Full LIFE-DEA recipe in `--mode dry` on the bench: sequencing,
      camera captures, scope reads, CSV/status writes all real; SG never
      commanded (verify with scope on SG output the whole run)

## §S — live on resistor dummy load (~100 MΩ HV probe-safe load, ≤200 V-scale levels)

- [ ] Watchdog baseline learn completes; credible-baseline gate exercised
- [ ] Forced trip (parallel resistor switch-in) → abort + trace freeze +
      events row; HV zeroed; post-mortem data present

## §T — sacrificial disc end-to-end

- [ ] Short LIFE-DEA variant (≤ 30 min) on a sacrificial planar disc:
      break-in, baseline interlude, cycle blocks, fast interludes, clean
      completion, report generated
- [ ] Deliberate mid-run kill -9 → resume with RECHAR pass → run
      completes; cycle ledger reconciles (blocks.csv sums = checkpoint)

## §U — bender fixture calibration (after vision phase)

- [ ] Angle gauges 0/15/30/45°: |bias| < 0.5°, σ < 0.2° per mount set
- [ ] Micrometer tip displacement 0.50/1.00/2.00 mm: |err| < 0.05 mm + 1%
- [ ] ArUco mm/px vs gauge block: ±0.5%
- [ ] 24 h drift soak: tag shift < 2 px, bg median drift < 10%; δ noise
      floor recorded → AMBER threshold floor set in fixture config
