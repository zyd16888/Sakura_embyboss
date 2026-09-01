"""
xserver（扩展服务器/测试服）数据层 —— 与原有 emby/emby2/Rcode 表完全隔离。

- xserver_account:  (server_id, tg) 复合主键，一个用户在一台扩展服务器一条记录；
                    embyid 为空但 us>0 表示「持有开号资格待使用」。
                    pool 记录占用哪个名额池：main=主服已有账号 / open=主服无账号。
- xserver_quota:    server_id 主键，双名额池 total/used（主服用户池）与
                    total_open/used_open（开放池），占用/回收均为单条原子SQL，杜绝超卖。
- xserver_code:     测试服注册码（独立于 Rcode），前缀 XREG/XRNV 区分注册/续期。
"""
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Integer, BigInteger, or_

from bot.sql_helper import Base, Session
from bot import LOGGER

POOL_MAIN = 'main'   # 主服已有账号用户
POOL_OPEN = 'open'   # 主服没有账号的纯测试用户


class XserverAccount(Base):
    """xserver账号表，(server_id, tg) 复合主键"""
    __tablename__ = 'xserver_account'
    server_id = Column(String(50), primary_key=True, autoincrement=False)
    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    embyid = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    pwd = Column(String(255), nullable=True)
    pwd2 = Column(String(255), nullable=True)
    # b=正常 c=已封印（沿用原表等级语义） d=仅持有资格未开号
    lv = Column(String(1), default='b')
    cr = Column(DateTime, nullable=True)
    ex = Column(DateTime, nullable=True)
    # 开号资格天数（管理员发放 xsgr / 注册码兑换），0=无资格
    us = Column(Integer, default=0)
    # 占用的名额池：main | open（未开号的行为 NULL）
    pool = Column(String(10), nullable=True)


class XserverQuota(Base):
    """xserver名额表，server_id 主键，双池"""
    __tablename__ = 'xserver_quota'
    server_id = Column(String(50), primary_key=True, autoincrement=False)
    total = Column(Integer, default=0)         # 主服用户池总数
    used = Column(Integer, default=0)
    total_open = Column(Integer, default=0)    # 无主服账号用户池总数
    used_open = Column(Integer, default=0)
    update_time = Column(DateTime, nullable=True)


class XserverCode(Base):
    """xserver注册码表，code 主键"""
    __tablename__ = 'xserver_code'
    code = Column(String(50), primary_key=True, autoincrement=False)
    server_id = Column(String(50), nullable=True)
    tg = Column(BigInteger)
    us = Column(Integer, default=30)          # 注册码=资格天数；续期码=续期天数
    kind = Column(String(10), default='reg')  # reg | renew
    used = Column(BigInteger, nullable=True)
    usedtime = Column(DateTime, nullable=True)


class XserverHistory(Base):
    """开号历史表 (server_id, tg)：账号删除后 xserver_account 行即消失，
    此表永久保留「该用户在这台服务器开过号」的事实，供 allow_reopen 判定。"""
    __tablename__ = 'xserver_history'
    server_id = Column(String(50), primary_key=True, autoincrement=False)
    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    first_cr = Column(DateTime, nullable=True)
    last_cr = Column(DateTime, nullable=True)
    open_count = Column(Integer, default=1)


def xhist_mark(server_id: str, tg: int) -> bool:
    """开号成功时记账：无行建行为首次，有行则累加次数"""
    with Session() as session:
        try:
            row = session.query(XserverHistory).filter(
                XserverHistory.server_id == server_id,
                XserverHistory.tg == tg).first()
            now = datetime.now()
            if row is None:
                session.add(XserverHistory(server_id=server_id, tg=tg,
                                           first_cr=now, last_cr=now, open_count=1))
            else:
                row.last_cr = now
                row.open_count = int(row.open_count or 0) + 1
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】hist_mark 失败 {server_id}/{tg}: {e}")
            session.rollback()
            return False


def xhist_has_opened(server_id: str, tg: int) -> bool:
    with Session() as session:
        try:
            return session.query(XserverHistory).filter(
                XserverHistory.server_id == server_id,
                XserverHistory.tg == tg).first() is not None
        except Exception as e:
            LOGGER.error(f"【xserver】hist_has_opened 失败 {server_id}/{tg}: {e}")
            return False


# ---------------- quota（双池） ----------------

def _pool_cols(pool: str):
    if pool == POOL_OPEN:
        return XserverQuota.total_open, XserverQuota.used_open
    return XserverQuota.total, XserverQuota.used


def xquota_ensure(server_id: str, total_seed: int, open_seed=None) -> bool:
    """确保名额行存在；total 以种子初始化（已有值不覆盖），open_seed 默认同 total_seed"""
    if open_seed is None:
        open_seed = total_seed
    with Session() as session:
        try:
            row = session.query(XserverQuota).filter(XserverQuota.server_id == server_id).first()
            if row is None:
                session.add(XserverQuota(server_id=server_id, total=int(total_seed), used=0,
                                         total_open=int(open_seed), used_open=0,
                                         update_time=datetime.now()))
                session.commit()
            elif row.total_open is None:
                # 老库升级：开放池补种子
                row.total_open = int(open_seed)
                if row.used_open is None:
                    row.used_open = 0
                session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】quota_ensure 失败 {server_id}: {e}")
            session.rollback()
            return False


