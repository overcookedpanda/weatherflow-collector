# Contributing

Thanks for helping. A few rules keep this easy to maintain:

- **Conventional commits** (`feat:`, `fix:`, `build:`, `ci:`, `docs:`,
  `refactor:`, `test:`, `chore:`). PRs are squash-merged, so the PR title
  should follow the same format.
- **Tests:** add or update tests under `tests/` for behavior changes, and run
  `ruff check . && pytest -q` before opening a PR.
- **No personal data:** never commit tokens, station, device, hub or serial
  numbers, coordinates, elevation, station names, IPs or hostnames, in code,
  configs, dashboards, test fixtures or screenshots. Use the placeholders in
  `.env.example`. Recorded sample messages must be scrubbed to fake serials,
  IDs and coordinates first.
- **Secret scanning:** install the hooks once with
  `pip install pre-commit && pre-commit install`. CI runs the same gitleaks
  scan on every PR.
- **Dashboards:** export with templated data source and station variables and
  no default values.

By contributing you agree your contributions are licensed under AGPL-3.0.
