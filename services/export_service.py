"""
数据导出服务
支持导出日志、任务配置、系统状态等
"""

import json
import csv
import os
import zipfile
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path


class ExportService:
    """数据导出服务"""
    
    def __init__(self, export_path: str = "./exports", max_size: int = 10 * 1024 * 1024):
        self.export_path = Path(export_path)
        self.max_size = max_size
        self.export_path.mkdir(exist_ok=True)
    
    def export_logs(self, logs: List[str], format: str = "json") -> str:
        """导出日志"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if format == "json":
            return self._export_logs_json(logs, timestamp)
        elif format == "csv":
            return self._export_logs_csv(logs, timestamp)
        elif format == "txt":
            return self._export_logs_txt(logs, timestamp)
        else:
            raise ValueError(f"不支持的导出格式: {format}")
    
    def _export_logs_json(self, logs: List[str], timestamp: str) -> str:
        """导出日志为JSON格式"""
        filename = f"logs_{timestamp}.json"
        filepath = self.export_path / filename
        
        data = {
            "export_time": datetime.now().isoformat(),
            "total_logs": len(logs),
            "logs": logs
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return str(filepath)
    
    def _export_logs_csv(self, logs: List[str], timestamp: str) -> str:
        """导出日志为CSV格式"""
        filename = f"logs_{timestamp}.csv"
        filepath = self.export_path / filename
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "时间", "日志内容"])
            
            for i, log in enumerate(logs, 1):
                # 解析日志时间戳
                try:
                    if log.startswith('[') and ']' in log:
                        time_part = log[1:log.find(']')]
                        content = log[log.find(']')+1:].strip()
                    else:
                        time_part = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        content = log
                except:
                    time_part = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    content = log
                
                writer.writerow([i, time_part, content])
        
        return str(filepath)
    
    def _export_logs_txt(self, logs: List[str], timestamp: str) -> str:
        """导出日志为TXT格式"""
        filename = f"logs_{timestamp}.txt"
        filepath = self.export_path / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"Mini PM2 日志导出\n")
            f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"日志总数: {len(logs)}\n")
            f.write("=" * 50 + "\n\n")
            
            for log in logs:
                f.write(log + "\n")
        
        return str(filepath)
    
    def export_tasks(self, tasks: List[Dict[str, Any]], format: str = "json") -> str:
        """导出任务配置"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if format == "json":
            return self._export_tasks_json(tasks, timestamp)
        elif format == "csv":
            return self._export_tasks_csv(tasks, timestamp)
        else:
            raise ValueError(f"不支持的导出格式: {format}")
    
    def _export_tasks_json(self, tasks: List[Dict[str, Any]], timestamp: str) -> str:
        """导出任务为JSON格式"""
        filename = f"tasks_{timestamp}.json"
        filepath = self.export_path / filename
        
        data = {
            "export_time": datetime.now().isoformat(),
            "total_tasks": len(tasks),
            "tasks": tasks
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return str(filepath)
    
    def _export_tasks_csv(self, tasks: List[Dict[str, Any]], timestamp: str) -> str:
        """导出任务为CSV格式"""
        filename = f"tasks_{timestamp}.csv"
        filepath = self.export_path / filename
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["任务ID", "脚本路径", "执行间隔(分钟)", "解释器路径", "是否启用"])
            
            for task in tasks:
                writer.writerow([
                    task.get('id', ''),
                    task.get('script_path', ''),
                    task.get('interval_minutes', ''),
                    task.get('execute_path', ''),
                    task.get('enabled', True)
                ])
        
        return str(filepath)
    
    def export_system_status(self, status: Dict[str, Any], format: str = "json") -> str:
        """导出系统状态"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if format == "json":
            return self._export_status_json(status, timestamp)
        else:
            raise ValueError(f"不支持的导出格式: {format}")
    
    def _export_status_json(self, status: Dict[str, Any], timestamp: str) -> str:
        """导出系统状态为JSON格式"""
        filename = f"system_status_{timestamp}.json"
        filepath = self.export_path / filename
        
        data = {
            "export_time": datetime.now().isoformat(),
            "system_status": status
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return str(filepath)
    
    def export_all(self, logs: List[str], tasks: List[Dict[str, Any]], 
                   status: Dict[str, Any]) -> str:
        """导出所有数据"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"mini_pm2_export_{timestamp}.zip"
        filepath = self.export_path / filename
        
        with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 导出日志
            logs_json = self._export_logs_json(logs, timestamp)
            zipf.write(logs_json, "logs.json")
            os.remove(logs_json)
            
            # 导出任务
            tasks_json = self._export_tasks_json(tasks, timestamp)
            zipf.write(tasks_json, "tasks.json")
            os.remove(tasks_json)
            
            # 导出系统状态
            status_json = self._export_status_json(status, timestamp)
            zipf.write(status_json, "system_status.json")
            os.remove(status_json)
            
            # 添加导出说明
            readme_content = f"""Mini PM2 数据导出包

导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
包含文件:
- logs.json: 系统日志
- tasks.json: 任务配置
- system_status.json: 系统状态

此文件包含 Mini PM2 的完整数据备份。
"""
            zipf.writestr("README.txt", readme_content)
        
        return str(filepath)
    
    def cleanup_old_exports(self, max_age_days: int = 30):
        """清理旧的导出文件"""
        cutoff_time = datetime.now().timestamp() - (max_age_days * 24 * 3600)
        
        for file_path in self.export_path.glob("*"):
            if file_path.is_file():
                if file_path.stat().st_mtime < cutoff_time:
                    file_path.unlink()
                    print(f"已删除旧导出文件: {file_path}")
    
    def get_export_size(self) -> int:
        """获取导出目录大小"""
        total_size = 0
        for file_path in self.export_path.rglob("*"):
            if file_path.is_file():
                total_size += file_path.stat().st_size
        return total_size
    
    def export_config(self, config: Dict[str, Any], format: str = "json") -> str:
        """导出系统配置"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if format == "json":
            return self._export_config_json(config, timestamp)
        elif format == "yaml":
            return self._export_config_yaml(config, timestamp)
        else:
            raise ValueError(f"不支持的导出格式: {format}")
    
    def _export_config_json(self, config: Dict[str, Any], timestamp: str) -> str:
        """导出配置为JSON格式"""
        filename = f"config_{timestamp}.json"
        filepath = self.export_path / filename
        
        data = {
            "export_time": datetime.now().isoformat(),
            "config": config
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return str(filepath)
    
    def _export_config_yaml(self, config: Dict[str, Any], timestamp: str) -> str:
        """导出配置为YAML格式"""
        try:
            import yaml
        except ImportError:
            raise ValueError("YAML格式需要安装PyYAML: pip install PyYAML")
        
        filename = f"config_{timestamp}.yaml"
        filepath = self.export_path / filename
        
        data = {
            "export_time": datetime.now().isoformat(),
            "config": config
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        
        return str(filepath)