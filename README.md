# shelf

`shelf` backs up folders under `~/work` and `~/personal` that have not changed for 90 days, checks the Google Drive copy, then removes the local originals. In folders you still use, it deletes dependency directories (`venv`, `node_modules`, `.next`, and the other names in `src/shelf/paths.py`) without archiving them.

`~/work` plus `~/personal` are a small part of this Mac's data volume. Run `plan` and read the estimate before `apply`. The command does not scan the rest of your home folder.

## Run

```sh
~/Projects/shelf/bin/shelf plan
~/Projects/shelf/bin/shelf apply --yes
~/Projects/shelf/bin/shelf restore oldproj
```

`plan` is the default and changes nothing. `apply` refuses to delete or upload unless you pass `--yes`.

Archives are written to `~/Library/CloudStorage/GoogleDrive-*/My Drive/Shelf`. A sibling `Name.shelf.json` file is left where the folder was, and a line is appended to `~/Library/Application Support/shelf/history.jsonl`.

Google Drive may keep its own local cache of the archive until you set the Shelf folder to online-only. Removing the original folder is what frees space under `~/work` and `~/personal`. The upload is written before the original is deleted, so the disk needs room for about twice the upload size during the copy.

## What counts as idle

A folder is idle when its newest real file is at least `--days` old (default 90). Dependency directories are ignored for that clock, so a freshly rebuilt `venv` does not make an old project look active. A directory that contains `pyvenv.cfg` is a virtualenv even when its folder is named something else, such as `myenv`. Git metadata is included, so a recent fetch keeps a repo. `~/work/repo` is treated as a container: each project inside it is judged on its own.

These are never uploaded or deleted: `PasswordDB`, `Recovery Codes`, and `.ssh`. A project that contains one of those names is left alone. Secret files inside an ordinary project (`.env`, keys, `credentials.json`) are listed in the plan and are included in the archive, because a restore without them is incomplete.

Uncommitted git changes are included and called out in the plan. Symlinks are not followed.

## Restore

```sh
~/Projects/shelf/bin/shelf restore ~/work/repo/some-project.shelf.json
```

Restore puts the archived files back at their original path and does not recreate dependency folders. Install those again with the project's package manager. Restore refuses to overwrite a path that already exists.
