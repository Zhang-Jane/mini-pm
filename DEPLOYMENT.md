# Mini PM2 部署指南

## 🚀 快速开始

### 1. 本地开发环境

```bash
# 克隆项目
git clone <repository-url>
cd task_web

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp env.example .env
# 编辑 .env 文件

# 启动应用
python main.py
```

### 2. Docker 部署

```bash
# 使用 Docker Compose
docker-compose up -d

# 或使用 Docker
docker build -t mini-pm2 .
docker run -p 8100:8100 mini-pm2
```

## 📋 环境配置

### 环境变量说明

| 变量名                | 默认值                   | 说明                   |
| --------------------- | ------------------------ | ---------------------- |
| `STORAGE_TYPE`        | `json`                   | 存储类型 (json/redis)  |
| `REDIS_URL`           | `redis://localhost:6379` | Redis 连接地址         |
| `REDIS_DB`            | `0`                      | Redis 数据库编号       |
| `TASKS_FILE`          | `tasks.json`             | JSON 任务文件路径      |
| `LOG_LIMIT`           | `500`                    | 日志缓存行数           |
| `CHECK_INTERVAL`      | `30`                     | 任务检查间隔(秒)       |
| `ENABLE_MONITORING`   | `true`                   | 是否启用系统监控       |
| `MONITORING_INTERVAL` | `60`                     | 监控数据收集间隔(秒)   |
| `EXPORT_PATH`         | `./exports`              | 导出文件路径           |
| `MAX_EXPORT_SIZE`     | `10485760`               | 最大导出文件大小(字节) |

### 生产环境配置

```env
# 生产环境配置示例
STORAGE_TYPE=redis
REDIS_URL=redis://redis-server:6379
REDIS_DB=0
LOG_LIMIT=1000
CHECK_INTERVAL=30
ENABLE_MONITORING=true
MONITORING_INTERVAL=60
EXPORT_PATH=/var/mini-pm2/exports
MAX_EXPORT_SIZE=52428800
DEBUG=false
```

## 🐳 Docker 部署

### 使用 Docker Compose

1. 创建 `docker-compose.yml` 文件
2. 配置环境变量
3. 启动服务

```bash
# 启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f mini-pm2

# 停止服务
docker-compose down
```

### 使用 Docker

```bash
# 构建镜像
docker build -t mini-pm2 .

# 运行容器
docker run -d \
  --name mini-pm2 \
  -p 8100:8100 \
  -v /path/to/logs:/app/logs \
  -v /path/to/exports:/app/exports \
  -e STORAGE_TYPE=redis \
  -e REDIS_URL=redis://redis-server:6379 \
  mini-pm2
```

## 🔧 生产环境部署

### 使用 Nginx 反向代理

1. 安装 Nginx
2. 配置 `nginx.conf`
3. 启动 Nginx

```bash
# 启动 Nginx
sudo systemctl start nginx
sudo systemctl enable nginx
```

### 使用 Supervisor 管理进程

1. 安装 Supervisor
2. 配置 `supervisor.conf`
3. 启动服务

```bash
# 复制配置文件
sudo cp supervisor.conf /etc/supervisor/conf.d/mini-pm2.conf

# 重新加载配置
sudo supervisorctl reread
sudo supervisorctl update

# 启动服务
sudo supervisorctl start mini-pm2
```

### 使用 systemd 管理服务

1. 配置 `systemd.service`
2. 启用服务

```bash
# 复制服务文件
sudo cp systemd.service /etc/systemd/system/mini-pm2.service

# 重新加载 systemd
sudo systemctl daemon-reload

# 启用并启动服务
sudo systemctl enable mini-pm2
sudo systemctl start mini-pm2

# 查看状态
sudo systemctl status mini-pm2
```

## 📊 监控和日志

### 日志管理

```bash
# 查看应用日志
tail -f logs/mini-pm2.log

# 查看 Nginx 日志
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log

# 查看 Supervisor 日志
sudo supervisorctl tail mini-pm2
```

### 系统监控

- 访问 `/api/monitoring/metrics` 获取系统资源信息
- 访问 `/api/system/info` 获取系统状态
- 使用 WebSocket 连接获取实时数据

### 数据备份

```bash
# 导出所有数据
curl -X POST http://localhost:8100/api/export/all

# 导出日志
curl -X POST http://localhost:8100/api/export/logs

# 导出任务配置
curl -X POST http://localhost:8100/api/export/tasks
```

## 🔒 安全配置

### 防火墙设置

```bash
# 只允许必要端口
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8100/tcp
sudo ufw enable
```

### SSL/TLS 配置

```bash
# 使用 Let's Encrypt
sudo certbot --nginx -d your-domain.com

# 或使用自签名证书
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/mini-pm2.key \
  -out /etc/ssl/certs/mini-pm2.crt
```

### 访问控制

```bash
# 配置 Nginx 访问控制
location / {
    allow 192.168.1.0/24;
    deny all;
    proxy_pass http://mini-pm2;
}
```

## 🧪 测试

### 运行测试

```bash
# 安装测试依赖
pip install -r requirements.txt

# 运行测试
pytest

# 运行特定测试
pytest tests/test_api.py::test_health_check

# 生成测试报告
pytest --html=report.html
```

### 性能测试

```bash
# 使用 Apache Bench
ab -n 1000 -c 10 http://localhost:8100/health

# 使用 wrk
wrk -t12 -c400 -d30s http://localhost:8100/health
```

## 🔄 更新和升级

### 应用更新

```bash
# 停止服务
sudo systemctl stop mini-pm2

# 备份数据
cp -r /var/mini-pm2 /var/mini-pm2.backup

# 更新代码
git pull origin main

# 安装新依赖
pip install -r requirements.txt

# 启动服务
sudo systemctl start mini-pm2
```

### 数据库迁移

```bash
# 从 JSON 迁移到 Redis
python -c "
import asyncio
from task_store.json_store import JSONTaskStore
from task_store.redis_store import RedisTaskStore
import redis.asyncio as redis

async def migrate():
    json_store = JSONTaskStore('tasks.json')
    redis_client = redis.from_url('redis://localhost:6379')
    redis_store = RedisTaskStore(redis_client)

    tasks = await json_store.get_all_tasks()
    for task in tasks:
        await redis_store.add_task(task)
    print(f'迁移了 {len(tasks)} 个任务')

asyncio.run(migrate())
"
```

## 🆘 故障排除

### 常见问题

1. **Redis 连接失败**

   ```bash
   # 检查 Redis 服务
   sudo systemctl status redis

   # 测试连接
   redis-cli ping
   ```

2. **端口被占用**

   ```bash
   # 查看端口占用
   sudo netstat -tlnp | grep 8100

   # 杀死进程
   sudo kill -9 <PID>
   ```

3. **权限问题**
   ```bash
   # 修复文件权限
   sudo chown -R www-data:www-data /var/mini-pm2
   sudo chmod -R 755 /var/mini-pm2
   ```

### 日志分析

```bash
# 查看错误日志
grep ERROR logs/mini-pm2.log

# 查看最近的日志
tail -n 100 logs/mini-pm2.log

# 实时监控日志
tail -f logs/mini-pm2.log | grep -E "(ERROR|WARNING)"
```

## 📞 支持

如果遇到问题，请：

1. 查看日志文件
2. 检查配置文件
3. 运行测试套件
4. 提交 Issue 到项目仓库
