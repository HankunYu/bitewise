import base64
import functools
import json
import os
from datetime import datetime, timedelta
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import config
from .db import Store


class AIError(Exception):
    pass


EFFORT = "medium"
# Checks are interactive (the user waits in the shortcut), so trade depth for speed.
CHECK_EFFORT = "low"
MAX_TOKENS = 16000
REQUEST_TIMEOUT = 300.0
# Server-side refusal fallback: a classifier-declined request is re-run on the
# model Anthropic recommends for that refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
IMAGE_MEDIA_TYPES = {
    "jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}


# ---------- output schemas ----------

class MealItem(BaseModel):
    name: str = Field(description="食物名")
    amount: str = Field(description="分量估计,如 约200g / 1碗")
    kcal: float


class MealEstimate(BaseModel):
    description: str = Field(description="一句话概括这顿饭")
    items: list[MealItem]
    kcal: float = Field(description="总卡路里")
    protein_g: float = Field(description="蛋白质克数")
    carbs_g: float = Field(description="碳水克数")
    fat_g: float = Field(description="脂肪克数")
    fiber_g: float = Field(description="膳食纤维克数")
    sugar_g: float = Field(description="添加糖及游离糖克数")
    sodium_mg: float = Field(description="钠毫克数,注意酱料、腌制和外食的隐形钠")
    comment: str = Field(description="结合用户目标的一句简短点评,不超过40字")


class FieldUpdate(BaseModel):
    # A list of {field, value} pairs instead of one optional property per
    # field: a nullable property each pushes the schema past the API's
    # structured-output complexity limit.
    field: Literal[
        "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg",
        "description", "comment", "meal_slot", "eaten_at",
    ]
    value: str = Field(
        description="新值;数字字段填数字,meal_slot 取 breakfast/lunch/dinner/snack,eaten_at 用 ISO 格式")


class TextAction(BaseModel):
    action: Literal["add_meal", "update_meal", "delete_meal", "add_weight"]
    meal: MealEstimate | None = Field(None, description="add_meal 时必填")
    eaten_at: str | None = Field(
        None, description="仅 add_meal 且用户明确提到时间(如\"昨天晚上\")时给出,ISO 格式")
    id: int | None = Field(None, description="update_meal / delete_meal 的目标记录 id")
    updates: list[FieldUpdate] = Field(description="update_meal 时只列要改的字段,其他 action 留空")
    weight_kg: float | None = Field(None, description="add_weight 时的体重,统一转成 kg")


class NutrientTargets(BaseModel):
    protein_g: float = Field(description="每日蛋白质下限克数")
    carbs_g: float = Field(description="每日碳水上限克数")
    fat_g: float = Field(description="每日脂肪上限克数")
    fiber_g: float = Field(description="每日膳食纤维下限克数")
    sugar_g: float = Field(description="每日添加糖上限克数")
    sodium_mg: float = Field(description="每日钠上限毫克数")


class FoodCheck(BaseModel):
    verdict: Literal["ok", "moderate", "avoid"] = Field(
        description="ok 可以放心吃;moderate 可以吃但要控制分量或搭配;avoid 不建议吃")
    advice: str = Field(description="具体建议,如吃多少、怎么搭配、吃了之后今天还剩多少额度,不超过80字")
    meal: MealEstimate


# ---------- API plumbing ----------

@functools.cache
def _client() -> anthropic.Anthropic:
    # Built on first use so the app starts (and non-AI pages work) without a key.
    # The client is thread-safe and shared by all worker lanes.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AIError("ANTHROPIC_API_KEY is not set, fill it in .env")
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT)


def _call(store: Store, content: list[dict], output_format: type[BaseModel] | None = None,
          effort: str = EFFORT):
    """Send one user turn; returns the parsed model when output_format is set, else the text."""
    params = dict(
        model=store.get_setting("model"),
        max_tokens=MAX_TOKENS,
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    )
    try:
        if output_format is not None:
            response = _client().beta.messages.parse(**params, output_format=output_format)
        else:
            response = _client().beta.messages.create(**params)
    except anthropic.AuthenticationError as exc:
        raise AIError("Anthropic API key missing or invalid, check .env") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("rate limited by the Anthropic API, retry later") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Anthropic API error {exc.status_code}: {exc.message}"[:500]) from exc
    except anthropic.APIConnectionError as exc:
        raise AIError(f"cannot reach the Anthropic API: {exc}") from exc
    except anthropic.AnthropicError as exc:
        # e.g. no credentials configured at all
        raise AIError(f"Anthropic client error: {exc}"[:500]) from exc

    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise AIError(f"model declined the request (category: {category})")
    if response.stop_reason == "max_tokens":
        raise AIError("model response was cut off at max_tokens")
    if output_format is not None:
        if response.parsed_output is None:
            raise AIError("model returned no structured output")
        return response.parsed_output
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _image_block(store: Store, image_rel_path: str) -> dict:
    path = store.data_dir / image_rel_path
    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lstrip(".").lower())
    if media_type is None:
        raise AIError(f"image format not supported by the API: {path.name}")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def _text_block(parts: list[str]) -> dict:
    return {"type": "text", "text": "\n".join(p for p in parts if p)}


