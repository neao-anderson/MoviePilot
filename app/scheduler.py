import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from app import schemas
from app.chain import ChainBase
from app.chain.mediaserver import MediaServerChain
from app.chain.site import SiteChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.chain.torrents import TorrentsChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.plugin import PluginManager
from app.log import logger
from app.utils.singleton import Singleton
from app.utils.timer import TimerUtils

# 获取 apscheduler 的日志记录器
scheduler_logger = logging.getLogger('apscheduler')

# 设置日志级别为 WARNING
scheduler_logger.setLevel(logging.WARNING)


class SchedulerChain(ChainBase):
    pass


class Scheduler(metaclass=Singleton):
    """
    定时任务管理
    """
    # 定时服务
    _scheduler = BackgroundScheduler(timezone=settings.TZ,
                                     executors={
                                         'default': ThreadPoolExecutor(100)
                                     })
    # 退出事件
    _event = threading.Event()
    # 锁
    _lock = threading.Lock()

    def __init__(self):

        def clear_cache():
            """
            清理缓存
            """
            TorrentsChain().clear_cache()
            SchedulerChain().clear_cache()

        # 各服务的运行状态
        self._jobs = {
            "cookiecloud": {
                "func": SiteChain().sync_cookies,
                "running": False,
            },
            "mediaserver_sync": {
                "func": MediaServerChain().sync,
                "running": False,
            },
            "subscribe_tmdb": {
                "func": SubscribeChain().check,
                "running": False,
            },
            "subscribe_search": {
                "func": SubscribeChain().search,
                "running": False,
                "kwargs": {
                    "state": "R"
                }
            },
            "subscribe_refresh": {
                "func": SubscribeChain().refresh,
                "running": False,
            },
            "transfer": {
                "func": TransferChain().process,
                "running": False,
            },
            "clear_cache": {
                "func": clear_cache,
                "running": False,
            }
        }

        # 调试模式不启动定时服务
        if settings.DEV:
            return

        # CookieCloud定时同步
        if settings.COOKIECLOUD_INTERVAL \
                and str(settings.COOKIECLOUD_INTERVAL).isdigit():
            self._scheduler.add_job(
                self.start,
                "interval",
                id="cookiecloud",
                name="同步CookieCloud站点",
                minutes=int(settings.COOKIECLOUD_INTERVAL),
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=1),
                kwargs={
                    'job_id': 'cookiecloud'
                }
            )

        # 媒体服务器同步
        if settings.MEDIASERVER_SYNC_INTERVAL \
                and str(settings.MEDIASERVER_SYNC_INTERVAL).isdigit():
            self._scheduler.add_job(
                self.start,
                "interval",
                id="mediaserver_sync",
                name="同步媒体服务器",
                hours=int(settings.MEDIASERVER_SYNC_INTERVAL),
                next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(minutes=5),
                kwargs={
                    'job_id': 'mediaserver_sync'
                }
            )

        # 新增订阅时搜索（5分钟检查一次）
        self._scheduler.add_job(
            self.start,
            "interval",
            minutes=5,
            kwargs={
                'job_id': 'subscribe_search',
                'state': 'N'
            }
        )

        # 检查更新订阅TMDB数据（每隔6小时）
        self._scheduler.add_job(
            self.start,
            "interval",
            id="subscribe_tmdb",
            name="订阅元数据更新",
            hours=6,
            kwargs={
                'job_id': 'subscribe_tmdb'
            }
        )

        # 订阅状态每隔24小时搜索一次
        if settings.SUBSCRIBE_SEARCH:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_search",
                name="订阅搜索",
                hours=24,
                kwargs={
                    'job_id': 'subscribe_search',
                    'state': 'R'
                }
            )

        if settings.SUBSCRIBE_MODE == "spider":
            # 站点首页种子定时刷新模式
            triggers = TimerUtils.random_scheduler(num_executions=30)
            for trigger in triggers:
                self._scheduler.add_job(
                    self.start,
                    "cron",
                    id=f"subscribe_refresh|{trigger.hour}:{trigger.minute}",
                    name="订阅刷新",
                    hour=trigger.hour,
                    minute=trigger.minute,
                    kwargs={
                        'job_id': 'subscribe_refresh'
                    })
        else:
            # RSS订阅模式
            if not settings.SUBSCRIBE_RSS_INTERVAL \
                    or not str(settings.SUBSCRIBE_RSS_INTERVAL).isdigit():
                settings.SUBSCRIBE_RSS_INTERVAL = 30
            elif int(settings.SUBSCRIBE_RSS_INTERVAL) < 5:
                settings.SUBSCRIBE_RSS_INTERVAL = 5
            self._scheduler.add_job(
                self.start,
                "interval",
                id="subscribe_refresh",
                name="RSS订阅刷新",
                minutes=int(settings.SUBSCRIBE_RSS_INTERVAL),
                kwargs={
                    'job_id': 'subscribe_refresh'
                }
            )

        # 下载器文件转移（每5分钟）
        if settings.DOWNLOADER_MONITOR:
            self._scheduler.add_job(
                self.start,
                "interval",
                id="transfer",
                name="下载文件整理",
                minutes=5,
                kwargs={
                    'job_id': 'transfer'
                }
            )

        # 后台刷新TMDB壁纸
        self._scheduler.add_job(
            TmdbChain().get_random_wallpager,
            "interval",
            minutes=30,
            next_run_time=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3)
        )

        # 公共定时服务
        self._scheduler.add_job(
            SchedulerChain().scheduler_job,
            "interval",
            minutes=10
        )

        # 缓存清理服务，每隔24小时
        self._scheduler.add_job(
            self.start,
            "interval",
            id="clear_cache",
            name="缓存清理",
            hours=settings.CACHE_CONF.get("meta") / 3600,
            kwargs={
                'job_id': 'clear_cache'
            }
        )

        # 注册插件公共服务
        for pid in PluginManager().get_running_plugin_ids():
            self.update_plugin_job(pid)

        # 打印服务
        logger.debug(self._scheduler.print_jobs())

        # 启动定时服务
        self._scheduler.start()

    def start(self, job_id: str, *args, **kwargs):
        """
        启动定时服务
        """
        # 处理job_id格式
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.get("running"):
                logger.warning(f"定时任务 {job_id} 正在运行 ...")
                return
            self._jobs[job_id]["running"] = True
        try:
            if not kwargs:
                kwargs = job.get("kwargs") or {}
            job["func"](*args, **kwargs)
        except Exception as e:
            logger.error(f"定时任务 {job_id} 执行失败：{str(e)}")
        # 如果在job["func"]()运行时, 编辑配置导致任务被移除时, 该id已经不存在, 忽略错误
        with self._lock:
            try:
                self._jobs[job_id]["running"] = False
            except KeyError:
                pass
            # 如果是单次任务, 应立即移除缓存
            if not self._scheduler.get_job(job_id):
                self._jobs.pop(job_id, None)

    def update_plugin_job(self, pid: str):
        """
        更新插件定时服务
        """
        # 移除该插件的全部服务
        self.remove_plugin_job(pid)
        # 获取插件服务列表
        with self._lock:
            try:
                plugin_services = PluginManager().run_plugin_method(pid, "get_service") or []
            except Exception as e:
                logger.error(f"运行插件 {pid} 服务失败：{str(e)} - {traceback.format_exc()}")
                return
            # 获取插件名称
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
            # 开始注册插件服务
            for service in plugin_services:
                try:
                    sid = f"{service['id']}"
                    job_id = sid.split("|")[0]
                    if job_id not in self._jobs:
                        self._jobs[job_id] = {
                            "func": service["func"],
                            "name": service["name"],
                            "pid": pid,
                            "plugin_name": plugin_name,
                            "running": False,
                        }
                    self._scheduler.add_job(
                        self.start,
                        service["trigger"],
                        id=sid,
                        name=service["name"],
                        **service["kwargs"],
                        kwargs={
                            'job_id': job_id
                        }
                    )
                    logger.info(f"注册插件{plugin_name}服务：{service['name']} - {service['trigger']}")
                except Exception as e:
                    logger.error(f"注册插件{plugin_name}服务失败：{str(e)} - {service}")

    def remove_plugin_job(self, pid: str):
        """
        移除插件定时服务
        """
        with self._lock:
            # 获取插件名称
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
            for job_id, service in self._jobs.copy().items():
                try:
                    if service.get("pid") == pid:
                        self._jobs.pop(job_id, None)
                        try:
                            self._scheduler.remove_job(job_id)
                        except JobLookupError:
                            pass
                        logger.info(f"移除插件服务({plugin_name})：{service.get('name')}")
                except Exception as e:
                    logger.error(f"移除插件服务失败：{str(e)} - {job_id}: {service}")

    def list(self) -> List[schemas.ScheduleInfo]:
        """
        当前所有任务
        """
        with self._lock:
            # 返回计时任务
            schedulers = []
            # 去重
            added = []
            jobs = self._scheduler.get_jobs()
            # 按照下次运行时间排序
            jobs.sort(key=lambda x: x.next_run_time)
            # 将正在运行的任务提取出来 (保障一次性任务正常显示)
            for job_id, service in self._jobs.items():
                name = service.get("name")
                plugin_name = service.get("plugin_name")
                if service.get("running") and name and plugin_name:
                    if name not in added:
                        added.append(name)
                    schedulers.append(schemas.ScheduleInfo(
                        id=job_id,
                        name=name,
                        provider=plugin_name,
                        status="正在运行",
                    ))
            # 获取其他待执行任务
            for job in jobs:
                if job.name not in added:
                    added.append(job.name)
                else:
                    continue
                job_id = job.id.split("|")[0]
                service = self._jobs.get(job_id)
                if not service:
                    continue
                # 任务状态
                status = "正在运行" if service.get("running") else "等待"
                # 下次运行时间
                next_run = TimerUtils.time_difference(job.next_run_time)
                schedulers.append(schemas.ScheduleInfo(
                    id=job_id,
                    name=job.name,
                    provider=service.get("plugin_name", "[系统]"),
                    status=status,
                    next_run=next_run
                ))
            return schedulers

    def stop(self):
        """
        关闭定时服务
        """
        self._event.set()
        if self._scheduler.running:
            self._scheduler.shutdown()
