"""
PUT /api/config 的输入校验 schema。

跟 PTBrushConfig 区分开：这里接受"前端友好的 {value, unit}"形态，
校验通过后再走 web.config_serializer.to_unit_string 拼成 toml 中的字符串字段。
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from config.config import parse_time_ranges


SizeUnit = Literal["B", "KiB", "MiB", "GiB", "TiB"]
SpeedUnit = Literal["B/s", "KiB/s", "MiB/s", "GiB/s"]


class SizeWithUnit(BaseModel):
    value: float = Field(ge=0)
    unit: SizeUnit


class SpeedWithUnit(BaseModel):
    value: float = Field(ge=0)
    unit: SpeedUnit


class BrushInput(BaseModel):
    min_disk_space: SizeWithUnit
    torrent_max_size: SizeWithUnit
    max_downloading_torrents: int = Field(ge=1, le=100)
    # 低速种子不占下载位：低于该下载速度（MB/s）持续一个采样周期就不计入上限；0 关闭
    download_speed_min: float = Field(default=2.0, ge=0, le=1024)
    slow_delete_minutes: int = Field(default=15, ge=0, le=1440)
    slow_delete_speed: float = Field(default=1.0, ge=0, le=1024)
    # 低速上传持续 N 分钟就删（盒子专用）：独立开关 + 阈值单位 MB/s，0 分钟或 0 阈值关闭
    slow_up_delete_enabled: bool = False
    slow_up_delete_minutes: int = Field(default=0, ge=0, le=1440)
    slow_up_delete_speed: float = Field(default=0.1, ge=0, le=1024)
    max_no_activate_time: int
    free_end_delete_minutes: int = Field(default=20, ge=0, le=60)
    work_time: str = ""
    upload_cycle: int = Field(default=600, ge=60)
    download_cycle: int = Field(default=1200, ge=60)
    # 选种条件：允许的促销类型（free / free_2x / 2x / half_down / all）
    promotions: List[str] = ["free", "free_2x"]
    # 是否排除带 H&R 标记的种子
    exclude_hr: bool = True
    # 免费剩余不足 N 小时不下载
    min_free_hours: int = Field(default=5, ge=0, le=720)
    # 刷流任务执行间隔，单位分钟，底线 5 分钟（出厂默认 10）
    brush_interval: int = Field(default=10, ge=5, le=1440)
    # 参与刷流的站点（站点名），留空表示全部站点
    target_sites: List[str] = []
    # 留空名单时是否表示「一个都不参与」：True 则不刷任何站点（只有少数站点时用来关掉最后一个）
    target_none: bool = False

    # ---- 对齐 MoviePilot 刷流的安全阀（全部带默认值，老前端不下发也不会报错）----
    site_ratio_control: bool = False
    site_ratio_target: float = Field(default=2.0, gt=0)
    seed_time: float = Field(default=12, ge=0)
    hr_seed_time: float = Field(default=72, ge=0)
    seed_ratio: float = Field(default=1.0, ge=0)
    seed_size: float = Field(default=0, ge=0)
    download_time: float = Field(default=0, ge=0)
    seed_avgspeed: float = Field(default=0, ge=0)
    delete_size_range: str = ""
    include: str = ""
    exclude: str = ""
    size: str = "1-100"
    seeder: str = "20"
    pubtime: str = "60"
    fail_backoff_minutes: int = Field(default=1440, ge=0)
    qb_category: str = "ptbrush"
    check_interval: int = Field(default=5, ge=1, le=1440)

    @field_validator("size", "seeder", "pubtime", "delete_size_range")
    @classmethod
    def _validate_number_range(cls, v: str) -> str:
        import re
        v = str(v or "").strip()
        if not v:
            return ""
        if not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", v):
            raise ValueError("请输入数字或数字范围，例如 10 或 10-80")
        return v

    @field_validator("include", "exclude")
    @classmethod
    def _validate_regex(cls, v: str) -> str:
        import re
        if v:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"正则表达式无效：{e}")
        return v or ""

    @field_validator("work_time")
    @classmethod
    def _validate_work_time(cls, v: str) -> str:
        if v:
            parse_time_ranges(v)
        return v

    @field_validator("promotions")
    @classmethod
    def _validate_promotions(cls, v: List[str]) -> List[str]:
        from config.config import normalize_promotions

        normalized = normalize_promotions(v)
        if not normalized:
            raise ValueError("促销类型至少要选一种")
        return normalized


class DownloaderInput(BaseModel):
    # 别名：站点绑定下载器时按它匹配
    alias: str = "默认"
    url: str
    auth_type: Literal["password", "api_key"] = "password"
    username: str = ""
    password: str = ""
    api_key: str = ""
    # 该下载器的默认保存路径：站点没填时种子落这里；留空用 qB 默认目录/_ptbrush
    save_path: str = ""


class DownloadersPayload(BaseModel):
    """下载器列表整体提交（新增/改名/删除都走这一个 PUT）。"""

    downloaders: List[DownloaderInput] = []


class HeaderInput(BaseModel):
    key: str
    value: str = ""


class SiteInput(BaseModel):
    name: str
    # 解析器类型：mteam / nexusphp / rss；留空则后端自动推断
    type: str = ""
    # NexusPHP 站的根地址
    base_url: str = ""
    # RSS 站的订阅地址（保存时写入 options.rss_url）
    rss_url: str = ""
    cookie: str = ""
    headers: List[HeaderInput] = []
    # 站点优先级，数字越小越优先
    priority: int = Field(default=5, ge=0, le=999)
    # 站点启用/停用
    enabled: bool = True
    # 此站点使用的下载器别名；留空表示用第一个（主下载器）
    downloader: str = ""
    # 此站点的保存路径；留空则用所绑定下载器的默认保存路径
    save_path: str = ""
    # 站点 API Key（敏感字段，回传 *** 时后端保留旧值）
    api_key: str = ""
    # 站点请求用的 User-Agent（保存时写入 options.user_agent）
    user_agent: str = ""
    # 站点请求超时（秒），0 表示不限制（保存时写入 options.timeout）
    timeout: int = Field(default=15, ge=0, le=300)


class CookieCloudInput(BaseModel):
    # CookieCloud 服务器地址，留空则用公共服务器
    server: str = ""
    uuid: str = ""
    # 敏感字段：回传 *** 时后端保留旧值
    password: str = ""


class ConfigPayload(BaseModel):
    brush: BrushInput
    # 用户是否真的动过「参与刷流的站点」开关：没动过却把名单改窄，视为页面数据过期，直接拒绝
    targets_touched: bool = False
    # 下载器由独立的「下载器配置」页维护，刷流配置页可以不传
    downloader: Optional[DownloaderInput] = None
    sites: List[SiteInput] = []
    # 凭据已搬到「安全设置」页：这个页面不传时不要写空值覆盖已有配置
    cookiecloud: Optional[CookieCloudInput] = None


class TestDownloaderPayload(BaseModel):
    # 要测试哪个下载器：带上别名才能匹配到它自己保存的密码
    alias: str = ""
    url: str
    auth_type: Literal["password", "api_key"] = "password"
    username: str = ""
    password: str = ""
    api_key: str = ""


class TestSitePayload(BaseModel):
    name: str
    type: str = ""
    base_url: str = ""
    rss_url: str = ""
    cookie: str = ""
    headers: List[HeaderInput] = []


class LoginPayload(BaseModel):
    password: str


class ChangePasswordPayload(BaseModel):
    # 当前密码：若当前未启用登录，可传空串
    current_password: str = ""
    # 新密码：传空串表示"关闭登录"
    new_password: str
