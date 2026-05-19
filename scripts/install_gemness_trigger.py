from __future__ import annotations

import argparse
from pathlib import Path

from gemness.gemness_trigger import END_MARKER, SKILL_CONTENT, START_MARKER, TRIGGER_BLOCK, install


def upsert_trigger_block(existing: str) -> str:
    from gemness.codex_install import upsert_marked_block

    return upsert_marked_block(existing, TRIGGER_BLOCK, START_MARKER, END_MARKER)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or update the Gemness trigger guidance.")
    parser.add_argument("--scope", choices=["project", "user", "both"], default="project")
    parser.add_argument("--project-root", default=".", help="Project root for --scope project or both.")
    args = parser.parse_args()

    for path in install(args.scope, Path(args.project_root)):
        print(f"updated {path}")


if __name__ == "__main__":
    main()