def xquota_set_total(server_id: str, total: int, pool: str = POOL_MAIN) -> bool:
    """管理员设置某池总名额"""
    total_col, _ = _pool_cols(pool)
    with Session() as session:
        try:
            row = session.query(XserverQuota).filter(XserverQuota.server_id == server_id).first()
            if row is None:
                session.add(XserverQuota(server_id=server_id,
                                         total=int(total) if pool == POOL_MAIN else 0,
                                         used=0,
                                         total_open=int(total) if pool == POOL_OPEN else 0,
                                         used_open=0,
                                         update_time=datetime.now()))
            else:
                setattr(row, total_col.name, int(total))
                row.update_time = datetime.now()
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】quota_set_total 失败 {server_id}: {e}")
            session.rollback()
            return False


def xquota_take(server_id: str, pool: str = POOL_MAIN) -> bool:
    """原子占坑（指定池）：UPDATE ... WHERE used < total。失败即该池已满"""
    _, used_col = _pool_cols(pool)
    total_col = XserverQuota.total_open if pool == POOL_OPEN else XserverQuota.total
    with Session() as session:
        try:
            cnt = session.query(XserverQuota).filter(
                XserverQuota.server_id == server_id,
                used_col < total_col,
            ).update({used_col: used_col + 1,
                      XserverQuota.update_time: datetime.now()},
                     synchronize_session=False)
            session.commit()
            return cnt == 1
        except Exception as e:
            LOGGER.error(f"【xserver】quota_take 失败 {server_id}/{pool}: {e}")
            session.rollback()
            return False


def xquota_give(server_id: str, pool: str = POOL_MAIN) -> bool:
    """原子回收（指定池）：used-1，下限0（幂等保护）"""
    _, used_col = _pool_cols(pool)
    with Session() as session:
        try:
            cnt = session.query(XserverQuota).filter(
                XserverQuota.server_id == server_id,
                used_col > 0,
            ).update({used_col: used_col - 1,
                      XserverQuota.update_time: datetime.now()},
                     synchronize_session=False)
            session.commit()
            return cnt == 1
        except Exception as e:
            LOGGER.error(f"【xserver】quota_give 失败 {server_id}/{pool}: {e}")
            session.rollback()
            return False


def xquota_get(server_id: str):
    """读取名额行，None 表示未初始化"""
    with Session() as session:
        try:
            return session.query(XserverQuota).filter(XserverQuota.server_id == server_id).first()
        except Exception as e:
            LOGGER.error(f"【xserver】quota_get 失败 {server_id}: {e}")
            return None


def xquota_pool_used(q, pool: str) -> int:
    return int(getattr(q, 'used_open' if pool == POOL_OPEN else 'used', 0) or 0)


def xquota_pool_total(q, pool: str) -> int:
    return int(getattr(q, 'total_open' if pool == POOL_OPEN else 'total', 0) or 0)


# ---------------- account ----------------

def xacc_get(server_id: str, tg: int):
    with Session() as session:
        try:
            return session.query(XserverAccount).filter(
                XserverAccount.server_id == server_id,
                XserverAccount.tg == tg).first()
        except Exception as e:
            LOGGER.error(f"【xserver】acc_get 失败 {server_id}/{tg}: {e}")
            return None


def xacc_get_any(server_id: str, key):
    """按 embyid / name / tg 模糊定位账号（管理查询用）"""
    with Session() as session:
        try:
            q = session.query(XserverAccount).filter(XserverAccount.server_id == server_id)
            if isinstance(key, int):
                return q.filter(XserverAccount.tg == key).first()
            return q.filter(or_(XserverAccount.name == key,
                                XserverAccount.embyid == key)).first()
        except Exception as e:
            LOGGER.error(f"【xserver】acc_get_any 失败 {server_id}/{key}: {e}")
            return None


def xacc_all(server_id: str = None):
    with Session() as session:
        try:
            q = session.query(XserverAccount)
            if server_id:
                q = q.filter(XserverAccount.server_id == server_id)
            return q.all()
        except Exception as e:
            LOGGER.error(f"【xserver】acc_all 失败 {server_id}: {e}")
            return []


def xacc_expired_rows(server_id: str, now: datetime):
    """到期行（ex < now）或半成品行（已建号但无到期时间）。走本模块 Session。"""
    with Session() as session:
        try:
            return session.query(XserverAccount).filter(
                XserverAccount.server_id == server_id,
                or_(XserverAccount.ex < now,
                    XserverAccount.ex.is_(None) & XserverAccount.embyid.isnot(None)),
            ).all()
        except Exception as e:
            LOGGER.error(f"【xserver】acc_expired_rows 失败 {server_id}: {e}")
            return []


