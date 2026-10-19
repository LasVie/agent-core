"""Pack one JiuwenSwarm session into a demo archive that import_demo.py can restore elsewhere.

Standard library only. Run it where the session lives:

    python3 pack_demo.py --runtime <task root>/.runtime --session <session id> \
        --task-root <task root as the agents saw it> --out <archive>.tar.gz

The archive holds the session (chat history, metadata, stored diagnoses), its trajectory store, the
team workspace with the deliverables, the team members' folders, the file-operation history, the
session's runtime state and the skills named with --skill.  Symbolic links go to the manifest, since
their absolute targets move.  --task-root is the absolute path written inside the trajectory (on
macOS, the /Users/... path); import_demo.py replaces it with the new location.
"""

import argparse
import datetime
import hashlib
import io
import json
import pathlib
import sqlite3
import tarfile
import tempfile


def session_parts(runtime: pathlib.Path, session: str, skills: list[str]) -> list[pathlib.Path]:
    """Paths (relative to the runtime) that make up the session."""
    team = f"jiuwen_team_{session}"
    candidates = [
        pathlib.Path("agent/sessions") / session,
        pathlib.Path(".agent_teams") / team,
        *(path.relative_to(runtime) for path in (runtime / ".agent_teams/jiuwen_team_members").glob(f"{team}#*")),
        *(path.relative_to(runtime) for path in (runtime / ".agent_history").glob(f"file_ops_{team}_*")),
        *(path.relative_to(runtime) for path in (runtime / "config/runtime_state").glob(f"{session}.*")),
        *(pathlib.Path("agent/workspace/skills") / name for name in skills),
    ]
    return [part for part in candidates if (runtime / part).exists()]


def trace_store(runtime: pathlib.Path, session: str) -> pathlib.Path:
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    return pathlib.Path(".trace/sessions") / digest[:2] / f"{digest}.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True, type=pathlib.Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--task-root", required=True, help="absolute task root written inside the trajectory")
    parser.add_argument("--skill", action="append", default=[], help="a skill folder under agent/workspace/skills")
    parser.add_argument("--title", default="")
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    store = trace_store(runtime, args.session)
    if not (runtime / store).exists():
        raise SystemExit(f"no trajectory store for {args.session}: {runtime / store}")
    parts = session_parts(runtime, args.session, args.skill)
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(args.out, "w:gz") as archive:
        copy = pathlib.Path(tmp) / "store.sqlite3"
        source = sqlite3.connect(runtime / store)
        target = sqlite3.connect(copy)
        source.backup(target)  # a consistent copy, WAL included
        target.close()
        source.close()
        archive.add(copy, arcname=f"runtime/{store}")
        files = [str(store)]
        links: list[dict[str, str]] = []

        def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            """Links are recorded in the manifest instead: their absolute targets move with the task root."""
            if info.issym() or info.islnk():
                links.append({"path": info.name.removeprefix("runtime/"), "target": info.linkname})
                return None
            return info

        for part in parts:
            archive.add(runtime / part, arcname=f"runtime/{part}", filter=keep)
            files.append(str(part))
        manifest = {
            "session_id": args.session,
            "title": args.title,
            "task_root": args.task_root.rstrip("/"),
            "packed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "parts": files,
            "links": links,
        }
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    print(f"packed {len(files)} parts of {args.session} into {args.out}")


if __name__ == "__main__":
    main()
