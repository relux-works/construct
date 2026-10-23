# Changelog

## Unreleased

- Extract persistent developer/reviewer lanes into one configurable kit.
- Add initialization, separate-git-dir worker setup, thread capture and serialized assignments.
- Bind every task run to its exact request event and task id instead of the latest inbox entry.
- Add signed exact-head landing, accepted-verdict Path B rebases and FIFO host slots.
- Add private run artifacts, explicit worker environments, negative-first local fixtures and narrowing mutants.
- Narrow Path B documentation exceptions to exact root doc filenames and docs/**.
- Preserve concurrently advanced feature refs with atomic reviewed-head deletion leases.
- Refuse landing verdicts whose optional review base is not on the default branch (exit 8).
- Bind main updates atomically to the SHA used for final ancestry/signature checks;
  refuse late main changes and skip feature cleanup with exit 9.