def xacc_add(server_id: str, tg: int, embyid: str, name: str, pwd: str, pwd2: str,
             ex: datetime, lv: str = 'b', us: int = 0, pool: str = POOL_MAIN) -> bool:
    with Session() as session:
        try:
            session.add(XserverAccount(server_id=server_id, tg=tg, embyid=embyid, name=name,
                                       pwd=pwd, pwd2=pwd2, lv=lv, cr=datetime.now(), ex=ex,
                                       us=us, pool=pool))
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】acc_add 失败 {server_id}/{tg}: {e}")
            session.rollback()
            return False


def xacc_update(server_id: str, tg: int, **kwargs) -> bool:
    with Session() as session:
        try:
            row = session.query(XserverAccount).filter(
                XserverAccount.server_id == server_id,
                XserverAccount.tg == tg).first()
            if row is None:
                return False
            for k, v in kwargs.items():
                setattr(row, k, v)
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】acc_update 失败 {server_id}/{tg}: {e}")
            session.rollback()
            return False


def xacc_delete(server_id: str, tg: int) -> bool:
    with Session() as session:
        try:
            cnt = session.query(XserverAccount).filter(
                XserverAccount.server_id == server_id,
                XserverAccount.tg == tg).delete()
            session.commit()
            return cnt > 0
        except Exception as e:
            LOGGER.error(f"【xserver】acc_delete 失败 {server_id}/{tg}: {e}")
            session.rollback()
            return False


def xacc_grant(server_id: str, tg: int, days: int) -> bool:
    """发放/累加开号资格：无行则建行（embyid 为空即待开号），有行则累加天数"""
    with Session() as session:
        try:
            row = session.query(XserverAccount).filter(
                XserverAccount.server_id == server_id,
                XserverAccount.tg == tg).first()
            if row is None:
                session.add(XserverAccount(server_id=server_id, tg=tg, us=int(days), lv='d'))
            else:
                row.us = int(row.us or 0) + int(days)
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】acc_grant 失败 {server_id}/{tg}: {e}")
            session.rollback()
            return False


# ---------------- code ----------------

def xcode_add(code_list: list, server_id: str, tg: int, us: int, kind: str = 'reg') -> bool:
    """批量添加注册码，重复 code 跳过"""
    with Session() as session:
        try:
            session.add_all([XserverCode(code=c, server_id=server_id, tg=tg, us=us, kind=kind)
                             for c in code_list])
            session.commit()
            return True
        except Exception as e:
            LOGGER.error(f"【xserver】code_add 失败: {e}")
            session.rollback()
            return False


def xcode_get(code: str):
    with Session() as session:
        try:
            return session.query(XserverCode).filter(XserverCode.code == code).first()
        except Exception as e:
            LOGGER.error(f"【xserver】code_get 失败: {e}")
            return None


def xcode_redeem(code: str, tg: int):
    """原子兑换：条件 UPDATE WHERE used IS NULL，rowcount=1 才算抢到。
    :return: ok, server_id, days
    """
    with Session() as session:
        try:
            cnt = session.query(XserverCode).filter(
                XserverCode.code == code,
                XserverCode.used.is_(None),
            ).update({XserverCode.used: tg, XserverCode.usedtime: datetime.now()},
                     synchronize_session=False)
            if cnt != 1:
                return False, None, 0
            session.commit()
            row = session.query(XserverCode).filter(XserverCode.code == code).first()
            return True, row.server_id, int(row.us or 0)
        except Exception as e:
            LOGGER.error(f"【xserver】code_redeem 失败: {e}")
            session.rollback()
            return False, None, 0

def xcode_restore(code: str, tg: int) -> bool:
    """入账失败时归还刚被该用户占用的注册码。"""
    with Session() as session:
        try:
            cnt = session.query(XserverCode).filter(
                XserverCode.code == code,
                XserverCode.used == tg,
            ).update({XserverCode.used: None, XserverCode.usedtime: None},
                     synchronize_session=False)
            session.commit()
            return cnt == 1
        except Exception as e:
            LOGGER.error(f"【xserver】code_restore 失败: {e}")
            session.rollback()
            return False


def xcode_unused_of(server_id: str, kind: str = 'reg'):
    with Session() as session:
        try:
            return session.query(XserverCode).filter(
                XserverCode.server_id == server_id,
                XserverCode.kind == kind,
                XserverCode.used.is_(None)).count()
        except Exception as e:
            LOGGER.error(f"【xserver】code_unused 统计失败: {e}")
            return 0


def xcode_list(server_id: str, kind: str, is_used: bool,
               page: int = 0, page_size: int = 10):
    """按服务器、码类型和使用状态分页查询，返回 (当前页, 总数)。"""
    with Session() as session:
        try:
            query = session.query(XserverCode).filter(
                XserverCode.server_id == server_id,
                XserverCode.kind == kind,
            )
            if is_used:
                query = query.filter(XserverCode.used.isnot(None))
                query = query.order_by(XserverCode.usedtime.desc(), XserverCode.code.asc())
            else:
                query = query.filter(XserverCode.used.is_(None))
                query = query.order_by(XserverCode.code.asc())
            total = query.count()
            rows = query.offset(max(0, page) * page_size).limit(page_size).all()
            return rows, total
        except Exception as e:
            LOGGER.error(f"【xserver】code_list 查询失败 {server_id}/{kind}/{is_used}: {e}")
            return [], 0
