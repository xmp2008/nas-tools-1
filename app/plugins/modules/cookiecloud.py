from datetime import datetime
from threading import Event
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import pytz
from app.helper import DbHelper
from app.message import Message
from app.plugins.modules._base import _IPluginModule
from app.sites import Sites
from app.utils import RequestUtils
from config import Config


class CookieCloud(_IPluginModule):
    # 插件名称
    module_name = "CookieCloud同步"
    # 插件描述
    module_desc = "从CookieCloud云端同步站点Cookie数据。"
    # 插件图标
    module_icon = "cloud.png"
    # 主题色
    module_color = "#77B3D4"
    # 插件版本
    module_version = "1.0"
    # 插件作者
    module_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    module_config_prefix = "cookiecloud_"
    # 加载顺序
    module_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    _site = None
    _dbhelper = None
    _message = None
    # 设置开关
    _req = None
    _server = None
    _key = None
    _password = None
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    # 通知
    _notify = False
    # 退出事件
    _event = Event()

    @staticmethod
    def get_fields():
        return [
            # 同一板块
            {
                'type': 'div',
                'content': [
                    # 同一行
                    [
                        {
                            'title': '服务器地址',
                            'required': "required",
                            'tooltip': '参考https://github.com/easychen/CookieCloud搭建私有CookieCloud服务器；也可使用默认的公共服务器，公共服务器不会存储任何非加密用户数据，也不会存储用户KEY、端对端加密密码，但要注意千万不要对外泄露加密信息，否则Cookie数据也会被泄露！',
                            'type': 'text',
                            'content': [
                                {
                                    'id': 'server',
                                    'placeholder': 'http://nastool.cn:8088'
                                }
                            ]

                        },
                        {
                            'title': '执行周期',
                            'required': "",
                            'tooltip': '设置移转做种任务执行的时间周期，支持5位cron表达式；应避免任务执行过于频繁',
                            'type': 'text',
                            'content': [
                                {
                                    'id': 'cron',
                                    'placeholder': '0 0 0 ? *',
                                }
                            ]
                        },
                    ]
                ]
            },
            {
                'type': 'div',
                'content': [
                    # 同一行
                    [
                        {
                            'title': '用户KEY',
                            'required': 'required',
                            'tooltip': '浏览器CookieCloud插件中获取，使用公共服务器时注意不要泄露该信息',
                            'type': 'text',
                            'content': [
                                {
                                    'id': 'key',
                                    'placeholder': '',
                                }
                            ]
                        },
                        {
                            'title': '端对端加密密码',
                            'required': "",
                            'tooltip': '浏览器CookieCloud插件中获取，使用公共服务器时注意不要泄露该信息',
                            'type': 'text',
                            'content': [
                                {
                                    'id': 'password',
                                    'placeholder': ''
                                }
                            ]
                        }
                    ]
                ]
            },
            {
                'type': 'div',
                'content': [
                    # 同一行
                    [
                        {
                            'title': '运行时通知',
                            'required': "",
                            'tooltip': '运行任务后会发送通知（需要打开自定义消息通知）',
                            'type': 'switch',
                            'id': 'notify',
                        },
                        {
                            'title': '立即运行一次',
                            'required': "",
                            'tooltip': '打开后立即运行一次（点击此对话框的确定按钮后即会运行，周期未设置也会运行），关闭后将仅按照定时周期运行（同时上次触发运行的任务如果在运行中也会停止）',
                            'type': 'switch',
                            'id': 'onlyonce',
                        }
                    ]
                ]
            }
        ]

    def init_config(self, config=None):
        self._dbhelper = DbHelper()
        self._site = Sites()
        self._message = Message()

        # 读取配置
        if config:
            self._server = config.get("server")
            self._cron = config.get("cron")
            self._key = config.get("key")
            self._password = config.get("password")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._req = RequestUtils(content_type="application/json")
            if self._server:
                if not self._server.startswith("http"):
                    self._server = "http://%s" % self._server
                if self._server.endswith("/"):
                    self._server = self._server[:-1]

            # 测试
            _, msg, flag = self.__download_data()
            if flag:
                self._enabled = True
            else:
                self._enabled = False
                self.info(msg)

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=Config().get_timezone())

            # 运行一次
            if self._onlyonce:
                self.info(f"同步服务启动，立即运行一次")
                self._scheduler.add_job(self.__cookie_sync, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(Config().get_timezone())))
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "server": self._server,
                    "cron": self._cron,
                    "key": self._key,
                    "password": self._password,
                    "notify": self._notify,
                    "onlyonce": self._onlyonce,
                })

            # 周期运行
            if self._cron:
                self.info(f"同步服务启动，周期：{self._cron}")
                self._scheduler.add_job(self.__cookie_sync,
                                        CronTrigger.from_crontab(self._cron))

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self):
        return self._enabled

    def __download_data(self) -> [dict, str, bool]:
        """
        从CookieCloud下载数据
        """
        if not self._server or not self._key or not self._password:
            return {}, "CookieCloud参数不正确", False
        req_url = "%s/get/%s" % (self._server, self._key)
        ret = self._req.post_res(url=req_url, json={"password": self._password})
        if ret and ret.status_code == 200:
            result = ret.json()
            if not result:
                return {}, "", True
            if result.get("cookie_data"):
                return result.get("cookie_data"), "", True
            return result, "", True
        elif ret:
            return {}, "同步CookieCloud失败，错误码：%s" % ret.status_code, False
        else:
            return {}, "CookieCloud请求失败，请检查服务器地址、用户KEY及加密密码是否正确", False

    def __cookie_sync(self):
        """
        同步站点Cookie
        """
        # 同步数据
        contents, msg, _ = self.__download_data()
        if not contents:
            self.__send_message(msg)
            return
        # 整理数据,使用domain域名的最后两级作为分组依据
        domain_groups = defaultdict(list)
        for site, cookies in contents.items():
            for cookie in cookies:
                if self._event.is_set():
                    self.info(f"同步服务停止")
                    return
                domain_parts = cookie["domain"].split(".")[-2:]
                domain_key = tuple(domain_parts)
                domain_groups[domain_key].append(cookie)
        success_count = 0
        for domain, content_list in domain_groups.items():
            if self._event.is_set():
                self.info(f"同步服务停止")
                return
            if not content_list:
                continue
            cookie_str = ";".join(
                [f"{content['name']}={content['value']}" for content in content_list]
            )
            site_info = self._site.get_sites_by_suffix(".".join(domain))
            if site_info:
                self._dbhelper.update_site_cookie_ua(tid=site_info.get("id"), cookie=cookie_str)
                success_count += 1
        if success_count:
            # 重载站点信息
            self._site.init_config()
            self.__send_message(f"同步完成，更新 {success_count} 个站点的Cookie数据")
        else:
            self.__send_message(f"同步完成，未更新任何站点的Cookie！")

    def __send_message(self, msg):
        """
        发送通知
        """
        self.info(msg)
        if self._notify:
            self._message.send_custom_message(
                title="CookieCloud同步任务执行完成",
                text=f"{msg}"
            )

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))