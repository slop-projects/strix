# Strix code review and improvement roadmap

This is a focused static review of the local checkout. Proposed improvements below have not been implemented or verified as runtime behavior.

## Existing foundation

The Python application separates agent orchestration, runtime backends, tools, reporting, and interfaces. The repository also includes a Go terminal interface and a React local viewer. Existing capabilities include scan resumption, model connection preflight, structured findings, and SARIF output. These should be improved where necessary rather than duplicated.

Local changes already present include read-only quality checks, a viewer request size cap, Docker security documentation, and release smoke checks. Previous test results were supplied in a completion report; this review does not independently certify those results.

## Recommended next changes

| Priority | Improvement | Evidence and benefit | Acceptance criteria |
| --- | --- | --- | --- |
| 1 | A standalone setup diagnostic command | Startup validation is spread across `strix/interface/environment.py`, `scan_setup.py`, and `main.py`. A diagnostic command would explain setup problems before starting a scan. | Check Docker reachability, image availability, model configuration, writable output paths, and optional tools; offer an offline mode; redact credentials; make any billable model connection check explicit. |
| 1 | A scan-copy option for local targets | `strix/runtime/session_manager.py` binds local source trees read-write. A copied workspace would let users review changes before applying them to their original source. | Explicitly choose a source copy or the existing bind mount, protect metadata, document output and patch retrieval, and support cleanup and resume. |
| 1 | A trustworthy quality baseline | The supplied completion report described existing Pyright and Bandit failures. A failing baseline makes new regressions harder to distinguish. | Reproduce failures in the supported environment, fix or narrowly justify them, and run the read-only quality checks and relevant tests in CI. Do not disable broad checks to achieve a passing result. |
| 2 | Release checks specific to this fork | `scripts/release_smoke.py` currently targets the upstream GitHub releases and GHCR image. Passing those checks does not prove that this copy has a working release. | After choosing a publishing owner and release process, distinguish upstream dependencies from fork-owned artifacts and verify the correct assets. |
| 2 | Automated viewer build validation | `strix/interface/viewer/frontend/package.json` provides build scripts but no dedicated test command. The viewer handles authentication state and scan steering. | Add targeted checks for auth state, steering errors, transcript rendering, and agreement between committed static assets and source. |

## Publishing notes

Publish this checkout under the chosen GitHub account while retaining upstream history, the Apache license, and author attribution. The README uses an existing local logo asset so its header does not depend on a remote banner. It explains how to run the fork's source and distinguishes the original hosted platform from this copy.

Before describing this copy as an independently released product, configure its own release assets and decide how its update checker, installer, telemetry, and hosted-platform links should behave. Existing upstream links should remain clearly identified until replacements actually exist.
