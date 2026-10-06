from fastapi import APIRouter, Request
from bot.sql_helper.sql_emby import Emby
from bot.sql_helper.sql_favorites import EmbyFavorites
from bot.sql_helper import Session
from bot.func_helper.emby import emby
from bot import LOGGER, bot
import json

router = APIRouter()

async def send_update_notification_to_user(tg_id: int, message: str):
    """发送通知到指定用户"""
    try:
        await bot.send_message(
            chat_id=tg_id,
            text=message
        )
        return True
    except Exception as e:
        LOGGER.error(f"发送通知失败: {str(e)}")
        return False

async def check_and_notify_series_update(item_data: dict):
    """检查并通知剧集更新"""
    try:
        # 获取剧集信息
        series_id = item_data.get("SeriesId")  # 剧集ID
        series_name = item_data.get("SeriesName")  # 剧集名称
        season_name = item_data.get("SeasonName", "")  # 季名
        episode_number = item_data.get("IndexNumber", "")  # 集号
        
        if not series_id:
            return
            
        session = Session()
        try:
            # 查找收藏了这个剧集的用户
            favorites = session.query(EmbyFavorites, Emby).join(
                Emby, EmbyFavorites.embyid == Emby.embyid
            ).filter(
                EmbyFavorites.item_id == series_id,
                Emby.tg.isnot(None)
            ).all()
            
            if favorites:
                message = (
                    f"📺 您喜欢的剧集更新啦\n"
                    f"剧集：《{series_name}》\n"
                    f"季度：{season_name}\n"
                    f"更新：第{episode_number}集 "
                )
                
                # 给每个收藏了该剧集的用户发送通知
                for favorite, user in favorites:
                    await send_update_notification_to_user(user.tg, message)
                    LOGGER.info(f"已发送剧集更新通知给用户 {user.tg}: {series_name} - {episode_number}")
        finally:
            session.close()
            
    except Exception as e:
        LOGGER.error(f"处理剧集更新通知失败: {str(e)}")

async def check_and_notify_person_update(item_data: dict):
    """检查并通知演员相关更新"""
    try:
        # 获取电影/剧集ID
        item_id = item_data.get("Id", "")
        if not item_id:
            return
            
        # 获取演员信息
        success, people_list = await emby.item_id_people(item_id=item_id)
        if not success:
            return
        session = Session()
        try:
            for person in people_list:
                # People 同时包含演员、导演等人员，仅演员触发此类通知。
                if person.get("Type") != "Actor":
                    continue
                person_id = person.get("Id")
                person_name = person.get("Name")
                
                if not person_id:
                    continue
                    
                # 查找收藏了这个演员的用户
                favorites = session.query(EmbyFavorites, Emby).join(
                    Emby, EmbyFavorites.embyid == Emby.embyid
                ).filter(
                    EmbyFavorites.item_id == person_id,
                    Emby.tg.isnot(None)
                ).all()
                
                if favorites:
                    # 获取作品信息
                    item_name = item_data.get("Name", "")
                    item_type = item_data.get("Type", "")
                    
                    message = (
                        f"🎭 您喜欢的演员有新作品啦\n"
                        f"演员：{person_name}\n"
                        f"作品：《{item_name}》\n"
                        f"类型：{item_type}\n"
                    )
                    
                    # 给每个收藏了该演员的用户发送通知
                    for favorite, user in favorites:
                        await send_update_notification_to_user(user.tg, message)
                        LOGGER.info(f"已发送演员新作品通知给用户 {user.tg}: {person_name} - {item_name}")
        finally:
            session.close()
            
    except Exception as e:
        LOGGER.error(f"处理演员更新通知失败: {str(e)}")

async def send_new_media_notification(item_data: dict):
    """发送新媒体通知"""
    try:
        item_type = item_data.get("Type", "")
        item_name = item_data.get("Name", "")
        
        # 根据媒体类型构建不同的消息
        if item_type == "Movie":
            # 检查演员相关通知
            await check_and_notify_person_update(item_data)
        elif item_type == "Series":
            # 检查演员相关通知
            await check_and_notify_person_update(item_data)
        elif item_type == "Episode":
            # 检查是否需要发送剧集更新通知
            await check_and_notify_series_update(item_data)
            return
        LOGGER.info(f"已发送新媒体通知: {item_name}")
    except Exception as e:
        LOGGER.error(f"发送新媒体通知失败: {str(e)}")

@router.post("/webhook/medias")
async def handle_media_webhook(request: Request):
    """处理Emby媒体库更新webhook"""
    try:
        # 检查Content-Type
        content_type = request.headers.get("content-type", "").lower()
        
        if "application/json" in content_type:
            # 处理JSON格式
            webhook_data = await request.json()
        else:
            # 处理form-data格式
            form_data = await request.form()
            form = dict(form_data)
            webhook_data = json.loads(form["data"]) if "data" in form else None
            
        if not webhook_data:
            return {
                "status": "error",
                "message": "No data received"
            }
            
        event = webhook_data.get("Event", "")
        item_data = webhook_data.get("Item", {})
        
        # 处理新增媒体事件
        if event in ["item.added", "library.new"]:
            # 检查媒体类型
            item_type = item_data.get("Type", "")
            
            if item_type == "Episode":
                # 处理剧集更新
                await check_and_notify_series_update(item_data)
                return {
                    "status": "success",
                    "message": "Episode update notification sent",
                    "data": {
                        "type": item_type,
                        "name": item_data.get("Name"),
                        "series": item_data.get("SeriesName"),
                        "event": event
                    }
                }
            elif item_type in ["Movie", "Series"]:
                # 处理新电影或新剧集
                await send_new_media_notification(item_data)
                return {
                    "status": "success",
                    "message": "New media notification sent",
                    "data": {
                        "type": item_type,
                        "name": item_data.get("Name"),
                        "event": event
                    }
                }
                
            return {
                "status": "ignored",
                "message": "Not a new media event",
                "event": event
            }
                
        return {
            "status": "ignored",
            "message": "Not a new media event",
            "event": event
        }
                
    except Exception as e:
        LOGGER.error(f"处理媒体库更新失败: {str(e)}")
        return {
            "status": "error",
            "message": str(e)
        }
