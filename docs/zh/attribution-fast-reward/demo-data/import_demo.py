"""Restore a demo session packed by pack_demo.py into a JiuwenSwarm runtime, moved to the new location.

Standard library only. Stop the backend first, then from anywhere:

    python3 import_demo.py <archive>.tar.gz --runtime <your task root>/.runtime

Every file of the session is copied to the same place under the new runtime.  The absolute task root the
agents worked in is written inside the trajectory, the chat history and the diagnoses; it is replaced with
the new task root (the parent of --runtime) so that the deliverables are found where they now are.  In the
trajectory store the payloads are zlib-compressed: each is decompressed, rewritten and compressed again.
An existing copy of the session is left alone unless --force is given.  Restart the backend afterwards.
"""

import argparse
import hashlib
import json
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile
import zlib

_COMPRESSED = {"otlp_span_records": ("raw_json", "raw_sha256"), "trajectory_blobs": ("content", None)}


def rewrite_bytes(data: bytes, old: bytes, new: bytes) -> bytes:
    return data.replace(old, new) if old != new else data


def rewrite_store(path: pathlib.Path, old: str, new: str) -> int:
    """Replace the old task root in every payload and text column of a trajectory store; returns rows changed."""
    if old == new:
        return 0
    connection = sqlite3.connect(path)
    changed = 0
    for table, (column, digest_column) in _COMPRESSED.items():
        key = "ingest_seq" if table == "otlp_span_records" else "blob_hash"
        rows = connection.execute(f"SELECT {key}, {column} FROM {table}").fetchall()
        for row_key, stored in rows:
            if not stored:
                continue
            try:
                raw, compressed = zlib.decompress(bytes(stored)), True
            except zlib.error:
                raw, compressed = bytes(stored), False
            updated = rewrite_bytes(raw, old.encode(), new.encode())
            if updated == raw:
                continue
            values = [zlib.compress(updated, 3) if compressed else updated]
            sets = f"{column} = ?"
            if digest_column:
                sets += f", {digest_column} = ?"
                values.append(hashlib.sha256(updated).hexdigest())
            connection.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values, row_key))
            changed += 1
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for column in connection.execute(f"PRAGMA table_info({table})").fetchall():
            if str(column[2]).upper() == "TEXT":
                cursor = connection.execute(
                    f"UPDATE {table} SET {column[1]} = replace({column[1]}, ?, ?) WHERE instr({column[1]}, ?) > 0",
                    (old, new, old),
                )
                changed += cursor.rowcount
    connection.commit()
    connection.close()
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=pathlib.Path)
    parser.add_argument("--runtime", required=True, type=pathlib.Path, help="the .runtime folder to restore into")
    parser.add_argument("--force", action="store_true", help="replace an existing copy of the session")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    new_root = str(runtime.parent)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(args.archive, "r:gz") as archive:
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in pathlib.PurePosixPath(member.name).parts:
                    raise SystemExit(f"refusing unsafe path in archive: {member.name}")
            if hasattr(tarfile, "data_filter"):
                archive.extractall(tmp, filter="data")
            else:
                archive.extractall(tmp)
        manifest = json.loads((pathlib.Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
        session, old_root = manifest["session_id"], manifest["task_root"]
        if (runtime / "agent/sessions" / session).exists() and not args.force:
            raise SystemExit(f"{session} already exists in {runtime}; pass --force to replace it")
        source = pathlib.Path(tmp) / "runtime"
        files = texts = 0
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sqlite3":
                rows = rewrite_store(path, old_root, new_root)
                for stale in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
                    stale.unlink(missing_ok=True)
                shutil.copyfile(path, target)
                print(f"trajectory store: {relative} ({rows} rows moved to the new location)")
            else:
                data = path.read_bytes()
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    updated = rewrite_bytes(data, old_root.encode(), new_root.encode())
                    texts += updated != data
                    data = updated
                target.write_bytes(data)
            files += 1
        for link in manifest.get("links") or ():
            path = runtime / link["path"]
            target = link["target"]
            if target.startswith(old_root):
                target = new_root + target[len(old_root) :]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.exists():
                path.unlink()
            path.symlink_to(target)
    print(f"restored {manifest.get('title') or session} ({session}): {files} files, {texts} text files moved")
    print(f"  from {old_root}\n  to   {new_root}\nRestart the backend, then open /chat/{session}.")


if __name__ == "__main__":
    main()
