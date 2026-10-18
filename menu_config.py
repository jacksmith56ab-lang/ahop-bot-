from __future__ import annotations
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "menu_labels.json"
DEFAULT_LABELS = {
    "daily": "🎁 Ежедневный бонус", "open": "🎲 Открыть бонус",
    "giveaways": "🏆 Розыгрыши", "store": "🛒 Купить голду",
    "balance": "💰 Мой баланс", "withdraw": "📤 Вывод голды",
    "orders": "📋 Мои заказы", "tasks": "🧩 Задания",
    "referral": "👥 Пригласить друга", "history": "📜 История операций",
    "support": "🆘 Поддержка",
}
def get_menu_labels():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {**DEFAULT_LABELS, **{k: str(v) for k, v in data.items() if k in DEFAULT_LABELS}}
    except (OSError, ValueError, TypeError):
        return DEFAULT_LABELS.copy()
def set_menu_label(key: str, value: str):
    labels = get_menu_labels()
    if key not in DEFAULT_LABELS:
        raise ValueError("unknown menu key")
    labels[key] = value
    CONFIG_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
