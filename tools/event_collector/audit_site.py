"""Fail a website build if private collector material was included."""

import argparse
from pathlib import Path


def audit(directory):
    failures = []
    forbidden = ("_event_collector", ".event-tools", "tools/event_collector", "tests/event_collector", "docs/event-collector")
    markers = (b'"generation_parameters"', b'"input_sha256"', b'"rejected_proposal"',
               b"GEMINI_API_KEY", b"<!--X-Body-of-Message-->")
    if not directory.is_dir():
        raise ValueError("Website build directory is missing")
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if any(value in relative for value in forbidden) or relative.startswith(".git/"):
            failures.append(relative)
        elif path.suffix in (".html", ".json", ".txt", ".js", ".md", ".yml"):
            if any(marker in path.read_bytes() for marker in markers):
                failures.append(relative)
    if failures:
        raise ValueError("Private collector material in website: " + ", ".join(sorted(set(failures))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        audit(args.directory)
        print("Website collector privacy check passed")
        return 0
    except ValueError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
