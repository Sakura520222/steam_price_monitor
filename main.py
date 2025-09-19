import re
import httpx
import traceback
import asyncio  # 补充导入
import datetime
import json
import os
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
import astrbot.api.message_components as Comp
from .price_convert import to_cny
from apscheduler.schedulers.asyncio import AsyncIOScheduler

ITAD_API_BASE = "https://api.isthereanydeal.com"
STEAMWEBAPI_PRICES = "https://api.steamwebapi.com/steam/prices"

@register("astrbot_plugins_steam_shop_price", "Maoer", "查询Steam游戏价格及史低", "1.0.0", "https://github.com/Maoer233/astrbot_plugins_steam_shop_price")
class SteamPricePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.itad_api_key = self.config.get("ITAD_API_KEY", "")
        self.steamwebapi_key = self.config.get("STEAMWEBAPI_KEY", "")
        self.compare_region = self.config.get("STEAM_COMPARE_REGION", "UA")
        
        # 价格监控相关初始化
        self.enable_price_monitor = self.config.get("ENABLE_PRICE_MONITOR", True)
        self.monitor_interval = self.config.get("PRICE_MONITOR_INTERVAL", 30)
        
        # 数据文件路径
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugins_steam_shop_price"))
        self.monitor_list_path = self.data_dir / "price_monitor_list.json"
        
        # 确保数据目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化监控列表
        self.monitor_list = {}
        self.monitor_list_lock = asyncio.Lock()
        
        # 初始化调度器
        self.scheduler = AsyncIOScheduler()
        
        # 如果启用价格监控，启动定时任务
        if self.enable_price_monitor:
            self.scheduler.add_job(
                self.run_price_monitor, 
                "interval", 
                minutes=self.monitor_interval
            )
            self.scheduler.start()
            logger.info(f"价格监控功能已启用，检查间隔：{self.monitor_interval}分钟")
        
        # 加载监控列表
        asyncio.create_task(self.load_monitor_list())

    @filter.command("史低")
    async def shidi(self, event: AstrMessageEvent, url: str, last_gid=None):
        '''查询Steam游戏价格及史低信息，格式：/史低 <steam商店链接/游戏名>'''
        # 新增：自动识别链接或游戏名
        # 修复参数丢失问题，直接用 event.message_str 去除指令前缀，保留全部参数内容
        raw_msg = event.message_str
        prefix_pattern = r"^[\.／/]*(史低|价格)\s*"
        param_str = re.sub(prefix_pattern, "", raw_msg, count=1, flags=re.IGNORECASE)
        # param_str 现在包含所有参数（包括空格和数字）
        if not param_str.lower().startswith("http"):
            # 检查是否为中文游戏名
            is_chinese = re.search(r'[\u4e00-\u9fff]', param_str)
            
            # 使用ITAD API搜索（优先使用英文搜索）
            search_name = param_str
            appid = None
            
            if is_chinese:
                # 如果是中文游戏名，先尝试翻译为英文进行ITAD搜索
                try:
                    prompt = f"请将以下游戏名翻译为steam商店页面的英文官方名称，仅输出英文名，不要输出其他内容：{param_str}"
                    llm_response = await self.context.get_using_provider().text_chat(
                        prompt=prompt,
                        contexts=[],
                        image_urls=[],
                        func_tool=None,
                        system_prompt=""
                    )
                    search_name = llm_response.completion_text.strip()
                    logger.info(f"[LLM][翻译游戏名] 中文: {param_str} -> 英文: {search_name}")
                except Exception as e:
                    logger.error(f"LLM翻译游戏名失败: {e}")
                    # 翻译失败，继续使用中文搜索
                    search_name = param_str
            
            yield event.plain_result(f"正在为主人搜索《{param_str}》，主人等一小会喵...")
            
            # 第一步：使用ITAD API搜索（英文）
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        f"{ITAD_API_BASE}/games/search/v1",
                        params={"key": self.itad_api_key, "title": search_name, "limit": 5}
                    )
                    data = resp.json()
                    
                    if data and isinstance(data, list) and len(data) > 0:
                        # 取第一个匹配的游戏
                        game = data[0]
                        game_name = game.get("title", "")
                        
                        # 获取AppID
                        for url_item in game.get("urls", []):
                            if "store.steampowered.com/app/" in url_item:
                                match = re.match(r".*store\.steampowered\.com/app/(\d+).*", url_item)
                                if match:
                                    appid = match.group(1)
                                    break
                        
                        if appid:
                            logger.info(f"[ITAD搜索成功] 游戏: {game_name}, AppID: {appid}")
                        else:
                            logger.warning(f"[ITAD搜索] 找到游戏但无法获取AppID: {game_name}")
                            appid = None
                    else:
                        logger.warning(f"[ITAD搜索] 未找到游戏: {search_name}")
                        appid = None
                        
            except Exception as e:
                logger.error(f"ITAD搜索失败: {e}")
                appid = None
            
            # 第二步：如果ITAD搜索失败且是中文游戏名，尝试中文备用搜索
            if not appid and is_chinese:
                try:
                    logger.info(f"[备用搜索-中文] 尝试中文搜索: {param_str}")
                    # 使用Steam商店搜索API（中文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": param_str, "l": "schinese", "cc": "CN"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-中文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-中文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-中文] 未找到游戏: {param_str}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-中文失败: {e}")
            
            # 第三步：如果中文搜索也失败，尝试英文备用搜索
            if not appid and is_chinese and search_name != param_str:
                try:
                    logger.info(f"[备用搜索-英文] 尝试英文搜索: {search_name}")
                    # 使用Steam商店搜索API（英文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": search_name, "l": "english", "cc": "US"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-英文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-英文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-英文] 未找到游戏: {search_name}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-英文失败: {e}")
            
            # 第四步：如果是英文游戏名且ITAD搜索失败，尝试英文备用搜索
            if not appid and not is_chinese:
                try:
                    logger.info(f"[备用搜索-英文] 尝试英文搜索: {param_str}")
                    # 使用Steam商店搜索API（英文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": param_str, "l": "english", "cc": "US"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-英文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-英文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-英文] 未找到游戏: {param_str}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-英文失败: {e}")
            
            if not appid:
                yield event.plain_result("未找到该游戏，请检查游戏名是否正确，或尝试直接输入Steam商店链接。")
                return
            
            # 如果成功获取到AppID，直接进入链接查询流程
            steam_url = f"https://store.steampowered.com/app/{appid}"
            async for result in self._query_by_url(event, steam_url):
                yield result
            return
        else:
            url = param_str
            # 直接进入链接查询流程
            async for result in self._query_by_url(event, url):
                yield result
            return

    async def _query_by_url(self, event, url):
        # 复制原有链接查询流程（appid解析及后续逻辑）
        m = re.match(r"https?://store\.steampowered\.com/app/(\d+)", url)
        if not m:
            yield event.plain_result("请提供正确的Steam商店链接！")
            return
        appid = m.group(1)
        # ...后续逻辑保持不变...
        # --- 并发请求国区Steam信息、ITAD信息、对比区Steam价格 ---
        async def fetch_steam_cn():
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese"
                    )
                    data = resp.json()
                    app_data = data.get(appid, {}).get("data", {})
                    steam_name = app_data.get("name")
                    header_img = app_data.get("header_image")
                    steam_image = None
                    if header_img:
                        small_img = header_img.replace("_header.jpg", "_capsule_184x69.jpg")
                        img_resp = await client.get(small_img)
                        if img_resp.status_code == 200:
                            steam_image = small_img
                        else:
                            steam_image = header_img
                    return steam_name, steam_image
            except Exception as e:
                logger.error(f"获取Steam国区游戏信息失败: {e}\n{traceback.format_exc()}")
                return None, None

        async def fetch_itad_lookup():
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        f"{ITAD_API_BASE}/games/lookup/v1",
                        params={"key": self.itad_api_key, "appid": appid}
                    )
                    data = resp.json()
                    gid = data["game"]["id"] if data.get("found") else None
                    logger.info(f"[ITAD][lookup] 成功获取 ITAD gid: {gid}")
                    if not data.get("found"):
                        return None
                    return gid
            except Exception as e:
                logger.error(f"获取ITAD gid失败: {e}\n{traceback.format_exc()}")
                return None

        async def fetch_compare_price():
            if self.compare_region.upper() == "NONE":
                return None, None, 0
            region = self.compare_region.upper()
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        "https://store.steampowered.com/api/appdetails",
                        params={"appids": appid, "cc": region.lower(), "l": "en"}
                    )
                    data = resp.json()
                    app_data = data.get(appid, {})
                    if app_data.get("success") and app_data.get("data"):
                        price_overview = app_data["data"].get("price_overview")
                        if price_overview and "final" in price_overview and "currency" in price_overview:
                            price = price_overview["final"] / 100
                            currency = price_overview["currency"]
                            logger.info(f"[STEAM][{region}] 成功获取价格: {price} {currency}")
                            return price, currency, price_overview.get("discount_percent", 0)
                    logger.info(f"[STEAM][{region}] 未获取到价格信息")
                    return None, None, 0
            except Exception as e:
                logger.error(f"获取{region}区实时价格失败: {e}\n{traceback.format_exc()}")
                return None, None, 0

        # 并发执行
        results = await asyncio.gather(
            fetch_steam_cn(),
            fetch_itad_lookup(),
            fetch_compare_price()
        )
        steam_name, steam_image = results[0]
        gid = results[1]
        compare_price, compare_currency, compare_discount_percent = results[2]

        # steam_name, steam_image = ...; gid = ...; compare_price, compare_currency, compare_discount_percent = ...
        # 兼容 yield event.plain_result
        if gid is None:
            yield event.plain_result("未找到该游戏的 isthereanydeal id \n（试一下换个名称搜索一下）。")
            return

        # ITAD游戏基本信息
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{ITAD_API_BASE}/games/info/v2",
                    params={"key": self.itad_api_key, "id": gid}
                )
                info = resp.json()
                logger.info(f"[ITAD][info] 成功获取游戏信息: {info.get('title', '未知游戏')}")
                name = info.get("title", "未知游戏")
                tags = ", ".join(info.get("tags", []))
                release = info.get("releaseDate", "")
                devs = ", ".join([d["name"] for d in info.get("developers", [])]) if info.get("developers") else ""
                itad_url = info.get("urls", {}).get("game", "")
                steam_review = ""
                for r in info.get("reviews", []):
                    if r.get("source") == "Steam":
                        steam_review = f"{r.get('score', '')}%"
                        break
        except Exception as e:
            logger.error(f"获取ITAD游戏信息失败: {e}\n{traceback.format_exc()}")
            name = tags = release = devs = itad_url = steam_review = ""

        # 国区价格和史低（ITAD）
        try:
            cn_price, cn_lowest, cn_currency, regular = await self._get_price_and_lowest(gid, "CN")
        except Exception as e:
            logger.error(f"获取ITAD价格失败: {e}\n{traceback.format_exc()}")
            cn_price = cn_lowest = cn_currency = regular = None

        # 如果ITAD没有国区价格，则用Steam官方API补充当前国区价格
        if cn_price is None:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        "https://store.steampowered.com/api/appdetails",
                        params={"appids": appid, "cc": "cn", "l": "zh"}
                    )
                    data = resp.json()
                    app_data = data.get(appid, {})
                    if app_data.get("success") and app_data.get("data"):
                        price_overview = app_data["data"].get("price_overview")
                        if price_overview and "final" in price_overview and "currency" in price_overview:
                            cn_price = price_overview["final"] / 100
                            cn_currency = price_overview["currency"]
                        # 只补充当前价，不补充史低，史低始终以ITAD为准
            except Exception as e:
                logger.error(f"补充获取Steam国区实时价格失败: {e}\n{traceback.format_exc()}")

        # 获取乌克兰区实时价格（Steam官方API）
        ua_price = ua_currency = None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={"appids": appid, "cc": "ua", "l": "en"}
                )
                data = resp.json()
                app_data = data.get(appid, {})
                if app_data.get("success") and app_data.get("data"):
                    price_overview = app_data["data"].get("price_overview")
                    if price_overview and "final" in price_overview and "currency" in price_overview:
                        ua_price = price_overview["final"] / 100
                        ua_currency = price_overview["currency"]
                        logger.info(f"[STEAM][UA] 成功获取价格: {ua_price} {ua_currency}")
                    else:
                        logger.info(f"[STEAM][UA] 未获取到价格信息")
                        ua_price = ua_currency = None
                else:
                    logger.info(f"[STEAM][UA] 未获取到价格信息")
                    ua_price = ua_currency = None
        except Exception as e:
            logger.error(f"获取乌克兰区实时价格失败: {e}\n{traceback.format_exc()}")
            ua_price = ua_currency = None

        # 5. 汇率（手动定义，不再请求第三方）
        uah2cny = 0.1718  # 1UAH=0.1718人民币
        usd2cny = 7.2     # 如有需要可手动调整

        # 6. 货币转换
        price_diff = ""
        cn_cny = to_cny(cn_price, cn_currency)
        compare_cny = to_cny(compare_price, compare_currency)

        # 7. 修正史低折扣百分比算法，优先用原价
        def percent_drop(now, low, regular=None):
            """
            计算折扣百分比。
            - now: 当前价
            - low: 史低价
            - regular: 原价（可选，若有则用原价和史低价算史低折扣）
            """
            if regular and low and regular > 0:
                return f"-{round((1-low/regular)*100):.0f}%"
            if now and low and now > 0:
                return f"-{round((1-low/now)*100):.0f}%"
            return "未知"

        # 史低折扣百分比
        shidi_percent = percent_drop(cn_price, cn_lowest, regular)

        # 8. 价格差（国区/对比区）
        price_diff = ""
        cn_cny = to_cny(cn_price, cn_currency)
        compare_cny = to_cny(compare_price, compare_currency)
        if cn_cny is not None and compare_cny is not None and compare_cny > 0:
            diff_val = cn_cny - compare_cny
            diff_percent = ((cn_cny - compare_cny) / compare_cny * 100)
            if diff_val < 0:
                price_diff = f"国区更便宜喵！便宜{abs(diff_val):.2f}元呢！ ({diff_percent:.2f}%)"
            else:
                price_diff = f"国区更贵喵，多花{diff_val:.2f}元呢！ (+{diff_percent:.2f}%)"
        else:
            price_diff = "无法获取当前价差"

        # 9. 金额格式化
        def fmt(price, currency):
            if price is None or currency is None:
                return "未知"
            symbol = "￥" if currency == "CNY" else "₴" if currency == "UAH" else "$" if currency == "USD" else currency + " "
            return f"{symbol}{price:.2f}"

        # 国区当前折扣
        cn_discount = ""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={"appids": appid, "cc": "cn", "l": "zh"}
                )
                data = resp.json()
                app_data = data.get(appid, {})
                if app_data.get("success") and app_data.get("data"):
                    price_overview = app_data["data"].get("price_overview")
                    if price_overview and "discount_percent" in price_overview and price_overview["discount_percent"] > 0:
                        cn_discount = f"-{price_overview['discount_percent']}%"
        except Exception as e:
            logger.error(f"获取国区实时折扣失败: {e}\n{traceback.format_exc()}")

        # 对比区当前折扣
        compare_discount = ""
        if compare_discount_percent and compare_discount_percent > 0:
            compare_discount = f"-{compare_discount_percent}%"
        # 对比区显示人民币对比
        compare_cny = to_cny(compare_price, compare_currency)
        compare_price_str = fmt(compare_price, compare_currency)
        # 修正：只有 compare_cny 不为 None 且大于0 时才拼接人民币价格
        if compare_cny is not None and compare_cny > 0:
            compare_price_str += f" （￥{compare_cny:.2f}）"
        if compare_discount:
            compare_price_str += f"{compare_discount}"

        # 国区价格字符串
        cn_price_str = fmt(cn_price, cn_currency)
        if cn_discount:
            cn_price_str += f" {cn_discount}"

        # 价格差（严格只显示百分比，前面加提示文字）
        if self.compare_region.upper() == "NONE":
            compare_price_str = "(未进行对比)"
            price_diff = ""
        else:
            compare_price_str = fmt(compare_price, compare_currency)
            if compare_discount_percent and compare_discount_percent > 0:
                compare_discount = f"-{compare_discount_percent}%"
                compare_price_str += f" {compare_discount}"
            compare_cny = to_cny(compare_price, compare_currency)
            if compare_cny is not None and compare_cny > 0:
                compare_price_str += f" （￥{compare_cny:.2f}）"
            if cn_cny is not None and compare_cny is not None and compare_cny > 0:
                diff_val = cn_cny - compare_cny
                diff_percent = ((cn_cny - compare_cny) / compare_cny * 100)
                if diff_val < 0:
                    price_diff = f"国区更便宜喵！便宜{abs(diff_val):.2f}元呢！ ({diff_percent:.2f}%)"
                else:
                    price_diff = f"国区更贵喵，多花{diff_val:.2f}元呢！ (+{diff_percent:.2f}%)"
            else:
                price_diff = "无法获取当前价差"

        # 构建精简消息链
        chain = []
        if steam_image:
            chain.append(Comp.Image.fromURL(steam_image))
        # 优先用国区中文名
        display_name = steam_name if steam_name else name

        # 优化输出格式：不对比时不显示“区价格: (未进行对比)”和多余换行
        if self.compare_region.upper() == "NONE":
            msg = (
                f"{display_name}\n"
                f"国区价格: {cn_price_str}\n"
                f"史低: {fmt(cn_lowest, cn_currency)} {shidi_percent}\n"
            )
        else:
            msg = (
                f"{display_name}\n"
                f"国区价格: {cn_price_str}\n"
                f"史低: {fmt(cn_lowest, cn_currency)} {shidi_percent}\n"
                f"\n"
                f"{self.compare_region}区价格: {compare_price_str}\n"
                f"\n"
                f"{price_diff}\n"
            )
        # 去除多余的游戏名（中括号内内容）
        import re as _re
        msg = _re.sub(r"\[.*?\]", "", msg)
        if steam_review:
            msg += f"好评率: {steam_review}"
        if appid:
            msg += f"\nsteam商店链接：https://store.steampowered.com/app/{appid}"
        chain.append(Comp.Plain(msg))
        yield event.chain_result(chain)

    @filter.command("搜索游戏")
    async def search_game(self, event: AstrMessageEvent, name: str):
        '''查找Steam游戏，格式：/查找游戏 <中文游戏名>，会展示多个结果的封面和原名'''
        try:
            # 1. LLM翻译
            prompt = f"请将以下游戏名翻译为steam页面的英文官方名称，仅输出英文名，不要输出其他内容：{name}"
            logger.info(f"[LLM][查找游戏] 输入prompt: {prompt}")
            llm_response = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                contexts=[],
                image_urls=[],
                func_tool=None,
                system_prompt=""
            )
            game_en_name = llm_response.completion_text.strip()
            logger.info(f"[LLM][查找游戏] 输出: {game_en_name}")
            # 修改提示，带上英文名
            yield event.plain_result(f"正在为主人查找游戏《{game_en_name}》，请稍等...")
        except Exception as e:
            logger.error(f"LLM翻译游戏名失败: {e}")
            yield event.plain_result("游戏名翻译失败，请重试。")
            return

        # 2. ITAD搜索
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{ITAD_API_BASE}/games/search/v1",
                    params={"key": self.itad_api_key, "title": game_en_name, "limit": 8}
                )
                data = resp.json()
                logger.info(f"[ITAD][search_game] 返回: {data}")
                if not data or not isinstance(data, list):
                    yield event.plain_result("未找到相关游戏。")
                    return
                # 3. 组装消息链
                chain = []
                from PIL import Image as PILImage
                import io
                import httpx as _httpx
                for game in data[:10]:
                    title = game.get("title", "未知")
                    # 优先用 boxart 或 banner145
                    img_url = ""
                    assets = game.get("assets", {})
                    # 优先选小图（宽高不超过100）
                    if assets.get("banner145"):
                        img_url = assets["banner145"]
                    elif assets.get("boxart"):
                        img_url = assets["boxart"]
                    elif assets.get("banner300"):
                        img_url = assets["banner300"]
                    elif assets.get("banner400"):
                        img_url = assets["banner400"]
                    elif assets.get("banner600"):
                        img_url = assets["banner600"]
                    # 获取价格（需进一步查info接口）
                    price_str = ""
                    try:
                        async with httpx.AsyncClient(timeout=8) as client2:
                            resp2 = await client2.get(
                                f"{ITAD_API_BASE}/games/info/v2",
                                params={"key": self.itad_api_key, "id": game.get("id")}
                            )
                            info2 = resp2.json()
                            # 取国区价格
                            price = None
                            currency = None
                            if "prices" in info2 and isinstance(info2["prices"], dict):
                                cn_price = info2["prices"].get("CN")
                                if cn_price and "price" in cn_price:
                                    price = cn_price["price"].get("amount")
                                    currency = cn_price["price"].get("currency")
                            if price is not None and currency:
                                price_str = f"￥{price:.2f}" if currency == "CNY" else f"{currency} {price:.2f}"
                    except Exception as e:
                        logger.error(f"查找游戏价格失败: {e}")
                    # 拼装消息
                    if img_url:
                        # 下载图片并压缩到100x100以内
                        try:
                            async with _httpx.AsyncClient(timeout=8) as img_client:
                                img_resp = await img_client.get(img_url)
                                img_resp.raise_for_status()
                                img_bytes = img_resp.content
                                with io.BytesIO(img_bytes) as f:
                                    with PILImage.open(f) as pil_img:
                                        pil_img = pil_img.convert("RGB")
                                        pil_img.thumbnail((200, 200))
                                        buf = io.BytesIO()
                                        pil_img.save(buf, format="JPEG")
                                        buf.seek(0)
                                        img_b64 = buf.read()
                                        import base64
                                        img_b64_str = base64.b64encode(img_b64).decode("utf-8")
                                        chain.append(Comp.Image.fromBase64(img_b64_str))
                        except Exception as e:
                            logger.error(f"图片压缩失败: {e}")
                    chain.append(Comp.Plain(f"{title}" + (f"  {price_str}" if price_str else "")))
                if not chain:
                    yield event.plain_result("未找到相关游戏。")
                    return
                # 不再追加“或许你要找的游戏是这些？”
                yield event.chain_result(chain)
        except Exception as e:
            logger.error(f"ITAD查找游戏失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result("查找游戏失败，请重试。")

    async def _get_price_and_lowest(self, gid, country):
        # 用/games/prices/v3 POST获取指定区价格和史低
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{ITAD_API_BASE}/games/prices/v3",
                    params={"key": self.itad_api_key, "country": country, "shops": 61},  # 61=Steam
                    json=[gid]
                )
                data = resp.json()
                # 只输出关键信息
                logger.info(f"[ITAD][prices][{country}] 成功获取价格和史低信息")
                
                # 先检查数据有效性，再访问数组元素
                if not data or not isinstance(data, list) or len(data) == 0 or not data[0].get("deals"):
                    logger.warning(f"[ITAD][prices][{country}] 未找到价格数据或数据格式错误")
                    return None, None, None, None
                
                logger.debug(f"[ITAD][prices][{country}] historyLow调试: {data[0].get('historyLow', {})}")
                deals = data[0]["deals"]
                # 取Steam的当前价和原价
                price = None
                currency = None
                regular = None
                for d in deals:
                    if d.get("shop", {}).get("name", "").lower() == "steam":
                        price = d.get("price", {}).get("amount")
                        currency = d.get("price", {}).get("currency")
                        if d.get("regular") and "amount" in d["regular"]:
                            regular = d["regular"]["amount"]
                        break
                # 取史低价
                lowest = None
                history_low = data[0].get("historyLow", {})
                for k in ["m3", "y1", "all"]:
                    if history_low.get(k) and "amount" in history_low[k]:
                        lowest = history_low[k]["amount"]
                        break
                return price, lowest, currency, regular
        except Exception as e:
            logger.error(f"_get_price_and_lowest error: {e}\n{traceback.format_exc()}")
            return None, None, None, None

    # ==================== 价格监控功能 ====================

    async def load_monitor_list(self):
        """加载价格监控列表"""
        try:
            if self.monitor_list_path.exists():
                async with self.monitor_list_lock:
                    with open(self.monitor_list_path, 'r', encoding='utf-8') as f:
                        self.monitor_list = json.load(f)
                logger.info(f"价格监控列表加载成功，共 {len(self.monitor_list)} 个游戏")
            else:
                # 创建空的监控列表文件
                async with self.monitor_list_lock:
                    with open(self.monitor_list_path, 'w', encoding='utf-8') as f:
                        json.dump({}, f, ensure_ascii=False, indent=2)
                logger.info("创建新的价格监控列表文件")
        except Exception as e:
            logger.error(f"加载价格监控列表失败: {e}")
            self.monitor_list = {}

    async def save_monitor_list(self):
        """保存价格监控列表"""
        try:
            async with self.monitor_list_lock:
                with open(self.monitor_list_path, 'w', encoding='utf-8') as f:
                    json.dump(self.monitor_list, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存价格监控列表失败: {e}")

    def _parse_unified_origin(self, origin: str):
        """
        解析 unified_msg_origin 字符串，提取平台、消息类型、用户ID和群ID
        格式示例: aiocqhttp:FriendMessage:UserID
                  aiocqhttp:GroupMessage:UserID_GroupID (带会话隔离)
                  aiocqhttp:GroupMessage:GroupID (不带会话隔离)
        """
        parts = origin.split(":")
        platform = parts[0]
        message_type = parts[1]
        identifiers = parts[2]

        user_id = None
        group_id = None

        if message_type == "FriendMessage":
            user_id = identifiers
        elif message_type == "GroupMessage":
            if "_" in identifiers:
                user_id, group_id = identifiers.split("_")
            else:
                group_id = identifiers

        return {
            "platform": platform,
            "message_type": message_type,
            "user_id": user_id,
            "group_id": group_id,
        }

    async def get_steam_price_for_monitor(self, appid, region="cn"):
        """
        获取游戏价格信息（用于监控）
        Args:
            appid (str or int): Steam 游戏的 AppID
            region (str): 区域代码，默认为 "cn" (中国)
        Returns:
            dict or None: 包含价格信息的字典，或 None（如果获取失败或游戏不存在）
        """
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l=zh-cn"
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url)
                data = resp.json()

            app_data = data.get(str(appid), {})
            if not app_data.get("success"):
                logger.warning(f"获取游戏 {appid} 价格失败或游戏不存在")
                return None

            game_data = app_data["data"]
            
            # 免费游戏
            if game_data.get("is_free"):
                return {
                    "is_free": True,
                    "current_price": 0,
                    "original_price": 0,
                    "discount": 100,
                    "currency": "FREE",
                    "name": game_data.get("name", f"AppID: {appid}")
                }

            price_info = game_data.get("price_overview")
            if not price_info:
                logger.info(f"游戏 {game_data.get('name', appid)} 没有价格信息")
                return None

            return {
                "is_free": False,
                "current_price": price_info["final"] / 100,  # 单位转换为元
                "original_price": price_info["initial"] / 100,
                "discount": price_info["discount_percent"],
                "currency": price_info["currency"],
                "name": game_data.get("name", f"AppID: {appid}")
            }
        except Exception as e:
            logger.error(f"获取游戏 {appid} 价格时发生异常：{e}")
            return None

    async def run_price_monitor(self):
        """执行价格监控检查"""
        if not self.enable_price_monitor:
            return
            
        logger.info("开始执行价格监控检查")
        
        # 复制当前监控列表，避免在检查过程中被修改
        current_monitor_list = self.monitor_list.copy()
        
        for appid, game_info in current_monitor_list.items():
            try:
                logger.info(f"检查游戏价格: {game_info.get('name', appid)}")
                
                # 获取当前价格
                price_data = await self.get_steam_price_for_monitor(appid)
                if not price_data:
                    logger.warning(f"无法获取游戏 {appid} 的价格信息")
                    continue

                # 检查价格变动
                last_price = game_info.get("last_price")
                current_price = price_data["current_price"]
                
                # 首次记录价格
                if last_price is None:
                    self.monitor_list[appid]["last_price"] = current_price
                    self.monitor_list[appid]["original_price"] = price_data["original_price"]
                    self.monitor_list[appid]["discount"] = price_data["discount"]
                    await self.save_monitor_list()
                    logger.info(f"首次记录游戏 {price_data['name']} 价格: ¥{current_price:.2f}")
                    continue

                # 检查价格变动
                price_change = current_price - last_price
                
                if price_change != 0:
                    logger.info(f"检测到价格变动: {price_data['name']} 变动: ¥{price_change:.2f}")
                    
                    # 发送价格变动通知
                    await self.send_price_change_notification(appid, game_info, price_data, price_change)
                    
                    # 更新价格记录
                    self.monitor_list[appid]["last_price"] = current_price
                    self.monitor_list[appid]["original_price"] = price_data["original_price"]
                    self.monitor_list[appid]["discount"] = price_data["discount"]
                    await self.save_monitor_list()
                else:
                    logger.info(f"游戏 {price_data['name']} 价格未变动")
                    
            except Exception as e:
                logger.error(f"检查游戏 {appid} 价格时发生错误: {e}")
        
        logger.info("价格监控检查完成")

    async def send_price_change_notification(self, appid, game_info, price_data, price_change):
        """发送价格变动通知"""
        try:
            # 构建消息内容
            if price_data["is_free"]:
                message = f"🎉🎉🎉 游戏《{price_data['name']}》已免费！"
            elif price_change > 0:
                message = f"⬆️ 游戏《{price_data['name']}》价格上涨：¥{price_change:.2f}"
            else:
                message = f"⬇️ 游戏《{price_data['name']}》价格下跌：¥{-price_change:.2f}"
            
            message += f"\n变动前价格：¥{game_info['last_price']:.2f}"
            message += f"\n当前价格：¥{price_data['current_price']:.2f}"
            message += f"\n原价：¥{price_data['original_price']:.2f}"
            message += f"\n折扣：{price_data['discount']}%"
            message += f"\n购买链接：https://store.steampowered.com/app/{appid}"
            
            # 获取订阅者列表
            subscribers = game_info.get("subscribers", [])
            
            for subscriber_origin in subscribers:
                parsed_origin = self._parse_unified_origin(subscriber_origin)
                
                # 构建消息链
                msg_components = [Comp.Plain(text=message)]
                
                # 群聊消息添加@功能
                if parsed_origin["message_type"] == "GroupMessage" and parsed_origin["user_id"]:
                    msg_components.append(Comp.At(qq=parsed_origin["user_id"]))
                
                # 发送消息
                await self.context.send_message(
                    subscriber_origin,
                    MessageChain(msg_components)
                )
                
                # 短暂延迟避免风控
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"发送价格变动通知失败: {e}")

    @filter.command("价格监控", alias={"订阅价格", "监控价格"})
    async def price_monitor_command(self, event: AstrMessageEvent):
        """价格监控命令 - 订阅游戏价格变动"""
        args = event.message_str.strip().split()[1:]
        
        if len(args) < 1:
            yield event.plain_result("使用方法：/价格监控 <游戏名或AppID>\n例如：/价格监控 Cyberpunk 2077 或 /价格监控 1091500")
            return
        
        # 解析游戏名或AppID
        input_text = " ".join(args)
        
        # 检查是否为AppID
        if input_text.isdigit():
            appid = input_text
            # 验证AppID是否存在
            try:
                price_data = await self.get_steam_price_for_monitor(appid)
                if not price_data:
                    yield event.plain_result(f"未找到 AppID 为 {appid} 的游戏，请检查输入是否正确。")
                    return
                game_name = price_data["name"]
            except Exception as e:
                yield event.plain_result(f"验证游戏失败: {e}")
                return
        else:
            # 通过游戏名搜索
            yield event.plain_result(f"正在搜索游戏《{input_text}》，请稍候...")
            
            # 检查是否为中文游戏名
            is_chinese = re.search(r'[\u4e00-\u9fff]', input_text)
            
            # 使用ITAD API搜索（优先使用英文搜索）
            search_name = input_text
            appid = None
            
            if is_chinese:
                # 如果是中文游戏名，先尝试翻译为英文进行ITAD搜索
                try:
                    prompt = f"请将以下游戏名翻译为steam商店页面的英文官方名称，仅输出英文名，不要输出其他内容：{input_text}"
                    llm_response = await self.context.get_using_provider().text_chat(
                        prompt=prompt,
                        contexts=[],
                        image_urls=[],
                        func_tool=None,
                        system_prompt=""
                    )
                    search_name = llm_response.completion_text.strip()
                    logger.info(f"[LLM][翻译游戏名] 中文: {input_text} -> 英文: {search_name}")
                except Exception as e:
                    logger.error(f"LLM翻译游戏名失败: {e}")
                    # 翻译失败，继续使用中文搜索
                    search_name = input_text
            
            # 第一步：使用ITAD API搜索（英文）
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        f"{ITAD_API_BASE}/games/search/v1",
                        params={"key": self.itad_api_key, "title": search_name, "limit": 5}
                    )
                    data = resp.json()
                    
                    if data and isinstance(data, list) and len(data) > 0:
                        # 取第一个匹配的游戏
                        game = data[0]
                        game_name = game.get("title", "")
                        
                        # 获取AppID
                        for url_item in game.get("urls", []):
                            if "store.steampowered.com/app/" in url_item:
                                match = re.match(r".*store\.steampowered\.com/app/(\d+).*", url_item)
                                if match:
                                    appid = match.group(1)
                                    break
                        
                        if appid:
                            logger.info(f"[ITAD搜索成功] 游戏: {game_name}, AppID: {appid}")
                        else:
                            logger.warning(f"[ITAD搜索] 找到游戏但无法获取AppID: {game_name}")
                            appid = None
                    else:
                        logger.warning(f"[ITAD搜索] 未找到游戏: {search_name}")
                        appid = None
                        
            except Exception as e:
                logger.error(f"ITAD搜索失败: {e}")
                appid = None
            
            # 第二步：如果ITAD搜索失败且是中文游戏名，尝试中文备用搜索
            if not appid and is_chinese:
                try:
                    logger.info(f"[备用搜索-中文] 尝试中文搜索: {input_text}")
                    # 使用Steam商店搜索API（中文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": input_text, "l": "schinese", "cc": "CN"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-中文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-中文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-中文] 未找到游戏: {input_text}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-中文失败: {e}")
            
            # 第三步：如果中文搜索也失败，尝试英文备用搜索
            if not appid and is_chinese and search_name != input_text:
                try:
                    logger.info(f"[备用搜索-英文] 尝试英文搜索: {search_name}")
                    # 使用Steam商店搜索API（英文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": search_name, "l": "english", "cc": "US"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-英文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-英文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-英文] 未找到游戏: {search_name}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-英文失败: {e}")
            
            # 第四步：如果是英文游戏名且ITAD搜索失败，尝试英文备用搜索
            if not appid and not is_chinese:
                try:
                    logger.info(f"[备用搜索-英文] 尝试英文搜索: {input_text}")
                    # 使用Steam商店搜索API（英文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": input_text, "l": "english", "cc": "US"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-英文成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-英文] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-英文] 未找到游戏: {input_text}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-英文失败: {e}")
            
            # 第五步：如果所有搜索都失败，尝试直接使用输入文本进行Steam搜索
            if not appid:
                try:
                    logger.info(f"[备用搜索-最终] 尝试直接搜索: {input_text}")
                    # 使用Steam商店搜索API（英文）
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.get(
                            "https://store.steampowered.com/api/storesearch/",
                            params={"term": input_text, "l": "english", "cc": "US"}
                        )
                        data = resp.json()
                        
                        if data and data.get("total", 0) > 0:
                            # 取第一个匹配的游戏
                            game = data["items"][0]
                            appid = str(game.get("id", ""))
                            game_name = game.get("name", "")
                            
                            if appid:
                                logger.info(f"[备用搜索-最终成功] 游戏: {game_name}, AppID: {appid}")
                            else:
                                logger.warning(f"[备用搜索-最终] 找到游戏但无法获取AppID: {game_name}")
                        else:
                            logger.warning(f"[备用搜索-最终] 未找到游戏: {input_text}")
                            
                except Exception as e:
                    logger.error(f"备用搜索-最终失败: {e}")
            
            if not appid:
                yield event.plain_result("未找到该游戏，请检查游戏名是否正确，或尝试直接输入AppID。")
                return
        
        # 获取当前会话标识
        current_origin = event.unified_msg_origin
        
        # 添加到监控列表
        if appid not in self.monitor_list:
            self.monitor_list[appid] = {
                "name": game_name,
                "subscribers": [current_origin],
                "last_price": None,
                "original_price": None,
                "discount": None
            }
            await self.save_monitor_list()
            yield event.plain_result(f"✅ 已成功订阅游戏《{game_name}》的价格监控！\n当价格变动时，我会及时通知您。")
        else:
            # 检查是否已经订阅
            subscribers = self.monitor_list[appid].get("subscribers", [])
            if current_origin in subscribers:
                yield event.plain_result(f"⚠️ 您已经订阅了游戏《{game_name}》的价格监控。")
            else:
                subscribers.append(current_origin)
                self.monitor_list[appid]["subscribers"] = subscribers
                await self.save_monitor_list()
                yield event.plain_result(f"✅ 已成功订阅游戏《{game_name}》的价格监控！")

    @filter.command("取消监控", alias={"取消订阅", "停止监控"})
    async def cancel_monitor_command(self, event: AstrMessageEvent):
        """取消价格监控"""
        args = event.message_str.strip().split()[1:]
        
        if len(args) < 1:
            yield event.plain_result("使用方法：/取消监控 <游戏名或AppID>\n例如：/取消监控 Cyberpunk 2077")
            return
        
        input_text = " ".join(args)
        current_origin = event.unified_msg_origin
        
        # 查找匹配的游戏
        found_games = []
        for appid, game_info in self.monitor_list.items():
            if input_text.isdigit() and appid == input_text:
                found_games.append((appid, game_info))
            elif input_text.lower() in game_info["name"].lower():
                found_games.append((appid, game_info))
        
        if not found_games:
            yield event.plain_result("未找到匹配的监控游戏，请检查输入是否正确。")
            return
        
        if len(found_games) > 1:
            # 多个匹配，让用户选择
            game_list = "\n".join([f"{i+1}. {info['name']} (AppID: {appid})" for i, (appid, info) in enumerate(found_games)])
            yield event.plain_result(f"找到多个匹配的游戏，请选择：\n{game_list}\n\n请使用 /取消监控 <序号> 来取消订阅")
            return
        
        # 单个匹配或指定序号
        if input_text.isdigit() and len(found_games) == 1:
            appid, game_info = found_games[0]
        else:
            # 处理序号选择
            try:
                index = int(input_text) - 1
                if 0 <= index < len(found_games):
                    appid, game_info = found_games[index]
                else:
                    yield event.plain_result("序号无效，请重新选择。")
                    return
            except ValueError:
                appid, game_info = found_games[0]
        
        # 取消订阅
        subscribers = game_info.get("subscribers", [])
        if current_origin in subscribers:
            subscribers.remove(current_origin)
            
            if subscribers:
                # 还有其他订阅者，只移除当前用户
                self.monitor_list[appid]["subscribers"] = subscribers
                await self.save_monitor_list()
                yield event.plain_result(f"✅ 已取消对游戏《{game_info['name']}》的价格监控订阅。")
            else:
                # 没有其他订阅者，移除整个游戏
                del self.monitor_list[appid]
                await self.save_monitor_list()
                yield event.plain_result(f"✅ 已取消对游戏《{game_info['name']}》的价格监控订阅。")
        else:
            yield event.plain_result(f"⚠️ 您尚未订阅游戏《{game_info['name']}》的价格监控。")

    @filter.command("监控列表")
    async def monitor_list_command(self, event: AstrMessageEvent):
        """查看当前监控的游戏列表"""
        if not self.monitor_list:
            yield event.plain_result("当前没有监控任何游戏。")
            return
        
        current_origin = event.unified_msg_origin
        
        # 筛选当前用户订阅的游戏
        user_games = []
        for appid, game_info in self.monitor_list.items():
            if current_origin in game_info.get("subscribers", []):
                user_games.append((appid, game_info))
        
        if not user_games:
            yield event.plain_result("您当前没有订阅任何游戏的价格监控。")
            return
        
        message = "您当前订阅的价格监控游戏：\n"
        for i, (appid, game_info) in enumerate(user_games, 1):
            last_price = game_info.get("last_price")
            price_str = f"¥{last_price:.2f}" if last_price is not None else "未记录"
            message += f"{i}. 《{game_info['name']}》 - 当前价格: {price_str}\n"
        
        message += f"\n共监控 {len(user_games)} 个游戏"
        yield event.plain_result(message)
