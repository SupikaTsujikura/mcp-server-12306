"""
用于 12306 API 请求的通用辅助函数，提供统一的重试、分页和错误处理。
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .http_client import RailwayHTTPClient

logger = logging.getLogger(__name__)

# 用户可见的错误消息（保持与 server.py 原有风格一致）
_ERROR_BLOCKED = "12306反爬虫拦截，请稍后重试或更换网络环境"
_ERROR_NETWORK = "网络请求失败，请检查网络连接"


async def make_12306_request(
    client: RailwayHTTPClient,
    url_key: str,
    params: Optional[Dict[str, str]] = None,
    station_info: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> Dict[str, Any]:
    """
    向 12306 API 发出请求，包含重试和 JSON 验证。

    返回解析后的 JSON 字典。

    抛出 ValueError：
    - 网络错误在 max_retries 次后仍失败
    - 响应状态码不是 200
    - 响应不是有效的 JSON
    - 检测到反爬虫页面（error.html 等）
    """
    last_exception: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = await client.request(
                "GET", url_key, params=params, station_info=station_info
            )

            # 检查状态码
            if resp.status_code != 200:
                raise ValueError(f"12306接口返回异常状态码: {resp.status_code}")

            # 检查是否被重定向到错误页面（跟随重定向后检查最终 URL）
            final_url = str(resp.url)
            if "error.html" in final_url or "ntce" in final_url:
                raise ValueError(_ERROR_BLOCKED)

            # 验证 JSON
            try:
                return resp.json()
            except Exception as e:
                logger.error(
                    "12306响应解析失败: %s，原始内容前200字符: %s",
                    e, resp.text[:200],
                )
                raise ValueError(f"12306响应解析失败: {e}")

        except ValueError:
            # 直接抛出业务错误（反爬虫、JSON 解析失败等），不重试
            raise

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            last_exception = e
            if attempt < max_retries - 1:
                logger.warning(
                    "网络请求失败，正在重试 (%d/%d): %s",
                    attempt + 1, max_retries, e,
                )
                await asyncio.sleep(retry_delay)
            else:
                raise ValueError(f"网络请求失败 (已重试{max_retries}次): {e}")

    # 防御性返回（不应执行到此）
    raise ValueError(f"{_ERROR_NETWORK}: {last_exception}")


async def make_paginated_12306_request(
    client: RailwayHTTPClient,
    url_key: str,
    base_params: Dict[str, str],
    station_info: Optional[Dict[str, str]] = None,
    page_size: int = 10,
    max_pages: int = 20,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    用于中转查询的分页请求辅助函数。
    循环遍历 result_index 直到数据用尽或达到 max_pages。
    """
    all_data: List[Dict[str, Any]] = []

    for attempt in range(max_retries):
        try:
            result_index = 0
            page_num = 1

            while page_num <= max_pages:
                params = dict(base_params)
                params["result_index"] = str(result_index)

                json_data = await make_12306_request(
                    client,
                    url_key,
                    params=params,
                    station_info=station_info,
                    # 内部单次请求重试由外层循环覆盖
                    max_retries=1,
                )

                data = json_data.get("data", {})
                if not isinstance(data, dict):
                    logger.warning("12306返回的data字段非预期格式: %s, 内容: %s", type(data), str(data)[:200])
                    return all_data if all_data else []
                logger.info("12306中转查询响应 data keys: %s, middleList 长度: %d",
                           list(data.keys()), len(data.get("middleList", [])))
                items = data.get("middleList", [])
                if not items:
                    logger.info("12306中转查询返回空数据, data keys: %s", list(data.keys()) if isinstance(data, dict) else type(data))
                    return all_data

                all_data.extend(items)

                if len(items) < page_size:
                    return all_data

                result_index += page_size
                page_num += 1

            return all_data

        except ValueError as e:
            # 反爬虫或 JSON 解析错误直接抛出
            if "反爬" in str(e) or "解析失败" in str(e) or "异常状态码" in str(e):
                raise
            if attempt < max_retries - 1:
                logger.warning(
                    "分页请求失败，正在重试 (%d/%d): %s",
                    attempt + 1, max_retries, e,
                )
                await asyncio.sleep(1)
            else:
                raise

    return all_data
