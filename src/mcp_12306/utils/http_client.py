"""
集中式 HTTP 客户端，用于 12306 API 交互。
管理会话持久性、重定向跟踪和反爬虫规避。
"""

import asyncio
import logging
import random
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 现代浏览器 User-Agent 池（随机轮换）
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

# 12306 API 基础 URL（不含动态后缀）
_BASE_URLS = {
    "init": "https://kyfw.12306.cn/otn/leftTicket/init",
    "query_left_ticket": "https://kyfw.12306.cn/otn/leftTicket/",
    "query_transfer": "https://kyfw.12306.cn/lcquery/",
    "query_price": "https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice",
    "query_route_stations": "https://kyfw.12306.cn/otn/czxx/queryByTrainNo",
}

# 默认查询后缀（会被 _discover_suffix 自动更新）
_DEFAULT_QUERY_SUFFIX = "queryG"

# 用于从 init 页面 JS 中提取最新 API 后缀的正则模式
_SUFFIX_PATTERNS = {
    "left_ticket": [
        re.compile(r"/otn/leftTicket/(query[A-Z])\b"),
        re.compile(r"""var\s+(?:api_)?(?:path|suffix|query)\s*[:=]\s*['"](query[A-Z])['"]"""),
    ],
    "lcquery": [
        re.compile(r"/lcquery/(query[A-Z])\b"),
    ],
}

# 向 12306 发请求时需携带的查询上下文 Cookie 键
_JC_SAVE_KEYS = [
    "_jc_save_fromStation",
    "_jc_save_toStation",
    "_jc_save_fromDate",
    "_jc_save_toDate",
]


class RailwayHTTPClient:
    """用于 12306 API 的单例 HTTP 客户端，具有会话持久性和路径自动发现功能。"""

    _instance: Optional["RailwayHTTPClient"] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._query_suffixes: Dict[str, str] = {
            "left_ticket": _DEFAULT_QUERY_SUFFIX,
            "lcquery": _DEFAULT_QUERY_SUFFIX,
        }
        self._init_done: bool = False

    @classmethod
    def get_instance(cls) -> "RailwayHTTPClient":
        """获取或创建单例实例（线程安全）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_client(self) -> httpx.AsyncClient:
        """确保底层 httpx 客户端已初始化。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(15.0, connect=10.0),
                verify=False,
            )
        return self._client

    def _build_base_headers(self) -> Dict[str, str]:
        """构建每次请求的基础请求头。"""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
            "Host": "kyfw.12306.cn",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://kyfw.12306.cn",
        }

    def _update_cookies_for_query(
        self, from_station: str, to_station: str, train_date: str
    ) -> None:
        """设置 12306 期望随查询请求一起发送的 _jc_save_* cookie。"""
        if self._client is None:
            return
        cookie_values = {
            "_jc_save_fromStation": urllib.parse.quote(from_station),
            "_jc_save_toStation": urllib.parse.quote(to_station),
            "_jc_save_fromDate": train_date,
            "_jc_save_toDate": train_date,
        }
        for key, value in cookie_values.items():
            self._client.cookies.set(key, value)

    def _discover_suffix(self, html_text: str) -> None:
        """从 init 页面的 HTML/JS 中提取当前 API 路径后缀。"""
        for suffix_key, patterns in _SUFFIX_PATTERNS.items():
            current = self._query_suffixes.get(suffix_key, _DEFAULT_QUERY_SUFFIX)
            for pattern in patterns:
                match = pattern.search(html_text)
                if match:
                    new_suffix = match.group(1)
                    if new_suffix and new_suffix != current:
                        logger.info(
                            "发现新的 [%s] 路径后缀: %s (之前: %s)",
                            suffix_key, new_suffix, current,
                        )
                        self._query_suffixes[suffix_key] = new_suffix
                    break

    async def init_session(self) -> httpx.Response:
        """访问 init 页面以建立 Cookie 并发现路径后缀。"""
        client = await self.ensure_client()
        headers = self._build_base_headers()
        resp = await client.get(_BASE_URLS["init"], headers=headers)
        self._init_done = True
        self._discover_suffix(resp.text)
        return resp

    def get_url(self, key: str) -> str:
        """获取指定 API 的完整 URL（自动附加动态后缀）。"""
        if key == "query_left_ticket":
            suffix = self._query_suffixes.get("left_ticket", _DEFAULT_QUERY_SUFFIX)
            return _BASE_URLS["query_left_ticket"] + suffix
        if key == "query_transfer":
            suffix = self._query_suffixes.get("lcquery", _DEFAULT_QUERY_SUFFIX)
            return _BASE_URLS["query_transfer"] + suffix
        return _BASE_URLS[key]

    async def request(
        self,
        method: str,
        url_key: str,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        station_info: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        对 12306 API 端点发起请求。

        - 首次请求自动调用 init_session() 以获取 Cookie
        - 若响应非 JSON（路径可能已变），自动重新 init + 重试一次
        - 根据 station_info 自动设置 _jc_save_* Cookie
        """
        async with self._lock:
            client = await self.ensure_client()
            if not self._init_done:
                logger.info("首次请求，初始化 12306 会话...")
                await self.init_session()

        # 构建请求头（基础头 + 调用方覆盖）
        request_headers = self._build_base_headers()
        if headers:
            request_headers.update(headers)

        # 若提供了查询上下文信息，设置相应的 Cookie
        if station_info:
            self._update_cookies_for_query(
                station_info.get("from_station", ""),
                station_info.get("to_station", ""),
                station_info.get("train_date", ""),
            )

        url = self.get_url(url_key)
        logger.debug("请求 12306: %s?%s", url, params)

        resp = await client.request(
            method, url, params=params, headers=request_headers, **kwargs
        )

        # 若返回非 JSON（路径可能已过时），尝试重新发现后缀并重试
        content_type = resp.headers.get("content-type", "")
        if (
            resp.status_code == 200
            and "application/json" not in content_type
            and "text/json" not in content_type
        ):
            logger.warning(
                "响应非 JSON (content-type=%s)，尝试路径后缀重新发现...", content_type
            )
            async with self._lock:
                await self.init_session()
                url = self.get_url(url_key)
            resp = await client.request(
                method, url, params=params, headers=request_headers, **kwargs
            )
            logger.info(
                "重试后状态码: %s, content-type: %s",
                resp.status_code, resp.headers.get("content-type", ""),
            )

        return resp

    async def close(self) -> None:
        """关闭底层 HTTP 客户端并重置状态。"""
        async with self._lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
            self._init_done = False
            self._query_suffixes = {
                "left_ticket": _DEFAULT_QUERY_SUFFIX,
                "lcquery": _DEFAULT_QUERY_SUFFIX,
            }


# 便捷函数：获取全局单例
def get_railway_client() -> RailwayHTTPClient:
    """获取全局共享的 12306 HTTP 客户端实例。"""
    return RailwayHTTPClient.get_instance()
