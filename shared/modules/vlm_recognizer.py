"""调用阿里云百炼 Qwen-VL 识别验证码。

prompt 与结果解析逻辑来自老师提供的 captcha_solver/vlm_recognizer.py，
这里改用 httpx 请求兼容 OpenAI 的接口，复用 Autovisor 现有依赖，
因此不需要额外安装 openai 包。
"""

import base64
import json

import httpx

from modules.logger import Logger

logger = Logger()

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-vl-max"
REQUEST_TIMEOUT = 60


def build_prompt(instruction: str) -> str:
    return (
        "这是一张网站的人机验证码图片。验证区域内分布着若干个 3D 立体字符"
        "（可能是大写字母、小写字母或数字），每个字符具有以下属性：\n"
        "  - 字符内容（如 Y / y / 8 / J / A / i / q / M）\n"
        "  - 颜色（如 灰色/紫色/黄色/浅蓝色/红色/蓝色）\n"
        "  - 朝向（正向=正面朝前 / 侧向左=侧面朝左 / 侧向右=侧面朝右）\n"
        "  - 大小写（大写 / 小写 / 数字）\n\n"
        f"验证指令是：{instruction}\n\n"
        "请按以下步骤思考：\n"
        "1. 逐一识别图中每个字符的内容、颜色、朝向、大小写和大致位置\n"
        "2. 严格依据指令文字（注意 颜色 + 数字/字母 + 朝向 + 大小写 全部条件）筛选\n"
        "3. 若指令要求依次点击多个字符，必须按指令中字符出现的先后顺序排列\n\n"
        "坐标使用归一化值（0-1000，左上为原点），坐标系以整张图片为基准。\n"
        "click_points 是按顺序点击的点数组，click_point 保留为第一个点。\n"
        "严格只返回如下 JSON，不要输出任何其他文字或解释：\n"
        "{\n"
        '  "candidates": [\n'
        '    {"char": "Y", "color": "紫色", "orientation": "正向", "case": "大写", "box": [x1, y1, x2, y2]}\n'
        "  ],\n"
        '  "answer": {"char": "Y", "reason": "简短理由", "box": [x1, y1, x2, y2]},\n'
        '  "click_points": [[cx, cy]],\n'
        '  "click_point": [cx, cy]\n'
        "}"
    )


def extract_json_object(text: str):
    """从可能夹带说明文字的回复里取出第一个完整 JSON 对象。

    直接正则 \\{.*\\} 是贪婪匹配，模型一旦返回多个 JSON 块就会跨越边界
    解析失败，这里改用能感知字符串转义的括号配对。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_points(text: str) -> list:
    """解析模型回复，返回归一化点击点列表 [(nx, ny), ...]。"""
    raw = extract_json_object(text or "")
    if not raw:
        raise ValueError(f"模型返回内容无法解析为 JSON: {text}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型返回 JSON 解析失败({exc}): {raw[:200]}") from None

    answer = data.get("answer") or {}

    points = []
    for point in data.get("click_points") or []:
        if isinstance(point, (list, tuple)) and len(point) == 2:
            points.append((float(point[0]), float(point[1])))
    if not points:
        single = data.get("click_point")
        if isinstance(single, (list, tuple)) and len(single) == 2:
            points.append((float(single[0]), float(single[1])))
    if not points and isinstance(answer, dict):
        box = answer.get("box")
        if box and len(box) == 4:
            points.append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    if not points:
        raise ValueError("模型未返回有效的点击坐标")
    return points


async def request_click_points(
    image_bytes: bytes,
    instruction: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = REQUEST_TIMEOUT,
    enable_thinking: bool = None,
) -> list:
    """把验证码截图交给视觉模型，拿回归一化点击点（0-1000）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(instruction)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image_bytes).decode("utf-8")
                        },
                    },
                ],
            }
        ],
    }
    if enable_thinking is not None:
        payload["enable_thinking"] = enable_thinking

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"模型响应格式异常: {str(data)[:200]}") from None
    return parse_points(content)
