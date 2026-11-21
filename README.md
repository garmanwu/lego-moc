# lego-moc

An agent skill that turns photos of loose LEGO parts into a parts inventory, then lets your agent design a model (MOC) from those parts and have it checked and rendered.

拍照识别散装乐高零件，生成零件清单；再让 agent 用这些零件设计作品（MOC），由服务器检验能否拼出来，通过后给出带 3D 预览和拼装步骤的链接。

Works with any agent that can run Python 3 and reach the internet: Claude Code, Codex, OpenClaw and others.

## What it does

1. **Scan.** The agent uploads 1–30 photos. The server finds each part, identifies its LDraw part number and colour, and the script saves the inventory to `~/.lego-parts/parts.json`.
2. **Design and verify.** The agent designs a model from that inventory and writes it as an LDraw file. The server checks that the parts really connect and the model holds together. If not, it returns fix notes that name the offending lines. If it passes, it returns a public link with a 3D view and step-by-step building instructions.

## Install

Clone this repository into your agent's skills folder. For Claude Code:

```bash
git clone https://github.com/garmanwu/lego-moc ~/.claude/skills/lego-moc
```

With ClawHub:

```bash
clawhub install lego-moc
```

Or copy `SKILL.md` and `scripts/lego.py` into a folder named `lego-moc` wherever your agent keeps skills.

## Use

Ask your agent, for example:

- "Scan these LEGO photos and tell me what parts I have."
- "Design a small car from my parts and give me building instructions."

Or run the script yourself:

```bash
python3 scripts/lego.py scan photo1.jpg photo2.jpg
python3 scripts/lego.py moc guide
python3 scripts/lego.py moc submit model.ldr
```

`SKILL.md` has the full command list, exit codes and output format.

## Requirements

- Python 3.8 or later. The script uses the standard library only.
- Internet access to the recognition server.

## What leaves your computer

- The script sends the photos you scan, and the model and parts list you submit, to the recognition server at `http://136.117.205.131`. The connection is plain HTTP, not encrypted.
- The server keeps the results under a random 8-character user ID, which the script stores in `~/.lego-parts/`. No account or API key is needed.
- A model that passes gets a public link. Anyone who has the link can open it.
- Do not scan photos that show anything you would not want to share.

## Limits

- 30 photos per request and 60 photos per user ID per day.
- 30 model submissions per user ID per day.
- The first scan after an idle period waits about two minutes while the GPU starts.
- Spread the parts out on a plain surface and shoot from directly above. Piled parts are recognised poorly.

## Licence

MIT-0. LEGO® is a trademark of the LEGO Group, which does not sponsor or endorse this project.
