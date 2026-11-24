"""Preset management - per-game configs as JSON."""

import json
from pathlib import Path
from .config import SessionConfig

CONFIG_DIR = Path.home() / '.config' / 'janus'
GAME_DIR = CONFIG_DIR / 'games'


class PresetManager:
    def __init__(self):
        GAME_DIR.mkdir(parents=True, exist_ok=True)

    # --- Per-game configs ---

    def save_game(self, appid: int, config: SessionConfig):
        path = GAME_DIR / f'{appid}.json'
        path.write_text(json.dumps(config.to_dict(), indent=2))

    def load_game(self, appid: int) -> SessionConfig | None:
        path = GAME_DIR / f'{appid}.json'
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return SessionConfig.from_dict(data)

    def game_has_config(self, appid: int) -> bool:
        return (GAME_DIR / f'{appid}.json').exists()

    def list_games(self) -> list[dict]:
        """Return list of {appid, name} for all configured games."""
        games = []
        for path in sorted(GAME_DIR.glob('*.json')):
            try:
                data = json.loads(path.read_text())
                appid = data.get('appid') or int(path.stem)
                name = data.get('game_name') or f'App {appid}'
                games.append({'appid': appid, 'name': name})
            except (json.JSONDecodeError, ValueError):
                continue
        return games

    def delete_game(self, appid: int):
        """Delete a game's config file."""
        path = GAME_DIR / f'{appid}.json'
        if path.exists():
            path.unlink()

