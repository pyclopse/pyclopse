# Release Workflow

---

## Versioning

Versioning is managed by `hatch-vcs` — the version is derived **automatically**
from the git tag at build time. The generated `pyclawops/_version.py` is gitignored.

**Never manually edit the version** in `pyproject.toml` or `__init__.py`.

---

## Cutting a Release

```bash
git tag v0.2.0
git push origin v0.2.0
gh release create v0.2.0 --title "v0.2.0" --notes "..." --latest
```

Tag format: `vMAJOR.MINOR.PATCH`. The `pyclawops update` stable path uses
`git ls-remote --tags --sort=-v:refname` to find the latest tag — it only
matches this exact format. Pre-release tags (`v0.2.0-beta.1`) are ignored by
stable but reachable via `pyclawops update --version 0.2.0-beta.1`.

---

## Installation

pyclawops is distributed as a `uv tool` installed from a private GitHub repo over SSH.
The SSH key is at `~/.ssh/pyclawops_github` with a Host entry in `~/.ssh/config`.

**First-time install:**
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jondecker76/pyclawops/main/install.sh)
```

Optional flags:
```bash
bash install.sh --beta             # latest from main
bash install.sh --version 0.2.0   # specific version
```

---

## Updates

```bash
pyclawops update                    # latest stable tagged release
pyclawops update --beta             # latest from main (unstable)
pyclawops update --version 0.2.0   # specific version
```

Updates never touch `~/.pyclawops/` — config, sessions, memory, and jobs are
always preserved.

---

## Removal

```bash
pyclawops uninstall          # removes binary; prompts about ~/.pyclawops/
pyclawops uninstall --purge  # removes binary + ~/.pyclawops/ without prompting
```

---

## Package Data

Non-Python files shipped with the wheel (e.g. `pyclawops/self/knowledge/`) are
included automatically by hatchling — it includes all files within the package
directory by default. No explicit `include` configuration is needed unless files
are outside the package tree.
