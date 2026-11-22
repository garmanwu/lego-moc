---
name: lego-moc
description: Recognise loose LEGO parts from 1-30 photos into an inventory (~/.lego-parts/parts.json, shared by all agents), then design a LEGO model (MOC) from those parts as an LDraw file and have a server verify it physically - it returns fix notes, or a public link with a 3D preview and step-by-step building instructions. Use when the user wants to identify, scan or count LEGO pieces from photos, build or update a parts inventory, design or build a MOC from parts they own, check, validate or verify a LEGO model or .ldr file, or get building instructions or a 3D preview for it. Chinese triggers - 乐高零件识别, 识别乐高零件, 拍照识别积木, 乐高零件清单, 零件库存, 用我有的零件设计 MOC, 设计乐高作品, 检验 MOC, 检查乐高模型能不能拼, 拼装步骤, 搭建说明, 3D 预览.
version: 1.2.0
metadata:
  openclaw:
    emoji: "🧱"
    homepage: https://github.com/garmanwu/lego-moc
    requires:
      anyBins:
        - python3
        - python
    envVars:
      - name: LEGO_PARTS_SERVER
        required: false
        description: Optional. Address of the recognition server, overriding the one built into the script.
      - name: LEGO_PARTS_HOME
        required: false
        description: Optional. Local data folder, default ~/.lego-parts.
---

# LEGO parts recognition and MOC check

Two stages, one script, one local folder shared by every agent (`~/.lego-parts/`):

1. **Scan.** Upload photos of loose parts; the server recognises them and the script saves the inventory `parts.json`.
2. **Design and verify.** You design a model (MOC) from that inventory yourself and write it as an LDraw file. The
   server checks it physically: on failure it returns fix notes naming the offending lines; on pass, a public link
   with a 3D view and step-by-step building instructions. The server designs nothing; the design is yours.

## Run

The script is `scripts/lego.py` inside this skill's folder. Python 3.8+, standard library only, nothing to install.

```
python3 <this skill folder>/scripts/lego.py <command> [options] [arguments]
```

On Windows use `python` or `py -3`. Put options before the photo list.

**Keep the user's approvals few.** Many agents ask the user to approve each new shell command, each network
host and each write outside the workspace. So:
- Always call the script in exactly this form, as one plain command: no `cd`, no pipes, no `&&`, no environment
  prefix, no wrapper script. Then the user can approve "always allow" once for `python3 <path>/lego.py` and is not
  asked again. Tell the user this the first time an approval appears.
- The script needs three things, all of which the user can allow once: run `lego.py`, reach the server host shown by
  `lego.py whoami`, and read/write `~/.lego-parts/`.
- Don't run the same step twice, don't poll in a loop of short commands (use `--wait`), and don't call the server
  with curl or code of your own.
- When you design a model, write `model.ldr` (and any helper you need) with your file-writing tool in as few steps
  as you can. Avoid a long series of throw-away scripts: each one is another approval for the user.

| Command | What it does |
|---|---|
| `scan [--no-wait] [--wait SECONDS] [--images] PHOTO...` | Uploads 1–30 photos (files, folders or glob patterns), waits up to 480 s (default) for the results and saves them. `--no-wait` returns right after uploading. |
| `status [--wait SECONDS] [--images] [TASK_ID...]` | Checks the pending tasks (or the given ones) once, or keeps polling up to SECONDS. Saves finished ones. |
| `parts [--images]` | Saves finished pending tasks, then reloads the whole inventory from the server into `parts.json`. |
| `whoami` | Shows the user ID, server and local folder. No network. |
| `moc guide [--parts-file PATH]` | Saves the design guide `moc/DESIGN_GUIDE.md` and the connector sheets of your parts `moc/connectors.txt`. |
| `moc submit [--title NAME] [--design design.json] [--moc MOC_ID] [--parts-file PATH] [--no-wait] [--wait SECONDS] MODEL.ldr` | Sends the model with `parts.json`, waits up to 300 s (default) for the check and prints the result. `--moc` submits a new version of an existing MOC. |
| `moc status [--wait SECONDS] [MOC_ID]` | Checks the pending MOC submissions (or the given one) once, or keeps polling up to SECONDS. |

