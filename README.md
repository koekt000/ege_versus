# EGE Battle (GitHub package)

This folder is prepared for GitHub upload with a strict per-file size limit.

## What's included

- `ege_battle/server.py`
- `ege_battle/game_manager.py`
- `ege_battle/database.py`
- `ege_battle/questions.py`
- `ege_battle/bot_player.py`
- `ege_battle/requirements.txt`
- `ege_battle/static/index.html`
- `ege_battle/static/style.css`
- `ege_battle/sdamgia_bank.db` (slimmed version)

## Data notes

- `sdamgia_bank.db` was reduced to keep file size below 25 MB.
- It includes all 4 subjects (`rus`, `math`, `phys`, `inf`) with fewer tasks per topic.
- The schema is reduced to fields required by the current app.

## Run locally

```bash
cd ege_battle
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open: `http://localhost:8000`

## Important

- This package intentionally does **not** include local runtime DB (`battle.db`).
- On first run, `battle.db` will be created automatically.
