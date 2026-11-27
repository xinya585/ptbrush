from pathlib import Path
import shutil
from typing import Any, Dict, List, Literal, Optional, Tuple, Type, Union
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
import re
from datetime import time
import datetime


from paths import bundle_dir, data_dir

CONFIG_FILE_PATH = data_dir() / "config.toml"


class HeaderParam(BaseModel):
    key: str
    value: str


# HTTP 头值里不能出现的字符：换行 / 回车 / 制表符 / 其它控制字符
_HEADER_BAD_CHARS = re.compile(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_header_value(value: Optional[str]) -> str:
    """把 Cookie、请求头值这类要直接进 HTTP 头的字符串清洗成合法单行。

    urllib3 对头值有严格校验：以空白开头、含回车换行都会被判 InvalidHeader，
    请求根本发不出去。用户从浏览器 DevTools 复制 Cookie 时很容易带上换行和空格，
    所以在模型入口统一处理：去掉控制字符，再剥掉首尾空白。
    """
    if value is None:
        return ""
    return _HEADER_BAD_CHARS.sub("", str(value)).strip()


# 促销类型：站点列表里的 Free / 2XFree / 2X / 50% 等标记，统一归一化成这几个值
PROMOTION_META = [
    {"value": "free", "label": "免费", "description": "下载不计下载量（Free）"},
    {"value": "free_2x", "label": "2X 免费", "description": "下载免费且上传双倍（2X Free）"},
    {"value": "2x", "label": "2X 上传", "description": "上传量双倍，下载照常计量（2X）"},
    {"value": "half_down", "label": "50% 下载", "description": "下载量按一半计算（50% / 半价）"},
    {"value": "all", "label": "不限制", "description": "不挑促销类型，非促销种子也会下载（费下载量，慎用）"},
]

PROMOTION_ALIASES = {
    "free": "free",
    "freeleech": "free",
    "免费": "free",
    "免费下载": "free",
    "free_2x": "free_2x",
    "free2x": "free_2x",
    "2xfree": "free_2x",
    "2x_free": "free_2x",
    "2x免费": "free_2x",
    "免费2x": "free_2x",
    "双倍免费": "free_2x",
    "2x": "2x",
    "2倍": "2x",
    "双倍": "2x",
    "twoup": "2x",
    "half_down": "half_down",
    "halfdown": "half_down",
    "50": "half_down",
    "50%": "half_down",
    "半价": "half_down",
    "五折": "half_down",
    "all": "all",
    "不限": "all",
    "any": "all",
}

VALID_PROMOTIONS = {item["value"] for item in PROMOTION_META}


def normalize_promotions(values) -> List[str]:
    """把用户写的促销类型（中英文、别名、大小写混杂）归一化成标准值列表。"""
    result = []
    for raw in values or []:
        key = str(raw).strip().lower().replace(" ", "")
        mapped = PROMOTION_ALIASES.get(key, key)
        if mapped in VALID_PROMOTIONS and mapped not in result:
            result.append(mapped)
    if "all" in result:
        return ["all"]
    return result


class SiteModel(BaseModel):
    name: str
    # 解析器类型：mteam / nexusphp / rss，留空时按站点名、base_url、rss_url 自动推断
    type: str = ""
    # 站点域名键：跨表关联（图标/统计/账号快照）和流控隔离都用它。
    # 没填时由 base_url 或 rss_url 自动推导，站点改成中文名也不影响键
    domain: str = ""
    # NexusPHP 类站点必填：站点根地址，如 https://pt.example.com
    base_url: str = ""
    cookie: Optional[str] = ""
    headers: Optional[List[HeaderParam]] = []
    # 站点优先级：数字越小越优先（多站同时有新种时先刷优先级高的）
    priority: int = 5
    # 站点启用/停用：停用的站点不抓种、不刷流，cookie 仍保留
    enabled: bool = True
    # 此站点使用的下载器别名；留空表示用第一个（主下载器）
    downloader: str = ""
    # 此站点的保存路径：留空则用所绑定下载器的默认保存路径
    save_path: str = ""
    # 站点 API Key（M-Team 这类走 x-api-key 的站）
    api_key: Optional[str] = ""
    # 备用令牌：少数站点用 Authorization: Bearer <token> 而不是 API Key
    token: Optional[str] = ""
    # 是否通过代理访问该站点（沿用容器的 HTTP_PROXY / HTTPS_PROXY 环境变量）
    proxy: bool = False
    # 本站排除关键词：种子名命中任意一个就跳过，逗号或换行分隔
    filter: str = ""
    # 备注：纯展示，不参与抓取
    note: str = ""
    # 公开站点标记：只用于界面徽标，不影响抓取
    public: bool = False
    # 流控：相邻两次请求的最小间隔，单位秒，0 表示不限
    limit_seconds: int = 0
    # 流控：统计窗口长度，单位秒，0 表示不启用窗口计数
    limit_interval: int = 0
    # 流控：窗口内允许的最大请求数，0 表示不限
    limit_count: int = 0
    # 适配器的细粒度参数：pages / request_interval / user_agent / timeout / rss_url 等
    options: Optional[Dict[str, Any]] = {}

    @model_validator(mode="after")
    def _validate_type_requirements(self):
        from ptsite import normalize_type

        # 先把请求头类字段洗干净：从浏览器复制 Cookie / 请求头时常带换行、制表符、
        # 前导空格，urllib3 会在发请求前直接抛 InvalidHeader，站点看着填满了却用不了
        self.cookie = clean_header_value(self.cookie)
        self.api_key = (self.api_key or "").strip()
        self.token = (self.token or "").strip()
        for h in self.headers or []:
            h.key = (h.key or "").strip()
            h.value = clean_header_value(h.value)

        # 域名键：显式填了就归一化，没填就从站点地址推导
        raw_domain = self.domain or self.base_url or (self.options or {}).get("rss_url") or ""
        resolved_domain = site_domain_of(raw_domain)
        if resolved_domain:
            self.domain = resolved_domain

        resolved = normalize_type(self.type)
        if resolved == "nexusphp" and not (self.base_url or self.options.get("base_url")):
            raise ValueError(f"站点 {self.name} 使用 nexusphp 解析器时必须填写 base_url")
        if resolved == "rss" and not (self.options.get("rss_url") or self.options.get("rss_urls")):
            raise ValueError(f"站点 {self.name} 使用 rss 解析器时必须在 options 里填写 rss_url")

        # 站点级促销 / H&R 覆盖（不填就跟随全局 [brush] 配置）
        options = dict(self.options or {})
        if options.get("promotions"):
            normalized = normalize_promotions(options["promotions"])
            if not normalized:
                raise ValueError(
                    f"站点 {self.name} 的 options.promotions 没有合法值，可填："
                    + "、".join(sorted(VALID_PROMOTIONS))
                )
            options["promotions"] = normalized
            self.options = options
        return self

    @field_validator("limit_seconds", "limit_interval", "limit_count")
    @classmethod
    def _non_negative(cls, v):
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0


class QBConfig(BaseModel):
    # 下载器别名：给站点绑定下载器时按它匹配，改别名后站点里的绑定要跟着改
    alias: str = "默认"
    url: str = ""
    auth_type: Literal["password", "api_key"] = "password"
    username: str = ""
    password: str = ""
    api_key: str = ""
    # 该下载器的默认保存路径：站点没单独填时种子落这里；留空用 qB 分类路径（默认目录/_ptbrush）
    save_path: str = ""


def _host_of(url: str) -> str:
    """从站点地址里取域名，取不到返回空串。"""
    try:
        from urllib.parse import urlparse

        raw = (url or "").strip()
        if not raw:
            return ""
        netloc = urlparse(raw if "://" in raw else "https://" + raw).netloc.lower()
        netloc = netloc.split("@")[-1].split(":")[0]
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def site_domain_of(url: str) -> str:
    """
    把一个地址归一成站点域名键。

    规则跟 MoviePilot 的 extract_domain 对齐：
    - 去掉 scheme、去掉末尾路径、去掉端口、去掉 www.
    - 域名标签 ≤3 段时取末两段（pt.example.com -> example.com，zp.m-team.io -> m-team.io）
    - 超过 3 段原样保留（避免把 a.b.c.d 这种真多级域名切错）
    """
    host = _host_of(url)
    if not host:
        return ""
    host = host.lstrip(".")
    labels = [x for x in host.split(".") if x]
    if not labels:
        return ""
    if len(labels) > 3:
        return ".".join(labels)
    return ".".join(labels[-2:]) if len(labels) >= 2 else labels[0]


def site_key(site) -> str:
    """
    站点的稳定标识。

    站点名是可以改的（很多人会把 hdsky.me 改成「天空」），所以「参与刷流的站点」
    这种跨会话保存的选择项不能拿名字当键，否则改名之后选择就对不上了。
    优先用显式 domain，其次从地址推导，最后才退回站点名。
    """
    if isinstance(site, dict):
        explicit = str(site.get("domain") or "")
        url = site.get("base_url") or (site.get("options") or {}).get("rss_url") or ""
        fallback = str(site.get("name") or "")
    else:
        explicit = str(getattr(site, "domain", "") or "")
        url = getattr(site, "base_url", "") or ""
        if not url:
            url = ((getattr(site, "options", None) or {}).get("rss_url")) or ""
        fallback = str(getattr(site, "name", "") or "")
    return explicit or site_domain_of(url) or fallback


def targets_site(target_sites, site, none=False) -> bool:
    """这个站点是否在「参与刷流的站点」里；target_sites 为空表示全部参与（none=True 表示一个都不参与）。"""
    if none:
        return False
    target = {str(t).strip() for t in (target_sites or []) if str(t).strip()}
    if not target:
        return True
    if isinstance(site, dict):
        name = str(site.get("name") or "")
    else:
        name = str(getattr(site, "name", "") or "")
    return name in target or site_key(site) in target


def filter_target_sites(target_sites, sites, none=False) -> List[Any]:
    """按「参与刷流的站点」过滤站点列表。"""
    if none:
        return []
    return [s for s in (sites or []) if targets_site(target_sites, s)]


def enabled_sites(sites) -> List[Any]:
    """只看启用的站点：站点卡片里「状态=停用」的站点不抓种、不刷流。"""
    result = []
    for s in sites or []:
        enabled = s.get("enabled", True) if isinstance(s, dict) else getattr(s, "enabled", True)
        if enabled:
            result.append(s)
    return result


def target_site_names(target_sites, sites, none=False) -> List[str]:
    """把选择项翻译成当前站点名集合（数据库里按站点名存的）。"""
    return [str(s.get("name") if isinstance(s, dict) else getattr(s, "name", "")) for s in filter_target_sites(target_sites, sites, none)]


def normalize_target_sites(target_sites, sites) -> List[str]:
    """
    把手里的选择项归一成域名键，顺带剔除已经不存在的站点。

    兼容存量的站点名写法：老配置里存的是 hdsky.me 或「天空」，保存时统一换成域名。
    """
    result: List[str] = []
    target = {str(t).strip() for t in (target_sites or []) if str(t).strip()}
    # 空 = 全部站点参与，保持空着，这样以后新加的站点才会自动加入
    if not target:
        return []
    for site in sites or []:
        if not targets_site(target_sites, site):
            continue
        key = site_key(site)
        if key and key not in result:
            result.append(key)
    return result


def parse_size(size: Union[str, int]) -> int:
    """Convert size string with units to bytes
    
    Supports formats like:
    - 1024 (assumed bytes)
    - "1024B"
    - "1KiB", "1MiB", "1GiB", "1TiB"
    - "1KB", "1MB", "1GB", "1TB"
    """
    if isinstance(size, (int, float)):
        return int(size)
        
    size = str(size).upper().strip()
    if size.isdigit():
        return int(size)
        
    units = {
        'B': 1,
        'KIB': 1024,
        'MIB': 1024 ** 2, 
        'GIB': 1024 ** 3,
        'TIB': 1024 ** 4,
        'KB': 1000,
        'MB': 1000 ** 2,
        'GB': 1000 ** 3,
        'TB': 1000 ** 4
    }
    
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Z]+)$', size)
    if not match:
        raise ValueError(f"Invalid size format: {size}")
        
    value, unit = match.groups()
    if unit not in units:
        raise ValueError(f"Invalid unit: {unit}")
        
    return int(float(value) * units[unit])


