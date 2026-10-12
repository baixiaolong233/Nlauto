"""自动处理智慧树点击式验证码。

识别逻辑来自老师提供的 captcha_solver/vlm_recognizer.py：只依赖 openai，
本身不涉及任何浏览器操作，因此可以直接搬过来复用。浏览器相关的操作
（定位验证码、截图、点击）由本模块用 Playwright 完成，两边职责分离。
"""

import asyncio

from playwright.async_api import Page

from modules.logger import Logger
from modules.tasks import VERIFICATION_SELECTORS, has_visible_verification
from modules.vlm_recognizer import request_click_points

logger = Logger()

REQUEST_TIMEOUT = 60
AFTER_CLICK_WAIT = 6
INSTRUCTION_KEYWORDS = ("请点击", "请选择", "依次点击", "按顺序点击")
MAX_INSTRUCTION_LEN = 60
MIN_CONTAINER_SIZE = 100
INSTRUCTION_WAIT_ROUNDS = 12
INSTRUCTION_WAIT_INTERVAL = 0.5

# 只遍历文本节点，避免像 contains(text(), ...) 那样命中祖先容器拿到一整坨文字。
INSTRUCTION_JS = """
(args) => {
    const keywords = args[0];
    const maxLen = args[1];
    const found = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const text = (walker.currentNode.nodeValue || '').trim();
        if (!text || text.length > maxLen) continue;
        if (keywords.some(kw => text.includes(kw))) found.push(text);
    }
    if (!found.length) return null;
    return found.sort((a, b) => a.length - b.length)[0];
}
"""


def captcha_ready(config) -> bool:
    """开关 + Key 配齐才介入；否则 wait_for_verify 走原有的人工流程。"""
    if not getattr(config, "enableVlmCaptcha", False):
        return False
    return bool(getattr(config, "vlmApiKey", ""))


async def _find_container(page):
    """定位可见的验证码容器，返回 (frame, locator, box)。

    bounding_box 相对主 frame 视口，与 page.mouse 坐标系一致，跨 iframe 也无需换算。
    """
    for frame in page.frames:
        for selector in VERIFICATION_SELECTORS:
            locator = frame.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    box = await item.bounding_box()
                except Exception:
                    continue
                if (
                    box
                    and box["width"] >= MIN_CONTAINER_SIZE
                    and box["height"] >= MIN_CONTAINER_SIZE * 0.6
                ):
                    return frame, item, box
    return None, None, None


async def _find_instruction(frame):
    try:
        return await frame.evaluate(
            INSTRUCTION_JS, [list(INSTRUCTION_KEYWORDS), MAX_INSTRUCTION_LEN]
        )
    except Exception as exc:
        logger.debug(f"读取验证指令失败: {logger.summarize_exception(exc)}")
        return None


async def _request_points(config, image_bytes: bytes, instruction: str) -> list:
    """调用视觉模型，拿回归一化点击点（0-1000）。"""
    try:
        kwargs = {"api_key": config.vlmApiKey}
        model = getattr(config, "vlmModel", "").strip()
        if model:
            kwargs["model"] = model
        base_url = getattr(config, "vlmBaseUrl", "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        timeout = getattr(config, "vlmTimeout", 0)
        if timeout:
            kwargs["timeout"] = timeout
        kwargs["enable_thinking"] = getattr(config, "vlmEnableThinking", False)
        return await request_click_points(image_bytes, instruction, **kwargs)
    except Exception as exc:
        logger.warn(f"视觉模型调用失败: {logger.summarize_exception(exc)}", shift=True)
        return []


def to_absolute_point(box: dict, nx: float, ny: float):
    """把 0-1000 归一化坐标换算成页面视口绝对坐标。

    bounding_box 与 page.mouse 都以主 frame 视口为原点，因此这里只做
    等比换算，不需要像 Selenium 那样补偿元素中心偏移。
    """
    return (
        box["x"] + nx / 1000.0 * box["width"],
        box["y"] + ny / 1000.0 * box["height"],
    )


async def solve_click_captcha(page: Page, config) -> bool:
    """尝试自动通过点击式验证码，失败时返回 False 交给人工兜底。"""
    frame, container, box = await _find_container(page)
    if container is None:
        logger.debug("未定位到验证码容器,跳过自动验证.")
        return False

    instruction = None
    for _attempt in range(INSTRUCTION_WAIT_ROUNDS):
        instruction = await _find_instruction(frame)
        if instruction:
            break
        await asyncio.sleep(INSTRUCTION_WAIT_INTERVAL)
    if not instruction:
        logger.debug("未读取到指令文字,跳过自动验证.")
        return False

    shot_target = container
    shot_box = box
    for img_sel in (".yidun_bg-img", ".yidun_bgimg"):
        try:
            loc = frame.locator(img_sel).first
            if await loc.is_visible():
                ib = await loc.bounding_box()
                if ib and ib["width"] >= 50 and ib["height"] >= 50:
                    shot_target = loc
                    shot_box = ib
                    break
        except Exception:
            pass

    try:
        screenshot = await shot_target.screenshot()
    except Exception as exc:
        logger.debug(f"验证码截图失败: {logger.summarize_exception(exc)}")
        return False

    try:
        import os
        debug_png = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "vlm_debug_last.png")
        with open(debug_png, "wb") as _f:
            _f.write(screenshot)
        logger.debug(f"验证码截图已保存: {debug_png}")
    except Exception:
        pass

    logger.info(f"检测到安全验证,指令: {instruction}")
    points = await _request_points(config, screenshot, instruction)
    if not points:
        return False

    for nx, ny in points:
        absolute_x, absolute_y = to_absolute_point(shot_box, nx, ny)
        try:
            await page.mouse.click(absolute_x, absolute_y)
        except Exception as exc:
            logger.warn(f"验证码点击失败: {logger.summarize_exception(exc)}")
            return False
        await asyncio.sleep(0.6)

    logger.info(f"已按识别结果点击 {len(points)} 个字符.")

    waited = 0.0
    while waited < AFTER_CLICK_WAIT:
        await asyncio.sleep(0.5)
        waited += 0.5
        if not await has_visible_verification(page):
            logger.info("自动验证已通过.", shift=True)
            return True

    logger.warn("点击后验证码仍在,将转为人工处理.", shift=True)
    return False