def _goal_block(store: Store) -> str:
    goal = store.get_setting("goal_prompt").strip()
    return f"用户的目标:{goal}\n" if goal else ""


def _meal_dict(meal: MealEstimate) -> dict:
    """Flatten a meal estimate into the DB's meal fields, rounded and trimmed."""
    return {
        "description": meal.description.strip(),
        "items": [
            {"name": item.name, "amount": item.amount, "kcal": round(item.kcal, 1)}
            for item in meal.items if item.name
        ],
        "kcal": round(meal.kcal, 1),
        **{key: round(getattr(meal, key), 1) for key in config.NUTRIENT_KEYS},
        "comment": meal.comment.strip(),
    }


# ---------- features ----------

def estimate_meal(store: Store, image_rel_path: str | None, text: str, correction: str = "") -> dict:
    """Estimate calories/macros for one meal from an image and/or text.

    correction: user-confirmed description of what the meal actually is;
    when present it overrides whatever the image/text suggest.
    """
    parts = ["你是一位营养师,请估算这顿饭的营养成分。"]
    if image_rel_path:
        parts.append("这顿饭的照片见上图。")
    if text.strip():
        parts.append(f"用户的描述/补充说明:{text.strip()}")
    if correction.strip():
        parts.append(
            f"注意:之前的识别有误,用户已更正,这顿饭实际是:「{correction.strip()}」。"
            "请以更正内容为准重新估算,图片和原描述仅用于参考分量。"
        )
    parts.append(_goal_block(store))
    parts.append("按常见分量合理估算,不确定时取中间值。")
    content = [_image_block(store, image_rel_path)] if image_rel_path else []
    content.append(_text_block(parts))
    return _meal_dict(_call(store, content, MealEstimate))


def interpret_text(store: Store, text: str) -> dict:
    """Classify a text-only message into an action; meal logs include the estimate inline."""
    recent = store.recent_done_meals(8)
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

{_goal_block(store)}用户消息:「{text.strip()}」

判断意图,设置 action 并只填写该 action 用到的字段:
- add_meal:记录一顿饭,填 meal(作为营养师估算,不确定时取常见分量中间值);仅当用户明确提到时间时填 eaten_at。
- update_meal:修改已有记录,填 id 和 updates(只列要改的字段)。
- delete_meal:删除记录,填 id。
- add_weight:记录体重,填 weight_kg。

"刚才那顿"、"上一条"指最近的一条记录。如果无法确定用户指哪条记录,当作 add_meal 处理。"""
    result = _call(store, [_text_block([prompt])], TextAction)
    if result.action == "add_meal" and result.meal is None:
        raise AIError("add_meal returned without a meal estimate")
    if result.action in ("update_meal", "delete_meal") and result.id is None:
        raise AIError(f"{result.action} returned without a target id")
    if result.action == "add_weight" and result.weight_kg is None:
        raise AIError("add_weight returned without weight_kg")
    action = result.model_dump(exclude_none=True, exclude={"updates"})
    if result.meal is not None:
        action["meal"] = _meal_dict(result.meal)
    if result.action == "update_meal":
        action["fields"] = _update_fields(result.updates)
    return action


def _update_fields(updates: list[FieldUpdate]) -> dict:
    """Turn {field, value} pairs into typed meal fields."""
    fields = {}
    for update in updates:
        if update.field in ("kcal", *config.NUTRIENT_KEYS):
            try:
                fields[update.field] = round(float(update.value), 1)
            except ValueError:
                raise AIError(f"model returned non-numeric {update.field}: {update.value!r}")
        else:
            fields[update.field] = update.value.strip()
    return fields


def check_food(store: Store, image_rel_path: str) -> dict:
    """Advise whether to eat the pictured food, given what was already eaten today.

    Returns {"verdict", "advice", "estimate"}; estimate has the meal shape so an
    eaten check can be stored as a meal without another model call.
    """
    today = datetime.now().date().isoformat()
    meals = [m for m in store.list_meals(f"{today}T00:00:00", f"{today}T23:59:59")
             if m["status"] == "done"]
    meals_brief = [
        {"meal_slot": m["meal_slot"], "description": m["description"], "kcal": m["kcal"]}
        for m in meals
    ]
    totals = {
        key: round(sum(m[key] or 0 for m in meals), 1)
        for key in ("kcal", *config.NUTRIENT_KEYS)
    }
    targets = store.get_nutrient_targets()
    targets_line = (
        f"每日营养目标(蛋白质/纤维为下限,其余为上限,JSON):{json.dumps(targets, ensure_ascii=False)}\n"
        if targets else ""
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    prompt = f"""你是一位营养师。用户还没吃,拍了上面这张食物照片问你:这个能不能吃、吃了会怎样。