def parse_speed(speed: Union[str, int]) -> int:
    """Convert speed string with units to bytes per second
    
    Supports formats like:
    - 1024 (assumed B/s)
    - "1KiB/s", "1MiB/s", "1GiB/s"
    """
    if isinstance(speed, (int, float)):
        return int(speed)
        
    speed = str(speed).upper().strip()
    if speed.isdigit():
        return int(speed)
        
    # Handle bytes/s format
    units = {
        'B/S': 1,
        'KIB/S': 1024,
        'MIB/S': 1024 ** 2,
        'GIB/S': 1024 ** 3,
    }
    
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Z/]+)$', speed)
    if not match:
        raise ValueError(f"Invalid speed format: {speed}")
        
    value, unit = match.groups()
    if unit not in units:
        raise ValueError(f"Invalid unit: {unit}")
        
    return int(float(value) * units[unit])


def parse_time_ranges(time_ranges: str) -> list[tuple[time, time]]:
    """Parse time ranges string to list of (start_time, end_time) tuples
    
    Supports formats like:
    - "1-4" (1:00-4:59)
    - "12-18" (12:00-18:59)
    - "20-23,0-6" (20:00-23:59 and 0:00-6:59)
    """
    if not time_ranges:
        return []
        
    result = []
    ranges = time_ranges.split(',')
    
    for time_range in ranges:
        if not re.match(r'^\d{1,2}-\d{1,2}$', time_range):
            raise ValueError(f"Invalid time range format: {time_range}")
            
        start, end = map(int, time_range.split('-'))
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise ValueError(f"Hours must be between 0-23: {time_range}")
            
        result.append((
            time(hour=start),
            time(hour=end, minute=59, second=59)
        ))
    
    return result