`--images` also downloads each inventory row's LDraw picture and photo crop into `images/`.

- **Long waits.** `scan` can run for 8 minutes, `moc submit` for 5. If your shell tool times out sooner (some
  default to 2 minutes), raise its timeout or pass a smaller `--wait` (e.g. `--wait 100`) and continue with
  `status --wait 100` / `moc status --wait 100`. Being stopped mid-wait is safe: IDs are saved in `pending.json` /
  `moc/pending.json` first, and any later command, from any agent, collects them.
- **Server address.** Normally built into the downloaded script. If a command says no server is configured,
  the user must set `LEGO_PARTS_SERVER` (e.g. `https://203.0.113.7`, without `/skill/v1`).
  `LEGO_PARTS_HOME` changes the local folder.

- **Blocked network.** If a command reports `HTTP 403 ... not from the LEGO server` or cannot reach the server,
  your sandbox, proxy or firewall is blocking it. Ask the user to allow network access to the server's host, then
  run the same command again. Do not call the API by hand with curl to work around it: new IDs are limited to
  5 per network per day, and results are only saved when the script itself runs.

## Exit codes and output

| Exit | Meaning |
|---|---|
| 0 | Scan: every task finished (done or failed), `parts.json` is current. MOC: the model **passed** the check |
| 4 | MOC: the model did not pass; the fix notes are printed |
| 3 | Still running (tasks, or a MOC check) |
| 1 | Error: read the `ERROR:` lines. For a MOC this includes status `error` (a server-side failure) |
| 2 | Usage error: bad arguments, more than 30 photos, a missing or unusable file named on the command line |

The last output line is always `RESULT {...}`, one line of JSON. Parse it instead of the text above it.
Scan fields: `exit_code`, `user_id`, `done`, `failed`, `pending`, `pending_task_ids`, `rejected` and
`not_uploaded` (files not accepted, with reasons), `notices`, `task_errors`, `errors`, `parts_file`, `tasks_dir`,
`total_quantity`, `distinct_parts`, `inventory_url`, `inventory_image`. MOC fields: `moc_id`, `version`, `status` (`queued`, `checking`, `stepping`,
`passed`, `failed`, `error`; null when nothing was checked), `url`, `fails` and `warnings` (counts), `report_file`,
`result_file`, `model_file`, `parts`, `steps`, `title`, `errors`; `moc guide` adds `guide_file`, `connectors_file`,
`unsupported_parts`.

## Rules for scanning

1. **Exit code 3 means tasks are still running.** Run `lego.py status --wait 300` again until it exits 0 before
   you read `parts.json`. Do the same whenever `RESULT` has `pending > 0`, whatever the exit code. If it keeps
   returning 3 for a long time, tell the user how many photos are still being processed instead of looping forever.
2. **Incomplete inventory.** If `parts.json` has `local_pending_tasks > 0` or `pending_tasks > 0`, the inventory
   is incomplete. Say so if you use it.
3. **Relay every notice.** Pass each entry of `notices` (e.g. `pile_photo`, `no_pieces`) to the user verbatim,
   in the user's language: translate the full message faithfully, don't drop or soften it. Also name the
   failed photos (`task_errors`) and the files in `rejected` / `not_uploaded`, with their reasons.
4. **Uncertain parts.** `low_confidence: true` means the part may be misidentified: point it out (the crop image
   helps the user check). `designable: false` means there is no usable LDraw geometry: don't build with it.
5. **Edits are local.** The server has no delete or edit API. To correct a part or a quantity, the user (or you,
   when asked) edits `parts.json`. `lego.py parts` overwrites it, and so do `scan` / `status` when they collect
   new results or get `--images`; the previous copy is kept as `parts.json.bak`. If the user has hand-edited
   `parts.json`, warn them before running any of these, and offer to re-apply their edits afterwards. Photographing
   the same parts twice counts them twice (the server only refuses the very same file, as `duplicate_photo`).
