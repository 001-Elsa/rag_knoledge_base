"""Locust 压测脚本：建立 API 吞吐量和延迟基线。

安装与运行：
  pip install locust
  locust -f scripts/loadtest.py --host http://localhost:8000
  # 打开 http://localhost:8089，设置并发用户数（如 50）与孵化速率（如 5/s）开始压测

压哪些接口、为什么：
  - /api/health   —— PostgreSQL + Redis 依赖健康基线（不检查 MinIO/LLM）
  - /api/kbs      —— 带鉴权 + 数据库查询的典型读接口
  - /api/stats/*  —— 聚合查询（较重的读）
  - /api/chat     —— 默认不压（会真实调 LLM 花钱），想压真实链路时取消注释，
                     并把并发控制在个位数

性能调优时逐项修改并记录环境、吞吐量和延迟，避免混合多个变量：
  1. uvicorn 加 --workers 4（多进程）
  2. 数据库连接池调大（db.py pool_size）
  3. 统计接口加 Redis 缓存
"""
import random
import string

from locust import HttpUser, between, task


def _rand_name(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


class ApiUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        """每个虚拟用户注册独立账号（模拟真实多用户，而不是全部打同一行数据）。"""
        self.username = _rand_name("load_")
        resp = self.client.post(
            "/api/auth/register",
            json={"username": self.username, "password": "loadtest123"},
        )
        data = resp.json()
        self.headers = {"Authorization": "Bearer " + data["access_token"]}
        # 每个用户建一个知识库，让列表接口有数据可查
        self.client.post("/api/kbs", json={"name": _rand_name("kb_")}, headers=self.headers)

    @task(3)
    def health(self):
        self.client.get("/api/health")

    @task(5)
    def list_kbs(self):
        self.client.get("/api/kbs", headers=self.headers)

    @task(3)
    def list_conversations(self):
        self.client.get("/api/chat/conversations", headers=self.headers)

    @task(2)
    def stats_overview(self):
        self.client.get("/api/stats/overview", headers=self.headers)

    @task(1)
    def me(self):
        self.client.get("/api/auth/me", headers=self.headers)

    # 真实问答链路（会调 DeepSeek 产生费用，需要时再打开，并发压到个位数即可）
    # @task(1)
    # def chat(self):
    #     with self.client.post(
    #         "/api/chat",
    #         json={"question": "介绍一下文档内容", "mode": "rag"},
    #         headers=self.headers,
    #         stream=True,
    #         catch_response=True,
    #     ) as resp:
    #         for _ in resp.iter_content(chunk_size=1024):
    #             pass
    #         resp.success()