class BrushConfig(BaseModel):

    # 保留最小剩余磁盘空间，支持单位如 "1GiB"、"100MiB" 等，默认单位为B
    min_disk_space: Union[str, int] = 100 * 1024 * 1024 * 1024  # 100GiB

    # 位于下载状态的种子数上限，当qb中下载状态种子数大于此值时不会添加新的任务
    max_downloading_torrents: int = 5

    # 低速种子不占下载位：未完成种子的下载速度低于该值（MB/s）并持续 download_cycle 秒以上，
    # 就不计入 max_downloading_torrents；0 表示关闭这条判断（等待状态本来就不占位）
    download_speed_min: float = Field(default=2.0, ge=0, le=1024)
    # 低速种子持续多少分钟就删：配合 download_speed_min 用，0 表示不删。
    # 只对没下完、且速度一直低于 download_speed_min 的种子生效，H&R 种子跳过。
    # 出厂默认 15 分钟（3 个检查周期）：必须短于 download_cycle（20 分钟），
    # 这样死种先被删掉、下载位随删释放，不会出现「已经开始让位、种子还赖在位里」的超发。
    # 死种阈值仍取 1.0：5 个下载位分摊下行时单种只有 1.2~1.4 MB/s，
    # 阈值定到 1.5 会把「还能在免费期内下完」的种误删。
    slow_delete_minutes: int = Field(default=15, ge=0, le=1440)
    # 低速删种自己的阈值（MB/s）：和「低速种子不占下载位」分开配，互不影响
    slow_delete_speed: float = Field(default=1.0, ge=0, le=1024)

    # 低速上传删种的独立开关（盒子专用）。默认关闭：这条规则杀伤力大（针对全部已下完的种子），
    # 只有明确打开、且阈值和分钟都 > 0 才会真删；关掉开关时下面两个数字只是记住的草稿。
    slow_up_delete_enabled: bool = False
    # 低速上传持续多少分钟就删（盒子专用）：只判已经下载完成的种子，
    # 上传速度一直低于 slow_up_delete_speed（MB/s）并持续 N 分钟，就删掉腾保种位/带宽。
    # 0 表示关闭。盒子上位子有限，一个 200G 的种挂着只跑 0.05 MB/s，不如换新热门种。
    # H&R 种子跳过（删了要挨站点处罚）。
    slow_up_delete_minutes: int = Field(default=0, ge=0, le=1440)
    # 低速上传的阈值（MB/s）：盒子上单种 0.1 MB/s 以下基本就是没人拉了；默认 0.1
    slow_up_delete_speed: float = Field(default=0.1, ge=0, le=1024)

    # 平均速度计算用的时间周期，默认即可，不推荐修改
    upload_cycle: int = 600  # 平均上传速度计算周期，单位秒，默认600秒即10分钟
    download_cycle: int = 1200  # 低速不占位的判定窗口（秒），默认 1200 秒即 20 分钟（两个刷流轮次）：
    # 取整数倍刷流间隔，判定才落在轮次边界上；同时必须长于 slow_delete_minutes，
    # 保证「先删死种、后让位」的顺序，避免并发超发

    # 单个种子的文件大小限制，支持以下格式:
    # - 纯数字 (默认单位为B)
    # - "1KiB", "1MiB", "1GiB", "1TiB"
    # 超过此限制后，会将种子中的部分文件设置为不下载
    # 默认值为50GiB
    torrent_max_size: Union[str, int] = 0  # 已下线「单个种子最大体积」，默认 0 表示不限制
    
    # 允许种子最大的无活跃(无下载也无上传)时间，超过此时间将会被删除，单位为:分钟，默认10分钟；<=0 表示关闭清理
    max_no_activate_time: int = 0
    # 免费期结束前多少分钟就把没下完的种删掉（0 关闭，10/20/30 三档）
    free_end_delete_minutes: int = Field(default=20, ge=0, le=60)
    
    # 工作时间范围，格式如: "1-4" 表示1:00-4:59, "20-23,0-6" 表示20:00-23:59和0:00-6:59
    # 留空则表示24小时工作
    work_time: str = ""

    # 自动下载哪些促销类型的种子（多选）：free / free_2x / 2x / half_down / all
    # 默认只挑「免费」和「2X免费」，即下载不计流量
    promotions: List[str] = ["free", "free_2x"]

    # 是否排除带 H&R（Hit and Run）标记的种子，默认排除
    exclude_hr: bool = True

    # 免费剩余时间不足 N 小时的种子不下载，避免刚下完免费就结束；0 表示不限制
    min_free_hours: int = 5

    # 刷流任务执行间隔，单位分钟，最小 5 分钟（填更小的值会被按 5 处理）
    # 出厂默认 10 分钟：和候选库抓取同频，避免 5 分钟一轮反复啃同一批旧数据
    brush_interval: int = 10

    # 参与刷流的站点（按域名多选）：只在这些站点上抓种和刷流
    # 用域名而不是站点名当键，站点改成中文名之后选择项依然有效
    # 留空表示所有已配置站点都参与；存量配置里的站点名写法也能识别
    target_sites: List[str] = []

    # 「所有站点都不参与」：target_sites 留空在旧语义里等于「全部参与」，
    # 只有一个站点时会把唯一的开关关掉又弹回来，所以单独用这个开关表达「一个都不刷」。
    # 默认 False（保持老配置语义：留空=全部）。
    target_none: bool = False

    # ---- 以下为对齐 MoviePilot 刷流的安全阀（新字段全部带默认值，老配置可直接加载）----

    # 站点分享率控制：参与站点的分享率达到目标就暂停新增，低于目标自动恢复
    site_ratio_control: bool = False
    site_ratio_target: float = Field(default=2.0, gt=0)

    # 条件删种（0 表示关闭该条件）
    seed_time: float = Field(default=12, ge=0)      # 做种时长达到 N 小时就删
    hr_seed_time: float = Field(default=72, ge=0)   # H&R 种子做种达到 N 小时才删
    seed_ratio: float = Field(default=1.0, ge=0)     # 分享率达到 N 就删（仅对已完成种子）；实测做种 12h 只到 0.1~0.4，3.0 永不触发
    seed_size: float = Field(default=0, ge=0)       # 累计上传达到 N GB 就删
    download_time: float = Field(default=0, ge=0)   # 未完成种子下载耗时超过 N 小时就删
    seed_avgspeed: float = Field(default=0, ge=0)   # 平均上传低于 N KB/s 就删（做种满 30 分钟才判）

    # 动态删种（保种体积）："100" 到 100GB 就删到 100GB；"50-100" 到 100GB 删到 50GB；留空关闭
    delete_size_range: str = ""

    # 选种增强（留空不启用）
    include: str = ""   # 标题/描述必须匹配的正则
    exclude: str = ""   # 标题/描述命中就跳过
    size: str = "1-100"  # 体积范围（GiB）："10" 表示不小于 10GB，"10-80" 表示这个区间
    seeder: str = "20"   # 做种人数范围："30" 表示不超过 30，"10-50" 表示这个区间
    pubtime: str = "60"  # 发布时间范围（分钟）："60" 表示 60 分钟内，"10-120" 表示这个区间

    # 下载失败退避（分钟）：加种失败的候选在这段时间内不再重试，避免反复消耗站点下载额度
    fail_backoff_minutes: int = Field(default=1440, ge=0)

    # 本工具在 qBittorrent 里使用的分类名（留空用默认 ptbrush）
    qb_category: str = "ptbrush"

    # 删种与种子状态检查的独立周期（分钟），与刷流周期分开
    # 出厂默认 5 分钟：比刷流间隔（10 分钟）更勤，下不动/等待的种子能更快腾出并发位
    check_interval: int = Field(default=5, ge=1, le=1440)

    @field_validator("promotions")
    def validate_promotions(cls, v):
        normalized = normalize_promotions(v)
        if not normalized:
            raise ValueError(
                "promotions 至少要选一种促销类型，可填："
                + "、".join(sorted(VALID_PROMOTIONS))
            )
        return normalized
    
    @field_validator('min_disk_space')
    def validate_min_disk_space(cls, v):
        try:
            return parse_size(v)
        except ValueError as e:
            raise ValueError(f"Invalid min_disk_space value: {e}")

    @field_validator('torrent_max_size')
    def validate_torrent_max_size(cls, v):
        try:
            return parse_size(v)
        except ValueError as e:
            raise ValueError(f"Invalid torrent_max_size value: {e}")

    @field_validator('work_time')
    def validate_work_time(cls, v):
        try:
            parse_time_ranges(v)
            return v
        except ValueError as e:
            raise ValueError(f"Invalid work_time value: {e}")

    @field_validator('brush_interval')
    def validate_brush_interval(cls, v):
        """刷流间隔的下限是 5 分钟：填更小的值直接按 5 处理，避免把下载器打爆。"""
        try:
            minutes = int(v)
        except (TypeError, ValueError):
            raise ValueError("brush_interval 必须是整数分钟数")
        return max(5, minutes)

    @field_validator("size", "seeder", "pubtime", "delete_size_range")
    def validate_number_range(cls, v):
        """单值或区间必须形如 10 或 10-80，空值表示不启用。"""
        if v is None:
            return ""
        v = str(v).strip()
        if not v:
            return ""
        if not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", v):
            raise ValueError("请输入数字或数字范围，例如 10 或 10-80")
        return v

    @field_validator("delete_size_range")
    def validate_delete_range_order(cls, v):
        """动态删种区间必须下限小于上限，否则删到下限后就该停了却没停。"""
        if v and "-" in v:
            low, high = [float(x) for x in v.split("-")]
            if low >= high:
                raise ValueError("动态删种区间下限必须小于上限，例如 50-100")
        return v

    @field_validator("include", "exclude")
    def validate_regex(cls, v):
        """提前把正则编译一遍，配置写错了保存时就报，不用等到跑任务。"""
        if v:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"正则表达式无效：{e}")
        return v or ""

    def is_work_time(self) -> bool:
        """Check if current time is in work time ranges"""
        if not self.work_time:
            return True
            
        now = datetime.datetime.now().time()
        ranges = parse_time_ranges(self.work_time)
        return any(start <= now <= end for start, end in ranges)


