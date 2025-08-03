import datetime
import json
import pytz
import requests
import time
import hmac
import hashlib
import base64
import urllib.parse
from typing import Optional


class DingTalkAlert:
    """钉钉告警工具类"""
    
    def __init__(self, ding_access_token: str, ding_url: str = "https://oapi.dingtalk.com/robot/send?access_token=", secret: str = ""):
        self.access_token = ding_access_token
        self.secret = secret
        self.base_url = ding_url
        
        # 构建带签名的URL
        if secret:
            self.url = self._build_signed_url()
        else:
            self.url = ding_url + ding_access_token
        
    def send_alert(self, level: str, msg: str, business: str = "Mini PM2"):
        """
        发送钉钉告警消息
        
        :param level: 错误级别 (info, warning, error)
        :param msg: 错误信息
        :param business: 业务模块
        :return: 发送结果
        """
        try:
            headers = {'Content-Type': 'application/json;charset=utf-8'}
            data = self._build_alert_msg(level, msg, business)
            response = requests.post(
                url=self.url,
                headers=headers,
                data=json.dumps(data),
                timeout=10
            )
            return response.json()
        except Exception as e:
            print(f"钉钉告警发送失败: {e}")
            return {"errcode": -1, "errmsg": str(e)}
    
    def send_system_alert(self, alert_type: str, details: str):
        """
        发送系统告警
        
        :param alert_type: 告警类型 (程序异常, 系统中断, 任务失败等)
        :param details: 详细信息
        :return: 发送结果
        """
        now = datetime.datetime.now(tz=pytz.timezone('Asia/Shanghai'))
        formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")
        
        level = "error" if "异常" in alert_type or "失败" in alert_type else "warning"
        
        msg = f"""
## 🚨 系统告警通知

**告警类型**: {alert_type}
**告警时间**: {formatted_time}
**详细信息**: {details}

---
*来自 Mini PM2 系统监控*
        """
        
        return self.send_alert(level, msg, "系统监控")
    
    def _build_signed_url(self) -> str:
        """
        构建带签名的钉钉机器人URL
        
        :return: 带签名的URL
        """
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f'{timestamp}\n{self.secret}'
        
        # 使用HmacSHA256算法计算签名
        hmac_code = hmac.new(
            self.secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        
        # Base64编码
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        
        # 构建完整URL
        url = f"{self.base_url}{self.access_token}&timestamp={timestamp}&sign={sign}"
        return url
    
    def _build_alert_msg(self, level: str, msg: str, business: str):
        """
        构建钉钉消息格式
        
        :param level: 错误级别
        :param msg: 错误信息
        :param business: 业务模块
        :return: 消息字典
        """
        title_msg = self._get_title_msg(level)
        
        common_msg = {
            'msgtype': 'markdown',
            'at': {
                'isAtAll': False,
                'atUserIds': [],
                'atMobiles': []
            },
            'markdown': {
                'title': f"{business} - {level.upper()}",
                'text': title_msg + '\n' + '## 📍 业务模块: ' + business + '\n' + '## 📝 详细信息: \n' + msg
            }
        }
        return common_msg
    
    def _get_title_msg(self, msg_level: str) -> str:
        """
        根据级别生成标题消息
        
        :param msg_level: 消息级别
        :return: 格式化的标题消息
        """
        if msg_level == 'info':
            return '<font color="green"><h2>✅ INFO</h2></font>'
        elif msg_level == 'error':
            return '<font color="red"><h2>❌ ERROR</h2></font>'
        elif msg_level == 'warning':
            return '<font color="orange"><h2>⚠️ WARNING</h2></font>'
        else:
            return '<font color="blue"><h2>ℹ️ INFO</h2></font>'
    
    def test_connection(self) -> bool:
        """
        测试钉钉连接
        
        :return: 连接是否成功
        """
        try:
            result = self.send_alert("info", "这是一条测试消息，用于验证钉钉告警配置是否正确。", "系统测试")
            return result.get("errcode") == 0
        except Exception:
            return False 