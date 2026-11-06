# Watch mode retains stopped Temporal workers after reload

## Summary

With `--watch`, repeated workflow-file changes can grow Zigflow's live Go heap
and process RSS. The `(worker, activity name)` keys in the global
`activityRegistry` keep stopped Temporal workers reachable after each reload.
This was reproduced locally using `v0.14.2` with 955 workflow files. Watch
mode is documented as not recommended for production; the retention also
affects local development and can compound memory pressure where watch is used.

## Reproduction

1. Run a local Temporal dev server and a locally built Zigflow worker with
   `--watch` on a directory containing activity-registering workflows.
2. Record `/proc/1/status` `VmRSS` and `go_memstats_heap_alloc_bytes` from the
   worker process. Trigger several reloads by rewriting a workflow file.
3. Capture Go heap profiles with GC before and after the reloads using a
   temporary **local-only** `net/http/pprof` listener.

The exact commands, synthetic corpus generator, sampling script, raw metrics,
and four heap profiles are in [README.md](README.md). The private workflow
corpus is omitted, so outside maintainers should not expect the same absolute
memory values from the synthetic one-step workflows.

## Observed result

- In one local run with 11 pairs of file writes, process RSS rose from
  133.55 MiB to 1722.13 MiB and live Go heap from 47.24 MiB to 824.19 MiB.
  The GC count rose from 390 to 952, so GC was running during the growth.
- After three reload pairs, the unmodified build's post-GC heap profile rose
  from 44.66 MiB to 250.16 MiB. With test-only cleanup of stopped-worker
  registry entries, it was 42.47 MiB before and 42.03 MiB after.
- The profile shows allocation sites in workflow parsing and building; the
  registry reference is identified by code inspection and the controlled
  cleanup comparison. The profile alone cannot identify the retaining edge.

## Expected result

After the old worker stops and GC runs, memory associated only with that
worker should become collectible. Activity registration deduplication should
still work for the currently active worker.

## Proposed change

Delete a stopped worker's entries from `activityRegistry` after `Stop()`.
Also delete entries for workers abandoned when workflow building, health-check
setup, or worker start fails. Regression tests verify that deduplication for a
worker still holds, stopped-worker entries disappear, and active-worker entries
remain.

This evidence supports this retention path as a substantial local contributor.
It does not show that every byte observed in production came from this path.
