import asyncio
import psutil
import time
from typing import Dict, Any, Optional
from datetime import datetime
import threading


class SystemMonitor:
    """系统监控服务"""
    
    def __init__(self, dingtalk_alert=None):
        self.dingtalk_alert = dingtalk_alert
        self.is_running = False
        self.monitor_task = None
        
        # 默认阈值配置
        self.thresholds = {
            "cpu_usage": 89.0,      # CPU使用率阈值
            "memory_usage": 90.0,    # 内存使用率阈值
            "disk_usage": 90.0,      # 磁盘使用率阈值
            "check_interval": 60     # 检查间隔（秒）
        }
        
        # 告警状态记录，避免重复告警
        self.alert_status = {
            "system_alerted": False  # 统一系统告警状态
        }
        
        # 告警冷却时间（秒）
        self.alert_cooldown = 300  # 5分钟
    
    async def start(self):
        """启动系统监控"""
        self.is_running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        print("✅ 系统监控服务已启动")
    
    async def stop(self):
        """停止系统监控"""
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        print("🛑 系统监控服务已停止")
    
    def update_thresholds(self, thresholds: Dict[str, float]):
        """更新监控阈值"""
        self.thresholds.update(thresholds)
        print(f"📊 监控阈值已更新: {thresholds}")
    
    async def _monitor_loop(self):
        """监控主循环"""
        while self.is_running:
            try:
                await self._check_system_metrics()
                await asyncio.sleep(self.thresholds["check_interval"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ 系统监控错误: {e}")
                await asyncio.sleep(10)
    
    async def _check_system_metrics(self):
        """检查系统指标 - 使用OR逻辑，任一条件满足就告警"""
        metrics = self._get_system_metrics()
        
        # 检查是否有任一指标超过阈值（OR关系）
        cpu_exceeded = metrics["cpu_usage"] > self.thresholds["cpu_usage"]
        memory_exceeded = metrics["memory_usage"] > self.thresholds["memory_usage"]
        disk_exceeded = metrics["disk_usage"] > self.thresholds["disk_usage"]
        
        # 任一条件满足就发送告警
        if (cpu_exceeded or memory_exceeded or disk_exceeded) and not self.alert_status["system_alerted"]:
            await self._send_system_alert(metrics, cpu_exceeded, memory_exceeded, disk_exceeded)
            self.alert_status["system_alerted"] = True
            # 设置冷却时间后重置告警状态
            asyncio.create_task(self._reset_alert_status("system_alerted"))
        elif not (cpu_exceeded or memory_exceeded or disk_exceeded):
            # 所有指标都正常时重置告警状态
            self.alert_status["system_alerted"] = False
    
    def _get_system_metrics(self) -> Dict[str, float]:
        """获取系统指标"""
        try:
            # CPU使用率
            cpu_usage = psutil.cpu_percent(interval=1)
            
            # 内存使用率
            memory = psutil.virtual_memory()
            memory_usage = memory.percent
            
            # 磁盘使用率（根目录）
            disk = psutil.disk_usage('/')
            disk_usage = (disk.used / disk.total) * 100
            
            return {
                "cpu_usage": cpu_usage,
                "memory_usage": memory_usage,
                "disk_usage": disk_usage,
                "timestamp": time.time()
            }
        except Exception as e:
            print(f"❌ 获取系统指标失败: {e}")
            return {
                "cpu_usage": 0.0,
                "memory_usage": 0.0,
                "disk_usage": 0.0,
                "timestamp": time.time()
            }
    
    async def _send_system_alert(self, metrics: Dict[str, float], cpu_exceeded: bool, memory_exceeded: bool, disk_exceeded: bool):
        """发送系统告警 - 统一处理所有类型的告警"""
        if not self.dingtalk_alert:
            return
        
        try:
            # 构建告警详情
            alert_types = []
            if cpu_exceeded:
                alert_types.append(f"CPU使用率({metrics['cpu_usage']:.1f}% > {self.thresholds['cpu_usage']}%)")
            if memory_exceeded:
                alert_types.append(f"内存使用率({metrics['memory_usage']:.1f}% > {self.thresholds['memory_usage']}%)")
            if disk_exceeded:
                alert_types.append(f"磁盘使用率({metrics['disk_usage']:.1f}% > {self.thresholds['disk_usage']}%)")
            
            alert_title = "系统资源告警"
            alert_details = f"""
## 🚨 系统资源告警

**告警类型**: {', '.join(alert_types)}
**告警时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**当前系统指标**:
- CPU使用率: {metrics['cpu_usage']:.1f}% (阈值: {self.thresholds['cpu_usage']}%)
- 内存使用率: {metrics['memory_usage']:.1f}% (阈值: {self.thresholds['memory_usage']}%)
- 磁盘使用率: {metrics['disk_usage']:.1f}% (阈值: {self.thresholds['disk_usage']}%)

**系统详细信息**:
- CPU核心数: {psutil.cpu_count()}
- 系统负载: {self._get_load_average()}
- 内存信息: {self._get_memory_info()}
- 磁盘信息: {self._get_disk_info()}

**建议操作**:
1. 检查系统资源使用情况
2. 识别高负载进程
3. 考虑释放不必要的资源
4. 必要时重启相关服务

---
*来自 Mini PM2 系统监控*
            """
            
            result = self.dingtalk_alert.send_system_alert(alert_title, alert_details)
            if result.get("errcode") == 0:
                print(f"✅ 系统告警发送成功: {', '.join(alert_types)}")
            else:
                print(f"❌ 系统告警发送失败: {result.get('errmsg', '未知错误')}")
        except Exception as e:
            print(f"❌ 发送系统告警失败: {e}")
    
    async def _reset_alert_status(self, alert_type: str):
        """重置告警状态（冷却时间后）"""
        await asyncio.sleep(self.alert_cooldown)
        self.alert_status[alert_type] = False
    
    def _get_load_average(self) -> str:
        """获取系统负载"""
        try:
            load_avg = psutil.getloadavg()
            return f"{load_avg[0]:.2f}, {load_avg[1]:.2f}, {load_avg[2]:.2f}"
        except:
            return "无法获取"
    
    def _get_memory_info(self) -> str:
        """获取内存详细信息"""
        try:
            memory = psutil.virtual_memory()
            return f"总内存: {self._format_bytes(memory.total)}, 已使用: {self._format_bytes(memory.used)}, 可用: {self._format_bytes(memory.available)}"
        except:
            return "无法获取"
    
    def _get_disk_info(self) -> str:
        """获取磁盘详细信息"""
        try:
            disk = psutil.disk_usage('/')
            return f"总空间: {self._format_bytes(disk.total)}, 已使用: {self._format_bytes(disk.used)}, 可用: {self._format_bytes(disk.free)}"
        except:
            return "无法获取"
    
    def _format_bytes(self, bytes_value: int) -> str:
        """格式化字节数"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.1f} PB"
    
    def get_current_metrics(self) -> Dict[str, Any]:
        """获取当前系统指标"""
        metrics = self._get_system_metrics()
        return {
            **metrics,
            "thresholds": self.thresholds,
            "alert_status": self.alert_status
        } 