"""OTA 控制器向界面发布的类型化事件。"""

from dataclasses import dataclass
from enum import Enum


class UpgradeStage(Enum):
    """OTA 升级的协议阶段。"""

    ERASE = "擦除"
    PROGRAM = "编程"
    VERIFY = "校验"
    END = "结束"


class UpgradeStatus(Enum):
    """一次 OTA 升级的运行结果。"""

    RUNNING = "运行中"
    SUCCESS = "成功"
    FAILED = "失败"
    CANCELLED = "已取消"


@dataclass(frozen=True, slots=True)
class OtaEvent:
    """状态机进度快照；进度和总量的单位均为固件字节。"""

    stage: UpgradeStage
    status: UpgradeStatus
    progress: int = 0
    total: int = 0
    message: str = ""
    speed: float = 0.0
