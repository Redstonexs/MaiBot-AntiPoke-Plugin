from src.plugin_system.base.base_plugin import BasePlugin, register_plugin
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.base.component_types import ComponentInfo
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.apis import generator_api
from src.common.logger import get_logger
from typing import Tuple, Optional, Dict, Any, List, Type
import random
import asyncio
import toml
import time
import os

logger = get_logger("anti_poke")

# 使用模块级别的全局变量来保存状态
_POKE_STATE = {
    'poke_count': 0,
    'is_silent': False,
    'silence_start_time': 0,
    'last_poke_time': 0,
    'current_silence_duration': 0,
    'current_poke_threshold': 0,
    'decay_task': None,
    'counter_lock': None,
    'last_poke_back_time': 0,
}

POKE_BACK_COOLDOWN = 10

def _get_or_create_lock():
    """获取或创建异步锁，处理事件循环未就绪的情况"""
    if _POKE_STATE['counter_lock'] is None:
        try:
            _POKE_STATE['counter_lock'] = asyncio.Lock()
        except RuntimeError:
            # 如果没有事件循环，返回None
            return None
    return _POKE_STATE['counter_lock']

@register_plugin
class AntiPokePlugin(BasePlugin):
    """防戳插件
    - 支持配置一些具体参数以更像人
    """

    # 插件基本信息
    plugin_name = "anti_poke_plugin"
    enable_plugin = True
    config_file_name = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本配置",
        "components": "组件启用控制",
        "poke_value":"防戳机制的参数配置（支持热重载）",
        "logging": "日志记录配置",
    }

    # 配置Schema定义
    config_schema = {
        "plugin": {
            "config_version": ConfigField(type=str, default="0.6.0", description="插件配置文件版本号"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "components": {
            "enable_anti_poke": ConfigField(type=bool, default=True, description="是否启用防戳插件本体组件"),
        },
        "poke_value": {
            "min_slience_time": ConfigField(type=int, default = 120, description="戳一戳沉默的最短时间，单位为秒"),
            "max_slience_time": ConfigField(type=int, default = 300, description="戳一戳沉默的最长时间，单位为秒"),
            "min_slience_counts": ConfigField(type=int, default = 5, description="沉默需要被戳的最小次数"),
            "max_slience_counts": ConfigField(type=int, default = 9, description="沉默需要被戳的最大次数"),
            "counts_decay_interval": ConfigField(type=int, default = 150, description="被戳次数的递减间隔，单位为秒"),
            "poke_probility": ConfigField(type=float, default = 0.3, description="戳回去的概率，取值0到1之间任意小数。注意，不反戳就会正常回复"),
        },
        "logging": {
            "level": ConfigField(
                type=str, default="INFO", description="日志级别", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
            ),
            "prefix": ConfigField(type=str, default="[Anti_Poke]", description="日志前缀"),
        },
    }


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""

        components = []

        if self.get_config("components.enable_anti_poke", True):
            components.append((AntiPokeCommand.get_command_info(), AntiPokeCommand))

        return components

class AntiPokeCommand(BaseCommand):
    command_name = "anti_poke"
    command_description = "防戳插件"
    command_pattern = r"^(?P<content>[\s\S]+)（这是QQ的一个功能，用于提及某人，但没那么明显）$"
    command_help = "无使用方法，自动触发"
    command_examples = []
    
    def __init__(self, message, plugin_config: dict = None):
        super().__init__(message,plugin_config)
        self.message = message
        self.log_prefix = f"[Command:{self.command_name}]"

    @property
    def SILENCE_DURATION_MIN(self):
        config = self._load_config()
        return config["poke_value"].get("min_slience_time", 120)

    @property
    def SILENCE_DURATION_MAX(self):
        config = self._load_config()
        return config["poke_value"].get("max_slience_time", 300)

    @property
    def POKE_COUNT_MIN(self):
        config = self._load_config()
        return config["poke_value"].get("min_slience_counts", 5)

    @property
    def POKE_COUNT_MAX(self):
        config = self._load_config()
        return config["poke_value"].get("max_slience_counts", 9)

    @property
    def DECAY_INTERVAL(self):
        config = self._load_config()
        return config["poke_value"].get("counts_decay_interval", 180)
    
    @property
    def POKE_PROBILITY(self):
        config = self._load_config()
        return config["poke_value"].get("poke_probility", 0.3)

    async def execute(self) -> Tuple[bool, Optional[str]]:
        try:
            if _POKE_STATE['is_silent']: # 沉默截断机制
                current_time = time.time()
                if current_time - _POKE_STATE['silence_start_time'] > _POKE_STATE['current_silence_duration']:
                    _POKE_STATE['is_silent'] = False
                    _POKE_STATE['poke_count'] = 0  # 重置计数器（仅当解除沉默时）
                    logger.info(f"沉默期结束，持续了 {_POKE_STATE['current_silence_duration']} 秒")
                else:
                    # 沉默期直接忽略所有通知
                    logger.info("当前处于沉默期，忽略戳一戳")
                    return True,"处于沉默期，直接拦截所有戳一戳消息"

            if not self.message.message_info.message_id == "notice":
                return False,"非戳一戳消息，无需使用命令"

            content = self.matched_groups.get("content") 
            target_id = self.message.message_info.user_info.user_id

            self.start_decay_task_if_needed()
            current_time = time.time()
            counter_lock = _get_or_create_lock()
            if counter_lock:
                async with counter_lock:
                    _POKE_STATE['last_poke_time'] = current_time
                    _POKE_STATE['poke_count'] += 1
            else:
                # 如果无法获取锁，直接更新（降级处理）
                _POKE_STATE['last_poke_time'] = current_time
                _POKE_STATE['poke_count'] += 1

            # === 动态后缀生成 ===
            if _POKE_STATE['current_poke_threshold'] == 0:
                self.generate_random_silence_params()

            if _POKE_STATE['poke_count'] >= _POKE_STATE['current_poke_threshold']:
                suffix = "（请一定要回答类似于“哼，我不理你了”的话语以表示对过多戳一戳的抗议）"

                # 触发沉默机制
                _POKE_STATE['is_silent'] = True
                _POKE_STATE['silence_start_time'] = time.time()
                logger.info(f"触发沉默机制，戳戳次数: {_POKE_STATE['poke_count']}/{_POKE_STATE['current_poke_threshold']}, 沉默时长: {_POKE_STATE['current_silence_duration']}秒")
                # 为下次沉默生成新的随机参数 
                self.generate_random_silence_params()
            else:
                suffix = "（这是QQ的一个功能，用于提及某人，但没那么明显）"

                # 检查是否可以反戳（新增逻辑）
            can_poke_back = True
            if _POKE_STATE['last_poke_back_time'] > 0:
                time_since_last_poke_back = current_time - _POKE_STATE['last_poke_back_time']
                if time_since_last_poke_back < POKE_BACK_COOLDOWN:
                    can_poke_back = False
                    logger.info(f"反戳冷却中，还需等待 {POKE_BACK_COOLDOWN - time_since_last_poke_back:.1f} 秒")

            if random.random() < self.POKE_PROBILITY and not _POKE_STATE['is_silent'] and can_poke_back:
                await asyncio.sleep(3)
                await self.send_command("SEND_POKE",{"qq_id": target_id})
                _POKE_STATE['last_poke_back_time'] = current_time  # 更新上次反戳时间
                return True,"反戳一下"
            else:
                if not can_poke_back:
                    if random.random() < 0.33:
                        await self.generate_reply(content, suffix, target_id)
                        return True,"选择言语回复"
                    else:
                        return False,"不想回复"
                else:
                    await self.generate_reply(content, suffix, target_id)
                    return True,"选择言语回复"
            
        except Exception as e:
            logger.error(f"{self.log_prefix} 执行错误: {e}")
            return False, f"执行失败: {str(e)}"
        
    def _load_config(self) -> Dict[str, Any]:
        """从同级目录的config.toml文件直接加载配置"""
        try:
            # 获取当前文件所在目录
            script_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(script_dir, "config.toml")
            
            # 读取并解析TOML配置文件
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            # 构建配置字典，使用get方法安全访问嵌套值
            config = {
                "poke_value": {
                    "min_slience_time": config_data.get("poke_value", {}).get("min_slience_time", 120),
                    "max_slience_time": config_data.get("poke_value", {}).get("max_slience_time", 300),
                    "min_slience_counts": config_data.get("poke_value", {}).get("min_slience_counts", 5),
                    "max_slience_counts": config_data.get("poke_value", {}).get("max_slience_counts", 9),
                    "counts_decay_interval": config_data.get("poke_value", {}).get("counts_decay_interval", 180),
                    "poke_probility": config_data.get("poke_value", {}).get("poke_probility", 0.3),
                }
            }
            return config
        except Exception as e:
            logger.error(f"{self.log_prefix} 加载配置失败: {e}")
            raise

    def generate_random_silence_params(self):
        """
        生成随机的沉默参数
        """
        
        # 随机生成沉默持续时间
        _POKE_STATE['current_silence_duration'] = random.randint(
            self.SILENCE_DURATION_MIN, 
            self.SILENCE_DURATION_MAX
        )
        
        # 随机生成触发沉默所需的戳戳次数
        _POKE_STATE['current_poke_threshold'] = random.randint(
            self.POKE_COUNT_MIN, 
            self.POKE_COUNT_MAX
        )
        
        logger.info(f"生成新的沉默参数 - 持续时间: {_POKE_STATE['current_silence_duration']}秒, 触发阈值: {_POKE_STATE['current_poke_threshold']}次")

    async def poke_count_decay_task(self):
        """
        戳一戳计数器衰减任务：每3分钟检查一次，如果没有新的戳戳则减1
        """
        try:
            while True:
                await asyncio.sleep(self.DECAY_INTERVAL)  
            
                if not _POKE_STATE['is_silent'] and _POKE_STATE['poke_count'] > 0:
                    current_time = time.time()
                    # 如果距离上次被戳超过3分钟，则计数器减1
                    if current_time - _POKE_STATE['last_poke_time'] >= self.DECAY_INTERVAL:
                        counter_lock = _get_or_create_lock()
                        if counter_lock:
                            async with counter_lock:
                                _POKE_STATE['poke_count'] = max(0, _POKE_STATE['poke_count'] - 1)
                                logger.info(f"戳一戳计数器衰减，当前计数: {_POKE_STATE['poke_count']}")
                        else:
                             # 如果无法获取锁，直接更新（降级处理）
                            _POKE_STATE['poke_count'] = max(0, _POKE_STATE['poke_count'] - 1)
                            logger.info(f"戳一戳计数器衰减（无锁），当前计数: {_POKE_STATE['poke_count']}")
        except asyncio.CancelledError:
            logger.info("戳一戳计数器衰减任务被取消")
            raise
        except Exception as e:
            logger.error(f"戳一戳计数器衰减任务异常: {e}")
            # 任务异常退出，重置任务引用
            _POKE_STATE['decay_task'] = None
            raise

    def start_decay_task_if_needed(self):
        """
        在有事件循环时启动衰减任务（只启动一次）
        """
        if _POKE_STATE['decay_task'] is None or _POKE_STATE['decay_task'].done():
            try:
                _POKE_STATE['decay_task'] = asyncio.create_task(self.poke_count_decay_task())
                logger.debug("戳一戳计数器衰减任务已启动")
            except RuntimeError:
                # 如果还没有事件循环，稍后再试
                logger.debug("事件循环未就绪，稍后启动衰减任务")


    async def generate_reply(self, content: str, suffix: str, target_id):
        result_status, result_message = await generator_api.generate_reply(
                action_data = { 
                "raw_reply": f"{content}{suffix}",
                "reason": "有人戳了戳你，可能是在找你，也可能是在搞怪，你需要对此做出简洁的回应",
                },
                chat_stream= self.message.chat_stream
            )
        if result_status:
            for reply_seg in result_message:
                data = reply_seg[1]
                await self.send_type(message_type = "text", content = data, typing = True)
                await asyncio.sleep(1.0)