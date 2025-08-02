"""
系统监控服务
监控CPU、内存、磁盘等系统资源
"""

import asyncio
import psutil
import time
from datetime import datetime
from typing import Dict, Any, List
from dataclasses import dataclass


@dataclass
class SystemMetrics:
    """系统指标数据类"""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used: int
    memory_total: int
    disk_percent: float
    disk_used: int
    disk_total: int
    network_sent: int
    network_recv: int
    load_average: List[float]
    # 新增磁盘IO指标
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    disk_read_count: int = 0
    disk_write_count: int = 0


class MonitoringService:
    """系统监控服务"""
    
    def __init__(self, enabled: bool = True, interval: int = 60):
        self.enabled = enabled
        self.interval = interval
        self.metrics_history: List[SystemMetrics] = []
        self.max_history_size = 1000
        self.is_running = False
        self.monitoring_task: asyncio.Task = None
    
    async def start(self):
        """启动监控服务"""
        if not self.enabled:
            print("⚠️ 监控服务已禁用")
            return
        
        self.is_running = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        print("✅ 系统监控服务已启动")
        print(f"📊 监控间隔: {self.interval}秒")
        print(f"📈 最大历史记录: {self.max_history_size}条")
    
    async def stop(self):
        """停止监控服务"""
        self.is_running = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        print("🛑 系统监控服务已停止")
    
    async def _monitoring_loop(self):
        """监控主循环"""
        print("🔄 监控循环已启动")
        while self.is_running:
            try:
                metrics = self._collect_metrics()
                self._store_metrics(metrics)
                print(f"📊 已收集监控数据: CPU={metrics.cpu_percent:.1f}%, 内存={metrics.memory_percent:.1f}%")
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                print("🛑 监控循环已取消")
                break
            except Exception as e:
                print(f"❌ 监控服务错误: {e}")
                await asyncio.sleep(5)
    
    def _collect_metrics(self) -> SystemMetrics:
        """收集系统指标"""
        timestamp = time.time()
        
        # CPU 使用率
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # 内存使用情况
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        memory_used = memory.used
        memory_total = memory.total
        
        # 磁盘使用情况（根据操作系统选择根目录）
        import platform
        if platform.system() == "Windows":
            disk = psutil.disk_usage('C:\\')
        else:
            disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        disk_used = disk.used
        disk_total = disk.total
        
        # 网络使用情况
        network = psutil.net_io_counters()
        network_sent = network.bytes_sent
        network_recv = network.bytes_recv
        
        # 磁盘IO使用情况
        disk_io = self._get_disk_io_counters()
        
        # 系统负载（Linux和macOS支持）
        try:
            if platform.system() in ["Linux", "Darwin"]:
                load_average = list(psutil.getloadavg())
            else:
                # Windows不支持load average，使用CPU使用率代替
                load_average = [cpu_percent]
        except:
            load_average = [0.0]
        
        return SystemMetrics(
            timestamp=timestamp,
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            memory_used=memory_used,
            memory_total=memory_total,
            disk_percent=disk_percent,
            disk_used=disk_used,
            disk_total=disk_total,
            network_sent=network_sent,
            network_recv=network_recv,
            load_average=load_average,
            disk_read_bytes=disk_io.get('read_bytes', 0),
            disk_write_bytes=disk_io.get('write_bytes', 0),
            disk_read_count=disk_io.get('read_count', 0),
            disk_write_count=disk_io.get('write_count', 0)
        )
    
    def _store_metrics(self, metrics: SystemMetrics):
        """存储指标数据"""
        self.metrics_history.append(metrics)
        
        # 限制历史数据大小
        if len(self.metrics_history) > self.max_history_size:
            self.metrics_history.pop(0)
    
    def get_current_metrics(self) -> Dict[str, Any]:
        """获取当前系统指标"""
        if not self.metrics_history:
            return {}
        
        latest = self.metrics_history[-1]
        
        # 格式化磁盘IO数据
        disk_io_formatted = self._format_disk_io(latest.disk_read_bytes, latest.disk_write_bytes)
        
        return {
            "timestamp": latest.timestamp,
            "cpu_percent": latest.cpu_percent,
            "memory_percent": latest.memory_percent,
            "memory_used": latest.memory_used,
            "memory_total": latest.memory_total,
            "memory_used_gb": round(latest.memory_used / (1024**3), 2),
            "memory_total_gb": round(latest.memory_total / (1024**3), 2),
            "disk_percent": latest.disk_percent,
            "disk_used": latest.disk_used,
            "disk_total": latest.disk_total,
            "disk_used_gb": round(latest.disk_used / (1024**3), 2),
            "disk_total_gb": round(latest.disk_total / (1024**3), 2),
            "network_sent_mb": round(latest.network_sent / (1024**2), 2),
            "network_recv_mb": round(latest.network_recv / (1024**2), 2),
            "load_average": latest.load_average,
            # 新增磁盘IO数据
            "disk_io": {
                "read_bytes": latest.disk_read_bytes,
                "write_bytes": latest.disk_write_bytes,
                "read_count": latest.disk_read_count,
                "write_count": latest.disk_write_count,
                "read_formatted": disk_io_formatted['read'],
                "write_formatted": disk_io_formatted['write']
            }
        }
    
    def get_metrics_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取历史指标数据"""
        history = []
        for metrics in self.metrics_history[-limit:]:
            history.append({
                "timestamp": metrics.timestamp,
                "cpu_percent": metrics.cpu_percent,
                "memory_percent": metrics.memory_percent,
                "disk_percent": metrics.disk_percent,
                "load_average": metrics.load_average
            })
        return history
    
    def get_system_info(self) -> Dict[str, Any]:
        """获取系统信息"""
        try:
            import platform
            import subprocess
            
            system_info = {
                "platform": psutil.sys.platform,
                "python_version": psutil.sys.version,
                "cpu_count": psutil.cpu_count(),
                "boot_time": psutil.boot_time(),
                "uptime": time.time() - psutil.boot_time()
            }
            
            # 根据操作系统获取不同的系统信息
            if psutil.sys.platform == "darwin":  # macOS
                system_info.update(self._get_macos_info())
            elif psutil.sys.platform.startswith("win"):  # Windows
                system_info.update(self._get_windows_info())
            elif psutil.sys.platform.startswith("linux"):  # Linux
                system_info.update(self._get_linux_info())
            else:
                # 其他系统使用通用方法
                system_info.update(self._get_generic_info())
            
            return system_info
        except Exception as e:
            print(f"获取系统信息失败: {e}")
            return {
                "platform": "unknown",
                "python_version": "unknown",
                "cpu_count": 0,
                "cpu_freq": None,
                "boot_time": 0,
                "uptime": 0
            }
    
    def get_io_metrics(self) -> Dict[str, Any]:
        """获取实时I/O指标"""
        try:
            import platform
            
            # 磁盘I/O
            disk_io = {}
            try:
                disk_counters = psutil.disk_io_counters()
                if disk_counters:
                    disk_io = {
                        "read_bytes": disk_counters.read_bytes,
                        "write_bytes": disk_counters.write_bytes,
                        "read_count": disk_counters.read_count,
                        "write_count": disk_counters.write_count
                    }
            except:
                pass
            
            # 网络I/O
            network_io = {}
            try:
                net_counters = psutil.net_io_counters()
                if net_counters:
                    network_io = {
                        "bytes_sent": net_counters.bytes_sent,
                        "bytes_recv": net_counters.bytes_recv,
                        "packets_sent": net_counters.packets_sent,
                        "packets_recv": net_counters.packets_recv
                    }
            except:
                pass
            
            # 根据操作系统获取额外的I/O信息
            os_specific_io = {}
            if platform.system() == "Darwin":  # macOS
                os_specific_io = self._get_macos_io_info()
            elif platform.system() == "Windows":
                os_specific_io = self._get_windows_io_info()
            elif platform.system() == "Linux":
                os_specific_io = self._get_linux_io_info()
            
            return {
                "disk_io": disk_io,
                "network_io": network_io,
                "os_specific": os_specific_io
            }
        except Exception as e:
            print(f"获取I/O指标失败: {e}")
            return {
                "disk_io": {},
                "network_io": {},
                "os_specific": {}
            }
    
    def _get_macos_io_info(self) -> Dict[str, Any]:
        """获取macOS特定的I/O信息"""
        import subprocess
        info = {}
        
        try:
            # 获取磁盘I/O统计
            result = subprocess.run(['iostat', '-d', '1', '1'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 2:
                    # 解析iostat输出
                    parts = lines[-1].split()
                    if len(parts) >= 6:
                        info["disk_read_kb"] = float(parts[1])
                        info["disk_write_kb"] = float(parts[2])
        except:
            pass
        
        return info
    
    def _get_windows_io_info(self) -> Dict[str, Any]:
        """获取Windows特定的I/O信息"""
        import subprocess
        info = {}
        
        try:
            # 使用wmic获取磁盘I/O信息
            result = subprocess.run(['wmic', 'path', 'Win32_PerfFormattedData_PerfDisk_PhysicalDisk', 
                                   'get', 'DiskReadBytesPerSec,DiskWriteBytesPerSec', '/format:csv'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    # 解析wmic输出
                    parts = lines[1].split(',')
                    if len(parts) >= 3:
                        info["disk_read_bytes_per_sec"] = int(parts[1])
                        info["disk_write_bytes_per_sec"] = int(parts[2])
        except:
            pass
        
        return info
    
    def _get_linux_io_info(self) -> Dict[str, Any]:
        """获取Linux特定的I/O信息"""
        import subprocess
        info = {}
        
        try:
            # 读取/proc/diskstats获取磁盘I/O信息
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 14 and parts[2] == 'sda':  # 主磁盘
                        info["disk_read_sectors"] = int(parts[5])
                        info["disk_write_sectors"] = int(parts[9])
                        break
        except:
            pass
        
        try:
            # 读取/proc/net/dev获取网络I/O信息
            with open('/proc/net/dev', 'r') as f:
                for line in f:
                    if 'eth0' in line or 'en0' in line:  # 主网络接口
                        parts = line.strip().split()
                        if len(parts) >= 16:
                            info["net_rx_bytes"] = int(parts[1])
                            info["net_tx_bytes"] = int(parts[9])
                            break
        except:
            pass
        
        return info
    
    def _get_macos_info(self) -> Dict[str, Any]:
        """获取macOS系统信息"""
        import subprocess
        info = {}
        
        try:
            # CPU频率信息
            try:
                freq = psutil.cpu_freq()
                if freq:
                    info["cpu_freq"] = {
                        "current": freq.current,
                        "min": freq.min,
                        "max": freq.max
                    }
            except (FileNotFoundError, OSError, AttributeError):
                pass
            
            # 系统版本信息
            try:
                result = subprocess.run(['sw_vers', '-productName'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    info["system_name"] = result.stdout.strip()
                else:
                    info["system_name"] = "macOS"
            except:
                info["system_name"] = "macOS"
            
            try:
                result = subprocess.run(['sw_vers', '-productVersion'], 
                                      capture_output=True, text=True, timeout=5, shell=False)
                if result.returncode == 0:
                    info["system_version"] = result.stdout.strip()
                else:
                    info["system_version"] = "Unknown"
            except Exception as e:
                print(f"获取macOS版本信息失败: {e}")
                info["system_version"] = "Unknown"
                
        except Exception as e:
            print(f"获取macOS信息失败: {e}")
        
        return info
    
    def _get_windows_info(self) -> Dict[str, Any]:
        """获取Windows系统信息"""
        import subprocess
        info = {}
        
        try:
            # CPU频率信息
            try:
                freq = psutil.cpu_freq()
                if freq:
                    info["cpu_freq"] = {
                        "current": freq.current,
                        "min": freq.min,
                        "max": freq.max
                    }
            except (FileNotFoundError, OSError, AttributeError):
                pass
            
            # 系统版本信息
            try:
                result = subprocess.run(['wmic', 'os', 'get', 'Caption,Version', '/value'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    output = result.stdout
                    for line in output.split('\n'):
                        if 'Caption=' in line:
                            info["system_name"] = line.split('=')[1].strip()
                        elif 'Version=' in line:
                            info["system_version"] = line.split('=')[1].strip()
                else:
                    info["system_name"] = "Windows"
                    info["system_version"] = "Unknown"
            except:
                info["system_name"] = "Windows"
                info["system_version"] = "Unknown"
                
        except Exception as e:
            print(f"获取Windows信息失败: {e}")
        
        return info
    
    def _get_linux_info(self) -> Dict[str, Any]:
        """获取Linux系统信息"""
        import subprocess
        info = {}
        
        try:
            # CPU频率信息
            try:
                freq = psutil.cpu_freq()
                if freq:
                    info["cpu_freq"] = {
                        "current": freq.current,
                        "min": freq.min,
                        "max": freq.max
                    }
            except (FileNotFoundError, OSError, AttributeError):
                pass
            
            # 系统版本信息
            try:
                # 尝试读取 /etc/os-release
                with open('/etc/os-release', 'r') as f:
                    os_release = f.read()
                    for line in os_release.split('\n'):
                        if line.startswith('PRETTY_NAME='):
                            info["system_name"] = line.split('=')[1].strip('"')
                            break
                    else:
                        info["system_name"] = "Linux"
            except:
                try:
                    # 尝试使用 lsb_release
                    result = subprocess.run(['lsb_release', '-d'], 
                                          capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        info["system_name"] = result.stdout.split(':')[1].strip()
                    else:
                        info["system_name"] = "Linux"
                except:
                    info["system_name"] = "Linux"
            
            try:
                result = subprocess.run(['uname', '-r'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    info["system_version"] = result.stdout.strip()
                else:
                    info["system_version"] = "Unknown"
            except:
                info["system_version"] = "Unknown"
                
        except Exception as e:
            print(f"获取Linux信息失败: {e}")
        
        return info
    
    def _get_disk_io_counters(self) -> Dict[str, int]:
        """获取磁盘IO计数器，支持不同操作系统"""
        import platform
        
        try:
            # 获取磁盘IO计数器
            disk_counters = psutil.disk_io_counters()
            if disk_counters:
                return {
                    'read_bytes': disk_counters.read_bytes,
                    'write_bytes': disk_counters.write_bytes,
                    'read_count': disk_counters.read_count,
                    'write_count': disk_counters.write_count
                }
        except Exception as e:
            print(f"获取磁盘IO计数器失败: {e}")
        
        # 如果psutil失败，尝试操作系统特定的方法
        system = platform.system()
        if system == "Darwin":  # macOS
            return self._get_macos_disk_io()
        elif system == "Windows":
            return self._get_windows_disk_io()
        elif system == "Linux":
            return self._get_linux_disk_io()
        
        return {'read_bytes': 0, 'write_bytes': 0, 'read_count': 0, 'write_count': 0}
    
    def _get_macos_disk_io(self) -> Dict[str, int]:
        """获取macOS磁盘IO数据"""
        import subprocess
        
        try:
            # 使用iostat命令获取磁盘IO统计
            result = subprocess.run(['iostat', '-d', '1', '1'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 2:
                    # 解析iostat输出，获取最后一行数据
                    parts = lines[-1].split()
                    if len(parts) >= 6:
                        # iostat输出格式: device KB_read KB_wrtn KB_read/s KB_wrtn/s
                        kb_read = float(parts[1]) if parts[1] != 'KB_read' else 0
                        kb_written = float(parts[2]) if parts[2] != 'KB_wrtn' else 0
                        return {
                            'read_bytes': int(kb_read * 1024),
                            'write_bytes': int(kb_written * 1024),
                            'read_count': 0,  # iostat不提供IO次数
                            'write_count': 0
                        }
        except Exception as e:
            print(f"获取macOS磁盘IO失败: {e}")
        
        return {'read_bytes': 0, 'write_bytes': 0, 'read_count': 0, 'write_count': 0}
    
    def _get_windows_disk_io(self) -> Dict[str, int]:
        """获取Windows磁盘IO数据"""
        import subprocess
        
        try:
            # 使用wmic获取磁盘IO信息
            result = subprocess.run(['wmic', 'path', 'Win32_PerfFormattedData_PerfDisk_PhysicalDisk', 
                                   'get', 'DiskReadBytesPerSec,DiskWriteBytesPerSec,DiskReadsPerSec,DiskWritesPerSec', '/format:csv'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    # 解析wmic输出
                    parts = lines[1].split(',')
                    if len(parts) >= 5:
                        return {
                            'read_bytes': int(parts[1]) if parts[1].isdigit() else 0,
                            'write_bytes': int(parts[2]) if parts[2].isdigit() else 0,
                            'read_count': int(parts[3]) if parts[3].isdigit() else 0,
                            'write_count': int(parts[4]) if parts[4].isdigit() else 0
                        }
        except Exception as e:
            print(f"获取Windows磁盘IO失败: {e}")
        
        return {'read_bytes': 0, 'write_bytes': 0, 'read_count': 0, 'write_count': 0}
    
    def _get_linux_disk_io(self) -> Dict[str, int]:
        """获取Linux磁盘IO数据"""
        try:
            # 读取/proc/diskstats获取磁盘IO信息
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 14:
                        # 查找主磁盘（通常是sda或nvme0n1）
                        device_name = parts[2]
                        if device_name in ['sda', 'nvme0n1', 'hda'] or device_name.startswith('sd') or device_name.startswith('nvme'):
                            # /proc/diskstats格式: major minor device reads reads_merged reads_sectors reads_ms writes writes_merged writes_sectors writes_ms
                            reads = int(parts[3])
                            writes = int(parts[7])
                            read_sectors = int(parts[5])
                            write_sectors = int(parts[9])
                            
                            return {
                                'read_bytes': read_sectors * 512,  # 扇区大小通常是512字节
                                'write_bytes': write_sectors * 512,
                                'read_count': reads,
                                'write_count': writes
                            }
        except Exception as e:
            print(f"获取Linux磁盘IO失败: {e}")
        
        return {'read_bytes': 0, 'write_bytes': 0, 'read_count': 0, 'write_count': 0}
    
    def _format_disk_io(self, read_bytes: int, write_bytes: int) -> Dict[str, str]:
        """格式化磁盘IO数据，转换为人类可读的格式"""
        def format_bytes(bytes_value: int) -> str:
            if bytes_value == 0:
                return "0 B"
            elif bytes_value < 1024:
                return f"{bytes_value} B"
            elif bytes_value < 1024**2:
                return f"{bytes_value / 1024:.1f} KB"
            elif bytes_value < 1024**3:
                return f"{bytes_value / (1024**2):.1f} MB"
            elif bytes_value < 1024**4:
                return f"{bytes_value / (1024**3):.1f} GB"
            else:
                return f"{bytes_value / (1024**4):.1f} TB"
        
        return {
            'read': format_bytes(read_bytes),
            'write': format_bytes(write_bytes)
        }
    
    def _get_generic_info(self) -> Dict[str, Any]:
        """获取通用系统信息"""
        info = {}
        
        try:
            # CPU频率信息
            try:
                freq = psutil.cpu_freq()
                if freq:
                    info["cpu_freq"] = {
                        "current": freq.current,
                        "min": freq.min,
                        "max": freq.max
                    }
            except (FileNotFoundError, OSError, AttributeError):
                pass
            
            # 使用platform模块获取基本信息
            info["system_name"] = platform.system()
            info["system_version"] = platform.release()
            
        except Exception as e:
            print(f"获取通用系统信息失败: {e}")
        
        return info