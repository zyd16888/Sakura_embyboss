#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
xserver（测试服等旁路扩展服务器）服务层。

设计红线：不修改 embyservice 主流程与任何原有表结构。
- Embyservice 的 Singleton 元类按 (cls, url, api_key) 缓存实例，
  XserverService 通过 _build() 绕开元类，实例生命周期由本模块 registry 管理。
- 建号策略（隐藏库/连接数限制）使用各 xserver 自己的配置，不引用全局 emby_block。
"""
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Union

from bot import config, LOGGER
from bot.func_helper.emby import Embyservice, create_policy, pwd_policy
from bot.func_helper.msg_utils import sendMessage
from bot.func_helper.utils import pwd_create
from bot.schemas import Xserver


class XserverService(Embyservice):
    """一台扩展emby服务器的操作服务，自带展示线路/前缀/独立策略配置。"""

    def __init__(self, xc: Xserver):
        super().__init__(xc.url, xc.api)
        self.server_id = xc.id
        self.xc = xc

    def ensure_ready(self):
        """首次使用时把 config 的双池名额种子写入 quota 表（幂等）"""
        from bot.sql_helper.sql_xserver import xquota_ensure
        xquota_ensure(self.server_id, self.xc.all_user, self.xc.all_user_open)

    async def emby_create_x(self, name: str, days: int,
                            password: Optional[str] = None) -> Union[Tuple[str, str, datetime], bool]:
        """
        在扩展服务器上创建账号：登录密码可由调用方指定（一键开号复制主服凭据）。
        安全码 pwd2 由调用方自行落库（仅存 bot 数据库，不发往 emby）。
        :return: (emby_id, 登录密码, 到期时间) 或 False
        """
        try:
            expiry_date = datetime.now() + timedelta(days=days)

            result = await self._request('POST', '/emby/Users/New', json={"Name": name})
            if not result.success:
                LOGGER.error(f"【xserver:{self.xc.id}】创建用户失败 {name}: {result.error}")
                return False
            user_id = result.data.get("Id")
            if not user_id:
                LOGGER.error(f"【xserver:{self.xc.id}】无法获取用户ID: {name}")
                return False

            new_pwd = password if password else await pwd_create(8)
            result = await self._request('POST', f'/emby/Users/{user_id}/Password',
                                         json=pwd_policy(user_id, new=new_pwd))
            if not result.success:
                LOGGER.error(f"【xserver:{self.xc.id}】设置密码失败 {name}: {result.error}")
                # 半成品账号立即远端删除，避免占用同名
                await self._request('DELETE', f'/emby/Users/{user_id}')
                return False

            # 各 xserver 自己的隐藏库与连接数限制，不污染主服策略
            block = list(self.xc.block_libs or []) + ['播放列表']
            policy = create_policy(False, False, limit=self.xc.limit, block=block)
            result = await self._request('POST', f'/emby/Users/{user_id}/Policy', json=policy)
            if not result.success:
                LOGGER.error(f"【xserver:{self.xc.id}】设置策略失败 {name}: {result.error}")
                await self._request('DELETE', f'/emby/Users/{user_id}')
                return False

            try:
                if self.xc.block_libs:
                    hide_ok = await self.hide_folders_by_names(user_id, list(self.xc.block_libs))
                    if not hide_ok:
                        LOGGER.warning(f"【xserver:{self.xc.id}】隐藏媒体库失败: {user_id}（不影响建号）")
            except Exception as e:
                LOGGER.error(f"【xserver:{self.xc.id}】隐藏媒体库异常 {name}({user_id}): {e}")

            LOGGER.info(f"【xserver:{self.xc.id}】成功创建用户: {name} (ID: {user_id})")
            return user_id, new_pwd, expiry_date
        except Exception as e:
            LOGGER.error(f"【xserver:{self.xc.id}】创建用户异常 {name}: {e}")
            return False

    async def x_delete(self, emby_id: str) -> bool:
        """远端删号（到期回收/用户注销/管理员删除共用）"""
        ok = await self.emby_del(emby_id)
        if not ok:
            LOGGER.warning(f"【xserver:{self.xc.id}】远端删号失败 emby_id={emby_id}")
        return ok


# ---------------- registry ----------------

_registry: Dict[str, XserverService] = {}
# 记录构造时的 (url, api)，用于感知 config 热更新
_signature: Dict[str, Tuple[str, str]] = {}


def _build(xc: Xserver) -> XserverService:
    """绕开 Embyservice 的 Singleton 元类缓存构造实例。

    元类以 (cls, 构造参数) 为键，而 Xserver 是 pydantic 模型（不可哈希），
    且热更新时缓存会返回携带旧 url/api 的实例。因此这里直接 __new__ + __init__，
    实例生命周期由本模块 _registry 按 server_id 管理。
    """
    inst = XserverService.__new__(XserverService)
    XserverService.__init__(inst, xc)
    return inst


def _cfg_of(server_id: str) -> Optional[Xserver]:
    for xc in config.xservers or []:
        if xc.id == server_id:
            return xc
    return None


def get_service(server_id: str) -> Optional[XserverService]:
    """按 id 取（并惰性构造）扩展服务器服务；未配置/未启用返回 None"""
    xc = _cfg_of(server_id)
    if xc is None or not xc.enable:
        return None
    sig = (xc.url, xc.api)
    svc = _registry.get(server_id)
    if svc is None or _signature.get(server_id) != sig:
        svc = _build(xc)
        _registry[server_id] = svc
        _signature[server_id] = sig
    svc.ensure_ready()
    return svc


def enabled_servers() -> list:
    return [xc for xc in (config.xservers or []) if xc.enable]


def xserver_lines() -> str:
    """给用户展示的线路文本（所有启用的扩展服务器）"""
    return '\n'.join(f"{xc.name} | {xc.line}" for xc in enabled_servers())


async def notify_xserver_opened(xc: Xserver, tg: int):
    """向主授权群发送脱敏的测试服开号通知。"""
    text = f'🎉 恭喜 [这位小伙伴](tg://user?id={tg}) 成功开通「{xc.name}」账号！'
    await _send_group_notice(text, tg)


async def notify_xserver_deleted(xc: Xserver, tg: int, reason: str):
    """向主授权群发送脱敏的测试服删号通知。"""
    text = (f'🗑️ [这位小伙伴](tg://user?id={tg}) 的「{xc.name}」账号已删除'
            f'（{reason}），名额已释放。')
    await _send_group_notice(text, tg)


async def _send_group_notice(text: str, tg: int):
    """群通知失败不反向影响账号事务。"""
    try:
        result = await sendMessage(None, text, send=True)
        if isinstance(result, str):
            LOGGER.warning(f'【xserver群通知】发送失败 tg={tg}: {result}')
    except Exception as e:
        LOGGER.warning(f'【xserver群通知】发送异常 tg={tg}: {e}')
