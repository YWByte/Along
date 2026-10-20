import re


def terms(text):
    lower = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", lower)
    chinese = re.findall(r"[\u4e00-\u9fff]+", lower)
    return words + [run[i : i + 2] for run in chinese for i in range(len(run) - 1)]


def current_step(state, plan):
    return next(
        (
            step
            for step in (plan or {}).get("steps", [])
            if state.get("steps", {}).get(step["id"]) != "user_reported_done"
        ),
        None,
    )
