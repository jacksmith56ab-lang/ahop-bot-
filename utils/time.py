def format_remaining(total_seconds: int) -> str:
    hours, remainder = divmod(max(0, total_seconds), 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."
