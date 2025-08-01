"""
任务服务
处理任务调度、执行和状态管理
参考 mini_pm2.py 的调度逻辑
"""

import asyncio
import subprocess
import time
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from task_store.base import TaskStore
from .log_manager import get_log_manager


class TaskService:
    """任务服务"""
    
    def __init__(self, task_store: TaskStore, log_buffer: List[str], 
                 task_status: Dict[str, Dict[str, Any]], 
                 websocket_connections: List):
        self.task_store = task_store
        self.log_buffer = log_buffer
        self.task_status = task_status
        self.websocket_connections = websocket_connections
        self.running_tasks: Dict[str, asyncio.Task] = {}
        self.scheduler_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.task_lock = asyncio.Lock()  # 任务状态锁
        self.last_config_check = 0
        self.config_check_interval = 5  # 配置文件检查间隔（秒）
        
    async def start(self):
        """启动任务服务"""
        self.is_running = True
        await self.update_jobs()
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
        get_log_manager().log("任务服务已启动", "INFO")
    
    async def stop(self):
        """停止任务服务"""
        self.is_running = False
        
        # 停止所有运行中的任务
        for task_id, task in self.running_tasks.items():
            task.cancel()
            get_log_manager().log(f"任务 {task_id} 已停止", "INFO", task_id)
        
        # 停止调度器
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        
        get_log_manager().log("任务服务已停止", "INFO")
    
    async def _scheduler_loop(self):
        """调度器主循环"""
        while self.is_running:
            try:
                await self._check_and_run_tasks()
                await self._check_config_changes()
                await asyncio.sleep(30)  # 每30秒检查一次
            except asyncio.CancelledError:
                break
            except Exception as e:
                get_log_manager().log(f"调度器错误: {e}", "ERROR")
                await asyncio.sleep(5)
    
    async def _check_config_changes(self):
        """检查配置文件变化"""
        current_time = time.time()
        if current_time - self.last_config_check >= self.config_check_interval:
            try:
                # 这里可以添加配置文件监控逻辑
                # 如果检测到配置文件变化，重新加载任务
                pass
            except Exception as e:
                get_log_manager().log(f"检查配置文件变化失败: {e}", "ERROR")
            finally:
                self.last_config_check = current_time
    
    async def _check_and_run_tasks(self):
        """检查并运行到期的任务"""
        tasks = await self.task_store.get_all_tasks()
        
        for task in tasks:
            if not task.get('enabled', True):
                continue
            
            task_id = task['id']
            last_run = self.task_status.get(task_id, {}).get('last_run', 0)
            interval_seconds = task['interval_minutes'] * 60
            
            # 检查是否需要运行
            if time.time() - last_run >= interval_seconds:
                await self._run_task(task)
    
    async def _run_task(self, task: Dict[str, Any]):
        """运行单个任务"""
        task_id = task['id']
        script_path = task['script_path']
        interpreter = task.get('execute_path') or sys.executable
        
        # 检查任务是否已在运行
        if task_id in self.running_tasks:
            get_log_manager().log(f"任务 {task_id} 已在运行中", "WARNING", task_id)
            return
        
        # 更新状态为运行中
        self.task_status[task_id] = {
            "status": "RUNNING",
            "start_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "last_run": time.time(),
            "last_error": None,
            "last_success": None
        }
        await self._broadcast_status()
        
        get_log_manager().log_task_start(task_id, script_path, interpreter)
        
        # 创建任务
        task_coro = self._execute_script(task_id, script_path, interpreter)
        self.running_tasks[task_id] = asyncio.create_task(task_coro)
    
    async def _execute_script(self, task_id: str, script_path: str, interpreter: str):
        """执行脚本"""
        script_name = os.path.basename(script_path)
        start_time = datetime.now()
        start_time_str = start_time.strftime('%Y-%m-%d %H:%M:%S')
        
        # 获取任务配置
        task = await self.task_store.get_task(task_id)
        if not task:
            get_log_manager().log(f"未找到任务 {task_id}", "ERROR", task_id)
            return

        try:
            # 设置环境变量
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"

            # 创建进程
            process = await asyncio.create_subprocess_exec(
                interpreter, "-X", "utf8", script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env
            )

            # 读取输出
            output_lines = []
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                
                output_line = line.decode('utf-8').strip()
                output_lines.append(output_line)
                get_log_manager().log(f"输出: {output_line}", "INFO", task_id)

            # 等待进程完成
            return_code = await process.wait()
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            end_time_str = end_time.strftime('%Y-%m-%d %H:%M:%S')
            
            if return_code == 0:
                # 任务成功
                get_log_manager().log_task_success(task_id, script_path, duration)
                async with self.task_lock:
                    current_status = self.task_status.get(task_id, {})
                    run_count = current_status.get("run_count", 0) + 1
                    self.task_status[task_id] = {
                        "status": "SUCCESS", 
                        "last_success": end_time_str,
                        "last_run": time.time(),
                        "duration": f"{duration:.2f}秒",
                        "run_count": run_count,
                        "output": output_lines[-10:] if output_lines else []  # 保留最后10行输出
                    }
            else:
                # 任务失败
                error_msg = f"退出码 {return_code}"
                error_detail = f"任务执行失败 - 脚本: {script_path}, 退出码: {return_code}"
                if output_lines:
                    error_detail += f"\n\n输出内容:\n" + "\n".join(output_lines[-20:])  # 保留最后20行输出
                
                get_log_manager().log_task_failure(task_id, script_path, error_msg, return_code)
                async with self.task_lock:
                    current_status = self.task_status.get(task_id, {})
                    run_count = current_status.get("run_count", 0) + 1
                    self.task_status[task_id] = {
                        "status": "FAILED", 
                        "last_error": error_msg,
                        "last_run": time.time(),
                        "duration": f"{duration:.2f}秒",
                        "run_count": run_count,
                        "output": output_lines[-10:] if output_lines else [],
                        "error_detail": error_detail,
                        "error_timestamp": end_time.strftime('%Y-%m-%d %H:%M:%S')
                    }

        except Exception as e:
            # 任务异常
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            error_detail = f"任务执行异常 - 脚本: {script_path}, 异常: {str(e)}"
            get_log_manager().log_task_exception(task_id, script_path, e)
            async with self.task_lock:
                current_status = self.task_status.get(task_id, {})
                run_count = current_status.get("run_count", 0) + 1
                self.task_status[task_id] = {
                    "status": "EXCEPTION", 
                    "last_error": str(e),
                    "last_run": time.time(),
                    "duration": f"{duration:.2f}秒",
                    "run_count": run_count,
                    "output": [],
                    "error_detail": error_detail,
                    "error_timestamp": end_time.strftime('%Y-%m-%d %H:%M:%S')
                }

        finally:
            # 清理运行中的任务
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]
            
            await self._broadcast_status()
    
    async def run_task_now(self, task_id: str):
        """立即运行任务"""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")
        
        # 允许运行已禁用的任务（手动运行）
        # if not task.get('enabled', True):
        #     raise ValueError(f"任务 {task_id} 已禁用")
        
        await self._run_task(task)
    
    async def restart_task(self, task_id: str):
        """重启任务"""
        # 先停止当前任务
        if task_id in self.running_tasks:
            self.running_tasks[task_id].cancel()
            del self.running_tasks[task_id]
        
        # 重新运行
        await self.run_task_now(task_id)
    
    async def restart_all(self):
        """重启所有任务"""
        # 停止所有运行中的任务
        for task_id, task in self.running_tasks.items():
            task.cancel()
        
        self.running_tasks.clear()
        
        # 重新加载任务
        await self.update_jobs()
    
    async def toggle_task(self, task_id: str, enable: bool):
        """切换任务启用/禁用状态"""
        print(f"toggle_task: task_id={task_id}, enable={enable}")
        
        task = await self.task_store.get_task(task_id)
        print(f"get_task result: {task}")
        
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")
        
        # 更新任务状态
        await self.task_store.update_task(task_id, {"enabled": enable})
        
        # 如果禁用任务，停止当前运行的任务
        if not enable and task_id in self.running_tasks:
            self.running_tasks[task_id].cancel()
            del self.running_tasks[task_id]
        
        # 重新加载任务配置
        await self.update_jobs()
        
        return True
    
    async def add_task(self, task_id: str, script_path: str, interval_minutes: int, execute_path: str = None):
        """添加新任务"""
        # 检查任务是否已存在
        existing_task = await self.task_store.get_task(task_id)
        if existing_task:
            raise ValueError(f"任务ID '{task_id}' 已存在")
        
        # 创建新任务
        task_data = {
            "id": task_id,
            "script_path": script_path,
            "interval_minutes": interval_minutes,
            "enabled": True,
            "execute_path": execute_path or sys.executable
        }
        
        await self.task_store.add_task(task_data)
        await self.update_jobs()
        return True
    
    async def remove_task(self, task_id: str):
        """删除任务"""
        # 停止运行中的任务
        if task_id in self.running_tasks:
            self.running_tasks[task_id].cancel()
            del self.running_tasks[task_id]
        
        # 从存储中删除
        await self.task_store.delete_task(task_id)
        await self.update_jobs()
        return True
        self._log("[INFO] 所有任务已重启")
    
    async def update_jobs(self):
        """更新任务调度"""
        tasks = await self.task_store.get_all_tasks()
        
        # 清空状态
        self.task_status.clear()
        
        # 初始化状态
        for task in tasks:
            task_id = task['id']
            task_enabled = task.get('enabled', True)
            
            if task_enabled:
                self.task_status[task_id] = {
                    "status": "IDLE",
                    "last_success": None,
                    "last_error": None,
                    "last_run": 0,
                    "duration": None,
                    "output": []
                }
            else:
                self.task_status[task_id] = {
                    "status": "DISABLED",
                    "last_success": None,
                    "last_error": None,
                    "last_run": 0,
                    "duration": None,
                    "output": []
                }
        
        await self._broadcast_status()
        get_log_manager().log(f"已加载 {len(tasks)} 个任务", "INFO")
    

    
    async def _broadcast_log(self, message: str):
        """广播日志到所有WebSocket连接"""
        if not self.websocket_connections:
            return
        
        message_data = {
            "type": "log",
            "message": message
        }
        
        # 发送到所有连接
        for websocket in self.websocket_connections[:]:  # 复制列表避免修改
            try:
                await websocket.send_json(message_data)
            except Exception:
                # 移除断开的连接
                if websocket in self.websocket_connections:
                    self.websocket_connections.remove(websocket)
    
    def add_log_to_buffer(self, message: str):
        """添加日志到缓冲区（用于兼容性）"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        
        # 添加到日志缓冲区
        self.log_buffer.append(log_message)
        if len(self.log_buffer) > 500:  # 限制日志数量
            self.log_buffer.pop(0)
        
        # 广播日志
        asyncio.create_task(self._broadcast_log(log_message))
    
    async def _broadcast_status(self):
        """广播状态到所有WebSocket连接"""
        if not self.websocket_connections:
            return
        
        status_data = {
            "type": "status",
            "data": self.task_status
        }
        
        # 发送到所有连接
        for websocket in self.websocket_connections[:]:
            try:
                await websocket.send_json(status_data)
            except Exception:
                if websocket in self.websocket_connections:
                    self.websocket_connections.remove(websocket)