6. **Show pictures, not just part numbers.** A table of bare LDraw numbers is not enough: most users cannot tell
   what `3020` is. When you present the inventory, do all that your environment allows:
   - Show the picture `inventory_image` (`~/.lego-parts/inventory.png`, refreshed with `parts.json`): one image
     with the main parts, each with its picture, quantity, number, name and colour (an orange frame and `?` mark
     low-confidence rows). Display it the way you display any local image file.
   - Always give the user the link `inventory_url`: a web page of the whole inventory, each part with its LDraw
     picture and the crop from the photo. If you have a browser or preview panel, open the link there for the user.
   - If your chat renders remote images, you may also show single parts with `ldraw_image_url` / `crop_url`.
7. **Photo advice.** Spread the parts out on a plain surface, not touching each other, and shoot from straight
   above in good light. Piled-up parts are recognised poorly. At most 30 photos per scan, 25 MB each; JPEG, PNG,
   WEBP or HEIC.
8. **The user ID is the only credential.** Anyone with the ID in `user.json` (it is also part of `inventory_url`)
   can read this inventory and add scans to it: give the link to the user only, and don't paste the ID or the link
   anywhere public. Traffic is HTTPS when the server address starts with `https://`. Never delete or replace
   `user.json`; the inventory and MOCs on the server belong to that ID. If a command says the server does not
   recognise the user ID, report it to the user; don't work around it.

## Design and verify a MOC

Notes before you start:
- `moc submit` needs a user ID. If `user.json` does not exist yet it requests a new one and saves it; tell the user
  when that happens. `moc guide` needs no ID.
- A `parts.json` the user supplies by hand may contain only `items` (each with `ldraw_part`, `ldraw_color`, `quantity`,
  and optionally `designable`, `low_confidence`, `name`). Treat missing `designable` as true and missing
  `low_confidence` as false; the pending-task checks apply only to inventories written by this script.
- The verifier checks physics only. It never judges whether the model looks like what the user asked for: say honestly
  what you built and, if you could not view the page, that you have not seen the result.
- A check usually takes well under a minute.

1. `lego.py moc guide` (needs `parts.json`: scan first). Run it again after the inventory changes.
2. Read `moc/DESIGN_GUIDE.md` in full: it is the server's current rule book (file format, coordinates, which
   connectors mate, the verifier's rules, worked examples). Take every part's origin and connector positions from
   `moc/connectors.txt`; never guess them.
3. Write `model.ldr`: a `0 <title>` line, then one `1 <colour> x y z a b c d e f g h i <part>.dat` line per part,
   `<colour>` = `ldraw_color` and `<part>` = `ldraw_part` from `parts.json`. Use only rows with `designable: true`,
   never a part that `connectors.txt` calls "not a supported part", and per `key` (part + colour) at most
   `quantity` across the whole model; if the user edited `parts.json`, their numbers count. Don't rely on
   `low_confidence` rows for structure unless the user confirms them. Count the parts per `key` before submitting.
   No sub-models and no `0 STEP` lines: the server works out the building steps itself.
4. `lego.py moc submit model.ldr` (add `--title`; for parts meant to move, a `design.json` as the guide describes,
   with `--design`; otherwise build a static model). Pass the same `--parts-file` to `guide` and `submit` if you
   don't use the default `parts.json`.
5. **Exit 4:** each `FAIL` block gives the rule, the reason, a hint and the parts involved as
   `line <n>: <part> colour <c> at (x, y, z)`. Line numbers count every line of the submitted file from 1 (a copy
   is in `moc/<moc_id>/v<N>.ldr`; all parts are in `report_file`). Fix those lines and submit a new version of the
   same MOC: `lego.py moc submit model.ldr --moc <moc_id>`.
6. **Exit 0:** give the user the `url`, the part and step counts, and every warning.
7. **Exit 3:** `lego.py moc status <moc_id> --wait 300`. **Exit 1** with `status: "error"`: a server-side failure,
   not a design problem; submit the same file again with `--moc`. Any other exit 1: report the `ERROR:` line
   (e.g. the daily submission limit, a full queue).

