---
name: release
description: Use when cutting, tagging, or publishing a mailtriage release or version bump.
---

# Cutting a release

## Steps — in this order

1. **Bump the version in BOTH places** (forgetting one is the reason this
   skill exists — they drift silently):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/mailtriage/__init__.py` → `__version__ = "X.Y.Z"`
   Semver: fixes = patch, new backend/skill/feature = minor, contract break
   (config/secret names) = major.
2. **THE GATE** (see CLAUDE.md — all four commands green).
3. Land the bump on `main` via **create-pr** (or include it in the feature PR
   being released).
4. **Tag + release from main's tip:**
   ```bash
   gh release create vX.Y.Z --target main --title "mailtriage vX.Y.Z — <one line>" --notes "..."
   ```
   Notes shape: what it does (one paragraph) → Highlights (bullets) →
   "Shipped in" (PR list) → the 🤖 Claude Code attribution line.
5. **Verify:** the release page renders, and
   `git ls-remote origin refs/tags/vX.Y.Z` matches `git rev-parse origin/main`.

## Red flags

- Tagging with the gate red or unmerged work ("I'll fix on main after").
- Releasing a contract change as a minor version.
- `gh release create` without `--target main` while on a feature branch.
