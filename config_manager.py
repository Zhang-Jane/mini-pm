 #!/usr/bin/env python3
"""
配置管理器
负责配置的持久化保存和加载
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_file: str = ".env"):
        self.config_file = config_file
        self.config_cache = {}
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            self.config_cache[key.strip()] = value.strip()
            except Exception as e:
                print(f"加载配置文件失败: {e}")
    
    def _save_config(self):
        """保存配置文件"""
        try:
            # 读取现有配置
            existing_lines = []
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    existing_lines = f.readlines()
            
            # 更新配置
            updated_lines = []
            updated_keys = set()
            
            for line in existing_lines:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key = line.split('=', 1)[0].strip()
                    if key in self.config_cache:
                        updated_lines.append(f"{key}={self.config_cache[key]}\n")
                        updated_keys.add(key)
                    else:
                        updated_lines.append(line + '\n')
                else:
                    updated_lines.append(line + '\n')
            
            # 添加新的配置项
            for key, value in self.config_cache.items():
                if key not in updated_keys:
                    updated_lines.append(f"{key}={value}\n")
            
            # 写入文件
            with open(self.config_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)
            
            return True
        except Exception as e:
            print(f"保存配置文件失败: {e}")
            return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self.config_cache.get(key, default)
    
    def set(self, key: str, value: Any) -> bool:
        """设置配置值"""
        self.config_cache[key] = str(value)
        return self._save_config()
    
    def update(self, config_dict: Dict[str, Any]) -> bool:
        """批量更新配置"""
        for key, value in config_dict.items():
            self.config_cache[key] = str(value)
        return self._save_config()
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self.config_cache.copy()
    
    def reload(self):
        """重新加载配置"""
        self.config_cache.clear()
        self._load_config()

# 全局配置管理器实例
config_manager = ConfigManager()

def get_config(key: str, default: Any = None) -> Any:
    """获取配置值"""
    return config_manager.get(key, default)

def set_config(key: str, value: Any) -> bool:
    """设置配置值"""
    return config_manager.set(key, value)

def update_config(config_dict: Dict[str, Any]) -> bool:
    """批量更新配置"""
    return config_manager.update(config_dict)

def reload_config():
    """重新加载配置"""
    config_manager.reload()

def get_all_config() -> Dict[str, Any]:
    """获取所有配置"""
    return config_manager.get_all()