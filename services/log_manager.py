 #!/usr/bin/env python3
"""
日志管理模块
提供统一的日志记录、存储和查询功能
"""

import os
import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path


class LogManager:
    """日志管理器"""
    
    def __init__(self, log_dir: str = "./logs", max_log_size: int = 10 * 1024 * 1024,  # 10MB
                 max_log_files: int = 5, log_level: str = "INFO", broadcast_callback=None, log_buffer=None):
        self.log_dir = Path(log_dir)
        self.max_log_size = max_log_size
        self.max_log_files = max_log_files
        self.log_level = getattr(logging, log_level.upper())
        self.broadcast_callback = broadcast_callback  # WebSocket广播回调
        self.log_buffer = log_buffer  # 日志缓冲区
        
        # 确保日志目录存在
        self.log_dir.mkdir(exist_ok=True)
        
        # 初始化日志记录器
        self._setup_loggers()
        
        # 任务异常日志存储
        self.task_exceptions: Dict[str, List[Dict[str, Any]]] = {}
        
    def _setup_loggers(self):
        """设置日志记录器"""
        # 主日志记录器
        self.main_logger = logging.getLogger('mini_pm2')
        self.main_logger.setLevel(self.log_level)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        self.main_logger.addHandler(console_handler)
        
        # 文件处理器
        main_log_file = self.log_dir / "main.log"
        file_handler = logging.FileHandler(main_log_file, encoding='utf-8')
        file_handler.setLevel(self.log_level)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        self.main_logger.addHandler(file_handler)
        
        # 任务日志记录器
        self.task_logger = logging.getLogger('mini_pm2.tasks')
        self.task_logger.setLevel(self.log_level)
        
        task_log_file = self.log_dir / "tasks.log"
        task_file_handler = logging.FileHandler(task_log_file, encoding='utf-8')
        task_file_handler.setLevel(self.log_level)
        task_file_handler.setFormatter(file_formatter)
        self.task_logger.addHandler(task_file_handler)
        
        # 异常日志记录器
        self.exception_logger = logging.getLogger('mini_pm2.exceptions')
        self.exception_logger.setLevel(logging.ERROR)
        
        exception_log_file = self.log_dir / "exceptions.log"
        exception_file_handler = logging.FileHandler(exception_log_file, encoding='utf-8')
        exception_file_handler.setLevel(logging.ERROR)
        exception_file_handler.setFormatter(file_formatter)
        self.exception_logger.addHandler(exception_file_handler)
    
    def log(self, message: str, level: str = "INFO", task_id: Optional[str] = None):
        """记录日志"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_level = getattr(logging, level.upper())
        
        # 格式化消息
        if task_id:
            formatted_message = f"[{task_id}] {message}"
        else:
            formatted_message = message
        
        # 记录到主日志
        self.main_logger.log(log_level, formatted_message)
        
        # 如果是任务相关日志，也记录到任务日志
        if task_id:
            self.task_logger.log(log_level, formatted_message)
        
        # 添加到日志缓冲区
        if self.log_buffer is not None:
            log_entry = f"{timestamp} - [{level.upper()}] {formatted_message}"
            self.log_buffer.append(log_entry)
            # 限制缓冲区大小
            if len(self.log_buffer) > 1000:
                self.log_buffer.pop(0)
        
        # 通过WebSocket广播日志
        if self.broadcast_callback:
            try:
                import asyncio
                import inspect
                # 格式化WebSocket广播消息，包含日志级别
                ws_message = f"{timestamp} - [{level.upper()}] {formatted_message}"
                # 判断 broadcast_callback 是否为协程函数
                if inspect.iscoroutinefunction(self.broadcast_callback):
                    asyncio.create_task(self.broadcast_callback(ws_message))
                else:
                    self.broadcast_callback(ws_message)
            except Exception as e:
                # 如果广播失败，不影响正常日志记录
                print(f"WebSocket广播失败: {e}")
    
    def log_task_start(self, task_id: str, script_path: str, interpreter: str):
        """记录任务启动"""
        message = f"任务启动 - 脚本: {script_path}, 解释器: {interpreter}"
        self.log(message, "INFO", task_id)
    
    def log_task_success(self, task_id: str, script_path: str, duration: float):
        """记录任务成功"""
        message = f"任务成功完成 - 脚本: {script_path}, 耗时: {duration:.2f}秒"
        self.log(message, "INFO", task_id)
    
    def log_task_failure(self, task_id: str, script_path: str, error: str, return_code: Optional[int] = None):
        """记录任务失败"""
        if return_code is not None:
            message = f"任务执行失败 - 脚本: {script_path}, 退出码: {return_code}, 错误: {error}"
        else:
            message = f"任务执行异常 - 脚本: {script_path}, 错误: {error}"
        
        self.log(message, "ERROR", task_id)
        
        # 记录异常详情
        self._log_exception(task_id, script_path, error, return_code)
    
    def log_task_exception(self, task_id: str, script_path: str, exception: Exception):
        """记录任务异常"""
        error_msg = f"任务执行异常 - 脚本: {script_path}, 异常: {str(exception)}"
        self.log(error_msg, "ERROR", task_id)
        
        # 记录异常详情
        self._log_exception(task_id, script_path, str(exception), None, exception)
    
    def _log_exception(self, task_id: str, script_path: str, error: str, 
                      return_code: Optional[int] = None, exception: Optional[Exception] = None):
        """记录异常详情"""
        exception_data = {
            "task_id": task_id,
            "script_path": script_path,
            "error": error,
            "return_code": return_code,
            "timestamp": datetime.now().isoformat(),
            "exception_type": type(exception).__name__ if exception else None,
            "exception_traceback": self._get_traceback(exception) if exception else None
        }
        
        # 存储到内存
        if task_id not in self.task_exceptions:
            self.task_exceptions[task_id] = []
        self.task_exceptions[task_id].append(exception_data)
        
        # 限制每个任务的异常记录数量
        if len(self.task_exceptions[task_id]) > 10:
            self.task_exceptions[task_id] = self.task_exceptions[task_id][-10:]
        
        # 记录到异常日志文件
        self.exception_logger.error(
            f"Task Exception - {task_id}: {error}",
            extra={"exception_data": exception_data}
        )
    
    def _get_traceback(self, exception: Exception) -> Optional[str]:
        """获取异常堆栈信息"""
        import traceback
        if exception:
            return ''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        return None
    
    def get_task_exceptions(self, task_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取任务的异常记录"""
        if task_id not in self.task_exceptions:
            return []
        
        exceptions = self.task_exceptions[task_id]
        return exceptions[-limit:] if limit > 0 else exceptions
    
    def get_all_exceptions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取所有异常记录"""
        all_exceptions = []
        for task_id, exceptions in self.task_exceptions.items():
            all_exceptions.extend(exceptions)
        
        # 按时间排序
        all_exceptions.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return all_exceptions[:limit] if limit > 0 else all_exceptions
    
    def clear_task_exceptions(self, task_id: str):
        """清空任务的异常记录"""
        if task_id in self.task_exceptions:
            del self.task_exceptions[task_id]
    
    def clear_all_exceptions(self):
        """清空所有异常记录"""
        self.task_exceptions.clear()
    
    def get_log_files(self) -> List[str]:
        """获取日志文件列表"""
        log_files = []
        for log_file in self.log_dir.glob("*.log"):
            log_files.append(str(log_file))
        return log_files
    
    def get_log_content(self, log_file: str, lines: int = 100) -> List[str]:
        """获取日志文件内容"""
        log_path = self.log_dir / log_file
        if not log_path.exists():
            return []
        
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                content = f.readlines()
                return content[-lines:] if lines > 0 else content
        except Exception as e:
            self.log(f"读取日志文件失败: {e}", "ERROR")
            return []
    
    def rotate_logs(self):
        """轮转日志文件"""
        for log_file in self.log_dir.glob("*.log"):
            if log_file.stat().st_size > self.max_log_size:
                # 创建备份文件
                backup_file = log_file.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
                log_file.rename(backup_file)
                
                # 重新创建日志文件
                log_file.touch()
                
                self.log(f"日志文件已轮转: {log_file.name}", "INFO")


# 全局日志管理器实例 - 延迟初始化
log_manager = None

def get_log_manager(broadcast_callback=None, log_buffer=None):
    """获取日志管理器实例"""
    global log_manager
    # 如果传入了新的参数，重新创建实例
    if log_manager is None or (broadcast_callback is not None or log_buffer is not None):
        log_manager = LogManager(broadcast_callback=broadcast_callback, log_buffer=log_buffer)
    return log_manager