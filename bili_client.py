import aiohttp
import asyncio
from astrbot.api import logger
from typing import Optional, Dict, Any, Tuple
import aiohttp
import asyncio
from astrbot.api import logger
from bilibili_api import user, Credential, video
from bilibili_api.utils.network import Api

# 尝试兼容不同版本的 settings 导入
try:
    from bilibili_api import settings
except ImportError:
    try:
        from bilibili_api.utils import network as network_utils
        settings = getattr(network_utils, "settings", None)
    except ImportError:
        settings = None


class BiliClient:
    """
    负责所有与 Bilibili API 的交互。
    """

    def __init__(
        self,
        sessdata: Optional[str] = None,
        bili_jct: Optional[str] = None,
        buvid3: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        """
        初始化 Bilibili API 客户端。
        """
        # 如果主人没填，默认模拟火狐浏览器
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0"
        
        # 尝试设置全局请求头
        if settings and hasattr(settings, "common_headers"):
            settings.common_headers.update({
                "User-Agent": self.user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            })
        
        self.credential = None
        if sessdata:
            # 填入尽可能完整的凭证
            self.credential = Credential(
                sessdata=sessdata, 
                bili_jct=bili_jct, 
                buvid3=buvid3,
                # 有些版本支持 buvid4，咱们也尝试从 buvid3 派生一个（或者如果有的话）
                buvid4=f"{buvid3}infoc" if buvid3 else None 
            )
        else:
            logger.warning("未提供 SESSDATA，部分需要登录的API可能无法使用。")

    async def get_user(self, uid: int) -> user.User:
        """
        根据UID获取一个 User 对象。
        """
        return user.User(uid=uid, credential=self.credential)

    async def get_video_info(self, bvid: str) -> Optional[Dict[str, Any]]:
        """
        获取视频的详细信息和在线观看人数。
        """
        try:
            v = video.Video(bvid=bvid, credential=self.credential)
            info = await v.get_info()
            online = await v.get_online()
            return {"info": info, "online": online}
        except Exception as e:
            logger.error(f"获取视频信息失败 (BVID: {bvid}): {e}")
            return None

    async def get_latest_dynamics(self, uid: int) -> Optional[Dict[str, Any]]:
        """
        获取用户的最新动态。
        """
        try:
            u = await self.get_user(uid)
            return await u.get_dynamics_new()
        except Exception as e:
            logger.error(f"获取用户动态失败 (UID: {uid}): {e}")
            return None

    async def get_live_info(self, uid: int) -> Optional[Dict[str, Any]]:
        """
        获取用户的直播间信息。
        DEPRECATED: 该方法已弃用，据反馈易引起412错误
        """
        try:
            u = await self.get_user(uid)
            return await u.get_live_info()
        except Exception as e:
            logger.error(f"获取直播间信息失败 (UID: {uid}): {e}")
            return None

    async def get_live_info_by_uids(self, uids: list[int]) -> Optional[Dict[str, Any]]:
        API_CONFIG = {
            "url": "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
            "method": "GET",
            "verify": False,
            "params": {"uids[]": "list<int>: 主播uid列表"},
            "comment": "通过主播uid列表获取直播间状态信息（是否在直播、房间号等）",
        }
        params = {"uids[]": uids}
        resp = await Api(**API_CONFIG, no_csrf=True, credential=self.credential).update_params(**params).result
        if not isinstance(resp, dict) or not resp:
            return None
        live_room = next(iter(resp.values()))
        return live_room

    async def get_user_info(self, uid: int) -> Optional[Tuple[Dict[str, Any], str]]:
        """
        获取用户的基本信息。
        """
        try:
            u = await self.get_user(uid)
            info = await u.get_user_info()
            return info, ""
        except Exception as e:
            if "code" in e.args[0] and e.args[0]["code"] == -404:
                logger.warning(f"无法找到用户 (UID: {uid})")
                return None, "啥都木有 (´;ω;`)"
            else:
                logger.error(f"获取用户信息失败 (UID: {uid}): {e}")
                return None, f"获取 UP 主信息失败: {str(e)}"

    async def b23_to_bv(self, url: str) -> Optional[str]:
        """
        b23短链转换为原始链接
        """
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://www.bilibili.com/"
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url=url, headers=headers, allow_redirects=False, timeout=10
                ) as response:
                    if 300 <= response.status < 400:
                        location_url = response.headers.get("Location")
                        if location_url:
                            base_url = location_url.split("?", 1)[0]
                            return base_url
            except Exception as e:
                logger.error(f"解析b23链接失败 (URL: {url}): {e}")
                return url
