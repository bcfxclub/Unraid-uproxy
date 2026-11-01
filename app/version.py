"""uproxy 版本号单一来源。

发版时只改这里的 __version__，其余地方（FastAPI OpenAPI、/api/version
接口、前端左下角）都从这里读取，保证「改一次就全链路同步」。
"""

__version__ = "1.0.19"