Hard rules:

- **At most 5 resubmissions** of one model. If it still fails, stop and tell the user honestly which rules still
  fail and on which parts; don't keep retrying.
- **Never claim a model is buildable, valid or verified unless the server said `passed`** (RESULT `status` is
  `"passed"`). A `moc status` exit 0 with `status: null` checked nothing.
- **Relay every warning.** Warnings don't fail the model, but the user should hear them.
- **The link is public:** anyone who has the URL can view the model. Tell the user so when you give it.
- **Passing is not a guarantee.** It means the model is physically consistent in the verifier's simulation, not
  that the real build will hold. Say so.
- **Without the script or the server** (you cannot run commands or reach it), you may still write an `.ldr` from the
  guide, but tell the user plainly that it was not verified and has no building instructions.

## Files in `~/.lego-parts/`

| File | Content |
|---|---|
| `user.json` | `{"user_id", "server", "created_at"}` |
| `pending.json` | `{"task_ids": [...]}`: uploaded, result not collected yet |
| `tasks/<task_id>.json` | One photo: `status`, `notice`, `error`, `pieces_found`, `pieces_accepted`, `pieces[]` (each with `score`, `accepted`, `low_confidence`, `key`, `ldraw_part`, `ldraw_color`, `name`, `designable`, `crop_url`, `ldraw_image_url`, `bbox`, `candidates`) |
| `parts.json` | The inventory, see below |
| `parts.json.bak` | The previous `parts.json` |
| `inventory.png` | One overview picture of the inventory, refreshed with `parts.json` |
| `images/ldraw/<part>_<colour>.png`, `images/crops/<task>_<nnn>.jpg` | Downloaded with `--images` |
| `moc/DESIGN_GUIDE.md`, `moc/connectors.txt` | Written by `moc guide` |
| `moc/pending.json` | `{"moc_ids": [...]}`: submitted, check result not collected yet |
| `moc/<moc_id>/v<N>.ldr` | The file submitted as version N |
| `moc/<moc_id>/report.v<N>.json`, `moc/<moc_id>/result.json` | The server's answer for version N (`status`, `url`, `parts`, `steps`, `report.issues[]` with `rule`, `severity`, `message`, `hint`, `parts[]` of `index`, `line`, `part`, `color`, `pos`); `result.json` is the latest one fetched |

`parts.json`:

```json
{
  "user_id": "k7m2x9qp", "updated_at": "2026-11-21T08:05:00Z", "pending_tasks": 0, "total_quantity": 57,
  "inventory_url": "https://…/skill/u/k7m2x9qp",
  "items": [
    {"key": "3001|4", "ldraw_part": "3001", "ldraw_color": 4, "color_name": "Red", "color_hex": "#C91A09",
     "bricklink_id": "3001", "name": "Brick 2 x 4", "quantity": 3, "designable": true, "min_score": 0.74,
     "low_confidence": false, "ldraw_image_url": "https://…/3001/4.png", "crop_url": "https://…/007.jpg",
     "ref_image_url": "https://…", "piece_ids": ["t4h7…/007"],
     "local_ldraw_image": "/home/me/.lego-parts/images/ldraw/3001_4.png", "local_crop": "/home/me/…/t4h7…_007.jpg"}
  ],
  "fetched_at": "2026-11-21T08:05:03Z", "local_pending_tasks": 0
}
```

Only pieces the server is fairly sure of are counted: score at least 0.60 and a clean cut-out; in a photo flagged
`pile_photo` at least 0.80, so such a photo adds few parts (the rest are in `tasks/<task_id>.json` with `accepted: false`). Rows come designable first, then by quantity. `key` is
`<ldraw_part>|<ldraw_color>`; a half that could not be converted reads `bl:<BrickLink id>`, and then the
matching `ldraw_*` field is null. `local_*` fields exist only after `--images` and are null when a download failed;
they are not sent with a MOC.
