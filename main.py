import re
import json
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event.filter import PermissionType, EventMessageType
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image, Plain

from .utils import *
from .renderer import Renderer
from .bili_client import BiliClient
from .listener import DynamicListener
from .data_manager import DataManager
from .constant import (
    VALID_FILTER_TYPES,
    BV,
    LOGO_PATH,
    BANNER_PATH,
    CARD_TEMPLATES,
    DEFAULT_TEMPLATE,
    get_template_names,
)
from .tools.bangumi import BangumiTool


@register("astrbot_plugin_bilibili", "Soulter", "哔哩哔哩助手", "1.4.18")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.rai = self.cfg.get("rai", True)
        self.enable_parse_miniapp = self.cfg.get("enable_parse_miniapp", True)
        self.enable_parse_BV = self.cfg.get("enable_parse_BV", True)
        # 读取样式配置
        self.style = self.cfg.get("renderer_template", DEFAULT_TEMPLATE)

        self.data_manager = DataManager()
        self.renderer = Renderer(self, self.rai, self.style)
        self.bili_client = BiliClient(
            self.cfg.get("sessdata"),
            self.cfg.get("bili_jct"),
            self.cfg.get("buvid3"),
            self.cfg.get("user_agent"),
        )
        self.dynamic_listener = DynamicListener(
            context=self.context,
            data_manager=self.data_manager,
            bili_client=self.bili_client,
            renderer=self.renderer,
            cfg=self.cfg,
        )
        self.context.add_llm_tools(BangumiTool())
        self.dynamic_listener_task = asyncio.create_task(self.dynamic_listener.start())

    @filter.command("卡片样式", alias={"bili_card_style"})
    @filter.permission_type(PermissionType.ADMIN)
    async def switch_style(self, event: AstrMessageEvent):
        """切换动态卡片样式。不带参数可以查看可用的卡片样式列表。"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        style = parts[1] if len(parts) > 1 else None
        
        available = get_template_names()

        # 不带参数：显示可用样式列表
        if not style:
            lines = ["📋 可用的卡片样式："]
            for tid in available:
                info = CARD_TEMPLATES[tid]
                current = " ← 当前" if tid == self.style else ""
                lines.append(f"  • {tid}: {info['name']}{current}")
                lines.append(f"    {info['description']}")
            lines.append(f"\n使用 /卡片样式 <样式名> 切换")
            yield event.plain_result("\n".join(lines))
            return

        # 带参数：切换样式
        if style not in available:
            yield event.plain_result(
                f"样式 '{style}' 不存在。可用样式：{', '.join(available)}"
            )
            return

        self.style = style
        self.renderer.style = style

        info = CARD_TEMPLATES[style]
        self.cfg["renderer_template"] = style
        self.cfg.save_config()
        yield event.plain_result(
            f"✅ 已切换样式为：{info['name']} ({style})"
        )
        event.stop_event()

    @filter.regex(BV)
    async def get_video_info(self, event: AstrMessageEvent):
        if self.enable_parse_BV:
            match_ = re.search(BV, event.message_str, re.IGNORECASE)
            if not match_:
                return
            # 匹配到短链接
            if match_.group(2):
                full_link = match_.group(0)
                converted_url = await self.bili_client.b23_to_bv(full_link)
                if not converted_url:
                    return
                match_bv = re.search(r"(BV[a-zA-Z0-9]+)", converted_url, re.IGNORECASE)
                if match_bv:
                    bvid = match_bv.group(1)
                else:
                    return
            # 匹配到长链接
            elif match_.group(1):
                bvid = match_.group(1)
            # 匹配到纯 BV 号
            elif match_.group(0):
                bvid = match_.group(0)
            else:
                return

            video_data = await self.bili_client.get_video_info(bvid=bvid)
            if not video_data:
                yield event.plain_result("获取视频信息失败了 (´;ω;`)")
                return
            info = video_data["info"]
            online = video_data["online"]

            render_data = await create_render_data()
            render_data["name"] = "AstrBot"
            render_data["avatar"] = await image_to_base64(LOGO_PATH)
            render_data["title"] = info["title"]
            render_data["text"] = (
                f"UP 主: {info['owner']['name']}<br>"
                f"播放量: {info['stat']['view']}<br>"
                f"点赞: {info['stat']['like']}<br>"
                f"投币: {info['stat']['coin']}<br>"
                f"总共 {online['total']} 人正在观看"
            )
            render_data["image_urls"] = [info["pic"]]

            img_path = await self.renderer.render_dynamic(render_data)
            if img_path:
                yield event.chain_result([Image.fromFileSystem(img_path)])
            else:
                msg = "渲染图片失败了 (´;ω;`)"
                text = "\n".join(
                    filter(None, render_data.get("text", "").split("<br>"))
                )
                yield event.chain_result([Plain(msg + "\n" + text), Image.fromURL(info["pic"])])

    @filter.command("订阅动态", alias={"bili_sub"})
    async def dynamic_sub(self, event: AstrMessageEvent):
        """订阅 Bilibili 动态。用法: /订阅动态 <UID> [过滤类型...]"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        if len(parts) < 2:
            yield event.plain_result("用法: /订阅动态 <UID> [过滤类型...]\n过滤类型可选: video, draw, article, forward, live, lottery")
            return
            
        uid = parts[1]
        args_list = parts[2:] if len(parts) > 2 else []

        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args_list:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            yield event.plain_result("UID 格式错误")
            event.stop_event()
            return

        # 检查是否已经存在该订阅
        if await self.data_manager.update_subscription(
            sub_user, int(uid), filter_types, filter_regex
        ):
            # 如果已存在，更新其过滤条件
            yield event.plain_result("该动态已订阅，已更新过滤条件。")
            event.stop_event()
            return
        # 以下为新增订阅
        _sub_data = {
            "uid": int(uid),
            "last": "",
            "is_live": False,
            "filter_types": filter_types,
            "filter_regex": filter_regex,
            "recent_ids": [],
        }
        try:
            # 获取最新一条动态 (用于初始化 last_id)
            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            if dyn:
                parsed_results = await self.dynamic_listener._parse_and_filter_dynamics(dyn, _sub_data)
                # 寻找列表里第一个出现的有效 ID (不管是哪种类型)
                for _, dyn_id in parsed_results:
                    if dyn_id:
                        _sub_data["last"] = dyn_id
                        _sub_data["recent_ids"] = [dyn_id]
                        break
        except Exception as e:
            logger.error(f"获取初始动态失败: {e}")
        finally:
            # 保存配置
            await self.data_manager.add_subscription(sub_user, _sub_data)
        # 获取用户信息(可能412，故后置)
        mid = uid
        name = "未知UP主"
        sex = "未知"
        avatar = ""
        try:
            res = await self.bili_client.get_user_info(int(uid))
            if res and res[0]:
                usr_info = res[0]
                mid = usr_info.get("mid", uid)
                name = usr_info.get("name", "未知UP主")
                sex = usr_info.get("sex", "未知")
                avatar = usr_info.get("face", "")
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")

        try:
            filter_desc = ""
            if filter_types:
                filter_desc += f"<br>过滤类型: {', '.join(filter_types)}"
            if filter_regex:
                filter_desc += f"<br>过滤正则: {filter_regex}"

            render_data = await create_render_data()
            render_data["uid"] = uid
            render_data["name"] = "AstrBot"
            render_data["avatar"] = await image_to_base64(LOGO_PATH)
            render_data["text"] = (
                f"📣 订阅成功！<br>"
                f"UP 主: {name} | 性别: {sex}"
                f"{filter_desc}"  # 显示过滤信息
            )
            render_data["image_urls"] = [avatar]
            render_data["url"] = f"https://space.bilibili.com/{mid}"
            render_data["qrcode"] = await create_qrcode(render_data["url"])
            if self.rai:
                img_path = await self.renderer.render_dynamic(render_data)
                if img_path:
                    yield event.chain_result([Image.fromFileSystem(img_path), Plain(render_data["url"])])
                    event.stop_event()
                    return
                else:
                    msg = "渲染图片失败了 (´;ω;`)"
                    text = "\n".join(
                        filter(None, render_data.get("text", "").split("<br>"))
                    )
                    yield event.chain_result([Plain(msg + "\n" + text), Image.fromURL(avatar)])
                    event.stop_event()
                    return
            else:
                chain = [
                    Plain(render_data["text"]),
                    Image.fromURL(avatar),
                ]
                yield event.chain_result(chain)
                event.stop_event()
                return
        except Exception as e:
            logger.warning(f"订阅出现问题: {e}")
            yield event.plain_result(f"订阅成功！但是:{e}")
            event.stop_event()
            return

    @filter.command("订阅列表", alias={"bili_sub_list"})
    async def sub_list(self, event: AstrMessageEvent):
        """查看 bilibili 动态监控列表"""
        sub_user = event.unified_msg_origin
        ret = """订阅列表：\n"""
        subs = self.data_manager.get_subscriptions_by_user(sub_user)

        if not subs:
            yield event.plain_result("无订阅")
            return
        else:
            for idx, uid_sub_data in enumerate(subs):
                uid = uid_sub_data["uid"]
                info, _ = await self.bili_client.get_user_info(int(uid))
                if not info:
                    ret += f"{idx + 1}. {uid} - 无法获取 UP 主信息\n"
                else:
                    name = info["name"]
                    ret += f"{idx + 1}. {uid} - {name}\n"
            yield event.plain_result(ret)
        event.stop_event()

    @filter.command("订阅删除", alias={"bili_sub_del"})
    async def sub_del(self, event: AstrMessageEvent):
        """删除 bilibili 动态监控。用法: /订阅删除 <UID>"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        if len(parts) < 2:
            yield event.plain_result("用法: /订阅删除 <UID>")
            event.stop_event()
            return
        uid = parts[1]
        
        sub_user = event.unified_msg_origin
        if not uid or not uid.isdigit():
            yield event.plain_result("参数错误，请提供正确的UID。")
            event.stop_event()
            return

        uid2del = int(uid)

        if await self.data_manager.remove_subscription(sub_user, uid2del):
            yield event.plain_result("删除成功")
        else:
            yield event.plain_result("未找到指定的订阅")
        event.stop_event()

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("全局删除", alias={"bili_global_del"})
    async def global_sub_del(self, event: AstrMessageEvent):
        """管理员指令。通过 SID 删除某一个群聊或者私聊的所有订阅。"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        sid = parts[1] if len(parts) > 1 else None
        
        if not sid:
            yield event.plain_result(
                "通过 SID 删除某一个群聊或者私聊的所有订阅。使用 /sid 指令查看当前会话的 SID。"
            )
            event.stop_event()
            return

        ret_msg = await self.data_manager.remove_all_for_user(sid)
        yield event.plain_result(ret_msg)
        event.stop_event()

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("全局订阅", alias={"bili_global_sub"})
    async def global_sub_add(self, event: AstrMessageEvent):
        """管理员指令。通过 UID 添加某一个用户的所有订阅。用法: /全局订阅 <SID> <UID> [过滤...]"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        if len(parts) < 3:
            yield event.plain_result("用法: /全局订阅 <SID> <UID> [过滤类型...]")
            event.stop_event()
            return
            
        sid = parts[1]
        uid = parts[2]
        args_list = parts[3:] if len(parts) > 3 else []
        
        if not sid or not uid.isdigit():
            yield event.plain_result(
                "请提供正确的SID与UID。使用 /sid 指令查看当前会话的 SID"
            )
            event.stop_event()
            return
            
        filter_types: List[str] = []
        filter_regex: List[str] = []
        for arg in args_list:
            if arg in VALID_FILTER_TYPES:
                filter_types.append(arg)
            else:
                filter_regex.append(arg)

        if await self.data_manager.update_subscription(
            sid, int(uid), filter_types, filter_regex
        ):
            yield event.plain_result("该动态已订阅，已更新过滤条件")
            event.stop_event()
            return

        usr_info = None
        try:
            _sub_data = {
                "uid": int(uid),
                "last": "",
                "is_live": False,
                "filter_types": filter_types,
                "filter_regex": filter_regex,
                "recent_ids": [],
            }

            dyn = await self.bili_client.get_latest_dynamics(int(uid))
            parsed_dyn = await self.dynamic_listener._parse_and_filter_dynamics(dyn, _sub_data)
            if parsed_dyn and parsed_dyn[0][1]:
                dyn_id = parsed_dyn[0][1]
                _sub_data["last"] = dyn_id
                _sub_data["recent_ids"] = [dyn_id]

            usr_info, err_msg = await self.bili_client.get_user_info(int(uid))
        except Exception as e:
            logger.error(f"获取初始动态失败: {e}")
        finally:
            # 保存配置
            await self.data_manager.add_subscription(sid, _sub_data)
            if not usr_info:
                yield event.plain_result(err_msg if 'err_msg' in locals() else str(e))
            else:
                yield event.plain_result(
                    f"订阅完成，已为{sid}添加订阅{uid} ({usr_info.get('name', '未知')})，详情见日志。"
                )
            event.stop_event()

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("全局列表", alias={"bili_global_list"})
    async def global_list(self, event: AstrMessageEvent):
        """管理员指令。查看所有订阅者"""
        ret = "订阅会话列表：\n"
        all_subs = self.data_manager.get_all_subscriptions()
        if not all_subs:
            yield event.plain_result("没有任何会话订阅过。")
            event.stop_event()
            return

        for sub_user in all_subs:
            ret += f"- {sub_user}\n"
            for sub in all_subs[sub_user]:
                uid = sub.get("uid")
                ret += f"  - {uid}\n"
        yield event.plain_result(ret)
        event.stop_event()

    @filter.event_message_type(EventMessageType.ALL)
    async def parse_miniapp(self, event: AstrMessageEvent, *args, **kwargs):
        if self.enable_parse_miniapp:
            for msg_element in event.message_obj.message:
                if (
                    hasattr(msg_element, "type")
                    and msg_element.type == "Json"
                    and hasattr(msg_element, "data")
                ):
                    json_string = msg_element.data

                    try:
                        parsed_data = json.loads(json_string)
                        meta = parsed_data.get("meta", {})
                        detail_1 = meta.get("detail_1", {})
                        title = detail_1.get("title")
                        qqdocurl = detail_1.get("qqdocurl")
                        desc = detail_1.get("desc")

                        if title == "哔哩哔哩" and qqdocurl:
                            if "https://b23.tv" in qqdocurl:
                                qqdocurl = await self.bili_client.b23_to_bv(qqdocurl)
                            ret = f"视频: {desc}\n链接: {qqdocurl}"
                            yield event.plain_result(ret)
                            event.stop_event()
                        news = meta.get("news", {})
                        tag = news.get("tag", "")
                        jumpurl = news.get("jumpUrl", "")
                        title = news.get("title", "")
                        if tag == "哔哩哔哩" and jumpurl:
                            if "https://b23.tv" in jumpurl:
                                jumpurl = await self.bili_client.b23_to_bv(jumpurl)
                            ret = f"视频: {title}\n链接: {jumpurl}"
                            yield event.plain_result(ret)
                            event.stop_event()
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode JSON string: {json_string}")
                    except Exception as e:
                        logger.error(f"An error occurred during JSON processing: {e}")

    @filter.command("订阅测试", alias={"bili_sub_test"})
    async def sub_test(self, event: AstrMessageEvent):
        """测试订阅功能。仅测试获取动态与渲染图片功能，不保存订阅信息。"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        if len(parts) < 2:
            yield event.plain_result("用法: /订阅测试 <UID>")
            return
        uid = parts[1]
        
        sub_user = event.unified_msg_origin
        dyn = await self.bili_client.get_latest_dynamics(int(uid))
        if dyn:
            parsed_results = await self.dynamic_listener._parse_and_filter_dynamics(
                dyn,
                {
                    "uid": uid,
                    "filter_types": [],
                    "filter_regex": [],
                    "last": "",
                    "recent_ids": [],
                },
            )
            # 寻找第一个有效的渲染数据
            render_data = None
            for r, _ in parsed_results:
                if r:
                    render_data = r
                    break
            
            if render_data:
                await self.dynamic_listener._handle_new_dynamic(sub_user, render_data)
            else:
                yield event.plain_result(f"未能解析有效动态。抓到 {len(dyn.get('items', []))} 条动态，但由于类型不符或被过滤，均无法显示。请查看后台日志。")
        else:
            yield event.plain_result("获取动态失败，请检查 UID 是否正确或网络是否正常。")
        event.stop_event()

    @filter.command("直播测试", alias={"bili_live_test"})
    async def live_test(self, event: AstrMessageEvent):
        """测试直播通知功能。仅测试获取直播状态与渲染图片功能，不保存状态。"""
        msg = event.message_str.strip()
        parts = re.split(r"\s+", msg)
        if len(parts) < 2:
            yield event.plain_result("用法: /直播测试 <UID>")
            event.stop_event()
            return
        uid = parts[1]

        sub_user = event.unified_msg_origin
        if not uid.isdigit():
            yield event.plain_result("UID 格式错误")
            event.stop_event()
            return

        live_room = await self.bili_client.get_live_info_by_uids([int(uid)])
        if live_room:
            # 模拟订阅数据
            mock_sub_data = {
                "uid": int(uid),
                "is_live": False  # 设为 False 以便触发“开播”逻辑
            }
            await self.dynamic_listener._handle_live_status(sub_user, mock_sub_data, live_room, test_mode=True)
        else:
            yield event.plain_result("获取直播信息失败，该用户可能从未开过直播或 UID 错误。")
        event.stop_event()

    async def terminate(self):
        if self.dynamic_listener_task and not self.dynamic_listener_task.done():
            self.dynamic_listener_task.cancel()
            try:
                await self.dynamic_listener_task
            except asyncio.CancelledError:
                logger.info(
                    "bilibili dynamic_listener task was successfully cancelled during terminate."
                )
            except Exception as e:
                logger.error(
                    f"Error awaiting cancellation of dynamic_listener task: {e}"
                )
