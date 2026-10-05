import json
import re
import subprocess
from datetime import datetime, timedelta

from . import config, db


class AIError(Exception):
    pass


def _call_claude(prompt: str, timeout: int = 300) -> str:
    """Run claude -p and return the result text."""
    cmd = [
        "claude", "-p",
        "--model", db.get_setting("model"),
        "--output-format", "json",
        "--no-session-persistence",
        "--allowedTools", "Read",
        "--add-dir", str(config.UPLOAD_DIR),
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(config.DATA_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise AIError(f"claude timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise AIError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    try:
        envelope = json.loads(proc.stdout)
    except ValueError as exc:
        raise AIError(f"claude output is not JSON: {proc.stdout[:500]}") from exc
    if envelope.get("is_error"):
        raise AIError(f"claude reported error: {envelope.get('result', '')[:500]}")
    return envelope.get("result", "")


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise AIError(f"no JSON object in response: {text[:300]}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise AIError(f"invalid JSON in response: {text[start:end + 1][:300]}") from exc


MEAL_JSON_SPEC = """{
  "description": "一句话概括这顿饭",
  "items": [{"name": "食物名", "amount": "分量估计,如 约200g / 1碗", "kcal": 数字}],
  "kcal": 总卡路里数字,
  "protein_g": 蛋白质克数,
  "carbs_g": 碳水克数,
  "fat_g": 脂肪克数,
  "fiber_g": 膳食纤维克数,
  "sugar_g": 添加糖及游离糖克数,
  "sodium_mg": 钠毫克数(注意酱料、腌制和外食的隐形钠),
  "comment": "结合用户目标的一句简短点评,不超过40字"
}"""


def _goal_block() -> str:
    goal = db.get_setting("goal_prompt").strip()
    return f"用户的目标:{goal}\n" if goal else ""


def estimate_meal(image_rel_path: str | None, text: str, correction: str = "") -> dict:
    """Estimate calories/macros for one meal from an image and/or text.

    correction: user-confirmed description of what the meal actually is;
    when present it overrides whatever the image/text suggest.
    """
    parts = ["你是一位营养师,请估算下面这顿饭的营养成分。"]
    if image_rel_path:
        parts.append(f"请先用 Read 工具读取图片 {image_rel_path} 查看这顿饭。")
    if text.strip():
        parts.append(f"用户的描述/补充说明:{text.strip()}")
    if correction.strip():
        parts.append(
            f"注意:之前的识别有误,用户已更正,这顿饭实际是:「{correction.strip()}」。"
            "请以更正内容为准重新估算,图片和原描述仅用于参考分量。"
        )
    parts.append(_goal_block())
    parts.append(
        "按常见分量合理估算,不确定时取中间值。"
        f"只输出一个 JSON 对象,不要输出任何其他文字:\n{MEAL_JSON_SPEC}"
    )
    result = _call_claude("\n".join(p for p in parts if p))
    return _normalize_meal(_extract_json(result))


def interpret_text(text: str) -> dict:
    """Classify a text-only message into an action; meal logs include the estimate inline."""
    recent = db.recent_done_meals(8)
    recent_brief = [
        {
            "id": m["id"],
            "eaten_at": m["eaten_at"],
            "meal_slot": m["meal_slot"],
            "description": m["description"],
            "kcal": m["kcal"],
        }
        for m in recent
    ]
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    prompt = f"""你是饮食记录工具的解析器。用户从手机发来一条消息,可能是:记录一顿饭、修改已有记录、删除记录、记录体重。

当前时间:{now}
最近的饮食记录(JSON):
{json.dumps(recent_brief, ensure_ascii=False)}

{_goal_block()}用户消息:「{text.strip()}」

判断意图并只输出一个 JSON 对象,不要输出任何其他文字。四种格式:

1. 记录一顿饭(作为营养师估算,不确定时取常见分量中间值):
{{"action": "add_meal", "eaten_at": "可选,仅当用户明确提到时间(如\\"昨天晚上\\")时给出 ISO 格式,否则省略", "meal": {MEAL_JSON_SPEC}}}

2. 修改已有记录(fields 只包含要改的字段,可用:kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg, description, comment, meal_slot, eaten_at):
{{"action": "update_meal", "id": 目标记录id, "fields": {{...}}}}

3. 删除记录:
{{"action": "delete_meal", "id": 目标记录id}}

4. 记录体重(单位统一转成 kg):
{{"action": "add_weight", "weight_kg": 数字}}

"刚才那顿"、"上一条"指最近的一条记录。如果无法确定用户指哪条记录,当作 add_meal 处理。"""
    action = _extract_json(_call_claude(prompt))
    if action.get("action") == "add_meal" and isinstance(action.get("meal"), dict):
        action["meal"] = _normalize_meal(action["meal"])
    return action


TARGETS_JSON_SPEC = """{
  "protein_g": 每日蛋白质下限克数,
  "carbs_g": 每日碳水上限克数,
  "fat_g": 每日脂肪上限克数,
  "fiber_g": 每日膳食纤维下限克数,
  "sugar_g": 每日添加糖上限克数,
  "sodium_mg": 每日钠上限毫克数
}"""


def nutrient_targets() -> dict:
    """Generate daily nutrient targets from the user's goal, weight, and kcal target."""
    weight = db.latest_weight()
    parts = [
        "你是一位营养师,请为用户制定每日营养摄入目标。",
        _goal_block(),
        f"每日卡路里目标:{db.get_setting('daily_kcal_target')} kcal",
    ]
    if weight:
        parts.append(f"当前体重:{weight['weight_kg']} kg({weight['measured_at'][:10]} 记录)")
    parts.append(
        "蛋白质和膳食纤维是每日应达到的下限,碳水、脂肪、糖、钠是不应超过的上限。"
        "按主流营养学共识估算(蛋白质按 g/kg 体重、纤维约 14g/1000kcal、糖和钠参考 WHO 建议),"
        "并结合用户目标调整。"
        f"只输出一个 JSON 对象,不要输出任何其他文字:\n{TARGETS_JSON_SPEC}"
    )
    data = _extract_json(_call_claude("\n".join(p for p in parts if p)))
    targets = {}
    for key in config.NUTRIENT_KEYS:
        try:
            targets[key] = round(float(data[key]), 1)
        except (KeyError, TypeError, ValueError):
            raise AIError(f"model returned bad target for {key}: {data.get(key)!r}")
    return targets


def daily_advice(date: str) -> str:
    """Generate the end-of-day advice paragraph for a date."""
    meals = [m for m in db.list_meals(f"{date}T00:00:00", f"{date}T23:59:59")
             if m["status"] == "done"]
    meals_brief = [
        {
            "meal_slot": m["meal_slot"],
            "description": m["description"],
            "kcal": m["kcal"],
            **{key: m[key] for key in config.NUTRIENT_KEYS},
        }
        for m in meals
    ]
    day = datetime.fromisoformat(f"{date}T00:00:00").date()
    week = db.daily_totals((day - timedelta(days=6)).isoformat(), date)
    weights = db.list_weights((day - timedelta(days=13)).isoformat())
    weights_brief = [{"date": w["measured_at"][:10], "kg": w["weight_kg"]} for w in weights]
    targets = db.get_nutrient_targets()
    targets_line = (
        f"每日营养目标(蛋白质/纤维为下限,其余为上限,JSON):{json.dumps(targets, ensure_ascii=False)}\n"
        if targets else ""
    )
    prompt = f"""你是一位营养师。根据用户 {date} 的饮食记录,写一段当日总结建议。

{_goal_block()}每日卡路里目标:{db.get_setting("daily_kcal_target")} kcal
{targets_line}
当日各餐(JSON):
{json.dumps(meals_brief, ensure_ascii=False)}

最近7天每日摄入汇总(JSON):
{json.dumps(week, ensure_ascii=False)}

最近14天体重记录(JSON):
{json.dumps(weights_brief, ensure_ascii=False)}

要求:中文,3-5句话,先评价当日摄入与目标的差距和营养结构,再结合近期趋势给出明天的具体建议。直接输出建议正文,不要标题、不要列表、不要输出其他内容。"""
    advice = _call_claude(prompt).strip()
    if not advice:
        raise AIError("empty advice from model")
    return advice


def _normalize_meal(data: dict) -> dict:
    """Coerce a model-produced meal estimate into clean typed fields."""
    def num(value, default=0.0):
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return default

    items = data.get("items")
    if not isinstance(items, list):
        items = []
    clean_items = []
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            clean_items.append({
                "name": str(item["name"]),
                "amount": str(item.get("amount", "")),
                "kcal": num(item.get("kcal")),
            })
    return {
        "description": str(data.get("description", "")).strip(),
        "items": clean_items,
        "kcal": num(data.get("kcal")),
        **{key: num(data.get(key)) for key in config.NUTRIENT_KEYS},
        "comment": str(data.get("comment", "")).strip(),
    }