当前时间:{now}
{_goal_block(store)}每日卡路里目标:{store.get_setting("daily_kcal_target")} kcal
{targets_line}今天已经吃的(JSON):{json.dumps(meals_brief, ensure_ascii=False)}
今天已摄入合计(JSON):{json.dumps(totals, ensure_ascii=False)}

估算这份食物的营养成分(填 meal),结合今天剩余额度、当前时间和用户目标,给出 verdict 和 advice。"""
    result = _call(
        store, [_image_block(store, image_rel_path), _text_block([prompt])], FoodCheck, effort=CHECK_EFFORT)
    return {
        "verdict": result.verdict,
        "advice": result.advice.strip(),
        "estimate": _meal_dict(result.meal),
    }


def nutrient_targets(store: Store) -> dict:
    """Generate daily nutrient targets from the user's goal, weight, and kcal target."""
    weight = store.latest_weight()
    parts = [
        "你是一位营养师,请为用户制定每日营养摄入目标。",
        _goal_block(store),
        f"每日卡路里目标:{store.get_setting('daily_kcal_target')} kcal",
    ]
    if weight:
        parts.append(f"当前体重:{weight['weight_kg']} kg({weight['measured_at'][:10]} 记录)")
    parts.append(
        "蛋白质和膳食纤维是每日应达到的下限,碳水、脂肪、糖、钠是不应超过的上限。"
        "按主流营养学共识估算(蛋白质按 g/kg 体重、纤维约 14g/1000kcal、糖和钠参考 WHO 建议),"
        "并结合用户目标调整。"
    )
    result = _call(store, [_text_block(parts)], NutrientTargets)
    return {key: round(getattr(result, key), 1) for key in config.NUTRIENT_KEYS}


def daily_advice(store: Store, date: str) -> str:
    """Generate the end-of-day advice paragraph for a date."""
    meals = [m for m in store.list_meals(f"{date}T00:00:00", f"{date}T23:59:59")
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
    week = store.daily_totals((day - timedelta(days=6)).isoformat(), date)
    weights = store.list_weights((day - timedelta(days=13)).isoformat())
    weights_brief = [{"date": w["measured_at"][:10], "kg": w["weight_kg"]} for w in weights]
    targets = store.get_nutrient_targets()
    targets_line = (
        f"每日营养目标(蛋白质/纤维为下限,其余为上限,JSON):{json.dumps(targets, ensure_ascii=False)}\n"
        if targets else ""
    )
    prompt = f"""你是一位营养师。根据用户 {date} 的饮食记录,写一段当日总结建议。

{_goal_block(store)}每日卡路里目标:{store.get_setting("daily_kcal_target")} kcal
{targets_line}
当日各餐(JSON):
{json.dumps(meals_brief, ensure_ascii=False)}

最近7天每日摄入汇总(JSON):
{json.dumps(week, ensure_ascii=False)}

最近14天体重记录(JSON):
{json.dumps(weights_brief, ensure_ascii=False)}

要求:中文,3-5句话,先评价当日摄入与目标的差距和营养结构,再结合近期趋势给出明天的具体建议。直接输出建议正文,不要标题、不要列表、不要输出其他内容。"""
    advice = _call(store, [_text_block([prompt])])
    if not advice:
        raise AIError("empty advice from model")
    return advice