class CookieCloudConfig(BaseModel):
    # CookieCloud 服务器地址（自建或公共）。留空则用公共的 https://ccc.youcl.com
    server: str = ""
    # CookieCloud 的 UUID（浏览器插件里能看到）
    uuid: str = ""
    # CookieCloud 的密码，只在本机解密用
    password: str = ""


class WebConfig(BaseModel):
    # Web 端登录密码；为空时不要求登录（保留对老用户的兼容性）
    password: str = ""
    # Flask session 签名 key；为空时启动会自动生成并写回 toml
    secret_key: str = ""


class UpdateConfig(BaseModel):
    # 安装包下载节点：cf = Cloudflare，cn = 国内节点，ov = 海外节点。
    # 只管「下到哪个节点」，更新清单始终三个节点自动挑最快的。
    download_node: str = "cf"


class PTBrushConfig(BaseSettings):
    # 多下载器：[[downloaders]] 数组，每项带 alias 别名；站点按别名绑定
    downloaders: Optional[List[QBConfig]] = []
    # 老配置兼容：单个 [downloader] 段，读取时自动当作 alias="默认" 的那一条
    downloader: Optional[QBConfig] = None
    sites: Optional[List[SiteModel]] = []
    brush: Optional[BrushConfig] = BrushConfig()
    web: Optional[WebConfig] = WebConfig()
    cookiecloud: Optional[CookieCloudConfig] = CookieCloudConfig()
    update: Optional[UpdateConfig] = UpdateConfig()

    model_config = SettingsConfigDict(toml_file=str(CONFIG_FILE_PATH))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (TomlConfigSettingsSource(settings_cls, toml_file=str(CONFIG_FILE_PATH)),)

    @classmethod
    def init_config(cls):
        if not CONFIG_FILE_PATH.exists():
            # 打包后 config.example.toml 在只读资源目录里，开发态就在 config/ 下
            example_config_path = bundle_dir() / "config" / "config.example.toml"
            if not example_config_path.exists():
                example_config_path = Path(__file__).parent / "config.example.toml"
            CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(example_config_path, CONFIG_FILE_PATH)
            logger.info(
                f"配置文件不存在已为您创建新的配置文件：{CONFIG_FILE_PATH.absolute()}"
            )
            logger.info(f"请编辑配置文件添加站点信息以及下载器信息后，开始刷流~")
        else:
            logger.info(
                f"配置文件已存在：{CONFIG_FILE_PATH.absolute()}，跳过初始化配置文件"
            )



    def downloader_list(self) -> List[QBConfig]:
        """
        归一化后的下载器列表。

        - 优先用 [[downloaders]] 数组；
        - 数组为空时回退老的单个 [downloader] 段（当作一条 alias="默认"）；
        - 别名去重：空别名或重名自动补成「下载器N」，保证站点里存的别名能稳定匹配。
        """
        items = [d for d in (self.downloaders or []) if d is not None and (d.url or "").strip()]
        if not items:
            single = self.downloader
            if single is not None and (single.url or "").strip():
                single.alias = (single.alias or "").strip() or "默认"
                return [single]
            return []

        used: set = set()
        for idx, dl in enumerate(items, 1):
            alias = (dl.alias or "").strip()
            if not alias:
                alias = f"下载器{idx}"
            base, n = alias, 2
            while alias in used:
                alias = f"{base}-{n}"
                n += 1
            dl.alias = alias
            used.add(alias)
        return items

    def downloader_named(self, alias: str = "") -> Optional[QBConfig]:
        """按别名取下载器；别名没填或匹配不到时退回列表第一个（即「主下载器」）。"""
        items = self.downloader_list()
        if not items:
            return None
        key = (alias or "").strip()
        if key:
            for dl in items:
                if (dl.alias or "").strip() == key:
                    return dl
        return items[0]

if __name__ == "__main__":
    e = PTBrushConfig()
    e.brush.is_work_time()
# PTBrushConfig.init_config()
# print(PTBrushConfig().model_dump_json())
